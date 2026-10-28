"""巨潮资讯：公告检索、公告 PDF 正文、互动易公司问答。"""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Optional

from pypdf import PdfReader

from app.sources.http import SourceError, request

CNINFO = "http://www.cninfo.com.cn"
STATIC = "http://static.cninfo.com.cn/"
IRM = "https://irm.cninfo.com.cn"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def org_id(code: str) -> str:
    rows = request("巨潮资讯", "POST", f"{CNINFO}/new/information/topSearch/query",
                   data={"keyWord": code, "maxNum": 5}, headers=FORM, ttl=86400 * 7)
    for r in rows:
        if r.get("code") == code:
            return r["orgId"]
    raise SourceError("巨潮资讯", f"找不到 {code} 的 orgId")


def _column(code: str) -> str:
    return "sse" if code.startswith("6") else "bj" if code[0] in "48" or code.startswith("92") else "szse"


def announcements(code: str, start: datetime, end: datetime, max_pages: int = 4) -> list[dict]:
    oid = org_id(code)
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        d = request("巨潮资讯", "POST", f"{CNINFO}/new/hisAnnouncement/query", headers=FORM, ttl=1800, data={
            "pageNum": page, "pageSize": 30, "column": _column(code), "tabName": "fulltext",
            "stock": f"{code},{oid}", "seDate": f"{start:%Y-%m-%d}~{end:%Y-%m-%d}", "searchkey": "",
            "isHLtitle": "false",
        })
        rows = d.get("announcements") or []
        for a in rows:
            out.append({
                "id": f"ann-{a['announcementId']}",
                "title": re.sub(r"</?em>", "", a["announcementTitle"]),
                "time_ms": a["announcementTime"],
                "url": STATIC + a["adjunctUrl"],
                "publisher": a.get("secName") or code,
            })
        if len(rows) < 30:
            break
    return out


def pdf_text(url: str) -> str:
    raw = request("巨潮资讯PDF", "GET", url, ttl=None, binary=True)
    reader = PdfReader(io.BytesIO(raw))
    text = "".join(p.extract_text() or "" for p in reader.pages)
    # PDF 按版面换行，合并成连续文本便于分句与原文回查
    return re.sub(r"[ \t]*\n[ \t]*", "", text)


def irm_questions(code: str, start: datetime, max_pages: int = 15) -> list[dict]:
    """互动易公司问答。只返回已回复的问答；按时间倒序翻页，超出时间窗即停止。"""
    oid = org_id(code)
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        d = request("互动易", "POST", f"{IRM}/newircs/company/question", headers=FORM, ttl=1800, data={
            "stockcode": code, "orgId": oid, "pageSize": 20, "pageNum": page,
            "keyWord": "", "startDay": "", "endDay": "",
        })
        rows = d.get("rows") or []
        oldest: Optional[int] = None
        for x in rows:
            pub = int(x["pubDate"])
            oldest = pub if oldest is None else min(oldest, pub)
            answer = x.get("attachedContent")
            if not answer or answer == "None":
                continue
            reply_ms = x.get("attachedPubDate")
            approx = not reply_ms or reply_ms == "None"
            out.append({
                "id": f"irm-{x.get('indexId') or x.get('esId') or pub}",
                "question": x["mainContent"],
                "answer": answer,
                "question_ms": pub,
                "reply_ms": int(x["updateDate"] if approx else reply_ms),
                "reply_approx": approx,
                "publisher": x.get("companyShortName") or code,
            })
        if not rows or (oldest is not None and oldest < start.timestamp() * 1000):
            break
    return out
