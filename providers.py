"""多厂商适配层 — 网关的核心.

把 DeepSeek / Kimi / Gemini / 火山方舟 等厂商的差异统一成同一套接口:

    Provider.list_models()    -> 有哪些模型
    Provider.fetch_balance()  -> 余额 / 配额还剩多少 (免费, 不消耗 token)
    Provider.chat(...)        -> 发对话请求 (消耗 token, 由网关记录用量)

统一之后, 上层网关和仪表盘都不用关心厂商差异.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

ENV_PATH = Path(__file__).parent / ".env"
CONFIG_PATH = Path(__file__).parent / "config.json"


def load_env() -> dict[str, str]:
    """读取配置. 优先级: 内置默认 < .env < config.json(UI 配置) < 环境变量.

    UI 配置存在 config.json, 通过网页修改后立即生效, 不需要重启.
    """
    values: dict[str, str] = {}

    # 1. .env (空值跳过, 避免 .env 里的占位行清掉 UI 配置)
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            if val:
                values[key.strip()] = val

    # 2. UI 配置 (config.json) 覆盖 .env
    ui = load_ui_config()
    for cls in PROVIDER_CLASSES:
        pc = ui.get(cls.id) or {}
        if pc.get("api_key"):
            values[cls.env_key] = pc["api_key"]
        if pc.get("base_url") and cls.env_base:
            values[cls.env_base] = pc["base_url"]

    # 3. 环境变量优先级最高 (但空值不覆盖, 否则会清掉 UI/.env 里的配置)
    for key in list(values) + [k for k in os.environ if k.endswith(("_API_KEY", "_KEY", "_BASE_URL"))]:
        if key in os.environ and os.environ[key].strip():
            values[key] = os.environ[key]
    return values


def load_ui_config() -> dict:
    """读取 UI 保存的配置."""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def env_or_file(key: str, default: str) -> str:
    """取配置: 环境变量优先, 其次 .env, 最后默认值.

    用于那些不属于"厂商 key"的全局设置 (比如代理内核地址).
    """
    val = os.environ.get(key, "").strip()
    if val:
        return val.rstrip("/")
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                v = v.strip().strip('"').strip("'")
                if v:
                    return v.rstrip("/")
    return default.rstrip("/")


def save_ui_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def mask_key(key: str) -> str:
    """脱敏显示: sk-1234...abcd"""
    if not key:
        return ""
    if len(key) <= 12:
        return key[:3] + "***"
    return f"{key[:6]}***{key[-4:]}"


def _iso_epoch(text: str | None) -> float | None:
    """把 ISO8601 时间串转成时间戳, 用于容错比较 (失败返回 None)."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# 套餐时间窗的名字: limit_5h -> 5 小时, limit_7d -> 7 天
_WINDOW_UNITS = {"m": "分钟", "h": "小时", "d": "天"}


def _window_label(key: str, window: dict | None = None) -> str:
    """给配额时间窗起个人话名字."""
    import re as _re
    m = _re.fullmatch(r"limit_(\d+)([mhd])", key or "")
    if m:
        return f"{m.group(1)} {_WINDOW_UNITS[m.group(2)]}"
    if window:
        dur, unit = window.get("duration"), (window.get("timeUnit") or "")
        if dur:
            # TIME_UNIT_MINUTE / TIME_UNIT_HOUR / TIME_UNIT_DAY
            for token, suffix, div in (("MINUTE", "小时", 60), ("HOUR", "小时", 1), ("DAY", "天", 1)):
                if token in unit:
                    if suffix == "小时" and div == 60:
                        return f"{round(dur / 60, 1):g} 小时"
                    return f"{dur:g} {suffix}"
    return key or "额度"


def set_provider_config(pid: str, api_key: str | None, base_url: str | None) -> None:
    """保存单个厂商配置 (UI 调用). 传空字符串表示清除该项."""
    cfg = load_ui_config()
    entry = cfg.get(pid) or {}
    if api_key is not None:
        if api_key.strip():
            entry["api_key"] = api_key.strip()
        else:
            entry.pop("api_key", None)
    if base_url is not None:
        if base_url.strip():
            entry["base_url"] = base_url.strip().rstrip("/")
        else:
            entry.pop("base_url", None)
    if entry:
        cfg[pid] = entry
    else:
        cfg.pop(pid, None)
    save_ui_config(cfg)


def add_custom_provider(name: str, base_url: str, api_key: str,
                        kind: str = "openai") -> str:
    """添加一个自定义 OpenAI 兼容端点. 返回生成的 id."""
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "custom"
    cfg = load_ui_config()
    custom = cfg.get("_custom") or []
    pid = slug
    n = 1
    existing = {c["id"] for c in custom}
    while pid in existing or pid in {c.id for c in PROVIDER_CLASSES}:
        n += 1
        pid = f"{slug}-{n}"
    custom.append({"id": pid, "name": name, "base_url": base_url.rstrip("/"),
                   "api_key": api_key.strip(), "kind": kind})
    cfg["_custom"] = custom
    save_ui_config(cfg)
    return pid


def remove_custom_provider(pid: str) -> bool:
    cfg = load_ui_config()
    custom = cfg.get("_custom") or []
    kept = [c for c in custom if c["id"] != pid]
    if len(kept) == len(custom):
        return False
    cfg["_custom"] = kept
    save_ui_config(cfg)
    return True


def _volc_signed_request(action: str, version: str, ak: str, sk: str,
                         host: str, region: str = "cn-beijing",
                         service: str = "billing") -> dict:
    """火山引擎 OpenAPI 的 HMAC-SHA256 签名调用 (V4 签名).

    火山的费用中心接口不能用 Bearer API Key, 必须用 AK/SK 按 V4 规则签名.
    返回解析后的 JSON; 失败抛异常, 由调用方包装成 BalanceInfo.error.
    """
    import hashlib
    import hmac
    from urllib.parse import quote as _q

    body = b""
    now = datetime.utcnow()
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    payload_hash = hashlib.sha256(body).hexdigest()

    query = f"Action={action}&Version={version}"
    content_type = "application/x-www-form-urlencoded"
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    canonical_request = "\n".join([
        "POST", "/", query, canonical_headers, signed_headers, payload_hash,
    ])
    credential_scope = f"{short_date}/{region}/{service}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256", x_date, credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _sign(sk.encode(), short_date)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    authorization = (
        f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": host,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
    }
    resp = httpx.post(f"https://{host}/?{query}", headers=headers, timeout=25)
    data = resp.json()
    meta = (data.get("ResponseMetadata") or {}).get("Error")
    if meta:
        raise RuntimeError(f"{meta.get('Code')}: {meta.get('Message', '')[:120]}")
    return data


@dataclass
class BalanceInfo:
    """余额 / 配额信息.

    三种形态 (kind):
      "balance"  — 按量付费的钱: total/granted/used
      "plan"     — 套餐窗口额度: windows 里是各时间窗的已用百分比
      "quota"    — 只有 key 有效性或各模型剩余百分比, 查不到额度
    """
    provider: str
    kind: str                       # "balance" | "plan" | "quota"
    available: bool = True
    error: str | None = None
    currency: str | None = None
    total: float | None = None      # 余额总额
    granted: float | None = None    # 赠送额度
    used: float | None = None       # 已用
    quotas: list[dict] = field(default_factory=list)   # 订阅制: 各模型剩余百分比
    windows: list[dict] = field(default_factory=list)  # 套餐制: 各时间窗用量
    raw: dict | None = None


class Provider:
    """厂商适配器基类."""

    id: str = ""
    name: str = ""
    kind: str = "openai"            # openai | gemini | antigravity
    base_url: str = ""
    env_key: str = ""               # .env 里 API key 的变量名
    env_base: str = ""              # 可选: 自定义 base_url 的变量名

    def __init__(self, config: dict[str, str]):
        self.config = config
        self.api_key = config.get(self.env_key, "").strip()
        if self.env_base and config.get(self.env_base):
            self.base_url = config[self.env_base].strip().rstrip("/")
        # 客户端原始 User-Agent, 由网关在转发时填入.
        # 透传它能让上游看到"真实工具"而不是 python-httpx, 更像正常客户端.
        self.client_ua: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.client_ua:
            headers["User-Agent"] = self.client_ua
        return headers

    # --- 子类实现 ---
    def list_models(self) -> list[str]:
        raise NotImplementedError

    def fetch_balance(self) -> BalanceInfo:
        raise NotImplementedError

    def chat(self, model: str, messages: list[dict], stream: bool = True,
             max_tokens: int = 1024, temperature: float | None = None,
             timeout: float = 120.0):
        raise NotImplementedError


# ---------------------------------------------------------------- OpenAI 兼容
class OpenAICompatProvider(Provider):
    """所有 OpenAI 兼容协议的厂商都用这套 (DeepSeek / Kimi / 火山 / SiliconFlow)."""

    kind = "openai"

    def list_models(self) -> list[str]:
        if not self.configured:
            return []
        resp = httpx.get(f"{self.base_url}/models", headers=self._headers(), timeout=20)
        resp.raise_for_status()
        return sorted(m["id"] for m in resp.json().get("data", []))

    def fetch_balance(self) -> BalanceInfo:
        raise NotImplementedError

    def chat(self, model: str, messages: list[dict], stream: bool = True,
             max_tokens: int = 1024, temperature: float | None = None,
             timeout: float = 120.0, extra: dict | None = None):
        """返回 (generator_of_chunks, usage_dict). 调用方负责统计和记录.

        temperature 为 None 表示客户端没指定 — 这时不能替它填一个默认值:
        Kimi 的 k3 系列只接受 temperature=1, 强注 0.7 会直接被 400 拒掉.
        extra 是客户端请求体里的其余字段, 原样透传 (厂商自己的新参数不用改代码).
        """
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if extra:
            # 客户端显式给的字段优先; 但 model/stream 由网关掌控, 不让覆盖
            for k, v in extra.items():
                if k not in ("model", "stream", "messages"):
                    payload[k] = v
        if stream:
            payload["stream_options"] = {"include_usage": True}

        url = f"{self.base_url}/chat/completions"
        usage: dict = {}

        def gen():
            nonlocal usage
            with httpx.stream("POST", url, headers=self._headers(), json=payload,
                              timeout=timeout) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        # 原地更新: 调用方持有的是这个 dict 的引用, 重新赋值它看不到
                        usage.clear()
                        usage.update(chunk["usage"])
                    yield chunk

        if stream:
            return gen(), usage
        resp = httpx.post(url, headers=self._headers(), json=payload, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
        return body, body.get("usage", {})


class DeepSeekProvider(OpenAICompatProvider):
    id, name = "deepseek", "DeepSeek"
    base_url = "https://api.deepseek.com"
    env_key, env_base = "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"

    def fetch_balance(self) -> BalanceInfo:
        if not self.configured:
            return BalanceInfo(self.id, "balance", available=False, error="未配置 API key")
        try:
            r = httpx.get(f"{self.base_url}/user/balance", headers=self._headers(), timeout=20)
            r.raise_for_status()
            d = r.json()
            infos = d.get("balance_infos") or []
            main = infos[0] if infos else {}
            return BalanceInfo(
                self.id, "balance", currency=main.get("currency", "CNY"),
                total=float(main.get("total_balance", 0)),
                granted=float(main.get("granted_balance", 0)) if main.get("granted_balance") else None,
                raw=d,
            )
        except Exception as exc:  # noqa: BLE001
            return BalanceInfo(self.id, "balance", available=False, error=str(exc)[:200])


class MoonshotProvider(OpenAICompatProvider):
    """Kimi (月之暗面).

    Kimi 有两种计费方式, base_url 也不同:
      - Coding Plan (api.kimi.com/coding/v1): 套餐制, 用 /usages 查各时间窗已用比例
      - 开放平台 (api.moonshot.cn/v1): 按量付费, 用 /users/me/balance 查余额
    两个接口在对方的域名上都是 404, 所以按 base_url 判断该调哪个, 并互相兜底.
    """

    id, name = "moonshot", "Kimi"
    base_url = "https://api.moonshot.cn/v1"
    env_key, env_base = "MOONSHOT_API_KEY", "MOONSHOT_BASE_URL"

    @property
    def _is_coding_plan(self) -> bool:
        return "kimi.com" in self.base_url

    def fetch_balance(self) -> BalanceInfo:
        if not self.configured:
            return BalanceInfo(self.id, "quota", available=False, error="未配置 API key")
        # 套餐额度优先; 不是套餐就退回按量付费余额
        order = (self._plan_quota, self._payg_balance) if self._is_coding_plan \
            else (self._payg_balance, self._plan_quota)
        for attempt in order:
            info = attempt()
            if info is not None:      # None = 该接口在这个端点上不存在, 试下一个
                return info
        return BalanceInfo(self.id, "quota", available=False,
                           error="套餐额度与余额接口都不可用")

    def _plan_quota(self) -> BalanceInfo | None:
        """Coding Plan 套餐: /usages 返回各时间窗的已用比例. 不是套餐则返回 None."""
        try:
            r = httpx.get(f"{self.base_url}/usages", headers=self._headers(), timeout=20)
        except Exception as exc:  # noqa: BLE001
            return BalanceInfo(self.id, "quota", available=False, error=str(exc)[:200])
        if r.status_code == 404:
            return None                       # 这个端点上没有套餐接口, 让调用方兜底
        if r.status_code != 200:
            return BalanceInfo(self.id, "quota", available=False,
                               error=f"HTTP {r.status_code}: {r.text[:150]}")
        d = r.json()

        # 同一个时间窗可能同时出现在 usages (比例) 和 limits (绝对值) 里,
        # 按 label 去重; usages 的 used_ratio 更直接, 优先保留它.
        windows: list[dict] = []
        seen: set[str] = set()

        def _add(win: dict) -> None:
            if win["label"] in seen:
                return
            seen.add(win["label"])
            windows.append(win)

        for key, val in (d.get("usages") or {}).items():
            if not isinstance(val, dict):
                continue
            ratio = val.get("used_ratio")
            _add({
                "label": _window_label(key),
                "used_pct": round(ratio * 100, 1) if ratio is not None else None,
                "remaining_pct": round((1 - ratio) * 100, 1) if ratio is not None else None,
                "reset_time": val.get("reset_time"),
            })
        for item in (d.get("limits") or []):
            detail = item.get("detail") or {}
            limit, used = detail.get("limit"), detail.get("used")
            try:
                used_pct = round(float(used) / float(limit) * 100, 1)
            except (TypeError, ValueError, ZeroDivisionError):
                used_pct = None
            _add({
                "label": _window_label("", item.get("window")),
                "used_pct": used_pct,
                "remaining_pct": round(100 - used_pct, 1) if used_pct is not None else None,
                "used": used, "limit": limit,
                "reset_time": detail.get("resetTime"),
            })
        # 顶层 usage 通常就是最长那个窗口的副本, 只在前面什么都没解析出来时兜底
        top = d.get("usage") or {}
        if top and not windows:
            try:
                used_pct = round(float(top.get("used")) / float(top.get("limit")) * 100, 1)
            except (TypeError, ValueError, ZeroDivisionError):
                used_pct = None
            _add({
                "label": "套餐总量", "used_pct": used_pct,
                "remaining_pct": round(100 - used_pct, 1) if used_pct is not None else None,
                "used": top.get("used"), "limit": top.get("limit"),
                "reset_time": top.get("resetTime"),
            })

        if not windows:
            return BalanceInfo(self.id, "quota", available=False,
                               error="套餐接口返回了空数据")
        # 窗口短的排前面 (5 小时比 7 天更需要盯着)
        windows.sort(key=lambda w: (_iso_epoch(w.get("reset_time")) or 0))
        return BalanceInfo(self.id, "plan", windows=windows, raw=d)

    def _payg_balance(self) -> BalanceInfo | None:
        """开放平台按量付费: /users/me/balance 返回可用余额. 不是该端点则返回 None."""
        try:
            r = httpx.get(f"{self.base_url}/users/me/balance",
                          headers=self._headers(), timeout=20)
        except Exception as exc:  # noqa: BLE001
            return BalanceInfo(self.id, "balance", available=False, error=str(exc)[:200])
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            return BalanceInfo(self.id, "balance", available=False,
                               error=f"HTTP {r.status_code}: {r.text[:150]}")
        d = r.json().get("data", {})
        return BalanceInfo(
            self.id, "balance", currency="CNY",
            total=float(d.get("available_balance", 0)),
            raw=d,
        )


class VolcanoProvider(OpenAICompatProvider):
    """火山方舟 (Coding Plan / Agent Plan).

    余额分两条路, 取决于有没有配 AK/SK:
      - 只有 API Key: 火山不提供任何额度查询接口 (coding/v3 下所有路径都 404),
        只能验证 key 有效性, 额度请去控制台看.
      - 配了 AK/SK (env: ARK_ACCESS_KEY / ARK_SECRET_KEY): 走费用中心的
        QueryBalanceAcct (billing.volcengineapi.com, HMAC-SHA256 签名), 能查到真余额.
    """

    id, name = "volcano", "火山方舟"
    base_url = "https://ark.cn-beijing.volces.com/api/v3"
    env_key, env_base = "ARK_API_KEY", "ARK_BASE_URL"
    env_ak, env_sk = "ARK_ACCESS_KEY", "ARK_SECRET_KEY"

    def __init__(self, config: dict[str, str]):
        super().__init__(config)
        self.access_key = config.get(self.env_ak, "").strip()
        self.secret_key = config.get(self.env_sk, "").strip()

    def fetch_balance(self) -> BalanceInfo:
        if not self.configured:
            return BalanceInfo(self.id, "quota", available=False, error="未配置 API key")
        if self.access_key and self.secret_key:
            info = self._billing_balance()
            if info is not None:
                return info
        return self._key_check()

    def _key_check(self) -> BalanceInfo:
        """没有 AK/SK 时的降级: 用 /models 验证 key 是否有效."""
        try:
            r = httpx.get(f"{self.base_url}/models", headers=self._headers(), timeout=20)
            if r.status_code == 200:
                return BalanceInfo(self.id, "quota", raw={
                    "note": "key 有效 · 火山无余额接口, 请到控制台查额度",
                    "console": "https://console.volcengine.com/ark/region:ark+cn-beijing/",
                })
            return BalanceInfo(self.id, "quota", available=False,
                               error=f"HTTP {r.status_code}: {r.text[:150]}")
        except Exception as exc:  # noqa: BLE001
            return BalanceInfo(self.id, "quota", available=False, error=str(exc)[:200])

    def _billing_balance(self) -> BalanceInfo | None:
        """费用中心 QueryBalanceAcct — 需要 AK/SK 的 HMAC-SHA256 签名."""
        try:
            payload = _volc_signed_request(
                "QueryBalanceAcct", "2022-01-01",
                self.access_key, self.secret_key,
                host="billing.volcengineapi.com", region="cn-beijing",
            )
        except Exception as exc:  # noqa: BLE001
            return BalanceInfo(self.id, "balance", available=False,
                               error=f"签名失败: {exc}"[:200])
        result = (payload.get("Result") or {})
        try:
            available = float(result.get("AvailableBalance", 0))
        except (TypeError, ValueError):
            return BalanceInfo(self.id, "balance", available=False,
                               error=f"响应格式意外: {str(payload)[:150]}")
        return BalanceInfo(
            self.id, "balance", currency="CNY",
            total=available,
            raw={"AvailableBalance": available,
                 "CashBalance": result.get("CashBalance"),
                 "CreditLimit": result.get("CreditLimit"),
                 "FreezeAmount": result.get("FreezeAmount")},
        )


class SiliconFlowProvider(OpenAICompatProvider):
    id, name = "siliconflow", "SiliconFlow"
    base_url = "https://api.siliconflow.cn/v1"
    env_key, env_base = "SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL"

    def fetch_balance(self) -> BalanceInfo:
        if not self.configured:
            return BalanceInfo(self.id, "balance", available=False, error="未配置 API key")
        try:
            r = httpx.get(f"{self.base_url}/user/info", headers=self._headers(), timeout=20)
            r.raise_for_status()
            d = r.json().get("data", {})
            return BalanceInfo(
                self.id, "balance", currency="CNY",
                total=float(d.get("totalBalance", 0)),
                used=float(d.get("totalUsage", 0)) if d.get("totalUsage") else None,
                raw=d,
            )
        except Exception as exc:  # noqa: BLE001
            return BalanceInfo(self.id, "balance", available=False, error=str(exc)[:200])


# -------------------------------------------------------------------- Gemini
class GeminiProvider(Provider):
    id, name, kind = "gemini", "Google Gemini", "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    env_key, env_base = "GEMINI_API_KEY", "GEMINI_BASE_URL"

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

    def list_models(self) -> list[str]:
        if not self.configured:
            return []
        r = httpx.get(f"{self.base_url}/models", headers=self._headers(), timeout=20)
        r.raise_for_status()
        out = []
        for m in r.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            if name:
                out.append(name)
        return sorted(out)

    def fetch_balance(self) -> BalanceInfo:
        # Gemini API 没有余额接口; 若配了 Antigravity 凭据, 走配额查询
        return BalanceInfo(self.id, "quota", raw={"note": "Gemini API key 无余额接口, 请用 Antigravity 配额"})

    def chat(self, model: str, messages: list[dict], stream: bool = True,
             max_tokens: int = 1024, temperature: float | None = None,
             timeout: float = 120.0, extra: dict | None = None):
        # OpenAI 格式 -> Gemini 格式
        contents = []
        for m in messages:
            role = "user" if m.get("role") in ("user", "system") else "model"
            contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
        gen_cfg: dict = {"maxOutputTokens": max_tokens}
        if temperature is not None:
            gen_cfg["temperature"] = temperature
        # 客户端给的其他生成参数透传 (topP / topK / stopSequences 等)
        for src, dst in (("top_p", "topP"), ("top_k", "topK"), ("stop", "stopSequences")):
            if extra and extra.get(src) is not None:
                gen_cfg[dst] = extra[src]
        payload = {"contents": contents, "generationConfig": gen_cfg}
        usage: dict = {}
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        url = f"{self.base_url}/models/{model}:{action}"

        def gen():
            nonlocal usage
            with httpx.stream("POST", url, headers=self._headers(), json=payload,
                              timeout=timeout) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        chunk = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    um = chunk.get("usageMetadata")
                    if um:
                        # 原地更新, 同 OpenAICompatProvider 的说明
                        usage.clear()
                        usage.update({
                            "prompt_tokens": um.get("promptTokenCount", 0),
                            "completion_tokens": um.get("candidatesTokenCount", 0),
                            "total_tokens": um.get("totalTokenCount", 0),
                        })
                    text = ""
                    for cand in chunk.get("candidates", []):
                        for part in cand.get("content", {}).get("parts", []):
                            text += part.get("text", "")
                    if text:
                        yield {"choices": [{"delta": {"content": text}}]}

        if stream:
            return gen(), usage
        r = httpx.post(url, headers=self._headers(), json=payload, timeout=timeout)
        r.raise_for_status()
        body = r.json()
        text = ""
        for cand in body.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                text += part.get("text", "")
        um = body.get("usageMetadata", {})
        return (
            {"choices": [{"message": {"content": text}}]},
            {"prompt_tokens": um.get("promptTokenCount", 0),
             "completion_tokens": um.get("candidatesTokenCount", 0),
             "total_tokens": um.get("totalTokenCount", 0)},
        )


# ----------------------------------------------------------------- 自定义厂商
class CustomProvider(OpenAICompatProvider):
    """用户通过 UI 添加的任意 OpenAI 兼容端点."""

    def __init__(self, entry: dict, config: dict[str, str]):
        self.id = entry["id"]
        self.name = entry.get("name") or entry["id"]
        self.kind = entry.get("kind", "openai")
        self.base_url = entry.get("base_url", "").rstrip("/")
        self.env_key = ""      # 直接来自 config.json, 不走 env
        self.env_base = ""
        self.api_key = entry.get("api_key", "")
        self.config = config

    def fetch_balance(self) -> BalanceInfo:
        """自定义端点没有统一的余额接口, 只验证连通性."""
        if not self.configured:
            return BalanceInfo(self.id, "quota", available=False, error="未配置 API key")
        try:
            r = httpx.get(f"{self.base_url}/models", headers=self._headers(), timeout=15)
            if r.status_code == 200:
                return BalanceInfo(self.id, "quota", raw={"note": "端点连通, 无余额接口"})
            return BalanceInfo(self.id, "quota", available=False,
                               error=f"HTTP {r.status_code}: {r.text[:120]}")
        except Exception as exc:  # noqa: BLE001
            return BalanceInfo(self.id, "quota", available=False, error=str(exc)[:200])


# 内置厂商类 (顺序决定仪表盘展示顺序)
PROVIDER_CLASSES = [
    DeepSeekProvider,
    MoonshotProvider,
    GeminiProvider,
    VolcanoProvider,
    SiliconFlowProvider,
]


# ----------------------------------------------------------------- 注册表
def build_providers() -> dict[str, Provider]:
    """构建厂商实例表. 每次调用都重新读配置, 所以改完配置调用它即可热生效."""
    config = load_env()
    instances: list[Provider] = [cls(config) for cls in PROVIDER_CLASSES]
    for entry in (load_ui_config().get("_custom") or []):
        try:
            instances.append(CustomProvider(entry, config))
        except (KeyError, TypeError):
            continue
    return {p.id: p for p in instances}


def provider_status(providers: dict[str, Provider]) -> list[dict]:
    """各厂商配置状态, 用于仪表盘展示."""
    ui = load_ui_config()
    custom_ids = {c["id"] for c in (ui.get("_custom") or [])}
    out = []
    for p in providers.values():
        out.append({
            "id": p.id,
            "name": p.name,
            "kind": p.kind,
            "configured": p.configured,
            "base_url": p.base_url,
            "custom": p.id in custom_ids,
            "api_key_masked": mask_key(getattr(p, "api_key", "")),
            "default_base_url": getattr(type(p), "base_url", ""),
            "env_key": getattr(type(p), "env_key", ""),
        })
    return out
