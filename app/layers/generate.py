"""⑥ 生成层：按用户身份组织回答，发出前做引用回查、问题命中和合规检查。

骨架固定（结论卡 → 事件链 → 节点详情 → 原文）；LLM 只负责写结论句，
且只能看到结构化的事件链，写出的每个数字都必须能在被引用的原文里找到。
"""
from __future__ import annotations

import re
from datetime import datetime

from app import llm
from app.layers import views
from app.models import SOURCE_LABEL, Claim, Document, Event, Node, Query, Relation

BANNED = r"利好|利空|必涨|必跌|建议买入|建议卖出|建议持有|目标价|抄底|满仓"
DISCLAIMER = "以上为公开信息整理，不构成投资建议。"
EXPAND = {"retail": 2, "researcher": 3, "pm": 2, "compliance": 3}
PERSONA_STYLE = {
    "retail": "面向个人投资者：1–2 句，先给结论，避免术语，必要时解释。",
    "researcher": "面向研究员：2–3 句，写清来源、口径差异与时间。",
    "pm": "面向基金经理：2 句，结论先行，点明事件阶段与对持仓的关联。",
    "compliance": "面向合规/IR：2–3 句，措辞精确，注明公告日期与渠道。",
}


def _src(c: Claim) -> str:
    return "公告" if c.source_type == "announcement" else "互动易回复" if c.source_type == "irm_reply" else SOURCE_LABEL[c.source_type]


def _best(n: Node) -> Claim:
    return next((c for c in n.claims if c.id == n.headline_claim), n.claims[0])


def _template(ev: Event, q: Query, rel: Relation) -> str:
    if not ev.nodes:
        return "在已接入的数据源中未检索到与该问题相关的公告或公司回复。"
    by_id = {n.id: n for n in ev.nodes}
    sn = by_id.get(ev.status_node or "", ev.nodes[-1])
    c = _best(sn)
    first = ev.nodes[0]
    quote = c.quote if len(c.quote) <= 70 else c.quote[:68] + "…"
    s = f"{ev.status}。{sn.time.month}月{sn.time.day}日，{c.publisher}在{_src(c)}中称：“{quote}”"
    if first.id != sn.id:
        fc = first.claims[0]
        what = "该传闻" if fc.type == "传闻" else "该事件"
        how = f"{_src(fc)}对传闻的转述" if fc.attributed_to else _src(fc)
        s += f"。{what}在已接入数据源中最早见于{first.time.month}月{first.time.day}日的{how}"
    s += "。"
    if q.persona == "researcher":
        s += f"共整理 {sum(len(n.claims) for n in ev.nodes)} 条声明、{len(ev.nodes)} 个节点。"
    if q.persona == "pm" and rel.market and rel.market.car is not None:
        s += f"首次披露后窗口内相对{rel.market.benchmark_name}累计超额收益 {rel.market.car}%（只描述数据，不代表因果）。"
    return s


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def cite_check(text: str, ev: Event, rel: Relation) -> tuple[str, list[str]]:
    """逐句检查数字和日期：都必须出现在事件链的原文、节点时间或行情计算结果中，否则删除该句。"""
    allowed: set[str] = set()
    for n in ev.nodes:
        allowed |= {str(n.time.year), str(n.time.month), str(n.time.day), f"{n.time.month:02d}", f"{n.time.day:02d}"}
        for c in n.claims:
            allowed |= _numbers(c.quote) | _numbers(c.event_time or "")
    allowed |= {str(sum(len(n.claims) for n in ev.nodes)), str(len(ev.nodes))}
    if rel.market and rel.market.car is not None:
        allowed |= _numbers(str(rel.market.car)) | {"5", "300", "50"}
    kept, removed = [], []
    for sent in re.split(r"(?<=[。！？])", text):
        if not sent.strip():
            continue
        bad = _numbers(sent) - allowed
        (removed if bad else kept).append(sent if not bad else f"{sent}（无出处数字：{'、'.join(sorted(bad))}）")
    return "".join(kept), removed


CONC_SYSTEM = ("你为投资事件情报产品写结论句。只能使用给定的事件链信息，不得引入任何外部信息或数字。"
               "第一句必须直接回答用户的问题并包含事件状态原词。事实写“公告显示”，传闻写“市场传闻称……尚未获官方证实”。"
               "公司否认不等于消息不实：不要写“不是真的”“是假的”“纯属谣言”这类定性，只陈述谁在何时如何回应。"
               "禁止给出买卖建议、涨跌预测，禁止使用利好、利空等词。"
               "涉及是否买入、仓位、目标价或收益保证时，系统会在结论前单独说明服务边界。"
               "结论正文专注回答可核验的事件事实，不忽略事实问题，也不要重复生硬的拒绝声明。"
               "面向用户使用自然、温和的日常语言，不说‘不合规’‘越界’‘意图识别’等内部术语。")
OVERCLAIM = r"不是真的|是假的|纯属谣言|确实是谣言|假消息|是真的。|确有其事"


def _llm_conclusion(ev: Event, q: Query, rel: Relation) -> str | None:
    lines = []
    for n in ev.nodes:
        # 每个节点给出最多 4 条证据，结论才能写出具体内容（例如更正前后分别是什么）
        claims = [_best(n)] + [c for c in n.claims if c.id != n.headline_claim][:3]
        lines.append(f"{n.id}｜{n.time:%Y-%m-%d}｜{n.grade}")
        lines += [f"    · {c.type}｜{c.publisher}{_src(c)}｜{c.quote[:300]}" for c in claims]
    res = llm.json_call("生成层·结论句", CONC_SYSTEM, (
        f"用户问题：{q.question}\n{PERSONA_STYLE[q.persona]}\n事件状态：{ev.status}（可信度：{ev.grade}）\n"
        f"事件链：\n" + "\n".join(lines)), llm.obj({"text": llm.STR}), effort="medium")
    return res["text"] if res else None


def _node_view(n: Node, ev: Event) -> dict:
    by_id = {x.id: x for x in ev.nodes}
    conflicts = []
    for e in ev.edges:
        if e.type in ("冲突", "更正") and n.id in (e.source, e.target):
            other = by_id[e.target if e.source == n.id else e.source]
            conflicts += [_claim_view(c, note=f"{e.label}（{other.time:%m-%d} 节点）") for c in other.claims[:3]]
    return {
        "id": n.id, "time": n.time.isoformat(), "date": f"{n.time:%m-%d}", "headline": n.headline,
        "headline_quote": _best(n).quote,
        "grade": n.grade, "grade_reason": n.grade_reason, "conflict": n.conflict, "state_effect": n.state_effect,
        "later_tags": n.later_tags, "first_hand": n.first_hand,
        "supports": [_claim_view(c) for c in n.claims], "conflicts": conflicts,
    }


def _claim_view(c: Claim, note: str = "") -> dict:
    # 转述的传闻：来源写清“谁在传、由谁的文件转述”，避免读者误以为原文就是传闻出处
    source = (f"{c.attributed_to}（见{c.publisher}{_src(c)}转述）" if c.attributed_to
              else f"{c.publisher} · {_src(c)}")
    return {"id": c.id, "doc_id": c.doc_id, "type": c.type, "statement": c.statement, "quote": c.quote,
            "relayed": bool(c.attributed_to), "relay_by": f"{c.publisher}{_src(c)}" if c.attributed_to else "",
            "source": source, "time": f"{c.disclosure_time:%Y-%m-%d %H:%M}",
            "attributed_to": c.attributed_to, "type_method": c.type_method, "allowed_types": c.allowed_types,
            "needs_review": c.needs_review, "event_time": c.event_time, "url": c.url, "note": note}


def _doc_view(d: Document) -> dict:
    return {"id": d.id, "title": d.title, "publisher": d.publisher, "source_type": SOURCE_LABEL[d.source_type],
            "url": d.url, "text": d.text, "context": d.context,
            "times": {"disclosure": d.disclosure_time.strftime("%Y-%m-%d %H:%M") + ("（只有日期）" if d.disclosure_date_only else ""),
                      "crawl": f"{d.crawl_time:%Y-%m-%d %H:%M}",
                      "update": d.update_time.strftime("%Y-%m-%d %H:%M") if d.update_time else "无",
                      "event": "见声明卡片（从正文抽取）"},
            "reply_time_approx": d.reply_time_approx, "duplicates": d.duplicates}


def fallback_answer(q: Query) -> dict:
    text = {
        "vague": q.clarify or "请补充公司名称或股票代码。",
        "non_event": "这个问题不属于事件追踪，本产品的核心能力是梳理投资事件的来源、变化与证据。可以换成具体事件来问，例如“网传 X 公司被收购是真的吗”。",
        "no_coverage": "当前数据只覆盖 A 股公司公告与互动易问答，暂不支持该市场或品种。",
        "off_topic": "本产品只回答 A 股投资事件相关的问题。",
    }[q.fallback or "vague"]
    return {"kind": "fallback", "fallback": q.fallback, "text": text}


def generate(q: Query, ev: Event, rel: Relation, docs: list[Document], gaps: list[str], now: datetime) -> tuple[dict, dict]:
    checks: dict = {}
    text = _llm_conclusion(ev, q, rel) if ev.nodes else None
    if text and re.search(OVERCLAIM, text):
        checks["overclaim_rejected"] = text  # 越过证据下定性，改用模板
        text = None
    checks["writer"] = "LLM" if text else "模板（规则降级）"
    text = text or _template(ev, q, rel)

    text, removed = cite_check(text, ev, rel)
    checks["citation_removed"] = removed
    if ev.status not in text.split("。")[0]:
        text = f"{ev.status}。" + text  # 问题命中：第一句必须给出状态
        checks["hit_fix"] = True
    hits = re.findall(BANNED, text)
    text = re.sub(BANNED, "", text)
    checks["banned_removed"] = hits
    prefix = ""
    if "G" in q.intents:
        prefix = ("关于是否买入，建议你结合自身财务状况和风险承受能力，理性判断。"
                  "EventLens 仅提供公开事实与证据链供你参考，不代你做买卖决定，也无法给出目标价或保证收益。"
                  "下面先梳理相关事件的事实与来源。")
        checks["compliance"] = "越界问题：拒绝建议部分，事实照常提供"

    # 数据截至时间：取抓取时间与查询时点中较早者（回看历史时点时，显示的是那个时点而不是重算时刻）
    cutoff = min(max([d.crawl_time for d in docs], default=now), now)
    latest = max((n.time for n in ev.nodes), default=None)
    used = {c.doc_id for n in ev.nodes for c in n.claims}
    answer = {
        "kind": "event",
        "persona": q.persona, "expand_level": EXPAND[q.persona],
        "conclusion": {"status": ev.status, "grade": ev.grade, "prefix": prefix, "text": text,
                       "cutoff": f"{cutoff:%Y-%m-%d %H:%M}", "disclaimer": DISCLAIMER,
                       "latest_disclosure": f"{latest:%Y-%m-%d}" if latest else None,
                       "doc_count": len({c.doc_id for n in ev.nodes for c in n.claims})},
        "event": {"id": ev.id, "title": ev.title, "flags": ev.flags, "status_node": ev.status_node,
                  "nodes": [_node_view(n, ev) for n in ev.nodes],
                  "edges": [e.model_dump() for e in ev.edges],
                  "possibly_related": [_claim_view(c) for c in ev.possibly_related]},
        "docs": {d.id: _doc_view(d) for d in docs if d.id in used},
        "relation": rel.model_dump(),
        "views": views.build(ev, rel, q, docs),
        "gaps": gaps,
    }
    return answer, checks
