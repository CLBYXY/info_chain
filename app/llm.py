"""LLM 调用封装（DeepSeek 或 Claude，由 EVENTLENS_LLM_PROVIDER 选择）。

所有 LLM 环节都只通过 json_call 调用，并且都有“未启用 / 调用失败 / 输出不合格”时的规则降级路径：
调用方拿到 None 就走降级逻辑。每次调用都记入 call_log，供过程记录展示。
"""
from __future__ import annotations

import json
import os
import time
from contextvars import ContextVar
from typing import Any, Optional

import httpx

from app.config import DEEPSEEK_BASE, LLM_ENABLED, LLM_MODEL, LLM_PROVIDER

call_log: ContextVar[list] = ContextVar("call_log", default=[])

_anthropic_client = None


def enabled() -> bool:
    return LLM_ENABLED


# ---------------- 输出校验：只覆盖本项目用到的 JSON Schema 子集 ----------------
def valid(value: Any, schema: dict) -> bool:
    t = schema.get("type")
    types = t if isinstance(t, list) else [t]
    if "enum" in schema and value not in schema["enum"]:
        return False
    if value is None:
        return "null" in types
    if "object" in types:
        if not isinstance(value, dict):
            return False
        props = schema.get("properties", {})
        return all(k in value and valid(value[k], props[k]) for k in schema.get("required", [])) and \
            all(valid(v, props[k]) for k, v in value.items() if k in props)
    if "array" in types:
        return isinstance(value, list) and all(valid(v, schema["items"]) for v in value)
    if "string" in types:
        return isinstance(value, str)
    if "number" in types:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _strip(value: Any, schema: dict) -> Any:
    """丢掉 schema 之外的多余字段，避免下游拿到意料之外的键。"""
    if isinstance(value, dict) and "properties" in schema:
        return {k: _strip(v, schema["properties"][k]) for k, v in value.items() if k in schema["properties"]}
    if isinstance(value, list) and "items" in schema:
        return [_strip(v, schema["items"]) for v in value]
    return value


# ---------------- 供应商实现 ----------------
def _deepseek(system: str, user: str, schema: dict, temperature: float) -> tuple[str, dict]:
    sys = (system + "\n\n只输出一个 JSON 对象，不要输出其他文字。JSON 必须符合以下 JSON Schema"
           "（required 字段必须全部给出，enum 字段只能取列出的值）：\n" + json.dumps(schema, ensure_ascii=False))
    r = httpx.post(f"{DEEPSEEK_BASE}/chat/completions", timeout=90,
                   headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"},
                   json={"model": LLM_MODEL, "temperature": temperature, "max_tokens": 8000,
                         "response_format": {"type": "json_object"},
                         "messages": [{"role": "system", "content": sys}, {"role": "user", "content": user}]})
    r.raise_for_status()
    d = r.json()
    u = d.get("usage") or {}
    return d["choices"][0]["message"]["content"], {"in": u.get("prompt_tokens"), "out": u.get("completion_tokens")}


def _anthropic(system: str, user: str, schema: dict, effort: str) -> tuple[Optional[str], dict]:
    global _anthropic_client
    import anthropic

    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(max_retries=2, timeout=60)
    kwargs = dict(model=LLM_MODEL, max_tokens=8000, system=system, messages=[{"role": "user", "content": user}],
                  output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
    try:
        # 拒答时由服务端按类别自动改用备用模型（server-side fallback）
        resp = _anthropic_client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except anthropic.BadRequestError as e:
        if "fallback" not in str(e).lower():
            raise
        resp = _anthropic_client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        return None, {}
    return next(b.text for b in resp.content if b.type == "text"), \
        {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}


def json_call(task: str, system: str, user: str, schema: dict, effort: str = "low",
              temperature: float = 0.0) -> Optional[Any]:
    """temperature 只对 DeepSeek 生效；需要独立多次判定（如类型软判定的 3 票）时调用方传入 > 0。"""
    entry = {"task": task, "model": f"{LLM_PROVIDER}:{LLM_MODEL}", "ok": False, "ms": 0}
    call_log.get().append(entry)
    if not LLM_ENABLED:
        entry["error"] = "LLM 未启用（未配置密钥）"
        return None
    # 内容级缓存：同样的输入（同一文档、同样的提示词）复用同一次结果。
    # 这样话题刷新时，只有新文档会得到新的抽取，新旧快照的差异只来自真实的新信息。
    from app.sources import http as _http

    key = "llm-" + _http._key(LLM_PROVIDER, LLM_MODEL, task, system, user, schema, temperature)
    hit, _ = _http._read_cache(key, False, None)
    if hit is not None:
        entry.update(ok=True, cached=True)
        return hit
    t0 = time.time()
    try:
        value, text = None, None
        for attempt in range(2):  # JSON 截断或为空时重试一次
            if LLM_PROVIDER == "deepseek":
                text, usage = _deepseek(system, user, schema, temperature)
            else:
                text, usage = _anthropic(system, user, schema, effort)
            entry["tokens"] = usage
            if text is None:
                entry["ms"] = int((time.time() - t0) * 1000)
                entry["error"] = "模型拒答"
                return None
            try:
                value = json.loads(text)
                break
            except json.JSONDecodeError:
                entry["retried"] = True
                if attempt == 1:
                    raise
        entry["ms"] = int((time.time() - t0) * 1000)
        if not valid(value, schema):
            entry["error"] = "输出不符合 schema，按规则降级"
            entry["raw"] = text[:300]
            return None
        entry["ok"] = True
        value = _strip(value, schema)
        _http._write_cache(key, value, False)
        return value
    except Exception as e:  # noqa: BLE001 - 任何失败都走规则降级
        entry["ms"] = int((time.time() - t0) * 1000)
        entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return None


def parallel(fns: list, workers: int = 8) -> list:
    """并发执行若干个无参函数，保留调用记录（contextvars 复制到各线程）。"""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(contextvars.copy_context().run, fn) for fn in fns]
        return [f.result() for f in futures]


def obj(props: dict, required: Optional[list] = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


STR = {"type": "string"}
NSTR = {"type": ["string", "null"]}
