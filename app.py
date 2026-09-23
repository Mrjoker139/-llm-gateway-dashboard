"""LLM 网关仪表盘 — 后端.

三部分功能:
1. LLM 网关 — 把 base_url 指向本服务, 流量经过时自动记录每次请求的
   token 用量、首字延迟、总耗时; 支持 DeepSeek / Kimi / Gemini / 火山方舟.
2. 用量看板 — 从网关账本里查询 token 消耗、会话耗时、模型分布.
3. 节点测速 — 通过 SakuraCat (sing-box 内核) 的 clash 风格控制 API,
   批量测速节点延迟、一键切换、出口 IP 质量检测.

启动:  python app.py    浏览器打开 http://127.0.0.1:8787
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import httpx
from flask import Flask, jsonify, request

import antigravity as ag
import gateway
import usage_db as db
from providers import (build_providers, provider_status, set_provider_config,
                       set_provider_enabled, add_custom_provider,
                       remove_custom_provider, set_kernel_config, env_or_file,
                       load_ui_config)

# ---- 代理内核 (Clash 风格 API) ----
# 默认对应 SakuraCat; 其他客户端只要提供兼容的 Clash 控制 API 就能用.
# 配置来源: 环境变量 > config.json 的 _kernel 段 (仪表盘 UI 可改) > .env > 默认值.
# 每次请求都重新读, 所以在仪表盘上改端口立即生效, 不需要重启.
#   CLASH_API=http://127.0.0.1:9090      (Clash Verge / mihomo / ClashX 默认端口)
#   CLASH_PROXY=http://127.0.0.1:7890
def kernel_api() -> str:
    return env_or_file("CLASH_API", "http://127.0.0.1:12440")


def proxy_url() -> str:
    return env_or_file("CLASH_PROXY", "http://127.0.0.1:12450")

TEST_URLS = {
    "Google (gstatic)": "http://www.gstatic.com/generate_204",
    "Cloudflare": "http://cp.cloudflare.com/generate_204",
    "GitHub": "http://github.com/robots.txt",
}

# 长的排前面, 否则 "印度" 会先匹配掉 "印度尼西亚"
REGIONS = sorted(
    [
        "香港", "新加坡", "台湾", "日本", "韩国", "澳门", "英国", "法国", "德国",
        "意大利", "挪威", "美国", "加拿大", "澳洲", "乌克兰", "土耳其", "阿联酋",
        "尼日利亚", "菲律宾", "泰国", "越南", "马来西亚", "印度尼西亚", "印度",
        "阿根廷", "巴西",
    ],
    key=len,
    reverse=True,
)

app = Flask(__name__, static_folder="static", static_url_path="")
app.json.ensure_ascii = False

# 厂商表是可变全局: 配置改动后调用 reload_providers() 热生效, 无需重启
PROVIDERS = build_providers()
db.init_db()


def _start_auto_purge() -> None:
    def _worker():
        # 后台守护: 启动时和每 24h 自动淘汰过期数据 (失败记录保留 7 天, 正常记录保留 90 天)
        while True:
            try:
                db.purge_older_than(days=90, error_days=7)
            except Exception:
                pass
            time.sleep(86400)

    t = threading.Thread(target=_worker, daemon=True, name="usage-auto-purge")
    t.start()


_start_auto_purge()


def reload_providers() -> None:
    global PROVIDERS
    PROVIDERS = build_providers()

JOBS: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def region_of(name: str) -> str:
    for region in REGIONS:
        if region in name:
            return region
    if "自动选择" in name:
        return "自动"
    if "优选" in name:
        return "优选"
    return "其他"


def _kernel_get(path: str, **params) -> httpx.Response:
    with httpx.Client(timeout=15.0) as client:
        return client.get(f"{kernel_api()}{path}", params=params or None)


def _delay_of(node: str, url: str, timeout_ms: int) -> dict:
    """对单个节点测延迟 (不切换当前节点)."""
    try:
        resp = _kernel_get(
            f"/proxies/{quote(node, safe='')}/delay",
            timeout=timeout_ms,
            url=url,
        )
        if resp.status_code == 200:
            return {"delay": resp.json().get("delay"), "error": None}
        return {"delay": None, "error": f"HTTP {resp.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"delay": None, "error": type(exc).__name__}


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/kernel")
def api_kernel_get():
    """读取代理内核配置 (控制 API + 代理端口)."""
    return jsonify({"ok": True, "kernel_api": kernel_api(), "proxy_url": proxy_url()})


@app.post("/api/kernel")
def api_kernel_set():
    """保存代理内核配置. 立即生效, 不需要重启."""
    body = request.get_json(force=True) or {}
    if "kernel_api" not in body and "proxy_url" not in body:
        return jsonify({"ok": False, "error": "kernel_api or proxy_url required"}), 400
    for key in ("kernel_api", "proxy_url"):
        val = (body.get(key) or "").strip()
        if val and not val.startswith(("http://", "https://")):
            return jsonify({"ok": False, "error": f"{key} 必须以 http:// 或 https:// 开头"}), 400
    set_kernel_config(body.get("kernel_api"), body.get("proxy_url"))
    return jsonify({"ok": True, "kernel_api": kernel_api(), "proxy_url": proxy_url()})


@app.get("/api/status")
def api_status():
    """内核版本 + 节点列表 + 当前选择."""
    try:
        with httpx.Client(timeout=10.0) as client:
            version = client.get(f"{kernel_api()}/version").json()
            proxies = client.get(f"{kernel_api()}/proxies").json()["proxies"]
    except Exception as exc:  # noqa: BLE001
        return jsonify({
            "ok": False,
            "error": f"代理内核 API 不可达: {exc}",
            "kernel_api": kernel_api(),
            "proxy_url": proxy_url(),
            "hint": ("需要 Clash 风格的本地控制 API。SakuraCat 默认 12440/12450；"
                     "Clash Verge / mihomo 通常是 9090/7890。用环境变量 "
                     "CLASH_API / CLASH_PROXY 指定。"),
        }), 502

    groups = {k: v for k, v in proxies.items() if v.get("type") == "Selector"}
    if not groups:
        return jsonify({"ok": False, "error": "未找到策略组 (Selector)",
                        "kernel_api": kernel_api()}), 502
    selector_name, selector = max(groups.items(), key=lambda kv: len(kv[1].get("all", [])))

    nodes = [
        {
            "name": name,
            "region": region_of(name),
            "type": proxies.get(name, {}).get("type", "?"),
        }
        for name in selector.get("all", [])
    ]
    return jsonify(
        {
            "ok": True,
            "kernel": version,
            "kernel_api": kernel_api(),
            "proxy_url": proxy_url(),
            "selector": selector_name,
            "current": selector.get("now"),
            "nodes": nodes,
            "test_urls": list(TEST_URLS),
            "gemini_unsupported_regions": gemini_unsupported_regions(),
        }
    )


@app.get("/api/node/selected")
def api_node_selected():
    """内核当前选择 — 轻量轮询接口. 前端几秒对一次, 发现外部改动就提醒用户."""
    try:
        with httpx.Client(timeout=5.0) as client:
            proxies = client.get(f"{kernel_api()}/proxies").json()["proxies"]
        groups = {k: v for k, v in proxies.items() if v.get("type") == "Selector"}
        if not groups:
            return jsonify({"ok": False, "error": "未找到策略组"}), 502
        name, selector = max(groups.items(), key=lambda kv: len(kv[1].get("all", [])))
        return jsonify({"ok": True, "selector": name, "current": selector.get("now")})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/delay_one")
def api_delay_one():
    node = request.args.get("node", "")
    url = TEST_URLS.get(request.args.get("url", ""), TEST_URLS["Google (gstatic)"])
    timeout_ms = int(request.args.get("timeout", 5000))
    if not node:
        return jsonify({"delay": None, "error": "node required"}), 400
    return jsonify(_delay_of(node, url, timeout_ms))


def _run_job(job: dict, nodes: list[str], url: str, timeout_ms: int, concurrency: int) -> None:
    async def probe_all() -> None:
        sem = asyncio.Semaphore(concurrency)
        async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 5) as client:

            async def probe_one(node: str) -> None:
                async with sem:
                    try:
                        resp = await client.get(
                            f"{kernel_api()}/proxies/{quote(node, safe='')}/delay",
                            params={"timeout": timeout_ms, "url": url},
                        )
                        if resp.status_code == 200:
                            job["results"][node] = {"delay": resp.json().get("delay"), "error": None}
                        else:
                            job["results"][node] = {"delay": None, "error": f"HTTP {resp.status_code}"}
                    except Exception as exc:  # noqa: BLE001
                        job["results"][node] = {"delay": None, "error": type(exc).__name__}
                    job["done"] += 1

            await asyncio.gather(*(probe_one(n) for n in nodes))

    try:
        asyncio.run(probe_all())
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(exc)


@app.post("/api/test")
def api_test():
    body = request.get_json(force=True)
    nodes = body.get("nodes") or []
    if not nodes:
        return jsonify({"ok": False, "error": "nodes required"}), 400
    url = TEST_URLS.get(body.get("url", ""), TEST_URLS["Google (gstatic)"])
    timeout_ms = int(body.get("timeout", 3000))
    concurrency = max(1, min(32, int(body.get("concurrency", 8))))

    job = {
        "id": uuid.uuid4().hex[:12],
        "status": "running",
        "total": len(nodes),
        "done": 0,
        "results": {},
        "url": url,
        "timeout_ms": timeout_ms,
    }
    with _jobs_lock:
        JOBS[job["id"]] = job
    threading.Thread(
        target=_run_job,
        args=(job, nodes, url, timeout_ms, concurrency),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job["id"]})


@app.get("/api/test/<job_id>")
def api_test_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, **job})


@app.post("/api/switch")
def api_switch():
    body = request.get_json(force=True)
    group, node = body.get("group"), body.get("node")
    if not group or not node:
        return jsonify({"ok": False, "error": "group and node required"}), 400
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.put(
                f"{kernel_api()}/proxies/{quote(group, safe='')}",
                json={"name": node},
            )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 502
    if resp.status_code in (200, 204):
        return jsonify({"ok": True, "now": node})
    return jsonify({"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}), 502


@app.post("/api/ip_check")
def api_ip_check():
    """切换节点 -> 查出口 IP 归属 -> 切回原节点. 目标节点测速失败时拒绝切换."""
    body = request.get_json(force=True)
    group, node = body.get("group"), body.get("node")
    if not group or not node:
        return jsonify({"ok": False, "error": "group and node required"}), 400

    # 切换前先确认目标节点可用, 避免把内核切挂
    probe = _delay_of(node, TEST_URLS["Google (gstatic)"], 5000)
    if probe["delay"] is None:
        return jsonify({"ok": False, "error": f"目标节点不可用({probe['error']}), 已取消切换"})

    try:
        with httpx.Client(timeout=10.0) as client:
            proxies = client.get(f"{kernel_api()}/proxies").json()["proxies"]
        original = proxies.get(group, {}).get("now")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"读取当前选择失败: {exc}"}), 502

    geo = None
    error = None
    try:
        with httpx.Client(timeout=15.0) as client:
            client.put(f"{kernel_api()}/proxies/{quote(group, safe='')}", json={"name": node})
        import time

        time.sleep(2.0)  # 等内核完成切换
        with httpx.Client(timeout=20.0, proxy=proxy_url()) as client:
            geo = client.get(
                "http://ip-api.com/json/?fields=status,country,city,isp,org,as,asname,query"
            ).json()
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    finally:
        if original:
            try:
                with httpx.Client(timeout=15.0) as client:
                    client.put(f"{kernel_api()}/proxies/{quote(group, safe='')}", json={"name": original})
            except Exception as exc:  # noqa: BLE001
                error = (error or "") + f" | 恢复原节点失败: {exc}"

    if error:
        return jsonify({"ok": False, "error": error, "restored": original})
    return jsonify({"ok": True, "geo": geo, "restored": original, "tested": node})


@app.get("/api/models/quota")
def api_models_quota():
    """查询所有模型的剩余配额 (复用 Antigravity OAuth 凭据)."""
    try:
        models = ag.fetch_models()
        return jsonify({"ok": True, "models": ag.summarize_quota(models)})
    except ag.AntigravityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


# ============================================== 节点对 Gemini 的可用性检测
# Antigravity 走 Google 内部 API, 有地区风控: 有些出口 IP 会被
# "User location is not supported" 拒掉.
#
# 设计上的两个要点:
#   1. 由用户勾选要测哪些节点 —— 不猜"前 N 个", 用户想用哪个就测哪个
#   2. 每个节点测多次 (默认 3 次) —— 单次失败可能只是网络抖动, 不作数
# 测完自动切回原节点, 不会把你的当前选择改掉.

GEMINI_TEST_MODEL = "gemini-2.5-flash-lite"   # 最便宜, 用来试水
GEMINI_TEST_ATTEMPTS = 3

# Google 地区限制的"先验名单": 命中地区的节点默认不选入检测 (零请求, 纯按节点名判断).
# 只是先验 — 用户仍可手动勾选强测; 实测结论 (node_checks 表) 永远优先于名单.
# 可用 config.json 的 _gemini_check.unsupported_regions 覆盖.
GEMINI_UNSUPPORTED_REGIONS = ["香港", "澳门"]


def gemini_unsupported_regions() -> list[str]:
    custom = (load_ui_config().get("_gemini_check") or {}).get("unsupported_regions")
    if isinstance(custom, list):
        return [r for r in custom if isinstance(r, str) and r]
    return GEMINI_UNSUPPORTED_REGIONS

NODE_JOBS: dict[str, dict] = {}
# 节点检测会真实切换节点, 必须串行 —— 两个任务同时跑会互相切乱,
# 而且后启动的那个可能把"中间态节点"误当成原节点, 最后恢复错.
_node_check_lock = threading.Lock()


def _node_check_running() -> bool:
    return any(j.get("status") == "running" for j in NODE_JOBS.values())


def _current_exit_ip(timeout: float = 20.0) -> dict:
    """通过代理查当前出口 IP 和归属地."""
    with httpx.Client(timeout=timeout, proxy=proxy_url()) as client:
        r = client.get("http://ip-api.com/json/?fields=status,country,city,isp,query")
        return r.json()


def _set_node(group: str, node: str) -> None:
    with httpx.Client(timeout=15.0) as client:
        r = client.put(f"{kernel_api()}/proxies/{quote(group, safe='')}", json={"name": node})
        # 节点名失效时内核会拒绝切换; 不检查的话会继续用"没切过去的旧出口"探测,
        # 把结论记到错误节点名下 (B 之后结论长期有效, 记错不会被重测纠正)
        r.raise_for_status()


def _probe_gemini(access_token: str | None = None) -> tuple[bool, str | None]:
    """发一个最小请求试 Gemini. 返回 (是否可用, 错误信息)."""
    try:
        r = ag.benchmark_model(GEMINI_TEST_MODEL, prompt="hi", max_tokens=8,
                               timeout=45, access_token=access_token)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]
    err = r.get("error")
    return (err is None), err


def _run_node_check(job: dict, group: str, nodes: list[str],
                    attempts: int, original: str | None) -> None:
    """逐个节点切换 -> 测 N 次 -> 记库. 结束后切回原节点."""
    # 整个任务共用一个 access token. 默认每次 probe 都拿 refresh_token 换票,
    # N 个节点 × 3 次就是 3N 次刷新 — 同一账号短时间从大量 IP 刷新是典型风控信号.
    try:
        access_token = ag.get_access_token()
    except Exception:  # noqa: BLE001
        access_token = None      # 刷新失败则退回每次 probe 自行换票
    try:
        for node in nodes:
            job["current"] = node
            try:
                _set_node(group, node)
            except Exception as exc:  # noqa: BLE001
                job["results"][node] = {"error": f"切换失败: {exc}"}
                job["done"] += 1
                continue
            time.sleep(2.0)          # 等内核完成切换

            # 先确认这个节点能上网, 否则测出来的是"节点挂了"而不是"被 Google 拒"
            ip, country, reachable = None, None, False
            try:
                geo = _current_exit_ip(timeout=15)
                ip, country = geo.get("query"), geo.get("country")
                reachable = bool(ip)
            except Exception:  # noqa: BLE001
                pass

            outcomes: list[tuple[bool, str | None]] = []
            if reachable:
                for _ in range(attempts):
                    outcomes.append(_probe_gemini(access_token))
                    if not outcomes[-1][0]:
                        time.sleep(1.0)      # 失败后稍等一下再试
            else:
                outcomes = [(False, "节点不可达 (切过去后连不上外网)")] * attempts

            ok_count = sum(1 for ok, _ in outcomes if ok)
            # 记库: 有成功过就算这一轮通过, 让 verdict 逻辑去判 flaky
            last_err = next((e for ok, e in reversed(outcomes) if not ok), None)
            rec = db.record_node_check(
                node=node, ip=ip or "", country=country,
                ok=(ok_count > 0), error=last_err,
            )
            job["results"][node] = {
                "ip": ip, "country": country, "reachable": reachable,
                "ok_count": ok_count, "attempts": attempts,
                "verdict": rec["verdict"], "error": last_err,
                "total_attempts": rec["attempts"], "total_fails": rec["fails"],
            }
            job["done"] += 1
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(exc)
    finally:
        # 无论如何都要切回去, 别把用户的节点选择改掉
        if original:
            try:
                _set_node(group, original)
                job["restored"] = original
            except Exception as exc:  # noqa: BLE001
                job["restore_error"] = str(exc)
        _node_check_lock.release()


@app.post("/api/node/gemini_check")
def api_node_gemini_check():
    """批量检测勾选的节点能否用 Gemini. 测完自动切回原节点.

    串行执行: 同时只允许一个检测任务, 否则两个任务会互相切乱节点.
    """
    body = request.get_json(force=True) or {}
    nodes = list(dict.fromkeys(body.get("nodes") or []))
    if not nodes:
        return jsonify({"ok": False, "error": "nodes required"}), 400

    # 测一次就长期有效: 已有结论的节点默认不重测 (检测会真实动用 Google 账号,
    # 频繁全量重测容易触发风控). 前端勾选"重测已测过的"时 force=True 才带上.
    force = bool(body.get("force"))
    skipped_tested: list[str] = []
    if not force:
        tested = db.node_checks()
        skipped_tested = [n for n in nodes if n in tested]
        nodes = [n for n in nodes if n not in tested]
    if not nodes:
        return jsonify({"ok": False,
                        "error": f"勾选的 {len(skipped_tested)} 个节点都已检测过, 无需重测 "
                                 f"(要重测请勾选「重测已测过的」)"}), 400

    # 读内核状态不需要持锁 (锁只保护"切节点"的串行性)
    attempts = max(1, min(5, int(body.get("attempts", GEMINI_TEST_ATTEMPTS))))
    try:
        with httpx.Client(timeout=10.0) as client:
            proxies = client.get(f"{kernel_api()}/proxies").json()["proxies"]
        groups = {k: v for k, v in proxies.items() if v.get("type") == "Selector"}
        if not groups:
            return jsonify({"ok": False, "error": "未找到策略组"}), 502
        group, selector = max(groups.items(), key=lambda kv: len(kv[1].get("all", [])))
        original = selector.get("now")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"读取内核状态失败: {exc}"}), 502

    if not _node_check_lock.acquire(blocking=False):
        return jsonify({"ok": False,
                        "error": "已有检测任务在跑, 请等它结束 (检测会切换节点, 不能并行)"}), 409
    job = {
        "id": uuid.uuid4().hex[:12], "status": "running",
        "total": len(nodes), "done": 0, "current": None,
        "results": {}, "original": original,
    }
    NODE_JOBS[job["id"]] = job
    # 锁由线程在结束时释放 (见 _run_node_check 的 finally)
    threading.Thread(target=_run_node_check,
                     args=(job, group, nodes, attempts, original), daemon=True).start()
    return jsonify({"ok": True, "job_id": job["id"], "original": original,
                    "attempts": attempts, "skipped_tested": skipped_tested})


@app.get("/api/node/gemini_check/<job_id>")
def api_node_gemini_check_status(job_id: str):
    job = NODE_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, **job})


@app.get("/api/node/checks")
def api_node_checks():
    """所有检测过的节点记录 (键为节点名)."""
    return jsonify({"ok": True, "checks": db.node_checks()})


# ============================================================ 网关: 请求入口
# 把工具的 base_url 指到 http://127.0.0.1:8787/v1 即可, 流量会被记录.
# 模型名格式:  <provider>/<model>   例如 deepseek/deepseek-chat
#            或用请求头 X-Provider 指定厂商.

def _resolve_provider(model: str) -> tuple[str | None, str]:
    """从 model 字段解析厂商前缀. 返回 (provider_id, 真实模型名)."""
    if "/" in model:
        prefix, _, real = model.partition("/")
        if prefix in PROVIDERS:
            return prefix, real
    return None, model


class UnknownProvider(Exception):
    """model 带了厂商前缀但该前缀未注册. 消息直接返回给客户端."""

    def __init__(self, prefix: str):
        known = ", ".join(sorted(PROVIDERS))
        super().__init__(
            f"未知厂商前缀 \"{prefix}\" — 已注册: {known}. "
            f"请检查模型名拼写, 或用请求头 X-Provider 指定厂商")


def _pick_provider(model: str):
    provider_id, real_model = _resolve_provider(model)
    if provider_id:
        p = PROVIDERS[provider_id]
        # 明确指定了厂商但没配 key / 被关闭: 返回 None 让上层给清晰的错误提示
        return (p, real_model) if p.configured and p.enabled else (None, real_model)
    # 带 "/" 但前缀没匹配上 = 拼写错误或未注册的厂商. 必须报错, 不能兜底 —
    # 静默落到别家厂商会把用量记错户头, 钱花了都不知道 (valcano→volcano 的教训).
    if "/" in model:
        raise UnknownProvider(model.partition("/")[0])
    # 没前缀: 按请求头, 否则取第一个已配置且启用的厂商 (老行为, 调用方自己决定要不要依赖)
    header = (request.headers.get("X-Provider") or "").strip()
    if header in PROVIDERS and PROVIDERS[header].configured and PROVIDERS[header].enabled:
        return PROVIDERS[header], model
    for pid, p in PROVIDERS.items():
        if p.configured and p.enabled:
            return p, model
    return None, model


@app.post("/v1/chat/completions")
def gw_chat_completions():
    body = request.get_json(force=True, silent=True) or {}
    model = body.get("model", "")
    try:
        provider, real_model = _pick_provider(model)
    except UnknownProvider as exc:
        return jsonify({"error": {"message": str(exc), "type": "unknown_provider"}}), 400
    if provider is None:
        pid, _ = _resolve_provider(model)
        if pid in PROVIDERS and not PROVIDERS[pid].enabled:
            hint = f"厂商 {pid} 已在仪表盘关闭"
        else:
            hint = (f"厂商 {pid} 未配置 API key" if pid else "没有任何已配置的厂商")
        return jsonify({"error": {"message": f"{hint} — 请在「厂商配置」页检查",
                                  "type": "no_provider"}}), 503
    body["model"] = real_model
    return gateway.handle_chat_completion(provider, body, request)


# ---- 模型列表: 缓存 + 容错 ----
# 之前每次请求 /v1/models 都要实时打各厂商的 /models 接口, 一个厂商慢/挂
# 会把整个接口拖到好几秒 (实测 5.9s). 现在按厂商缓存 5 分钟, 并用线程池
# 并行拉取, 单个厂商失败只影响自己, 不影响其他厂商的模型.

_MODELS_TTL = 300                     # 缓存秒数
_MODELS_CACHE: dict[str, tuple[float, list[dict], str | None]] = {}


def _fetch_provider_models(pid: str, p) -> tuple[str, list[dict] | None, str | None]:
    """拉单个厂商的模型列表. 失败返回 (pid, None, error), 不影响别的厂商."""
    try:
        return pid, [f"{pid}/{m}" for m in p.list_models()], None
    except Exception as exc:  # noqa: BLE001
        return pid, None, f"{type(exc).__name__}: {exc}"[:200]


def _models_cached(force: bool = False) -> dict:
    """返回 {providers: [...], errors: {...}, fetched_at, age}."""
    now = time.time()
    if not force and _MODELS_CACHE.get("_at") and now - _MODELS_CACHE["_at"] < _MODELS_TTL:
        return _MODELS_CACHE

    providers = [p for p in PROVIDERS.values() if p.configured and p.enabled]
    with ThreadPoolExecutor(max_workers=max(4, len(providers))) as ex:
        results = list(ex.map(lambda p: _fetch_provider_models(p.id, p), providers))

    out = {"providers": [], "errors": {}, "fetched_at": now}
    for pid, models, err in results:
        if err is not None:
            out["errors"][pid] = err
        else:
            out["providers"].append({"id": pid, "name": PROVIDERS[pid].name, "models": models})
    _MODELS_CACHE.clear()
    _MODELS_CACHE.update(out)
    _MODELS_CACHE["_at"] = now
    return _MODELS_CACHE


@app.get("/v1/models")
def gw_models():
    """聚合所有已配置厂商的模型列表, 带厂商前缀 (OpenAI 标准格式)."""
    data = _models_cached()
    out = [
        {"id": m, "object": "model", "owned_by": g["id"]}
        for g in data["providers"] for m in g["models"]
    ]
    return jsonify({"object": "list", "data": out})


@app.get("/api/models")
def api_models():
    """看板用的模型列表: 按厂商分组 + 每个厂商的配置/拉取状态.

    比 /v1/models 多带厂商名和失败原因, 让用户一眼看出"哪个厂商没拉到"。
    ?refresh=1 强制刷新缓存 (改完厂商配置后手动刷新用).
    """
    force = request.args.get("refresh") == "1"
    data = _models_cached(force=force)
    return jsonify({
        "ok": True,
        "providers": data["providers"],
        "errors": data["errors"],
        "fetched_at": data["fetched_at"],
        "age": int(time.time() - data["fetched_at"]),
    })


# ============================================================ 用量看板 API
@app.get("/api/usage/summary")
def api_usage_summary():
    hours = float(request.args.get("hours", 24))
    return jsonify({"ok": True, **db.summary(hours)})


@app.get("/api/usage/by_provider")
def api_usage_by_provider():
    hours = float(request.args.get("hours", 24))
    return jsonify({"ok": True, "providers": db.by_provider(hours)})


@app.get("/api/usage/by_model")
def api_usage_by_model():
    hours = float(request.args.get("hours", 24))
    return jsonify({"ok": True, "models": db.by_model(hours)})


@app.get("/api/usage/sessions")
def api_usage_sessions():
    limit = int(request.args.get("limit", 30))
    return jsonify({"ok": True, "sessions": db.recent_sessions(limit)})


@app.get("/api/usage/model_stats")
def api_usage_model_stats():
    """按 厂商+模型 聚合的性能统计 (TTFT/TPOT/速度/效率)."""
    hours = float(request.args.get("hours", 168))
    return jsonify({"ok": True, "stats": db.model_stats(hours)})


@app.get("/api/usage/model_detail")
def api_usage_model_detail():
    """单个模型的逐条请求明细."""
    provider = request.args.get("provider", "")
    model = request.args.get("model", "")
    limit = int(request.args.get("limit", 200))
    if not provider or not model:
        return jsonify({"ok": False, "error": "provider and model required"}), 400
    return jsonify({"ok": True, "requests": db.model_detail(provider, model, limit)})


@app.get("/api/usage/benchmark_detail")
def api_usage_benchmark_detail():
    """单个模型的基准测试明细 (Antigravity 那些不经过网关的模型)."""
    provider = request.args.get("provider", "")
    model = request.args.get("model", "")
    limit = int(request.args.get("limit", 100))
    if not provider or not model:
        return jsonify({"ok": False, "error": "provider and model required"}), 400
    return jsonify({"ok": True, "benchmarks": db.benchmark_detail(provider, model, limit)})


@app.get("/api/usage/session/<session_id>")
def api_usage_session_detail(session_id: str):
    return jsonify({"ok": True, **db.session_detail(session_id)})


@app.get("/api/usage/congestion")
def api_usage_congestion():
    """模型的时段拥堵画像 — 用来决定什么时段该换个模型."""
    hours = float(request.args.get("hours", 168))
    return jsonify({"ok": True, "models": db.model_congestion(hours)})


@app.get("/api/usage/congestion_hourly")
def api_usage_congestion_hourly():
    """按小时的性能数据 (画趋势图用)."""
    hours = float(request.args.get("hours", 168))
    model = request.args.get("model") or None
    return jsonify({"ok": True, "hourly": db.congestion_by_hour(hours, model)})


@app.get("/api/usage/timeline")
def api_usage_timeline():
    hours = float(request.args.get("hours", 24))
    buckets = int(request.args.get("buckets", 24))
    return jsonify({"ok": True, "timeline": db.timeline(hours, buckets)})


@app.post("/api/usage/delete_record")
def api_usage_delete_record():
    """删除单条请求或基准测试记录."""
    body = request.get_json(force=True) or {}
    record_type = body.get("type", "request")
    record_id = body.get("id")
    if record_id is None:
        return jsonify({"ok": False, "error": "id required"}), 400
    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid id"}), 400
    deleted = db.delete_record(record_type, record_id)
    return jsonify({"ok": True, "deleted": deleted})


@app.post("/api/usage/clear_errors")
def api_usage_clear_errors():
    """清除失败记录 (单模型或全局)."""
    body = request.get_json(force=True) or {}
    provider = body.get("provider") or None
    model = body.get("model") or None
    res = db.clear_errors(provider, model)
    return jsonify({"ok": True, **res})


@app.post("/api/usage/clear_model")
def api_usage_clear_model():
    """清空指定模型的全部历史数据."""
    body = request.get_json(force=True) or {}
    provider = body.get("provider", "")
    model = body.get("model", "")
    if not provider or not model:
        return jsonify({"ok": False, "error": "provider and model required"}), 400
    res = db.clear_model_history(provider, model)
    return jsonify({"ok": True, **res})


@app.post("/api/usage/purge")
def api_usage_purge():
    """清理过期数据 (支持指定正常数据保留天数与失败保留天数)."""
    body = request.get_json(force=True) or {}
    days = int(body.get("days", 90))
    error_days = body.get("error_days")
    if error_days is not None:
        error_days = int(error_days)
    res = db.purge_older_than(days=days, error_days=error_days)
    return jsonify({"ok": True, **res})


@app.get("/api/providers")
def api_providers():
    return jsonify({"ok": True, "providers": provider_status(PROVIDERS)})


@app.post("/api/providers/<pid>/balance")
def api_provider_balance(pid: str):
    p = PROVIDERS.get(pid)
    if not p:
        return jsonify({"ok": False, "error": "unknown provider"}), 404
    info = p.fetch_balance()
    return jsonify({"ok": True, **info.__dict__})


@app.post("/api/balances/all")
def api_balances_all():
    """并发查询所有已配置厂商的资金余额与套餐额度."""
    results = {}

    def _fetch(pid, prov):
        try:
            info = prov.fetch_balance()
            d = info.__dict__.copy()
            d["name"] = prov.name
            d["enabled"] = getattr(prov, "enabled", True)
            return pid, d
        except Exception as exc:  # noqa: BLE001
            from providers import BalanceInfo
            info = BalanceInfo(pid, "balance", available=False, error=str(exc))
            d = info.__dict__.copy()
            d["name"] = prov.name
            d["enabled"] = getattr(prov, "enabled", True)
            return pid, d

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_fetch, pid, p) for pid, p in PROVIDERS.items() if p.configured]
        for fut in futs:
            pid, data = fut.result()
            results[pid] = data

    return jsonify({"ok": True, "balances": results})



@app.post("/api/providers/<pid>/models")
def api_provider_models(pid: str):
    p = PROVIDERS.get(pid)
    if not p:
        return jsonify({"ok": False, "error": "unknown provider"}), 404
    try:
        return jsonify({"ok": True, "models": p.list_models()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502


@app.post("/api/providers/<pid>/config")
def api_set_provider_config(pid: str):
    """保存厂商配置 (UI 表单). 立即生效, 不需要重启."""
    body = request.get_json(force=True) or {}
    if pid not in PROVIDERS:
        return jsonify({"ok": False, "error": "unknown provider"}), 404
    if "api_key" not in body and "base_url" not in body:
        return jsonify({"ok": False, "error": "api_key or base_url required"}), 400
    set_provider_config(pid, body.get("api_key"), body.get("base_url"))
    reload_providers()
    p = PROVIDERS[pid]
    return jsonify({"ok": True, "configured": p.configured,
                    "base_url": p.base_url, "api_key_masked": _mask(p.api_key)})


@app.post("/api/providers/<pid>/toggle")
def api_toggle_provider(pid: str):
    """开关厂商: 关闭后网关不路由、模型列表不含它. 立即生效."""
    body = request.get_json(force=True) or {}
    if pid not in PROVIDERS:
        return jsonify({"ok": False, "error": "unknown provider"}), 404
    enabled = bool(body.get("enabled", not PROVIDERS[pid].enabled))
    set_provider_enabled(pid, enabled)
    reload_providers()
    return jsonify({"ok": True, "id": pid, "enabled": PROVIDERS[pid].enabled})


@app.post("/api/providers/custom")
def api_add_custom_provider():
    """添加自定义 OpenAI 兼容端点."""
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    if not name or not base_url:
        return jsonify({"ok": False, "error": "名称和 base_url 必填"}), 400
    if not base_url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "base_url 必须以 http:// 或 https:// 开头"}), 400
    pid = add_custom_provider(name, base_url, api_key, body.get("kind", "openai"))
    reload_providers()
    return jsonify({"ok": True, "id": pid})


@app.delete("/api/providers/custom/<pid>")
def api_remove_custom_provider(pid: str):
    if not remove_custom_provider(pid):
        return jsonify({"ok": False, "error": "not a custom provider"}), 404
    reload_providers()
    return jsonify({"ok": True})


def _mask(key: str) -> str:
    if not key:
        return ""
    return f"{key[:6]}***{key[-4:]}" if len(key) > 12 else key[:3] + "***"


BENCH_JOBS: dict[str, dict] = {}


def _run_bench(job: dict, models: list[str], prompt: str, max_tokens: int,
               provider: str = "antigravity") -> None:
    """跑基准测试. 结果除了放在 job 里给前端轮询, 也写进账本 —
    这样「模型性能」页能看到 Antigravity 那些走不了网关的模型."""
    try:
        for model in models:
            job["current"] = model
            result = ag.benchmark_model(model, prompt=prompt, max_tokens=max_tokens)
            job["results"][model] = result
            job["done"] += 1
            try:
                db.record_benchmark(
                    provider=provider, model=model,
                    ttft_ms=result.get("ttft_ms"), total_ms=result.get("total_ms"),
                    tokens=result.get("tokens"),
                    tokens_per_sec=result.get("tokens_per_sec"),
                    error=result.get("error"),
                )
            except Exception:  # noqa: BLE001
                pass          # 存不上不该让整个测试挂掉
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(exc)


@app.post("/api/models/benchmark")
def api_models_benchmark():
    """对一批模型做流式基准测试 (串行执行, 避免相互干扰影响 TTFT 准确性)."""
    body = request.get_json(force=True)
    models = body.get("models") or []
    if not models:
        return jsonify({"ok": False, "error": "models required"}), 400
    prompt = body.get("prompt") or "Count from 1 to 5."
    # 默认给足: Gemini 3.x 的思考过程也吃额度, 给小了正文吐不出来
    max_tokens = int(body.get("max_tokens", 2048))

    job = {
        "id": uuid.uuid4().hex[:12],
        "status": "running",
        "total": len(models),
        "done": 0,
        "current": None,
        "results": {},
    }
    BENCH_JOBS[job["id"]] = job
    threading.Thread(
        target=_run_bench, args=(job, models, prompt, max_tokens), daemon=True
    ).start()
    return jsonify({"ok": True, "job_id": job["id"]})


@app.get("/api/models/benchmark/<job_id>")
def api_models_benchmark_status(job_id: str):
    job = BENCH_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, **job})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8787, debug=False)