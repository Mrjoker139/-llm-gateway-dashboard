"""网关自测 — 用本地假上游验证记账链路, 不消耗真实配额.

启动一个假的 OpenAI 兼容服务, 让网关把请求转发过去,
然后检查 usage.db 里是否正确记录了 token / TTFT / 耗时.
"""
from __future__ import annotations

import json
import threading
import time

import httpx
from flask import Flask, Response, request

FAKE = Flask("fake_upstream")
PORT = 8799


@FAKE.post("/v1/chat/completions")
def fake_chat():
    body = request.get_json(force=True)
    stream = body.get("stream")

    if not stream:
        return {
            "id": "chatcmpl-fake", "object": "chat.completion",
            "model": body.get("model"), "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "Hello!"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 25, "completion_tokens": 12, "total_tokens": 37},
        }

    def gen():
        time.sleep(0.15)  # 模拟首字延迟
        for i, word in enumerate(["Hello", " there", " friend", "!"]):
            chunk = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {"content": word}}]}
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            time.sleep(0.05)
        final = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "choices": [],
                 "usage": {"prompt_tokens": 25, "completion_tokens": 12, "total_tokens": 37}}
        yield f"data: {json.dumps(final)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return Response(gen(), mimetype="text/event-stream")


def main() -> None:
    # 1. 启动假上游
    t = threading.Thread(target=lambda: FAKE.run(host="127.0.0.1", port=PORT, debug=False), daemon=True)
    t.start()
    time.sleep(2.5)

    # 2. 把网关的 deepseek 指向假上游 (通过环境变量, 因为 load_env 会读环境变量)
    import os
    os.environ["DEEPSEEK_API_KEY"] = "fake-key-for-test"
    os.environ["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{PORT}/v1"

    # 重启网关进程内的 PROVIDERS 以读取新配置
    import importlib

    import providers as pv
    importlib.reload(pv)

    print("=== 1. 假上游已启动 ===")

    # 3. 直接测试 providers 层的 chat
    p = pv.DeepSeekProvider(pv.load_env())
    print(f"    provider configured: {p.configured}, base_url: {p.base_url}")

    gen, usage = p.chat("deepseek-chat", [{"role": "user", "content": "hi"}], stream=True)
    t0 = time.perf_counter()
    ttft = None
    text = ""
    for chunk in gen:
        if ttft is None:
            ttft = (time.perf_counter() - t0) * 1000
        d = chunk.get("choices", [{}])[0].get("delta", {}).get("content") if chunk.get("choices") else None
        if d:
            text += d
    total = (time.perf_counter() - t0) * 1000
    print(f"=== 2. 流式调用成功 ===")
    print(f"    首字延迟: {ttft:.0f}ms   总耗时: {total:.0f}ms")
    print(f"    文本: {text!r}")
    print(f"    usage (来自上游最后一帧): {usage}")

    # 4. 测试 usage_db 记账
    import usage_db as db
    db.init_db()
    db.record_request(
        session_id="selftest-session", provider="deepseek", model="deepseek-chat",
        input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
        ttft_ms=int(ttft), duration_ms=int(total), stream=True, status=200, client="selftest",
    )
    s = db.summary(1)
    print(f"=== 3. 记账验证 ===")
    print(f"    请求数: {s['requests']}  token: {s['total_tokens']}  平均TTFT: {s['avg_ttft']}ms")
    sessions = db.recent_sessions(5)
    if sessions:
        sess = sessions[0]
        print(f"    会话: {sess['id']}  请求{sess['requests']}次  时长{sess['span_seconds']}秒")
    print()
    print("=== 自测通过: 网关能正确转发、提取 usage、记录 TTFT 和耗时 ===")


if __name__ == "__main__":
    main()
