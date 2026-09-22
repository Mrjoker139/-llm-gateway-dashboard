"""用量记录数据库 — 网关的"账本".

每条经过网关的模型请求都会在这里留下一行: 哪个会话、哪个模型、
输入/输出多少 token、首字延迟多久、总共耗时多久.

有了这些数据, "监控 token 用量"和"测每次会话用时"就都只是 SQL 查询.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "usage.db"
PRICING_PATH = Path(__file__).parent / "pricing.json"

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,          -- ISO8601 UTC
    session_id    TEXT,                      -- 会话分组
    provider      TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    client        TEXT,                      -- 哪个工具发的 (claude-cli / cline / ...)
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    ttft_ms       INTEGER,                   -- 首字延迟
    duration_ms   INTEGER,                   -- 总耗时
    stream        INTEGER DEFAULT 0,
    status        INTEGER,                   -- HTTP 状态码
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts      ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);
CREATE INDEX IF NOT EXISTS idx_requests_provider ON requests(provider, model);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    provider    TEXT,
    model       TEXT,
    client      TEXT,                        -- 哪个工具在用
    label       TEXT                         -- 用户可自定义的会话名
);

-- 基准测试结果. Antigravity 走 Google 内部 OAuth API, 不经过网关,
-- 所以它的实测数据单独存这里, 再合并进模型性能视图.
CREATE TABLE IF NOT EXISTS benchmarks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    provider       TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    ttft_ms        INTEGER,
    total_ms       INTEGER,
    tokens         INTEGER,
    tokens_per_sec REAL,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_benchmarks_model ON benchmarks(provider, model);

-- 节点出口 IP 对 Gemini 的可用性. 按节点名索引, 这样节点列表能直接标记.
-- 单次测试不作数 (网络会抖), 所以记 attempts/fails, 由 verdict 给出结论.
CREATE TABLE IF NOT EXISTS node_checks (
    node       TEXT PRIMARY KEY,      -- 节点名 (节点列表用它标记)
    ip         TEXT,                  -- 最近一次测到的出口 IP
    country    TEXT,
    ok         INTEGER,               -- 1=可用, 0=不可用
    attempts   INTEGER DEFAULT 0,     -- 一共测了几次
    fails      INTEGER DEFAULT 0,     -- 其中失败几次
    verdict    TEXT,                  -- ok | blocked | flaky | unknown
    error      TEXT,
    ts         TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列: 早期版本的 requests 表没有 client 字段."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(requests)").fetchall()}
    if "client" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN client TEXT")
        conn.commit()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


def init_db() -> None:
    with _write_lock:
        conn = _conn()
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_request(
    *,
    session_id: str | None,
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    ttft_ms: int | None = None,
    duration_ms: int | None = None,
    stream: bool = False,
    status: int = 200,
    error: str | None = None,
    client: str | None = None,
) -> None:
    """记录一次请求, 并更新会话信息."""
    ts = now_iso()
    with _write_lock:
        conn = _conn()
        conn.execute(
            """INSERT INTO requests
               (ts, session_id, provider, model, client, input_tokens, output_tokens,
                ttft_ms, duration_ms, stream, status, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, session_id, provider, model, client, input_tokens, output_tokens,
             ttft_ms, duration_ms, int(stream), status, error),
        )
        if session_id:
            conn.execute(
                """INSERT INTO sessions (id, started_at, last_seen, provider, model, client)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       last_seen = excluded.last_seen,
                       model     = COALESCE(excluded.model, sessions.model),
                       provider  = COALESCE(excluded.provider, sessions.provider)""",
                (session_id, ts, ts, provider, model, client),
            )
        conn.commit()


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in _conn().execute(sql, params).fetchall()]


def _since(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def summary(hours: float = 24) -> dict:
    """总体概览: 请求数 / token 总量 / 平均首字延迟."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    row = _rows(
        """SELECT COUNT(*)              AS requests,
                  COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  AVG(ttft_ms)         AS avg_ttft,
                  AVG(duration_ms)     AS avg_duration,
                  SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors
           FROM requests WHERE ts >= ?""",
        (since,),
    )[0]
    row["avg_ttft"] = round(row["avg_ttft"]) if row["avg_ttft"] else None
    row["avg_duration"] = round(row["avg_duration"]) if row["avg_duration"] else None
    row["total_tokens"] = row["input_tokens"] + row["output_tokens"]
    row["window_hours"] = hours
    return row


def by_provider(hours: float = 24) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = _rows(
        """SELECT provider,
                  COUNT(*) AS requests,
                  COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  AVG(ttft_ms) AS avg_ttft
           FROM requests WHERE ts >= ?
           GROUP BY provider ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC""",
        (since,),
    )
    for r in rows:
        r["total_tokens"] = r["input_tokens"] + r["output_tokens"]
        r["avg_ttft"] = round(r["avg_ttft"]) if r["avg_ttft"] else None
    return rows


def by_model(hours: float = 24) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = _rows(
        """SELECT model, provider,
                  COUNT(*) AS requests,
                  COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  AVG(ttft_ms) AS avg_ttft
           FROM requests WHERE ts >= ?
           GROUP BY model, provider
           ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC""",
        (since,),
    )
    for r in rows:
        r["total_tokens"] = r["input_tokens"] + r["output_tokens"]
        r["avg_ttft"] = round(r["avg_ttft"]) if r["avg_ttft"] else None
    return rows


def recent_sessions(limit: int = 30) -> list[dict]:
    """会话列表: 每次 vibecoding 会话的耗时、token 消耗、请求数."""
    rows = _rows(
        """SELECT s.id, s.started_at, s.last_seen, s.client, s.label,
                  COUNT(r.id) AS requests,
                  COALESCE(SUM(r.input_tokens), 0)  AS input_tokens,
                  COALESCE(SUM(r.output_tokens), 0) AS output_tokens,
                  AVG(r.ttft_ms) AS avg_ttft,
                  (julianday(s.last_seen) - julianday(s.started_at)) * 86400 AS span_seconds
           FROM sessions s
           LEFT JOIN requests r ON r.session_id = s.id
           GROUP BY s.id
           ORDER BY s.last_seen DESC LIMIT ?""",
        (limit,),
    )
    for r in rows:
        r["total_tokens"] = r["input_tokens"] + r["output_tokens"]
        r["avg_ttft"] = round(r["avg_ttft"]) if r["avg_ttft"] else None
        r["span_seconds"] = round(r["span_seconds"]) if r["span_seconds"] else 0
    return rows


# 生成阶段耗时 (总耗时 - 首字延迟)
_GEN_MS_EXPR = """CASE WHEN output_tokens > 0
                           AND duration_ms IS NOT NULL AND ttft_ms IS NOT NULL
                      THEN duration_ms - ttft_ms END"""

# 生成速度 (token/秒). 退化样本要剔除: 生成窗口不到 10ms 或输出不足 10 个
# token 时, 除出来的数字没有意义 (还会把 avg / p99 整个带偏).
_TPS_EXPR = f"""CASE WHEN {_GEN_MS_EXPR} >= 10 AND output_tokens >= 10
                     THEN 1000.0 * output_tokens / ({_GEN_MS_EXPR}) END"""

# 参与分位数统计的列. 键名 -> SQL 表达式
_METRIC_COLUMNS = {
    "ttft": "ttft_ms",
    "duration": "duration_ms",
    "tps": _TPS_EXPR,
}

# 各厂商默认单价 (元 / 百万 token). 可用 pricing.json 覆盖.
# 这里只放常用模型; 表里没有的模型不显示成本 (—), 不猜.
DEFAULT_PRICING: dict[str, dict] = {
    "deepseek": {
        "deepseek-chat":     {"input": 2.0, "output": 8.0},
        "deepseek-reasoner": {"input": 4.0, "output": 16.0},
    },
    "moonshot": {
        "kimi-k2-0905-preview":    {"input": 4.0, "output": 16.0},
        "kimi-k2-turbo-preview":   {"input": 8.0, "output": 32.0},
        "moonshot-v1-8k":          {"input": 12.0, "output": 12.0},
        "moonshot-v1-32k":         {"input": 24.0, "output": 24.0},
        "moonshot-v1-128k":        {"input": 60.0, "output": 60.0},
    },
    "gemini": {
        "gemini-2.5-flash": {"input": 0.5, "output": 3.0},
        "gemini-2.5-pro":   {"input": 4.0, "output": 20.0},
    },
    "volcano": {
        "doubao-seed-1-6":     {"input": 0.8, "output": 2.0},
        "doubao-seed-1-6-flash": {"input": 0.15, "output": 0.6},
    },
}


def load_pricing() -> dict[str, dict]:
    """单价表: pricing.json 覆盖内置默认 (按 厂商 -> 模型 覆盖)."""
    table = json.loads(json.dumps(DEFAULT_PRICING))
    if PRICING_PATH.exists():
        try:
            override = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return table
        for pid, models in (override or {}).items():
            if isinstance(models, dict):
                table.setdefault(pid, {}).update(models)
    return table


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """线性插值分位数 (和 numpy.percentile 的默认算法一致)."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[int(pos)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _metric_percentiles(rows: list[dict]) -> dict:
    """算一组请求各指标的分位数: {ttft:{p10,p50,p90,avg}, duration:{...}, tps:{...}}."""
    out: dict[str, dict] = {}
    for name in _METRIC_COLUMNS:
        vals = sorted(r[name] for r in rows if r.get(name) is not None)
        if not vals:
            out[name] = {"avg": None, "p10": None, "p50": None, "p90": None, "p95": None, "p99": None}
            continue
        out[name] = {
            "avg": round(sum(vals) / len(vals), 1),
            "p10": round(_percentile(vals, 0.10), 1),
            "p50": round(_percentile(vals, 0.50), 1),
            "p90": round(_percentile(vals, 0.90), 1),
            "p95": round(_percentile(vals, 0.95), 1),
            "p99": round(_percentile(vals, 0.99), 1),
        }
    return out


def model_stats(hours: float = 168, include_benchmarks: bool = True) -> list[dict]:
    """按 厂商+模型 聚合的性能统计 — 横向比较模型的核心视图.

    包含: 首字延迟(TTFT) / 生成速度(TPS) / 耗时的 avg 与 P50-P99 分位数,
    以及 token 量、成本估算、输出输入比.

    include_benchmarks=True 时, 把基准测试 (Antigravity 那些走不了网关的模型)
    也并进来. 它们的 token 量/成本是空的, 但延迟和速度是真测出来的.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = _rows(
        """SELECT provider, model,
                   COUNT(*)                        AS requests,
                   COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   AVG(ttft_ms)      AS avg_ttft,
                   MIN(ttft_ms)      AS min_ttft,
                   MAX(ttft_ms)      AS max_ttft,
                   AVG(duration_ms)  AS avg_duration,
                   AVG(input_tokens) AS avg_input,
                   AVG(output_tokens) AS avg_output,
                   COUNT(DISTINCT client) AS client_count,
                   GROUP_CONCAT(DISTINCT client) AS clients,
                   MAX(ts) AS last_used,
                   SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors
            FROM requests
            WHERE ts >= ?
            GROUP BY provider, model
            ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC""",
        (since,),
    )
    pricing = load_pricing()
    # 分位数要拿原始样本算, 不能从聚合值推 — 一次取回所有需要的列
    selects = ", ".join(f"{expr} AS {name}" for name, expr in _METRIC_COLUMNS.items())
    samples = _rows(
        f"""SELECT provider, model, {selects} FROM requests WHERE ts >= ?""",
        (since,),
    )
    by_model: dict[tuple[str, str], list[dict]] = {}
    for s in samples:
        by_model.setdefault((s["provider"], s["model"]), []).append(s)

    for r in rows:
        r["total_tokens"] = r["input_tokens"] + r["output_tokens"]
        for key in ("avg_ttft", "min_ttft", "max_ttft", "avg_duration"):
            r[key] = round(r[key]) if r[key] else None
        r["avg_input"] = round(r["avg_input"]) if r["avg_input"] else 0
        r["avg_output"] = round(r["avg_output"]) if r["avg_output"] else 0
        # 输出/输入比: 同样输入下产出多少, 越高越"划算"
        r["io_ratio"] = round(r["output_tokens"] / r["input_tokens"], 2) if r["input_tokens"] else None

        pct = _metric_percentiles(by_model.get((r["provider"], r["model"]), []))
        r["ttft"] = pct["ttft"]
        r["duration"] = pct["duration"]
        r["tps"] = pct["tps"]
        r["source"] = "gateway"        # 数据来自网关实测流量
        r["bench_requests"] = 0
        # 旧版 API 的兼容字段 (TPOT 是 TPS 的倒数). 新前端用 ttft/duration/tps 那三组.
        avg_tps = pct["tps"]["avg"]
        r["avg_tpot"] = round(1000 / avg_tps, 1) if avg_tps else None
        r["speed_tps"] = round(avg_tps, 1) if avg_tps else None

        # 成本估算: 只认单价表里有的模型, 没有就留空 (不猜)
        price = (pricing.get(r["provider"]) or {}).get(r["model"])
        if price:
            r["cost"] = round(
                r["input_tokens"] / 1e6 * price.get("input", 0)
                + r["output_tokens"] / 1e6 * price.get("output", 0), 4)
            r["currency"] = price.get("currency", "CNY")
        else:
            r["cost"] = None
            r["currency"] = None

    if include_benchmarks:
        _merge_benchmarks(rows, hours)
    return rows


def _merge_benchmarks(rows: list[dict], hours: float) -> None:
    """把基准测试结果并进 model_stats 的结果里.

    同一个模型两边都有数据时 (比如火山模型既走网关又跑过基准), 以网关的
    真实流量为准, 只把基准次数记下来; 只有基准数据的模型才补一行.
    """
    bench = benchmark_stats(hours)
    if not bench:
        return
    index = {(r["provider"], r["model"]): r for r in rows}
    for b in bench:
        key = (b["provider"], b["model"])
        existing = index.get(key)
        if existing:
            existing["bench_requests"] = b["ok_count"]
            existing["source"] = "gateway+bench"
            continue
        # 纯基准数据: 没有 token 量/成本, 只有延迟和速度
        rows.append({
            "provider": b["provider"], "model": b["model"],
            "requests": 0, "input_tokens": 0, "output_tokens": 0,
            "total_tokens": 0, "avg_input": 0, "avg_output": 0,
            "avg_ttft": b["avg_ttft"], "min_ttft": None, "max_ttft": None,
            "avg_duration": b["avg_duration"],
            "clients": None, "client_count": 0, "last_used": b["last_used"],
            "errors": b["errors"],
            "ttft": b["ttft"], "duration": b["duration"], "tps": b["tps"],
            "io_ratio": None, "cost": None, "currency": None,
            "source": "bench", "bench_requests": b["ok_count"],
            "avg_tpot": round(1000 / b["avg_tps"], 1) if b["avg_tps"] else None,
            "speed_tps": round(b["avg_tps"], 1) if b["avg_tps"] else None,
        })


def model_detail(provider: str, model: str, limit: int = 200) -> list[dict]:
    """单个模型的逐条请求明细 (供展开查看)."""
    rows = _rows(
        f"""SELECT id, ts, session_id, client, input_tokens, output_tokens,
                   ttft_ms, duration_ms, stream, status, error,
                   {_TPS_EXPR} AS speed_tps
            FROM requests
            WHERE provider = ? AND model = ?
            ORDER BY ts DESC LIMIT ?""",
        (provider, model, limit),
    )
    for r in rows:
        r["speed_tps"] = round(r["speed_tps"], 1) if r["speed_tps"] else None
    return rows


def session_detail(session_id: str) -> dict:
    rows = _rows(
        """SELECT ts, model, provider, input_tokens, output_tokens,
                  ttft_ms, duration_ms, status
           FROM requests WHERE session_id = ? ORDER BY ts""",
        (session_id,),
    )
    meta = _rows("SELECT * FROM sessions WHERE id = ?", (session_id,))
    return {"session": meta[0] if meta else None, "requests": rows}


def timeline(hours: float = 24, buckets: int = 24) -> list[dict]:
    """按时间分桶的 token 消耗, 用于画曲线."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    bucket_minutes = max(1, int(hours * 60 / buckets))
    rows = _rows(
        """SELECT ts, input_tokens, output_tokens FROM requests WHERE ts >= ?""",
        (since.isoformat(timespec="seconds"),),
    )
    buckets_data: dict[str, dict] = {}
    for r in rows:
        try:
            t = datetime.fromisoformat(r["ts"])
        except ValueError:
            continue
        minutes_ago = (datetime.now(timezone.utc) - t).total_seconds() / 60
        idx = int(minutes_ago // bucket_minutes)
        key = f"{idx * bucket_minutes}"
        b = buckets_data.setdefault(key, {"input": 0, "output": 0})
        b["input"] += r["input_tokens"]
        b["output"] += r["output_tokens"]
    out = []
    for i in range(buckets):
        key = str(i * bucket_minutes)
        b = buckets_data.get(key, {"input": 0, "output": 0})
        out.append({"minutes_ago": i * bucket_minutes, **b})
    out.reverse()
    return out


def record_benchmark(*, provider: str, model: str, ttft_ms: int | None,
                     total_ms: int | None, tokens: int | None,
                     tokens_per_sec: float | None, error: str | None = None) -> None:
    """存一条基准测试结果."""
    with _write_lock:
        conn = _conn()
        conn.execute(
            """INSERT INTO benchmarks
               (ts, provider, model, ttft_ms, total_ms, tokens, tokens_per_sec, error)
               VALUES (?,?,?,?,?,?,?,?)""",
            (now_iso(), provider, model, ttft_ms, total_ms, tokens, tokens_per_sec,
             error[:300] if error else None),
        )
        conn.commit()


def benchmark_stats(hours: float = 168) -> list[dict]:
    """基准测试的聚合结果, 按 厂商+模型 汇总 (和 model_stats 同构, 便于合并)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = _rows(
        """SELECT provider, model,
                  COUNT(*)                                   AS requests,
                  SUM(CASE WHEN error IS NULL THEN 1 ELSE 0 END) AS ok_count,
                  AVG(CASE WHEN error IS NULL THEN ttft_ms END)  AS avg_ttft,
                  AVG(CASE WHEN error IS NULL THEN total_ms END) AS avg_duration,
                  AVG(CASE WHEN error IS NULL THEN tokens_per_sec END) AS avg_tps,
                  MAX(ts) AS last_used
           FROM benchmarks WHERE ts >= ?
           GROUP BY provider, model""",
        (since,),
    )
    # 分位数同样要原始样本
    samples = _rows(
        """SELECT provider, model, ttft_ms AS ttft, total_ms AS duration,
                  tokens_per_sec AS tps
           FROM benchmarks WHERE ts >= ? AND error IS NULL""",
        (since,),
    )
    by_model: dict[tuple[str, str], list[dict]] = {}
    for s in samples:
        by_model.setdefault((s["provider"], s["model"]), []).append(s)

    for r in rows:
        pct = _metric_percentiles(by_model.get((r["provider"], r["model"]), []))
        r["ttft"], r["duration"], r["tps"] = pct["ttft"], pct["duration"], pct["tps"]
        r["errors"] = r["requests"] - r["ok_count"]
        r["avg_ttft"] = round(r["avg_ttft"]) if r["avg_ttft"] else None
        r["avg_duration"] = round(r["avg_duration"]) if r["avg_duration"] else None
    return rows


def benchmark_detail(provider: str, model: str, limit: int = 100) -> list[dict]:
    """单个模型的基准测试逐条明细."""
    return _rows(
        """SELECT id, ts, ttft_ms, total_ms, tokens, tokens_per_sec, error
           FROM benchmarks WHERE provider = ? AND model = ?
           ORDER BY ts DESC LIMIT ?""",
        (provider, model, limit),
    )


def record_node_check(*, node: str, ip: str, country: str | None,
                      ok: bool, error: str | None = None) -> dict:
    """记一次节点检测. 同一个节点累积统计, 并给出结论.

    单次失败可能是网络抖动, 所以结论看多次结果:
      ok      — 每次都通
      blocked — 每次都因为地区限制被拒 (Google 明确拒绝, 换节点才有用)
      flaky   — 有时通有时不通 (网络不稳或节点不稳定)
    """
    with _write_lock:
        conn = _conn()
        row = conn.execute("SELECT attempts, fails FROM node_checks WHERE node = ?",
                           (node,)).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        fails = (row["fails"] if row else 0) + (0 if ok else 1)
        # 只有"地区限制"这类硬拒绝才叫 blocked; 超时/网络错误算 flaky
        hard_block = (not ok) and error and "location is not supported" in error.lower()
        if fails == 0:
            verdict = "ok"
        elif fails == attempts:
            verdict = "blocked" if hard_block else "unknown"
        else:
            verdict = "flaky"
        conn.execute(
            """INSERT INTO node_checks
               (node, ip, country, ok, attempts, fails, verdict, error, ts)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(node) DO UPDATE SET
                   ip=excluded.ip, country=excluded.country, ok=excluded.ok,
                   attempts=excluded.attempts, fails=excluded.fails,
                   verdict=excluded.verdict, error=excluded.error, ts=excluded.ts""",
            (node, ip, country, int(ok), attempts, fails, verdict,
             error[:200] if error else None, now_iso()),
        )
        conn.commit()
        return {"node": node, "ip": ip, "country": country, "ok": ok,
                "attempts": attempts, "fails": fails, "verdict": verdict,
                "error": error}


def node_checks() -> dict[str, dict]:
    """所有检测过的节点 -> 结果. 键是节点名."""
    return {r["node"]: r for r in _rows("SELECT * FROM node_checks ORDER BY ts DESC")}


def _fit_prefill_slope(samples: list[tuple[int, float]]) -> float:
    """拟合 TTFT 随输入 token 增长的斜率 (ms/token), 用来消除 prompt 长度的干扰.

    首字延迟里很大一部分是"预填充"——prompt 越长, TTFT 越大. 如果只比较各时段
    的原始 TTFT, 就会把"那个时段正好在处理大文件"误判成"那个时段模型拥堵".
    拟合出斜率后就能把不同长度的请求折算到同一基准上比较.

    模型: ttft ≈ a + b * input_tokens (最小二乘). 返回 b.
    样本不足或拟合不出正斜率时返回 0 (即不做归一化, 退化为原始比较).
    """
    pts = [(t, v) for t, v in samples if t and t > 0 and v is not None]
    if len(pts) < 8:                       # 样本太少, 拟合不可靠
        return 0.0
    n = len(pts)
    sx = sum(t for t, _ in pts)
    sy = sum(v for _, v in pts)
    sxx = sum(t * t for t, _ in pts)
    sxy = sum(t * v for t, v in pts)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    b = (n * sxy - sx * sy) / denom
    # 负斜率不合理 (prompt 更长反而更快), 说明没有真实关联, 不做归一化
    return b if b > 0 else 0.0


def congestion_by_hour(hours: float = 168, model: str | None = None) -> list[dict]:
    """按「本地小时」聚合的性能数据 — 用来看什么时段模型会拥堵.

    关键: 各时段的 TTFT 会受 prompt 长度影响, 所以先把每个样本按该模型的
    预填充斜率折算到统一基准 (norm_ttft), 再比较. 用中位数而不是均值,
    避免个别超长请求带偏整个时段.

    只看经过网关的真实流量 (基准测试样本太少, 混进来会失真).
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    where = "ts >= ?"
    params: list = [since]
    if model:
        where += " AND model = ?"
        params.append(model)
    rows = _rows(
        f"""SELECT provider, model, ts, input_tokens, ttft_ms, duration_ms, output_tokens
            FROM requests WHERE {where} AND status < 400""",
        tuple(params),
    )

    # 第一遍: 按模型收集样本, 拟合预填充斜率
    by_model: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for r in rows:
        if r["ttft_ms"] is not None:
            by_model.setdefault((r["provider"], r["model"]), []).append(
                (r["input_tokens"] or 0, float(r["ttft_ms"])))
    slopes = {k: _fit_prefill_slope(v) for k, v in by_model.items()}
    ref_tokens = {}
    for k, v in by_model.items():
        toks = sorted(t for t, _ in v if t > 0)
        ref_tokens[k] = toks[len(toks) // 2] if toks else 0

    # 第二遍: 折算到统一基准后按小时分桶
    buckets: dict[tuple, dict] = {}
    for r in rows:
        try:
            t = datetime.fromisoformat(r["ts"]).astimezone()
        except ValueError:
            continue
        key = (r["provider"], r["model"], t.hour)
        b = buckets.setdefault(key, {
            "provider": r["provider"], "model": r["model"], "hour": t.hour,
            "requests": 0, "ttft": [], "norm": [], "tps": [], "input": [],
        })
        b["requests"] += 1
        inp = r["input_tokens"] or 0
        if inp:
            b["input"].append(inp)
        if r["ttft_ms"] is not None:
            b["ttft"].append(r["ttft_ms"])
            slope = slopes.get((r["provider"], r["model"]), 0.0)
            ref = ref_tokens.get((r["provider"], r["model"]), 0)
            # 折算成"如果 prompt 是这个模型的典型长度, TTFT 会是多少"
            b["norm"].append(max(1.0, r["ttft_ms"] - slope * (inp - ref)))
        if (r["duration_ms"] is not None and r["ttft_ms"] is not None
                and r["output_tokens"] and r["output_tokens"] >= 10):
            gen_ms = r["duration_ms"] - r["ttft_ms"]
            if gen_ms >= 10:
                b["tps"].append(1000.0 * r["output_tokens"] / gen_ms)

    out = []
    for b in buckets.values():
        ttft = sorted(b["ttft"])
        norm = sorted(b["norm"])
        tps = sorted(b["tps"])
        inputs = sorted(b["input"])
        out.append({
            "provider": b["provider"], "model": b["model"], "hour": b["hour"],
            "requests": b["requests"],
            # median: 比均值抗离群值
            "median_ttft": round(_percentile(ttft, 0.50)) if ttft else None,
            "norm_ttft": round(_percentile(norm, 0.50)) if norm else None,
            "p90_ttft": round(_percentile(ttft, 0.90)) if ttft else None,
            "median_input": round(_percentile(inputs, 0.50)) if inputs else None,
            "median_tps": round(_percentile(tps, 0.50), 1) if tps else None,
        })
    out.sort(key=lambda r: (r["model"], r["hour"]))
    return out


def model_congestion(hours: float = 168) -> list[dict]:
    """每个模型的"拥堵画像": 最慢时段 vs 最快时段的差距.

    用归一化后的 TTFT 比较 (已消除 prompt 长度干扰), 回答
    "这个模型有没有明显的拥堵时段, 有的话该在什么时候换掉它".

    每个模型单独统计, 所以多个模型混用不会互相污染.
    """
    hourly = congestion_by_hour(hours)
    by_model: dict[tuple[str, str], list[dict]] = {}
    for h in hourly:
        by_model.setdefault((h["provider"], h["model"]), []).append(h)

    MIN_SAMPLES = 3        # 每个时段至少 3 次请求, 否则不进结论 (样本太少的噪声大)
    out = []
    for (provider, model), hs in by_model.items():
        solid = [h for h in hs if h["requests"] >= MIN_SAMPLES and h["norm_ttft"]]
        if len(solid) < 2:      # 至少要有两个时段才能比较
            continue
        worst = max(solid, key=lambda h: h["norm_ttft"])
        best = min(solid, key=lambda h: h["norm_ttft"])
        ratio = (worst["norm_ttft"] / best["norm_ttft"]) if best["norm_ttft"] else None
        out.append({
            "provider": provider, "model": model,
            "hours_covered": len(solid),
            "hours_total": len(hs),
            "samples": sum(h["requests"] for h in hs),
            "best_hour": best["hour"], "best_ttft": best["median_ttft"],
            "worst_hour": worst["hour"], "worst_ttft": worst["median_ttft"],
            # 拥堵倍数: 用归一化值算, 2 倍以上说明有明显的时段差异
            "congestion_ratio": round(ratio, 2) if ratio else None,
            "hourly": sorted(hs, key=lambda h: h["hour"]),
        })
    out.sort(key=lambda r: (r["congestion_ratio"] or 0), reverse=True)
    return out


def delete_record(record_type: str, record_id: int) -> bool:
    """按 ID 删除单条 request 或 benchmark 记录."""
    table = "requests" if record_type == "request" else "benchmarks" if record_type == "benchmark" else None
    if not table:
        return False
    with _write_lock:
        conn = _conn()
        cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
        conn.commit()
        return cur.rowcount > 0


def clear_errors(provider: str | None = None, model: str | None = None) -> dict[str, int]:
    """清除失败记录. 若指定 provider/model 则仅清指定模型, 否则清空全库所有失败记录."""
    with _write_lock:
        conn = _conn()
        if provider and model:
            c1 = conn.execute(
                "DELETE FROM requests WHERE provider = ? AND model = ? AND (status >= 400 OR error IS NOT NULL)",
                (provider, model),
            ).rowcount
            c2 = conn.execute(
                "DELETE FROM benchmarks WHERE provider = ? AND model = ? AND error IS NOT NULL",
                (provider, model),
            ).rowcount
        else:
            c1 = conn.execute("DELETE FROM requests WHERE status >= 400 OR error IS NOT NULL").rowcount
            c2 = conn.execute("DELETE FROM benchmarks WHERE error IS NOT NULL").rowcount
        conn.commit()
        return {"requests": c1, "benchmarks": c2, "total": c1 + c2}


def clear_model_history(provider: str, model: str) -> dict[str, int]:
    """彻底清空指定模型在网关流量和基准测试中的全部历史数据."""
    with _write_lock:
        conn = _conn()
        c1 = conn.execute("DELETE FROM requests WHERE provider = ? AND model = ?", (provider, model)).rowcount
        c2 = conn.execute("DELETE FROM benchmarks WHERE provider = ? AND model = ?", (provider, model)).rowcount
        conn.execute(
            "DELETE FROM sessions WHERE id NOT IN (SELECT DISTINCT session_id FROM requests WHERE session_id IS NOT NULL)"
        )
        conn.commit()
        return {"requests": c1, "benchmarks": c2, "total": c1 + c2}


def purge_older_than(days: int = 90, error_days: int | None = 7) -> dict[str, int]:
    """清理过期数据. 正常数据保留 days 天, 失败记录可设置更短的保留天数 error_days."""
    with _write_lock:
        conn = _conn()
        req_purged = 0
        bench_purged = 0

        # 先清理较短保留期内的失败记录 (避免历史错误长期污染看板)
        if error_days is not None and error_days < days:
            err_since = _since(error_days)
            c1 = conn.execute(
                "DELETE FROM requests WHERE (status >= 400 OR error IS NOT NULL) AND ts < ?",
                (err_since,),
            ).rowcount
            c2 = conn.execute(
                "DELETE FROM benchmarks WHERE error IS NOT NULL AND ts < ?",
                (err_since,),
            ).rowcount
            req_purged += c1
            bench_purged += c2

        # 淘汰超过总保留天数的所有记录
        total_since = _since(days)
        c3 = conn.execute("DELETE FROM requests WHERE ts < ?", (total_since,)).rowcount
        c4 = conn.execute("DELETE FROM benchmarks WHERE ts < ?", (total_since,)).rowcount
        req_purged += c3
        bench_purged += c4

        # 清理孤立 session
        conn.execute(
            "DELETE FROM sessions WHERE id NOT IN (SELECT DISTINCT session_id FROM requests WHERE session_id IS NOT NULL)"
        )
        conn.commit()
        return {"requests": req_purged, "benchmarks": bench_purged, "total": req_purged + bench_purged}

