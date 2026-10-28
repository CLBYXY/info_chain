"""⑥ 生成层 · 身份视角：同一条事件链，按四种身份各自关心的问题组织成专属模块。

事实部分（结论、事件链、证据）对所有人一致，保证口径统一；
差异在于“先回答什么、展开什么”。全部由事件链确定性计算，不额外调用 LLM，也不引入新的数字。
"""
from __future__ import annotations

from collections import Counter

from app.models import FIRST_HAND, SOURCE_LABEL, Document, Event, Query, Relation

MEANING = {
    "公司已否认": "公司已经正式否认。目前没有官方证据支持这条消息；但“否认”不等于“绝对不会发生”，之后有新公告会提醒你。",
    "传闻中": "目前只有传闻，公司和监管都还没有表态，不要把它当成已经发生的事。",
    "公司已回应": "公司有回应，但既没有明确确认，也没有明确否认。",
    "已披露预案": "公司公布了计划，但计划还要经过审批或执行，存在变数。",
    "进行中": "事情正在推进，最终结果还没有确定。",
    "已完成": "公司公告确认这件事已经完成。",
    "已终止": "公司公告这件事已经终止。",
    "已延期": "原定时间到了没有完成，公司宣布延期。新的完成时间同样存在不确定性。",
    "已更正": "公司更正了此前的说法，请以更正后的内容为准。",
    "未检索到相关信息": "在公告和董秘回复里都没有找到相关内容：可能只是传闻，也可能还没有披露。",
}
# 面向读者只解释颜色的含义，不讲内部分级规则
COLOR_LEGEND = {
    "已证实": ("green", "绿色", "公司公告或董秘回复里直接写明了这件事"),
    "有依据": ("yellow", "黄色", "多个互不相关的来源都这么说，但公司还没有表态"),
    "未证实": ("red", "红色", "只是传闻，或只有一个来源，目前无法核实"),
}
WATCH_POINTS = {
    "公司已否认": ["是否出现新的媒体报道或监管问询", "公司后续公告是否与否认口径一致"],
    "传闻中": ["公司是否发布澄清公告或在互动平台回应", "交易所是否发出问询函"],
    "公司已回应": ["是否有正式公告明确确认或否认"],
    "已披露预案": ["董事会、股东大会审议结果", "监管审核或问询进展"],
    "进行中": ["交割、完成或进展公告", "是否出现终止或变更"],
    "已完成": ["后续财报中是否体现影响"],
    "已终止": ["终止原因及对业绩的影响说明"],
    "已延期": ["新的完成时间能否兑现", "延期原因是否涉及资金或项目可行性变化"],
    "已更正": ["更正前后差异是否引发监管关注"],
}


def _channel(src: str) -> str:
    return "公告" if src == "announcement" else "互动易" if src == "irm_reply" else SOURCE_LABEL.get(src, src)


def build(ev: Event, rel: Relation, q: Query, docs: list[Document]) -> dict:
    nodes = ev.nodes
    by_id = {n.id: n for n in nodes}
    status_node = by_id.get(ev.status_node or "")
    claims = [c for n in nodes for c in n.claims]
    watch = set(q.watchlist)

    # ---------- 个人投资者：这对我意味着什么 ----------
    key = []
    for n in [nodes[0] if nodes else None, status_node, nodes[-1] if nodes else None]:
        if n and n.id not in [k["id"] for k in key]:
            key.append({"id": n.id, "date": f"{n.time:%m-%d}", "headline": n.headline, "grade": n.grade})
    hits = [s for s in rel.stocks if s.in_watchlist]
    retail = {
        "meaning": MEANING.get(ev.status, ""),
        "key_steps": key,
        "watch_hit": [f"你自选的 {s.stock.name} 是本事件的{s.role}" for s in hits],
        "legend": [{"grade": g, "color": col, "name": name, "desc": d}
                   for g, (col, name, d) in COLOR_LEGEND.items() if g in {n.grade for n in nodes}],
        "risk": "以上是公开信息整理，不构成投资建议。传闻发酵和澄清前后，股价可能出现大幅波动。",
    }

    # ---------- 研究员：证据扎不扎实 ----------
    used = {c.doc_id for c in claims}
    conflicts = [f"{by_id[e.source].time:%m-%d}「{by_id[e.source].headline}」{e.label}「{by_id[e.target].headline}」"
                 for e in ev.edges if e.type in ("冲突", "更正")]
    conflicts += [f"{n.time:%m-%d} 节点内：{n.conflict}" for n in nodes if n.conflict]
    first = nodes[0] if nodes else None
    earliest = ""
    if first:
        fc = first.claims[0]
        # 传闻出现时间和转述它的文件披露时间分开写，避免混成“08-11 某某公告”
        earliest = (f"{first.time:%Y-%m-%d} 出现于{fc.attributed_to}，"
                    f"内容取自 {fc.disclosure_time:%Y-%m-%d} 的{fc.publisher}{_channel(fc.source_type)}中的引述；"
                    f"最初发布的帖子不在收录范围内"
                    if fc.attributed_to else f"{first.time:%Y-%m-%d}，{fc.publisher}{_channel(fc.source_type)}")
    researcher = {
        "sources": dict(Counter(_channel(d.source_type) for d in docs if d.id in used)),
        "claim_types": dict(Counter(c.type for c in claims)),
        "uncertain": sum(c.needs_review for c in claims),
        "conflicts": conflicts,
        "earliest": earliest,
        "possibly_related": len(ev.possibly_related),
    }

    # ---------- 基金经理：对持仓的影响、接下来看什么 ----------
    m = rel.market
    pm = {
        "stage": ev.status, "grade": ev.grade,
        "since": f"{status_node.time:%Y-%m-%d}" if status_node else "",
        "holdings": [{"name": s.stock.name, "code": s.stock.thscode, "role": s.role, "in_watchlist": s.in_watchlist}
                     for s in rel.stocks],
        "market": {"car": m.car, "benchmark": m.benchmark_name, "t0": m.t0, "note": m.note} if m else None,
        "valuation": {"pe_ttm": rel.valuation.get("pe_ttm"), "pb_mrq": rel.valuation.get("pb_mrq")} if rel.valuation else None,
        "watch_points": WATCH_POINTS.get(ev.status, []) + ev.flags,
    }

    # ---------- 合规 / IR：口径是否一致、回应是否及时 ----------
    official = sorted([c for c in claims if c.source_type in FIRST_HAND and not c.attributed_to],
                      key=lambda c: c.disclosure_time)
    rumor_nodes = [n for n in nodes if all(c.type == "传闻" for c in n.claims)]
    first_resp = next((n for n in nodes if n.first_hand and n.state_effect in ("公司已否认", "公司已回应", "已更正")), None)
    lag = None
    if rumor_nodes and first_resp:
        hours = (first_resp.time - rumor_nodes[0].time).total_seconds() / 3600
        lag = {"rumor": f"{rumor_nodes[0].time:%Y-%m-%d}", "response": f"{first_resp.time:%Y-%m-%d}",
               "text": f"传闻在已接入数据中最早出现于 {rumor_nodes[0].time:%m-%d}，公司首次回应在 {first_resp.time:%m-%d}，"
                       f"间隔约 {max(0, round(hours / 24))} 天（按日期计，公告多数只有日期）"}
    stances = {c.stance for c in official if c.type == "事实" and c.stance != "neutral"}
    compliance = {
        "statements": [{"time": f"{c.disclosure_time:%Y-%m-%d %H:%M}", "channel": _channel(c.source_type),
                        "quote": c.quote, "doc_id": c.doc_id} for c in official[:30]],
        "total_statements": len(official),
        "response_lag": lag,
        "consistency": ("存在不一致：公司官方表态中既有支持也有否认，需逐条核对" if stances == {"support", "refute"}
                        else "口径一致：公司官方表态之间没有相互矛盾" if official else "暂无公司官方表态"),
        "rumor_sources": list(dict.fromkeys(
            f"{c.attributed_to}（见{c.publisher}{_channel(c.source_type)}转述）" if c.attributed_to
            else f"{c.publisher}（{_channel(c.source_type)}）" for n in rumor_nodes for c in n.claims))[:10],
    }
    return {"retail": retail, "researcher": researcher, "pm": pm, "compliance": compliance}
