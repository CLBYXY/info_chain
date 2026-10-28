"""扶摇（同花顺金融数据）MCP over HTTP 的最小客户端。

只用到 initialize + tools/call 两个 JSON-RPC 方法；结果取 structuredContent。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.config import FUYAO_BASE, FUYAO_KEY
from app.models import Stock
from app.sources.http import SourceError, request

_sessions: dict[str, str] = {}
_lock = threading.Lock()


def _post(server: str, body: dict, sid: str | None = None):
    import httpx

    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "X-api-key": FUYAO_KEY}
    if sid:
        headers["Mcp-Session-Id"] = sid
    with httpx.Client(timeout=20) as c:
        r = c.post(f"{FUYAO_BASE}/{server}", json=body, headers=headers)
    r.raise_for_status()
    return r


def _session(server: str) -> str:
    with _lock:
        if server not in _sessions:
            r = _post(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "eventlens", "version": "0.1"}}})
            sid = r.headers.get("Mcp-Session-Id", "")
            _post(server, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
            _sessions[server] = sid
        return _sessions[server]


def call(server: str, tool: str, args: dict, ttl: float | None = 3600) -> Any:
    if not FUYAO_KEY:
        raise SourceError("扶摇", "未配置 HITHINK_FINANCE_API_KEY")

    def fetch():
        try:
            r = _post(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": tool, "arguments": args}}, _session(server))
        except Exception:
            _sessions.pop(server, None)  # 会话过期时重建一次
            r = _post(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": tool, "arguments": args}}, _session(server))
        res = r.json().get("result") or {}
        sc = res.get("structuredContent") or {}
        if res.get("isError") or sc.get("code", 0) != 0:
            raise SourceError("扶摇", f"{tool}: {sc.get('message') or res}")
        return sc.get("data")

    # 复用 http.request 的缓存与降级逻辑：把 fetch 包成一次“请求”
    return _cached(f"fuyao:{server}:{tool}", args, fetch, ttl)


def _cached(name: str, args: dict, fetch, ttl):
    from app.sources import http

    key = http._key(name, args)
    if ttl:
        hit, _ = http._read_cache(key, False, ttl)
        if hit is not None:
            return hit
    try:
        value = fetch()
        http._write_cache(key, value, False)
        return value
    except Exception as e:  # noqa: BLE001
        stale, mtime = http._read_cache(key, False, None)
        if stale is not None:
            http.cache_log.stale_from.append(("扶摇", mtime))
            return stale
        raise e if isinstance(e, SourceError) else SourceError("扶摇", f"{type(e).__name__}: {e}")


# ---------- 业务封装 ----------
def search(q: str, limit: int = 5) -> list[Stock]:
    data = call("meta", "get_meta_tickers_search", {"q": q, "asset_type": "a-share", "limit": limit}, ttl=86400)
    return [Stock(thscode=x["thscode"], name=x["name"]) for x in (data or {}).get("item", [])]


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def daily_prices(thscode: str, start: datetime, end: datetime, index: bool = False) -> list[dict]:
    server, tool = ("a-share-index", "get_a_share_index_prices_historical") if index else \
        ("a-share", "get_a_share_prices_historical")
    args = {"thscode": thscode, "start": _ms(start), "end": _ms(end), "interval": "1d"}
    if not index:
        args["adjust"] = "forward"
    data = call(server, tool, args)
    return sorted((data or {}).get("item", []), key=lambda x: x["date_ms"])


def valuation(thscode: str) -> dict | None:
    data = call("a-share", "get_a_share_valuations_snapshot", {"thscodes": thscode}, ttl=3600)
    items = (data or {}).get("item", [])
    return items[0] if items else None


def financial_indicators(thscode: str, report: str) -> dict:
    return call("a-share", "get_a_share_financials_indicators", {"thscode": thscode, "report": report}, ttl=86400)
