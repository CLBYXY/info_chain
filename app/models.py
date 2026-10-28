"""贯穿六层的数据结构。每一层的输出就是下一层的输入。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

SourceType = Literal["announcement", "irm_reply", "news", "research", "selfmedia"]
ClaimType = Literal["事实", "观点", "推测", "传闻"]
Grade = Literal["已证实", "有依据", "未证实"]
EdgeType = Literal["作证", "冲突", "更正", "推进"]
Persona = Literal["retail", "researcher", "pm", "compliance"]

FIRST_HAND: set[str] = {"announcement", "irm_reply"}
SOURCE_LABEL = {
    "announcement": "公司公告",
    "irm_reply": "董秘回复（互动易）",
    "news": "新闻",
    "research": "研报",
    "selfmedia": "自媒体",
}


class Stock(BaseModel):
    thscode: str
    name: str

    @property
    def code(self) -> str:
        return self.thscode.split(".")[0]


# ---------- ① 理解层输出 ----------
class Query(BaseModel):
    question: str
    persona: Persona = "retail"
    intents: list[str] = Field(default_factory=list)  # A–G；H 表示兜底
    fallback: Optional[str] = None  # H 子类：non_event / no_coverage / vague / off_topic
    stocks: list[Stock] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    start: datetime
    end: datetime
    time_defaulted: bool = False
    clarify: Optional[str] = None
    watchlist: list[str] = Field(default_factory=list)


# ---------- ② 取数层输出 ----------
class Document(BaseModel):
    id: str
    source_type: SourceType
    publisher: str
    title: str
    url: str
    text: str
    disclosure_time: datetime
    disclosure_date_only: bool = False  # 只有日期、没有具体时刻
    crawl_time: datetime
    update_time: Optional[datetime] = None
    reply_time_approx: bool = False  # 互动易回复时间取自 updateDate 时为 True
    context: str = ""  # 互动易：投资者提问，只作上下文，不抽取为声明
    duplicates: list[str] = Field(default_factory=list)  # 被合并的转载
    matched_keywords: list[str] = Field(default_factory=list)


# ---------- ③ 证据层输出 ----------
class Claim(BaseModel):
    id: str
    doc_id: str
    source_type: SourceType
    publisher: str
    quote: str  # 原文片段，已回查
    statement: str  # 规范化表述
    aspect: str  # 说的是事件的哪个要素
    stance: Literal["support", "refute", "neutral"] = "neutral"  # 相对事件核心说法的立场
    response_type: Literal["确认", "否认", "未置可否", "无"] = "无"
    attributed_to: str = ""  # 非空表示转述他人说法
    stage: str = "无"  # 该声明表明事件处于哪个阶段（LLM 提议；只有一手来源的才会改变事件状态）
    event_time: Optional[str] = None  # 从正文抽取的事件发生时间
    numbers: list[str] = Field(default_factory=list)
    counterparties: list[str] = Field(default_factory=list)
    allowed_types: list[ClaimType] = Field(default_factory=list)
    type: ClaimType = "传闻"
    type_method: str = ""  # 硬规则 / LLM×3 一致 / LLM 不一致取保守 / 规则降级
    needs_review: bool = False
    disclosure_time: datetime
    url: str = ""


# ---------- ④ 事件层输出 ----------
class Node(BaseModel):
    id: str
    time: datetime
    headline: str
    headline_claim: str = ""
    claims: list[Claim]
    grade: Grade = "未证实"
    grade_reason: str = ""
    conflict: Optional[str] = None  # 节点内冲突说明
    state_effect: Optional[str] = None  # 该节点让事件进入的状态
    later_tags: list[str] = Field(default_factory=list)
    first_hand: bool = False


class Edge(BaseModel):
    source: str
    target: str
    type: EdgeType
    label: str
    method: str = ""


class Event(BaseModel):
    id: str
    title: str
    stock: Optional[Stock] = None
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    status: str = "传闻中"
    status_node: Optional[str] = None
    grade: Grade = "未证实"
    flags: list[str] = Field(default_factory=list)
    possibly_related: list[Claim] = Field(default_factory=list)


# ---------- ⑤ 关联层输出 ----------
class RelatedStock(BaseModel):
    stock: Stock
    role: str
    basis: str
    in_watchlist: bool = False


class MarketWindow(BaseModel):
    thscode: str
    benchmark: str
    benchmark_name: str
    t0: str
    t0_rule: str
    dates: list[str]
    stock_cum: list[float]
    bench_cum: list[float]
    car: Optional[float]
    note: str = "只描述数据，不代表因果"


class Relation(BaseModel):
    stocks: list[RelatedStock] = Field(default_factory=list)
    market: Optional[MarketWindow] = None
    valuation: Optional[dict] = None
    financial: Optional[dict] = None


# ---------- 过程记录（可追溯） ----------
class TraceStep(BaseModel):
    layer: str
    summary: str
    detail: dict = Field(default_factory=dict)
    ms: int = 0
