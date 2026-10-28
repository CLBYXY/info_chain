"""④ 事件层：把声明串成按时间发展的事件链。

每条声明：判断是否属于本事件 → 按时间排位 → 并入已有节点或成为新节点 →
与之前的节点连线（作证 / 冲突 / 更正 / 推进）→ 全部节点重新定级 → 推导当前状态。
"""
from __future__ import annotations

import re
from datetime import datetime

from app import llm
from app.layers import rules
from app.models import FIRST_HAND, Claim, Edge, Event, Node, Query

NODE_MERGE_DAYS = 3


# ---------------- 工具函数 ----------------
def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[\s，。、“”\"：:（）()]", "", s)
    return {s[i:i + 2] for i in range(max(0, len(s) - 1))}


def _sim(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    return len(A & B) / min(len(A), len(B)) if A and B else 0.0


def _quoted(c: Claim) -> set[str]:
    """声明里引号中的原话（去空白），用于识别同一条传闻的不同表述。"""
    return {re.sub(r"\s", "", q) for q in re.findall(r"[“\"]([^”\"]{4,60})[”\"]", c.statement + c.quote)}


RETRO_VERB = r"披露|刊登|发布|审议通过"


def claim_time(c: Claim, since: datetime | None = None, disclosed: set | None = None) -> datetime:
    """一般按披露时间排位。两种情况按正文写明的发生日期排位：
    ① 转述的传闻（传闻出现早于转述它的公告）；
    ② 公告回溯提到的早先事件（如更正公告写明“公司于 10 月 27 日披露了三季报”），且日期在查询时间窗内。
    后者常见于原文件已被替换、检索不到的情况。"""
    if c.event_time:
        try:
            t = datetime.fromisoformat(c.event_time)
        except ValueError:
            return c.disclosure_time
        earlier_day = t.date() < c.disclosure_time.date()
        if c.attributed_to and t < c.disclosure_time:
            return t
        # 只回溯公司自己的披露或决议，且那天的原文件不在已检索到的文件中（多半已被更正版替换）
        if (earlier_day and c.source_type in FIRST_HAND and since is not None and t >= since
                and re.search(RETRO_VERB, c.quote) and t.date() not in (disclosed or set())):
            return t
    return c.disclosure_time


def _is_rumor(n: Node) -> bool:
    return all(c.type == "传闻" for c in n.claims)


def _stance(n: Node) -> str:
    """节点对事件核心说法的立场：有否认即否认，有支持即支持，否则中性。
    推测和观点（如“若顺利交付将带来现金流”）不构成对事实的支持或否认。"""
    s = {c.stance for c in n.claims if c.type in ("事实", "传闻")}
    return "refute" if "refute" in s else "support" if "support" in s else "neutral"


def _opposite(a: str, b: str) -> bool:
    return {a, b} == {"support", "refute"}


def _text(n: Node) -> str:
    return " ".join(c.quote for c in n.claims)


def _related(a: Node, b: Node, keywords: list[str]) -> bool:
    """内容层面相关：同出一份文档不算（一份公告里常有多件事）。"""
    # 公司否认的句子里引用了传闻原话
    if any(c1.statement and c1.statement in c2.quote for c1 in a.claims for c2 in b.claims):
        return True
    ka = {k for k in keywords if k in _text(a) or any(k in c.aspect for c in a.claims)}
    kb = {k for k in keywords if k in _text(b) or any(k in c.aspect for c in b.claims)}
    if ka & kb:
        return True
    return max(_sim(c1.aspect, c2.aspect) for c1 in a.claims for c2 in b.claims) >= 0.4


# ---------------- 1. 同一事件识别 ----------------
SAME_SYSTEM = "判断每条声明是否属于给定的投资事件。只输出 JSON。"
SAME_SCHEMA = llm.obj({
    "title": llm.STR,
    "labels": llm.arr(llm.obj({"id": llm.STR, "relation": {
        "type": "string", "enum": ["same", "new_stage", "independent", "unrelated"]}})),
})


def _counterparty_veto(claims: list[Claim]) -> set[str]:
    """关键要素否决：交易对手与事件主对手方不重合的声明，判为相关但独立的事件。"""
    counts: dict[str, int] = {}
    for c in claims:
        c.counterparties = [p for p in c.counterparties if re.search(r"公司|集团|股份|有限", p)
                            and not re.search(r"公安|政府|委员会|交易所|证监|法院|平台|社区|媒体|事务所", p)]
        for p in c.counterparties:
            counts[p] = counts.get(p, 0) + 1
    if not counts:
        return set()
    main = max(counts, key=counts.get)
    return {c.id for c in claims if c.counterparties and main not in c.counterparties}


def identify(claims: list[Claim], q: Query) -> tuple[str, list[Claim], list[Claim], str]:
    stock = q.stocks[0].name if q.stocks else ""
    default_title = f"{stock}：{'、'.join(q.keywords)}" if q.keywords else f"{stock} 近期事项"
    vetoed = _counterparty_veto(claims)
    res = llm.json_call("事件层·同一事件识别", SAME_SYSTEM, (
        f"用户问题：{q.question}\n请先给事件起一个 8–20 字的标题（title），再逐条判断："
        "same=同一事件；new_stage=同一事件的新阶段；independent=相关但独立的另一事件；unrelated=无关。\n"
        "“同一事件”包括：用户所问的那件事本身、关于它的传闻、公司的回应与澄清、以及由它引发的后续"
        "（如追责、报警、监管或公安处理、交易进展）。independent 只用于另一件事，例如同一家公司的另一笔订单、另一次回购。"
        "与事件无关的经营信息（如其他业务的订单规模）判为 unrelated。\n\n" +
        "\n".join(f"{c.id}｜{c.disclosure_time:%Y-%m-%d}｜{c.publisher}｜{c.statement}" for c in claims)),
        SAME_SCHEMA)
    if res:
        rel = {x["id"]: x["relation"] for x in res["labels"]}
        title, method = res["title"] or default_title, "LLM 判定 + 关键要素否决"
    else:
        rel = {}
        for c in claims:
            hit = any(k in c.quote or k in c.aspect for k in q.keywords) or not q.keywords
            rel[c.id] = "same" if hit else "unrelated"
        # 同一文档内与已命中声明并列的表态，也视为同一事件
        hit_docs = {c.doc_id for c in claims if rel[c.id] == "same"}
        for c in claims:
            if rel[c.id] == "unrelated" and c.doc_id in hit_docs and re.search(rules.CUES, c.quote):
                rel[c.id] = "same"
        title, method = default_title, "规则降级：关键词命中 + 关键要素否决"
    keep, maybe = [], []
    for c in claims:
        r = "independent" if c.id in vetoed else rel.get(c.id, "unrelated")
        if r in ("same", "new_stage"):
            keep.append(c)
        elif r == "independent":
            maybe.append(c)
    return title, keep, maybe, method


# ---------------- 2–3. 排位、并入节点 ----------------
def _join_node(nodes: list[Node], c: Claim, t: datetime) -> Node | None:
    # 同一条传闻被后续公告再次转述：并回最初的传闻节点，不新建节点。
    # LLM 每次措辞不同，所以同时比较引号里的传闻原话。
    if c.type == "传闻":
        for n in nodes:
            if not _is_rumor(n):
                continue
            if max(_sim(c.statement, x.statement) for x in n.claims) >= 0.6 or \
                    _quoted(c) & set().union(*[_quoted(x) for x in n.claims]):
                return n
    for n in reversed(nodes):
        if abs((n.time - t).total_seconds()) > NODE_MERGE_DAYS * 86400:
            continue
        same_kind = (c.type == "传闻") == _is_rumor(n)
        same_doc = any(x.doc_id == c.doc_id for x in n.claims)
        close_aspect = max(_sim(c.aspect, x.aspect) for x in n.claims) >= 0.5
        # 同一份文档：立场不对立即可并入（中性的补充说明归入同一阶段）；跨文档：立场相同且要素相近
        # 同一文档里回溯的早先事件（日期不同）单独成节点，不并入本文档当天的节点
        if same_doc and t.date() != n.time.date():
            continue
        if same_kind and same_doc and not _opposite(c.stance, _stance(n)):
            return n
        same_words = max(_sim(c.statement, x.statement) for x in n.claims) >= 0.6
        if same_kind and c.stance == _stance(n) and (close_aspect or same_words):
            return n
    return None


def build_nodes(claims: list[Claim], since: datetime | None = None) -> list[Node]:
    disclosed = {c.disclosure_time.date() for c in claims}
    ordered = sorted(claims, key=lambda c: (claim_time(c, since, disclosed), 0 if c.attributed_to else 1, c.id))
    nodes: list[Node] = []
    for c in ordered:
        t = claim_time(c, since, disclosed)
        n = _join_node(nodes, c, t)
        if n:
            n.claims.append(c)
            n.time = min(n.time, t)
        else:
            nodes.append(Node(id=f"n{len(nodes) + 1}", time=t, headline="", claims=[c]))
    nodes.sort(key=lambda n: (n.time, 0 if _is_rumor(n) else 1))
    for i, n in enumerate(nodes):
        n.id = f"n{i + 1}"
    return nodes


# ---------------- 4. 节点定级（只看截至该节点时间点的信息） ----------------
def grade_node(n: Node, keywords: list[str] | None = None) -> None:
    fh = [c for c in n.claims if c.source_type in FIRST_HAND and not c.attributed_to and c.type in ("事实", "推测")]
    n.first_hand = bool(fh)
    publishers = {c.publisher for c in n.claims}
    if _is_rumor(n):
        n.grade, n.grade_reason = "未证实", "传闻：只有转述或匿名来源，未经证实"
    elif fh:
        n.grade, n.grade_reason = "已证实", f"有一手来源：{fh[0].publisher}（{'公告' if fh[0].source_type == 'announcement' else '董秘回复'}）"
    elif len(publishers) >= 2:
        n.grade, n.grade_reason = "有依据", f"{len(publishers)} 个独立来源，尚无一手来源"
    else:
        n.grade, n.grade_reason = "未证实", "单一来源"

    kws = keywords or []
    best = sorted(n.claims, key=lambda c: (
        c.source_type not in FIRST_HAND, c.type != "事实",
        not any(k in c.quote for k in kws),
        c.stance != _stance(n),
        not (c.stance == "refute" and re.search(rules.DENY, c.quote)),
        not re.search(r"不属实|与事实不符|不存在|未筹划", c.quote),
        len(c.quote)))[0]
    n.headline_claim = best.id
    text = best.statement if len(best.statement) <= 40 else best.statement[:38] + "…"
    n.headline = ("传闻：" + text) if _is_rumor(n) else text

    # 节点内数值冲突：同一要素、带金额/比例单位的数字，在不同文档里说法不一（同一文档内的不同数字通常是不同事实）
    by_aspect: dict[str, dict[str, set[str]]] = {}
    for c in n.claims:
        for num in c.numbers:
            if re.search(r"\d\s*(亿|万|元|%|股|美元)", num):
                by_aspect.setdefault(c.aspect, {}).setdefault(re.sub(r"\s", "", num), set()).add(c.doc_id)
    multi = {a: set(v) for a, v in by_aspect.items()
             if len(v) > 1 and len(set().union(*v.values())) > 1}
    n.conflict = "；".join(f"“{a}”出现不同数值：{' / '.join(sorted(v))}" for a, v in multi.items()) or None

    if n.first_hand:
        # 阶段由抽取环节提议，规则把关：只看一手来源；“公司已否认”还必须有公司的否认表态，
        # 法律意见书里的“不存在违规情形”之类不算
        stages = {c.stage for c in fh if c.stage != "无"}
        denied = any((c.response_type == "否认" or (c.stance == "refute" and c.type == "事实"))
                     and not re.search(rules.COMPLIANCE_BOILER, c.quote) for c in fh)
        if not denied:
            stages.discard("公司已否认")
        else:
            stages.add("公司已否认")
        if any(c.response_type == "未置可否" for c in fh):
            stages.add("公司已回应")
        n.state_effect = next((s for s in rules.STAGE_PRIORITY if s in stages), None)


# ---------------- 5. 连线 ----------------
def link(nodes: list[Node], keywords: list[str]) -> list[Edge]:
    edges: list[Edge] = []
    for j, b in enumerate(nodes):
        if j == 0:
            continue
        earlier = list(reversed(nodes[:j]))
        rel = [a for a in earlier if _related(a, b, keywords)]
        edge = None
        if b.first_hand and "更正" in _text(b) and rel:
            edge = Edge(source=rel[0].id, target=b.id, type="更正", label="更正", method="规则：一手来源的更正公告")
        # 与最近的一个有立场的相关节点比较：相反为冲突，相同为作证；中性节点不参与立场比较
        stanced = [a for a in rel if _stance(a) != "neutral"]
        if edge is None and stanced and _stance(b) != "neutral":
            a = stanced[0]
            if _opposite(_stance(a), _stance(b)):
                denied = b.first_hand and _stance(b) == "refute"
                edge = Edge(source=a.id, target=b.id, type="冲突", label="冲突 · 被否认" if denied else "冲突",
                            method="规则：立场相反（立场由声明抽取给出）")
            else:
                confirm = _is_rumor(a) and b.first_hand
                edge = Edge(source=a.id, target=b.id, type="作证", label="证实" if confirm else "作证",
                            method="规则：立场相同且要素相关")
        if edge is None and b.first_hand:
            fh = [a for a in earlier if a.first_hand] or earlier
            new_state = b.state_effect and b.state_effect != fh[0].state_effect
            edge = Edge(source=fh[0].id, target=b.id, type="推进",
                        label="推进 · " + (b.state_effect if new_state else "后续进展"),
                        method="规则：一手来源的后续进展")
        if edge:
            edges.append(edge)
    return edges


def _tags(nodes: list[Node], edges: list[Edge]) -> None:
    by_id = {n.id: n for n in nodes}
    for e in edges:
        src, tgt = by_id[e.source], by_id[e.target]
        d = f"{tgt.time:%m-%d}"
        if e.type == "冲突" and tgt.first_hand and _stance(tgt) == "refute":
            src.later_tags.append(f"→ {d} 被公司否认")
        elif e.type == "更正":
            src.later_tags.append(f"→ {d} 被更正")
        elif e.type == "作证" and e.label == "证实":
            src.later_tags.append(f"→ {d} 被证实")


# ---------------- 6. 当前状态与到期标记 ----------------
PROGRESS_RANK = {"公司已回应": 0, "已披露预案": 1, "进行中": 2, "已完成": 3}


def _monotonic(nodes: list[Node]) -> list[Node]:
    """推进类状态只能前进：已完成之后出现“已披露预案”不会让事件倒退；否认、终止、更正随时生效。"""
    out, rank = [], -1
    for n in nodes:
        r = PROGRESS_RANK.get(n.state_effect or "")
        if r is None:
            out.append(n)
            rank = -1 if n.state_effect == "已更正" else rank
        elif r >= rank:
            out.append(n)
            rank = r
    return out


def derive_status(ev: Event, now: datetime) -> None:
    effectful = _monotonic([n for n in ev.nodes if n.first_hand and n.state_effect])
    if effectful:
        last = effectful[-1]
        # 状态依据取“最近一段相同状态”中最早的节点，例如首次否认，而不是后来的重申
        first_of_run = last
        for n in reversed(effectful):
            if n.state_effect != last.state_effect:
                break
            first_of_run = n
        ev.status, ev.status_node, ev.grade = last.state_effect, first_of_run.id, first_of_run.grade
    elif any(n.first_hand for n in ev.nodes):
        last = [n for n in ev.nodes if n.first_hand][-1]
        ev.status, ev.status_node, ev.grade = "公司已回应", last.id, last.grade
    elif ev.nodes:
        last = ev.nodes[-1]
        ev.status, ev.status_node, ev.grade = "传闻中", last.id, last.grade
    else:
        ev.status, ev.grade = "未检索到相关信息", "未证实"

    for n in ev.nodes:
        for c in n.claims:
            if c.type != "推测":
                continue
            m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?\s*(?:前|之前|底|末)?", c.quote)
            if not m:
                continue
            due = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3) or 28))
            later_fh = any(x.first_hand and x.time > due for x in ev.nodes)
            if due < now and due > c.disclosure_time and not later_fh:
                ev.flags.append(f"到期未见进展：{c.publisher}曾预计 {due:%Y-%m-%d} 前完成，此后未见进展公告")


def build(claims: list[Claim], q: Query, now: datetime | None = None) -> tuple[Event, dict]:
    now = now or datetime.now()
    title, keep, maybe, method = identify(claims, q)
    nodes = build_nodes(keep, q.start)
    for n in nodes:
        grade_node(n, q.keywords)
    edges = link(nodes, q.keywords)
    _tags(nodes, edges)
    ev = Event(id=f"ev-{q.stocks[0].thscode if q.stocks else 'na'}-{'-'.join(q.keywords)[:20]}",
               title=title, stock=q.stocks[0] if q.stocks else None, nodes=nodes, edges=edges,
               possibly_related=maybe)
    derive_status(ev, now)
    return ev, {"same_event_method": method, "kept": len(keep), "possibly_related": len(maybe),
                "dropped_unrelated": len(claims) - len(keep) - len(maybe), "nodes": len(nodes), "edges": len(edges)}
