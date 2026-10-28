"""把六层串起来，并为每一层留下过程记录（输入输出概要、耗时、LLM 调用、降级情况）。"""
from __future__ import annotations

import time
from datetime import datetime

from app import llm
from app.config import LLM_ENABLED, LLM_MODEL, LLM_PROVIDER
from app.layers import evidence, event, fetch, generate, relate, understand
from app.models import Persona, TraceStep
from app.sources import http


def run(question: str, persona: Persona = "retail", watchlist: list[str] | None = None,
        now: datetime | None = None, on_step=None) -> dict:
    """on_step(layer, state, summary)：每层开始（running）和结束（done）时回调，用于前端逐层进度。"""
    now = now or datetime.now()
    llm.call_log.set([])
    http.cache_log.stale_from = []
    trace: list[TraceStep] = []

    def step(layer: str, fn, summarize):
        if on_step:
            on_step(layer, "running", "")
        t = time.time()
        out = fn()
        trace.append(TraceStep(layer=layer, summary=summarize(out), ms=int((time.time() - t) * 1000)))
        if on_step:
            on_step(layer, "done", trace[-1].summary)
        return out

    q = step("① 理解层", lambda: understand.understand(question, persona, watchlist, now),
             lambda q: f"意图 {'+'.join(q.intents)}；股票 {', '.join(f'{s.name}({s.thscode})' for s in q.stocks) or '无'}；"
                       f"关键词 {'、'.join(q.keywords) or '无'}；时间窗 {q.start:%Y-%m-%d}~{q.end:%Y-%m-%d}"
                       f"{'（默认近90天）' if q.time_defaulted else ''}")
    trace[-1].detail = q.model_dump(mode="json")

    if q.fallback:
        ans = generate.fallback_answer(q)
        return _finish(ans, q, trace, [])

    docs, fstats, gaps = step("② 取数层", lambda: fetch.fetch(q),
                              lambda r: f"公告 {r[1]['announcements']} 份、互动易已回复问答 {r[1]['irm']} 条 → 选中 {len(r[0])} 份文档")
    trace[-1].detail = fstats

    claims, estats = step("③ 证据层", lambda: evidence.extract(docs, q),
                          lambda r: f"抽取 {len(r[0])} 条声明（原文回查失败丢弃 {r[1].get('dropped_unverified', 0)} 条）")
    trace[-1].detail = estats

    ev, evstats = step("④ 事件层", lambda: event.build(claims, q, now),
                       lambda r: f"{len(r[0].nodes)} 个节点、{len(r[0].edges)} 条连线；状态：{r[0].status}（{r[0].grade}）")
    trace[-1].detail = evstats

    rel, rgaps = step("⑤ 关联层", lambda: relate.relate(ev, q, now),
                      lambda r: f"关联标的 {len(r[0].stocks)} 个；"
                                + (f"t0={r[0].market.t0}，累计超额收益 {r[0].market.car}%" if r[0].market else "无行情窗口"))
    gaps += rgaps

    ans, checks = step("⑥ 生成层", lambda: generate.generate(q, ev, rel, docs, gaps, now),
                       lambda r: f"结论由{r[1]['writer']}撰写；引用回查删除 {len(r[1]['citation_removed'])} 句；"
                                 f"违禁词 {len(r[1]['banned_removed'])} 个")
    trace[-1].detail = checks
    return _finish(ans, q, trace, gaps)


def _finish(ans: dict, q, trace: list[TraceStep], gaps: list[str]) -> dict:
    for src, mtime in http.cache_log.stale_from:
        msg = f"部分数据暂时无法刷新，使用的是 {datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M} 获取的版本"
        if msg not in gaps:
            gaps.append(msg)
    ans["query"] = {"question": q.question, "intents": q.intents, "persona": q.persona,
                    "stocks": [s.model_dump() for s in q.stocks], "keywords": q.keywords,
                    "window": [f"{q.start:%Y-%m-%d}", f"{q.end:%Y-%m-%d}"], "time_defaulted": q.time_defaulted}
    ans["trace"] = [t.model_dump() for t in trace]
    ans["llm"] = {"enabled": LLM_ENABLED, "model": f"{LLM_PROVIDER}:{LLM_MODEL}", "calls": llm.call_log.get()}
    return ans
