"""② 取数层：从公告和互动易里挑出与事件相关的文档，去重，并记录四种时间。

选中条件：主体是查询的公司，且标题或正文命中事件关键词。
数据源出错时记为“缺口”，不让整条链路失败。
"""
from __future__ import annotations

import re
from datetime import datetime

from app.models import Document, Query
from app.sources import cninfo
from app.sources.http import SourceError

RUMOR_TITLE = r"澄清|传闻|媒体报道|网络传闻|致歉"
MAJOR_TITLE = r"澄清|收购|重组|减持|增持|重大合同|中标|诉讼|终止|更正|问询|立案|处罚|回购|定增|发行"
MAX_PDF = 12
MAX_DOCS = 8


def _dt(ms: int) -> tuple[datetime, bool]:
    d = datetime.fromtimestamp(ms / 1000)
    return d, (d.hour, d.minute, d.second) == (0, 0, 0)


def _hits(text: str, keywords: list[str]) -> list[str]:
    return [k for k in keywords if k and k in text]


def _shingles(s: str) -> set[str]:
    s = re.sub(r"\s", "", s)
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))}


def _dedupe(docs: list[Document]) -> list[Document]:
    """正文高度相似（3-gram Jaccard ≥ 0.85）视为转载，并入最早的一份。"""
    docs = sorted(docs, key=lambda d: d.disclosure_time)
    kept: list[tuple[Document, set[str]]] = []
    for d in docs:
        sh = _shingles(d.text)
        for k, ksh in kept:
            if sh and ksh and len(sh & ksh) / len(sh | ksh) >= 0.85:
                k.duplicates.append(d.id)
                break
        else:
            kept.append((d, sh))
    return [k for k, _ in kept]


def fetch(q: Query) -> tuple[list[Document], dict, list[str]]:
    now = datetime.now()
    gaps: list[str] = ["目前只收录公司公告和董秘问答，尚未收录新闻与研报；文中“最早出现”指在这些来源中最早"]
    stats = {"announcements": 0, "irm": 0, "selected": [], "unselected_sample": []}
    docs: list[Document] = []
    kws = q.keywords
    rumor_mode = "B" in q.intents

    for stock in q.stocks[:2]:
        # ---- 公告 ----
        try:
            anns = cninfo.announcements(stock.code, q.start, q.end)
        except SourceError as e:
            gaps.append("公司公告暂时无法获取，结论可能不完整")
            anns = []
        stats["announcements"] += len(anns)
        cands = []
        for a in anns:
            title_hit = _hits(a["title"], kws)
            if title_hit or re.search(RUMOR_TITLE, a["title"]) or (not kws and re.search(MAJOR_TITLE, a["title"])):
                cands.append((a, title_hit))
            elif len(stats["unselected_sample"]) < 8:
                stats["unselected_sample"].append({"title": a["title"], "reason": "标题未命中关键词"})
        for a, title_hit in cands[:MAX_PDF]:
            try:
                text = cninfo.pdf_text(a["url"])
            except SourceError as e:
                gaps.append(f"有一份公告暂时无法打开：{a['title']}")
                continue
            body_hit = _hits(text, kws)
            if kws and not (title_hit or body_hit):
                if len(stats["unselected_sample"]) < 8:
                    stats["unselected_sample"].append({"title": a["title"], "reason": "正文未提及关键词"})
                continue
            t, date_only = _dt(a["time_ms"])
            docs.append(Document(
                id=a["id"], source_type="announcement", publisher=a["publisher"], title=a["title"],
                url=a["url"], text=text, disclosure_time=t, disclosure_date_only=date_only, crawl_time=now,
                matched_keywords=sorted(set(title_hit + body_hit))))

        # ---- 互动易：只取已回复的问答，回复是声明来源，提问只作上下文 ----
        try:
            qas = cninfo.irm_questions(stock.code, q.start)
        except SourceError as e:
            gaps.append("董秘问答暂时无法获取，可能遗漏公司的回应")
            qas = []
        stats["irm"] += len(qas)
        for x in qas:
            hit = _hits(x["question"] + x["answer"], kws)
            if not hit and not (rumor_mode and not kws and re.search(r"传闻|谣言|不实|澄清", x["answer"])):
                continue
            t, _ = _dt(x["reply_ms"])
            if not (q.start <= t <= q.end):
                continue
            docs.append(Document(
                id=x["id"], source_type="irm_reply", publisher=f"{x['publisher']}董秘",
                title="互动易回复：" + x["question"][:30], url=f"https://irm.cninfo.com.cn/ircs/company/companyDetail?stockcode={stock.code}",
                text=x["answer"], context=x["question"], disclosure_time=t, crawl_time=now,
                reply_time_approx=x["reply_approx"], matched_keywords=hit))

    docs = _dedupe(docs)
    if len(docs) > MAX_DOCS:
        # 保留最相关的：命中关键词多的优先，其次公告优先，再按时间新近
        docs.sort(key=lambda d: (-len(d.matched_keywords), d.source_type != "announcement", -d.disclosure_time.timestamp()))
        gaps.append(f"相关文件共 {len(docs)} 份，这里展示最相关的 {MAX_DOCS} 份")
        docs = sorted(docs[:MAX_DOCS], key=lambda d: d.disclosure_time)
    if any(d.reply_time_approx for d in docs):
        gaps.append("部分董秘回复没有单独的回复时间，按问答记录的更新时间显示，可能略晚于实际回复")
    stats["selected"] = [{"id": d.id, "title": d.title, "keywords": d.matched_keywords,
                          "disclosure": d.disclosure_time.isoformat(), "dup": len(d.duplicates)} for d in docs]
    return docs, stats, gaps
