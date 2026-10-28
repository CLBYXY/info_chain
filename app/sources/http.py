"""带重试和磁盘缓存的 HTTP 访问。

失败时按“重试 2 次 → 读缓存（标注数据截至时间）→ 抛 SourceError”处理，
由取数层把 SourceError 记为数据缺口，而不是让整条链路失败。
"""
from __future__ import annotations

import hashlib
import json
import time
from contextvars import ContextVar
from typing import Any, Optional

import httpx

from app.config import CACHE_DIR, HTTP_RETRIES, HTTP_TIMEOUT

UA = {"User-Agent": "Mozilla/5.0 (EventLens research prototype)"}
test_source_failure: ContextVar[bool] = ContextVar("test_source_failure", default=False)


class SourceError(Exception):
    def __init__(self, source: str, message: str):
        super().__init__(f"{source}: {message}")
        self.source = source


class CacheHit:
    """记录本次请求是否用了缓存，以及缓存写入时间。"""

    def __init__(self) -> None:
        self.stale_from: list[tuple[str, float]] = []


cache_log = CacheHit()


def _key(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _cache_path(key: str, binary: bool):
    return CACHE_DIR / f"{key}.{'bin' if binary else 'json'}"


def _read_cache(key: str, binary: bool, ttl: Optional[float]):
    p = _cache_path(key, binary)
    if not p.exists():
        return None, None
    age = time.time() - p.stat().st_mtime
    if ttl is not None and age > ttl:
        return None, p.stat().st_mtime
    return (p.read_bytes() if binary else json.loads(p.read_text("utf-8"))), p.stat().st_mtime


def _write_cache(key: str, value, binary: bool) -> None:
    p = _cache_path(key, binary)
    if binary:
        p.write_bytes(value)
    else:
        p.write_text(json.dumps(value, ensure_ascii=False), "utf-8")


def request(
    source: str,
    method: str,
    url: str,
    *,
    data: Optional[dict] = None,
    json_body: Optional[dict] = None,
    headers: Optional[dict] = None,
    ttl: Optional[float] = 3600,
    binary: bool = False,
    cache_parts: tuple = (),
):
    """ttl=None 表示永久缓存（如公告 PDF 原文）；ttl=0 表示不读新鲜缓存但失败时仍可回退。"""
    key = _key(source, method, url, data, json_body, cache_parts)
    injected = test_source_failure.get() and source in ("巨潮资讯", "巨潮资讯PDF", "互动易")
    if ttl and not injected:
        cached, _ = _read_cache(key, binary, ttl)
        if cached is not None:
            return cached
    if ttl is None and not injected:
        cached, _ = _read_cache(key, binary, None)
        if cached is not None:
            return cached

    last_err: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            if injected:
                raise httpx.ConnectError("测试注入：公告/问答数据接口连接失败")
            with httpx.Client(timeout=HTTP_TIMEOUT, headers={**UA, **(headers or {})}) as c:
                r = c.request(method, url, data=data, json=json_body)
            r.raise_for_status()
            value = r.content if binary else r.json()
            _write_cache(key, value, binary)
            return value
        except Exception as e:  # noqa: BLE001 - 任何网络/解析错误都走重试与降级
            last_err = e
            time.sleep(0.6 * (attempt + 1))

    stale, mtime = (None, None) if injected else _read_cache(key, binary, None)
    if stale is not None:
        cache_log.stale_from.append((source, mtime))
        return stale
    raise SourceError(source, f"{type(last_err).__name__}: {last_err}")
