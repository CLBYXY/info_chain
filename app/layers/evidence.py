"""③ 证据层：从文档中拆出相关句子，整理成声明卡片并判定类型。

流程：候选句 → 声明抽取（LLM，原文回查）→ 硬规则圈定可选类型 → 软判定（LLM×3）选一个。
"""
from __future__ import annotations

import re
from collections import Counter

from app import llm
from app.layers import rules
from app.models import Claim, Document, Query

MAX_SENTENCES = 20
OFFICIAL_SPEAKER = r"律师|会计师|事务所|董事会|监事会|股东会|股东大会|保荐|独立董事|公司|交易所|证监会|公安"


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _candidates(doc: Document, keywords: list[str]) -> list[str]:
    sents = [s for s in rules.split_sentences(doc.text) if not re.search(rules.BOILER, s)]
    scored = []
    for i, s in enumerate(sents):
        score = 2 * sum(k in s for k in keywords) + (1 if re.search(rules.CUES, s) else 0)
        if score:
            scored.append((score, i, s))
    top = sorted(scored, key=lambda x: (-x[0], x[1]))[:MAX_SENTENCES]
    return [s for _, _, s in sorted(top, key=lambda x: x[1])]


def _event_time(s: str) -> str | None:
    m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s)
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


# ---------------- 声明抽取 ----------------
EXTRACT_SYSTEM = (
    "你从上市公司公告或董秘回复中抽取与指定事件相关的原子声明。每条声明只说一件事。"
    "quote 必须逐字复制自给定句子（可以是句子的一部分），不得改写。"
    "如果句子在转述媒体、网络、市场传闻的说法，把被转述的说法单独抽成一条，attributed_to 写转述对象；"
    "公司自己的表态另抽一条，attributed_to 为空。律师、会计师等中介机构的意见和董事会决议属于公告内容，attributed_to 为空。"
    "只抽取与事件相关的内容，套话不抽。每份文档最多抽取 12 条最重要的声明，statement 不超过 60 字。"
)
EXTRACT_SCHEMA = llm.obj({"claims": llm.arr(llm.obj({
    "quote": llm.STR,
    "statement": llm.STR,
    "aspect": llm.STR,
    "stance": {"type": "string", "enum": ["support", "refute", "neutral"]},
    "response_type": {"type": "string", "enum": ["确认", "否认", "未置可否", "无"]},
    "attributed_to": llm.STR,
    "stage": {"type": "string", "enum": rules.STAGES},
    "event_time": llm.NSTR,
    "numbers": llm.arr(llm.STR),
    "counterparties": llm.arr(llm.STR),
}))})


def _llm_extract(doc: Document, sents: list[str], q: Query) -> list[dict] | None:
    body = "\n".join(f"[{i}] {s}" for i, s in enumerate(sents))
    ctx = f"\n（该回复对应的投资者提问，仅作上下文，不要从中抽取声明：{doc.context}）" if doc.context else ""
    res = llm.json_call("证据层·声明抽取", EXTRACT_SYSTEM, (
        f"事件关键词：{'、'.join(q.keywords) or '（未指定，抽取重大事项）'}\n"
        f"来源：{doc.title}（{doc.publisher}，{doc.disclosure_time:%Y-%m-%d}）{ctx}\n"
        f"aspect 用 4–12 字概括声明涉及的事件要素；event_time 为事情实际发生的日期（YYYY-MM-DD），正文没写则为 null。\n"
        f"stance 是该声明对“用户所关心的核心说法”的立场：support=支持或证实该说法（转述的传闻本身也是 support），"
        f"refute=否认或反驳该说法（包括说明相关业务正常、合规），neutral=不涉及真假的补充信息（如报警、处罚进展、风险提示）。\n"
        f"response_type 只在公司自己表态时填写，否则为“无”。\n"
        f"stage：该声明表明用户所问事件处于哪个阶段——公司否认相关说法填“公司已否认”；公司表态但未确认也未否认填“公司已回应”；"
        f"披露计划、预案、框架协议填“已披露预案”；正在执行填“进行中”；明确完成填“已完成”；明确终止或取消填“已终止”；宣布延期、推迟或未能按期完成填“已延期”；"
        f"更正此前披露填“已更正”；与事件阶段无关（如背景介绍、合规意见、风险提示）填“无”。\n"
        f"counterparties 只填交易对手方的公司名称（收购方、被收购方、客户、供应商）；监管机构、公安、政府部门、媒体、平台都不要填，没有就给空数组。\n"
        f"用户问题：{q.question}\n\n"
        f"候选句：\n{body}"), EXTRACT_SCHEMA, effort="medium")
    return res["claims"] if res else None


def _rule_extract(sents: list[str], q: Query, first_hand: bool) -> list[dict]:
    out = []
    for s in sents:
        # 公告里转述的传闻：引号内、且句子带“传闻/言论/报道”等线索
        if re.search(r"传闻|言论|报道|网传|消息|信息", s):
            for quoted in re.findall(r"[“\"]([^”\"]{4,60})[”\"]", s):
                out.append({"quote": quoted, "statement": quoted, "aspect": quoted[:12], "stance": "support",
                            "response_type": "无", "attributed_to": "市场传闻（公告转述）", "stage": "无",
                            "event_time": None, "numbers": re.findall(r"\d+(?:\.\d+)?亿?万?元?", quoted),
                            "counterparties": []})
        deny = bool(re.search(rules.DENY, s)) and not re.search(rules.COMPLIANCE_BOILER, s)
        aspect = "、".join(k for k in q.keywords if k in s) or ("澄清传闻" if deny else "事件进展")
        stage = "公司已否认" if deny else (rules.state_effect(s) or "无")
        if stage in rules.CORE_STATES and not any(k in s for k in q.keywords):
            stage = "无"  # 规则降级时，正则命中的“进展/实施”必须与事件关键词同句才算
        out.append({"quote": s, "statement": s[:80], "aspect": aspect,
                    "stance": "refute" if deny else "neutral",
                    "response_type": ("否认" if deny else "无") if first_hand else "无",
                    "attributed_to": "", "stage": stage, "event_time": _event_time(s),
                    "numbers": re.findall(r"\d+(?:\.\d+)?(?:亿|万)?元", s), "counterparties": []})
    return out


# ---------------- 类型软判定 ----------------
TYPE_SYSTEM = (
    "判断每条声明的类型。事实=已发生、可核验的陈述；观点=对事实的评价；推测=对未来的判断或计划；传闻=未经证实的说法。"
    "只能从每条声明给出的可选项中选择。"
)


def _soft_judge(claims: list[Claim]) -> None:
    multi = [c for c in claims if len(c.allowed_types) > 1]
    if not multi:
        return
    schema = llm.obj({"labels": llm.arr(llm.obj({"id": llm.STR, "type": llm.STR}))})
    prompt = "\n".join(f"{c.id}｜可选：{'/'.join(c.allowed_types)}｜{c.quote}" for c in multi)
    votes: dict[str, list[str]] = {c.id: [] for c in multi}
    # 三次独立判定（带采样温度），并发执行
    results = llm.parallel([
        (lambda k=k: llm.json_call(f"证据层·类型软判定 第{k + 1}次", TYPE_SYSTEM, prompt, schema, temperature=0.8))
        for k in range(3)])
    for res in results:
        if res is None:
            continue
        for lab in res["labels"]:
            if lab["id"] in votes:
                votes[lab["id"]].append(lab["type"])

    for c in multi:
        v = votes[c.id]
        if not v:
            c.type = rules.cue_type(c.allowed_types, c.quote)
            c.type_method = "规则降级：LLM 未启用，按措辞线索在可选范围内判定"
            continue
        valid = [t for t in v if t in c.allowed_types]
        if len(v) == 3 and len(valid) == 3 and len(set(valid)) == 1:
            c.type = valid[0]  # type: ignore[assignment]
            c.type_method = "LLM 判 3 次一致"
        else:
            pool = valid or list(c.allowed_types)
            c.type = rules.most_conservative(pool)
            c.needs_review = True
            c.type_method = f"LLM 判定不一致（{'/'.join(v)}），取最保守的一类，待复核"


# ---------------- 主流程 ----------------
def extract(docs: list[Document], q: Query) -> tuple[list[Claim], dict]:
    claims: list[Claim] = []
    stats = Counter()
    work = [(doc, _candidates(doc, q.keywords)) for doc in docs]
    work = [(d, s) for d, s in work if s]
    extracted = llm.parallel([(lambda d=d, s=s: _llm_extract(d, s, q)) for d, s in work])
    for (doc, sents), raw in zip(work, extracted):
        first_hand = doc.source_type in ("announcement", "irm_reply")
        method = "LLM"
        if raw is None:
            raw, method = _rule_extract(sents, q, first_hand), "规则降级"
        text_n = _norm(doc.text)
        for i, r in enumerate(raw):
            # 中介机构意见、董事会/股东会决议属于公告文件本身的内容，不是转述的传闻
            if re.search(OFFICIAL_SPEAKER, r["attributed_to"] or "") or not re.search(rules.RUMOR_SPEAKER, r["attributed_to"] or ""):
                r["attributed_to"] = ""
            if r["stance"] == "refute" and re.search(rules.COMPLIANCE_BOILER, r["quote"]):
                r["stance"], r["response_type"] = "neutral", "无"  # 合规套话不是否认
            if _norm(r["quote"]) not in text_n:
                stats["dropped_unverified"] += 1  # 原文回查失败：可能是编造，直接丢弃
                continue
            c = Claim(
                id=f"{doc.id}-c{i}", doc_id=doc.id, source_type=doc.source_type, publisher=doc.publisher,
                quote=r["quote"], statement=r["statement"], aspect=r["aspect"], stance=r["stance"],
                response_type=r["response_type"], attributed_to=r["attributed_to"], stage=r.get("stage", "无"),
                event_time=r["event_time"] or _event_time(r["quote"]), numbers=r["numbers"],
                counterparties=r["counterparties"], disclosure_time=doc.disclosure_time, url=doc.url)
            c.allowed_types = rules.allowed_types(doc.source_type, c.quote, c.attributed_to)
            if len(c.allowed_types) == 1:
                c.type = c.allowed_types[0]
                c.type_method = "硬规则：可选类型只有一个，不调用 LLM"
            claims.append(c)
            stats[f"extract_{method}"] += 1
    _soft_judge(claims)
    stats.update(f"type_{c.type}" for c in claims)
    return claims, dict(stats)
