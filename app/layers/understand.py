"""① 理解层：把一句话变成结构化查询（意图 + 股票代码 + 时间窗 + 事件关键词）。

意图识别和关键词抽取由 LLM 完成，未启用时退回关键词规则；
公司名到代码的映射永远走扶摇检索接口，不交给 LLM。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from app import llm
from app.config import DEFAULT_WINDOW_DAYS
from app.models import Persona, Query, Stock
from app.sources import fuyao
from app.sources.http import SourceError

INTENTS = {
    "A": "单事件追踪", "B": "真伪/冲突核验", "C": "从标的出发", "D": "行业/主题",
    "E": "影响数据", "F": "订阅", "G": "越界（投资建议/预测）", "H": "兜底",
}

INTENT_RULES = [
    ("G", r"(该不该|能不能|可以|要不要)?(买|卖|加仓|减仓|抄底)|能涨|会涨|会跌|目标价|明天"),
    ("B", r"真的吗|是真是假|真假|属实|网传|传闻|谣言|辟谣|否认|以哪个为准"),
    ("A", r"最早|来源|进展|到哪|怎么回事|始末|发展"),
    ("E", r"股价|涨跌|走势|超额|行情|反应"),
    ("F", r"提醒|通知|订阅|有进展告诉"),
    ("D", r"行业|板块|赛道|一批|有哪些"),
]

STOPWORDS = r"网传|传闻|听说|据说|是真的吗|是不是真的|真的吗|是真是假|最近|有没有|什么|怎么|请问|关于|公司|股份|集团|的|了|吗|呢|？|\?|，|,|。|要|将|会|在|被|和|与"

OFF_TOPIC = r"写诗|天气|笑话|做饭|翻译"

SYNONYMS = {
    "三季报": ["第三季度报告"], "一季报": ["第一季度报告"], "中报": ["半年度报告"], "半年报": ["半年度报告"],
    "年报": ["年度报告"], "业绩预告": ["业绩预告"], "定增": ["向特定对象发行"], "募投项目": ["募集资金投资项目", "募投"],
    "减持": ["减持计划"], "回购": ["回购股份"], "延期": ["延期", "推迟"], "并购": ["收购", "重组"],
}


def _rule_intents(q: str) -> list[str]:
    hits = [k for k, pat in INTENT_RULES if re.search(pat, q)]
    return hits or ["A"]


def _parse_time(q: str, now: datetime) -> tuple[datetime, datetime, bool]:
    m = re.search(r"近\s*(\d+)\s*(天|日|周|个月|月)", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"天": 1, "日": 1, "周": 7, "个月": 30, "月": 30}[unit]
        return now - timedelta(days=days), now, False
    for pat, days in [(r"上周|最近一周|这周", 14), (r"最近一个月|上个月|本月", 31), (r"今年", (now - datetime(now.year, 1, 1)).days)]:
        if re.search(pat, q):
            return now - timedelta(days=days), now, False
    m = re.search(r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})", q)
    if m:
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d - timedelta(days=30), min(now, d + timedelta(days=60)), False
    return now - timedelta(days=DEFAULT_WINDOW_DAYS), now, True


def _link_stocks(mentions: list[str], strict: bool) -> tuple[list[Stock], list[str]]:
    """每个提及都用接口确认；唯一匹配才采用，多个匹配返回候选供追问。
    strict=True（规则降级时的 n-gram 猜测）只接受名称或代码完全一致的结果。"""
    found: list[Stock] = []
    ambiguous: list[str] = []
    for m in dict.fromkeys(x.strip() for x in mentions):
        if len(m) < 2 or any(m in s.name for s in found):
            continue
        try:
            cands = fuyao.search(m, limit=5)
        except SourceError:
            continue
        exact = [c for c in cands if c.name == m or c.code == m or c.thscode == m]
        pick = exact[:1] or (cands[:1] if len(cands) == 1 and not strict else [])
        if pick:
            if pick[0].thscode not in {s.thscode for s in found}:
                found.append(pick[0])
        elif cands and not strict:
            ambiguous += [f"{c.name}（{c.thscode}）" for c in cands[:3]]
    return found, ambiguous


def _rule_mentions(q: str) -> list[str]:
    mentions = re.findall(r"\b\d{6}\b", q)
    rest = re.sub(STOPWORDS, " ", q)
    chunks = [c for c in re.split(r"\s+", rest) if 2 <= len(c) <= 8]
    # 先试较短的片段：公司简称通常 2–4 个字
    for c in chunks:
        for n in (4, 3, 2):
            for i in range(0, max(1, len(c) - n + 1)):
                mentions.append(c[i:i + n])
    return mentions[:24]


def _rule_keywords(q: str, stocks: list[Stock]) -> list[str]:
    rest = q
    for s in stocks:
        rest = rest.replace(s.name, " ").replace(s.code, " ")
    rest = re.sub(INTENT_RULES[0][1], " ", rest)  # 越界语气（该不该买等）不是事件关键词
    rest = re.sub(STOPWORDS + r"|\d{6}|该不该|能不能|要不要", " ", rest)
    return [c for c in re.split(r"\s+", rest) if len(c) >= 2][:6]


SYSTEM = "你是A股投资事件问答系统的问题解析器。只做结构化解析，不回答问题本身。"
SCHEMA = llm.obj({
    "intents": llm.arr({"type": "string", "enum": list("ABCDEFG")}),
    "confidence": {"type": "number"},
    "company_mentions": llm.arr(llm.STR),
    "event_keywords": llm.arr(llm.STR),
    "fallback": {"type": ["string", "null"], "enum": ["non_event", "no_coverage", "vague", "off_topic", None]},
})


def understand(question: str, persona: Persona = "retail", watchlist: list[str] | None = None,
               now: datetime | None = None) -> Query:
    now = now or datetime.now()
    start, end, defaulted = _parse_time(question, now)

    parsed = llm.json_call("理解层·意图与提及抽取", SYSTEM, (
        "意图类型：" + "；".join(f"{k}={v}" for k, v in INTENTS.items() if k != "H") +
        "。\n事件关键词：描述事件本身的 1–4 个短词（如“收购”“海关”“减持”），不要包含公司名和“传闻”“真的吗”这类语气词。"
        "\n公司提及：原文中出现的公司名、简称或 6 位代码。"
        "\nfallback：置信度低于 0.6 时填写——金融相关但不是事件问题填 non_event，"
        "数据不覆盖（非A股）填 no_coverage，太模糊填 vague，与金融无关填 off_topic；否则为 null。"
        f"\n\n问题：{question}"), SCHEMA)

    if parsed:
        intents = parsed["intents"] or ["A"]
        mentions = parsed["company_mentions"] + re.findall(r"\b\d{6}\b", question)
        fallback = parsed["fallback"] if parsed["confidence"] < 0.6 else None
    else:
        intents = _rule_intents(question)
        mentions = _rule_mentions(question)
        fallback = "off_topic" if re.search(OFF_TOPIC, question) else None

    stocks, ambiguous = _link_stocks(mentions, strict=parsed is None)
    keywords = (parsed["event_keywords"] if parsed else _rule_keywords(question, stocks))
    keywords = [k for k in keywords if all(k not in s.name for s in stocks)]
    # 口语说法补上公告里的正式写法，否则“三季报”匹配不到《第三季度报告》
    keywords += [syn for k in keywords for syn in SYNONYMS.get(k, []) if syn not in keywords]

    clarify = None
    if fallback is None and not stocks and "D" not in intents:
        fallback = "vague"
    if fallback == "vague":
        clarify = ("请问您指的是哪家公司？" + (" 可能是：" + "、".join(ambiguous) if ambiguous else
                   " 请提供公司名称或 6 位股票代码。"))

    return Query(question=question, persona=persona, intents=intents, fallback=fallback, stocks=stocks,
                 keywords=keywords, start=start, end=end, time_defaulted=defaulted, clarify=clarify,
                 watchlist=watchlist or [])
