"""Antigravity 模型客户端 — OAuth 刷新 / 配额查询 / 性能基准测试.

复用 Antigravity IDE 保存在 ~/.gemini/oauth_creds.json 里的 OAuth 凭据,
直接调用 Google 的内部 API (daily-cloudcode-pa.googleapis.com), 无需启动 IDE.

注意: 凭据文件由 Antigravity 自己维护, 本模块只读取不写入.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

CREDS_PATH = Path.home() / ".gemini" / "oauth_creds.json"

API_BASE = "https://daily-cloudcode-pa.googleapis.com/v1internal"
TOKEN_URL = "https://oauth2.googleapis.com/token"

USER_AGENT = "antigravity/2.15.0 windows/amd64"
PROJECT = "aicode-consumers"


def _read_env_file() -> dict[str, str]:
    """读 .env (这个文件不进版本库, 用来放本地凭据)."""
    out: dict[str, str] = {}
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return out
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            if v:
                out[k.strip()] = v
    except OSError:
        pass
    return out


_ENV = _read_env_file()


def _cfg(key: str, default: str = "") -> str:
    """配置来源: 环境变量 > .env > 默认值."""
    return (os.environ.get(key) or _ENV.get(key) or default).strip()


# Antigravity 的 OAuth 客户端凭据. 这是 Antigravity 应用自带的安装型应用凭据,
# 不是用户个人凭据 — 但也不该硬编码进公开仓库 (会被滥用).
# 放在 .env 里, 用 ANTIGRAVITY_CLIENT_ID / ANTIGRAVITY_CLIENT_SECRET 配置.
# 见 README「Antigravity 配额」一节.
CLIENT_ID = _cfg("ANTIGRAVITY_CLIENT_ID")
CLIENT_SECRET = _cfg("ANTIGRAVITY_CLIENT_SECRET")

# 走哪个本地代理. Google 对 Antigravity 有地区风控, 所以这里通常需要代理.
# 默认对应 SakuraCat; 其他 Clash 兼容客户端用 CLASH_PROXY 覆盖.
PROXY = _cfg("CLASH_PROXY", "http://127.0.0.1:12450").rstrip("/")


class AntigravityError(RuntimeError):
    pass


def _read_refresh_token() -> str:
    if not CREDS_PATH.exists():
        raise AntigravityError(f"凭据文件不存在: {CREDS_PATH} (请先登录 Antigravity)")
    data = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
    token = data.get("refresh_token")
    if not token:
        raise AntigravityError("凭据文件里没有 refresh_token")
    return token


def get_access_token(use_proxy: bool = True) -> str:
    """用 refresh_token 换一个新鲜的 access_token."""
    proxy = PROXY if use_proxy else None
    try:
        resp = httpx.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": _read_refresh_token(),
                "grant_type": "refresh_token",
            },
            proxy=proxy,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        raise AntigravityError(f"令牌刷新请求失败: {exc}") from exc
    if resp.status_code != 200:
        raise AntigravityError(f"令牌刷新被拒 (HTTP {resp.status_code}): {resp.text[:200]}")
    token = resp.json().get("access_token")
    if not token:
        raise AntigravityError("令牌响应里没有 access_token")
    return token


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def fetch_models(use_proxy: bool = True) -> dict:
    """获取全部模型及其配额信息 (remainingFraction / resetTime)."""
    token = get_access_token(use_proxy)
    proxy = PROXY if use_proxy else None
    try:
        resp = httpx.post(
            f"{API_BASE}:fetchAvailableModels",
            headers=_headers(token),
            json={},
            proxy=proxy,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        raise AntigravityError(f"模型列表请求失败: {exc}") from exc
    if resp.status_code != 200:
        raise AntigravityError(f"模型列表被拒 (HTTP {resp.status_code}): {resp.text[:200]}")
    return resp.json().get("models", {})


def _model_sort_key(m: dict) -> tuple:
    import re
    mid = m["id"]
    is_internal = mid.startswith(("chat_", "tab_"))
    prov = (m.get("provider") or "").lower()
    
    # 厂商优先级: Claude / OpenAI 系列最高, 其次 Gemini
    prov_pri = 0
    if "anthropic" in prov or "claude" in mid.lower():
        prov_pri = 3
    elif "openai" in prov or "gpt" in mid.lower():
        prov_pri = 2
    elif "google" in prov or "gemini" in mid.lower():
        prov_pri = 1

    # 版本号提取 (例如 4.6, 3.8, 3.7, 3.1, 2.5, 120b)
    version = (0, 0, 0)
    if not is_internal:
        v_match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", mid)
        if v_match:
            version = (int(v_match.group(1)), int(v_match.group(2) or 0), int(v_match.group(3) or 0))

    # 档位优先: pro > high > medium > low > extra-low / lite
    tier_pri = 0
    mid_lower = mid.lower()
    if "pro" in mid_lower:
        tier_pri += 10
    if "high" in mid_lower:
        tier_pri += 4
    elif "medium" in mid_lower:
        tier_pri += 3
    elif "low" in mid_lower:
        tier_pri += 2
    elif "lite" in mid_lower:
        tier_pri += 1

    # 是否有配额
    has_quota = 1 if (m.get("remaining") is not None and m.get("remaining") > 0) else 0

    return (
        not is_internal,              # 1. 真实可用模型排前面, 内部 preview/tab 排最后
        m.get("recommended", False),  # 2. 官方推荐模型排前面
        has_quota,                    # 3. 有配额的排前面
        prov_pri,                     # 4. 重点厂商 (Claude/GPT/Gemini)
        version,                      # 5. 最新代际版本倒序 (3.8 > 3.7 > 3.6 > 3.1 > 2.5)
        tier_pri,                     # 6. 旗舰 Pro / High 优先
        m.get("remaining") or 0,      # 7. 剩余配额高优先
        mid,
    )


def summarize_quota(models: dict) -> list[dict]:
    """把原始模型数据整理成前端友好的列表, 优先展现最新、常用、推荐的高性能模型."""
    rows = []
    for mid, m in models.items():
        quota = m.get("quotaInfo") or {}
        rf = quota.get("remainingFraction")
        rows.append(
            {
                "id": mid,
                "display_name": m.get("displayName") or "",
                "remaining": round(rf * 100, 1) if rf is not None else None,
                "reset_time": quota.get("resetTime"),
                "thinking": bool(m.get("supportsThinking")),
                "images": bool(m.get("supportsImages")),
                "recommended": bool(m.get("recommended")),
                "max_output_tokens": m.get("maxOutputTokens"),
                "provider": m.get("modelProvider", ""),
            }
        )
    # 按模型新旧、推荐、可用配额与能力降序排列 (最新且好用的在最上面)
    rows.sort(key=_model_sort_key, reverse=True)
    return rows


def benchmark_model(
    model: str,
    prompt: str = "Count from 1 to 5.",
    max_tokens: int = 128,
    timeout: float = 90.0,
    use_proxy: bool = True,
    access_token: str | None = None,
) -> dict:
    """流式调用模型, 测量首字延迟 (TTFT)、总耗时、输出 token 数.

    返回: {ttft_ms, total_ms, tokens, text, error}
    access_token 传入则跳过刷新 — 批量任务复用同一个 token,
    避免每次调用都拿 refresh_token 换票 (次数多了容易被风控).
    """
    token = access_token or get_access_token(use_proxy)
    proxy = PROXY if use_proxy else None
    body = {
        "model": model,
        "project": PROJECT,
        "request": {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        },
    }
    url = f"{API_BASE}:streamGenerateContent?alt=sse"

    ttft_ms = None
    total_ms = None
    text_parts: list[str] = []
    token_count = 0
    error = None

    t0 = time.perf_counter()
    try:
        with httpx.stream(
            "POST", url, headers={**_headers(token), "Accept": "text/event-stream"},
            json=body, proxy=proxy, timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                detail = resp.read()[:300].decode("utf-8", "replace")
                return {"error": f"HTTP {resp.status_code}: {detail}"}

            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t0) * 1000
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    for cand in chunk.get("response", {}).get("candidates", []):
                        for part in cand.get("content", {}).get("parts", []):
                            if "text" in part:
                                text_parts.append(part["text"])
                    usage = chunk.get("response", {}).get("usageMetadata")
                    if usage:
                        token_count = usage.get("candidatesTokenCount", token_count)
        total_ms = (time.perf_counter() - t0) * 1000
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        total_ms = (time.perf_counter() - t0) * 1000

    text = "".join(text_parts).strip()
    result = {
        "model": model,
        "ttft_ms": round(ttft_ms) if ttft_ms is not None else None,
        "total_ms": round(total_ms) if total_ms is not None else None,
        "tokens": token_count or None,
        "text": text[:200],
        "error": error,
    }
    # 生成速度: 生成窗口太小算出来的值没有意义 (首字和结束几乎同时到,
    # 除出来的会是几千 tok/s 这种不可能的数), 所以要求窗口至少 50ms.
    if result["tokens"] and result["total_ms"] and result["ttft_ms"]:
        gen_ms = result["total_ms"] - result["ttft_ms"]
        if gen_ms >= 50:
            result["tokens_per_sec"] = round(result["tokens"] / (gen_ms / 1000), 1)
    return result
