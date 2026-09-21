"""LLM 网关 — 拦截请求、转发、记账.

这是整个项目的核心. 工作方式:

    你的工具 (Claude Code / Cline / 任何 OpenAI 客户端)
              │  base_url 指向 http://127.0.0.1:8787/v1
              ▼
    ┌─────────────────────────────┐
    │  网关 (本文件)               │
    │  1. 收到请求                 │
    │  2. 转发给真正的厂商          │
    │  3. 边转发边统计:            │
    │     - 首字延迟 (TTFT)        │
    │     - 输入/输出 token        │
    │     - 总耗时                 │
    │  4. 写进 usage.db            │
    └─────────────────────────────┘
              │
              ▼
        厂商 API (DeepSeek / Kimi / Gemini / 火山)

关键点: token 用量不需要自己数 — 厂商在响应的 usage 字段里会返回,
流式请求则通过 stream_options.include_usage 让厂商在最后一帧带上.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Iterator

from flask import Response, jsonify, request

import usage_db as db
from providers import Provider


def _extract_session(req) -> str:
    """从请求头里推断会话 ID.

    不同工具带的头不一样, 按优先级尝试:
      X-Session-Id  — 我们自己约定的
      X-Client-Request-Id / X-Request-Id — 很多工具会带
      没有就按天 + 客户端名生成一个粗粒度会话
    """
    for header in ("X-Session-Id", "X-Client-Request-Id", "X-Request-Id"):
        val = req.headers.get(header)
        if val:
            return val[:64]
    client = (req.headers.get("User-Agent") or "unknown").split("/")[0][:24]
    return f"{client}-{time.strftime('%Y%m%d-%H')}"


def _extract_client(req) -> str:
    ua = req.headers.get("User-Agent") or "unknown"
    for name in ("claude-cli", "Claude", "Cline", "Cursor", "Antigravity", "Continue",
                 "OpenAI", "python", "node", "curl"):
        if name.lower() in ua.lower():
            return name
    return ua.split("/")[0][:32]


def _usage_from_chunk(chunk: dict) -> dict:
    u = chunk.get("usage") or {}
    if not u:
        return {}
    return {
        "input_tokens": u.get("prompt_tokens", 0) or 0,
        "output_tokens": u.get("completion_tokens", 0) or 0,
    }


def _status_of(exc: Exception) -> int:
    """从异常里挖出厂商返回的 HTTP 状态码, 挖不到就按 502 (上游故障)."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else 502


def handle_chat_completion(provider: Provider, body: dict, req) -> Response:
    """转发 /v1/chat/completions 并记录用量.

    请求体里的参数原样透传给厂商 — 只把 model 换成去掉厂商前缀的真实名字.
    不替客户端补默认值: 不同厂商对参数的容忍度不一样 (Kimi 的 k3 只接受
    temperature=1), 网关自作主张填默认值会把本来能用的请求搞坏.
    """
    model = body.get("model", "")
    stream = bool(body.get("stream"))
    session_id = _extract_session(req)
    client = _extract_client(req)

    # 把客户端的 User-Agent 透传给上游. 否则上游看到的是 python-httpx,
    # 那是个很扎眼的"非官方客户端"特征 — 透传真实工具名更像正常流量.
    provider.client_ua = (req.headers.get("User-Agent") or "")[:200]

    messages = body.get("messages", [])
    max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 4096)
    temperature = body.get("temperature")
    if temperature is not None:
        temperature = float(temperature)
    # 其余字段原样带过去, 但 model 由网关控制 (已换成真实模型名)
    extra = {k: v for k, v in body.items()
             if k not in ("model", "messages", "stream", "max_tokens",
                          "max_completion_tokens", "temperature")}

    started = time.perf_counter()
    ttft_ms: int | None = None
    usage: dict = {}
    output_chars = 0

    try:
        result, usage_ref = provider.chat(
            model, messages, stream=stream,
            max_tokens=max_tokens, temperature=temperature, extra=extra,
        )
    except Exception as exc:  # noqa: BLE001
        code = _status_of(exc)
        db.record_request(
            session_id=session_id, provider=provider.id, model=model,
            duration_ms=int((time.perf_counter() - started) * 1000),
            status=code, error=str(exc)[:300], client=client,
        )
        return jsonify({"error": {"message": f"上游请求失败: {exc}", "type": "upstream_error"}}), code

    if not stream:
        # 非流式: 一次性拿到结果
        duration_ms = int((time.perf_counter() - started) * 1000)
        u = (usage_ref if isinstance(usage_ref, dict) else {}) or {}
        # OpenAI 兼容的非流式响应里 usage 在 body 里
        if isinstance(result, dict) and result.get("usage"):
            u = result["usage"]
        db.record_request(
            session_id=session_id, provider=provider.id, model=model,
            input_tokens=u.get("prompt_tokens", 0) or 0,
            output_tokens=u.get("completion_tokens", 0) or 0,
            ttft_ms=duration_ms, duration_ms=duration_ms,
            stream=False, status=200, client=client,
        )
        return jsonify(result)

    # 流式: 边转发边打点
    def generate() -> Iterator[bytes]:
        nonlocal ttft_ms, usage, output_chars
        status, error = 200, None
        try:
            for chunk in result:
                if ttft_ms is None:
                    ttft_ms = int((time.perf_counter() - started) * 1000)
                delta_obj = (
                    chunk.get("choices", [{}])[0].get("delta", {})
                    if chunk.get("choices") else {}
                )
                # 推理模型把思考过程放在 reasoning_content 里, 也算输出
                delta = (delta_obj.get("content") or "") + (delta_obj.get("reasoning_content") or "")
                if delta:
                    output_chars += len(delta)
                u = _usage_from_chunk(chunk)
                if u:
                    # 原地更新, 保持 provider 返回的那个 dict 引用有效
                    usage.update(u)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001
            # 厂商在流中途断掉 (限流/超时/网络). 已经吐出去的 chunk 收不回来,
            # 但账本要记成失败, 否则看板上全是"全部成功".
            status, error = _status_of(exc), str(exc)[:300]
            yield f"data: {json.dumps({'error': {'message': str(exc)[:300], 'type': 'upstream_error'}}, ensure_ascii=False)}\n\n".encode()
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            db.record_request(
                session_id=session_id, provider=provider.id, model=model,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0) or (output_chars // 4),
                ttft_ms=ttft_ms or duration_ms, duration_ms=duration_ms,
                stream=True, status=status, error=error, client=client,
            )

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
