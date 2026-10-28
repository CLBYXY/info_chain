"""⑤ 关联层：找出事件牵涉的股票，并展示事件前后的行情数据。只描述，不归因。"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models import FIRST_HAND, Event, MarketWindow, Query, RelatedStock, Relation, Stock
from app.sources import fuyao
from app.sources.http import SourceError

WINDOW = 5


def benchmark(thscode: str) -> tuple[str, str]:
    code = thscode.split(".")[0]
    if code.startswith(("300", "301")):
        return "399006.SZ", "创业板指"
    if code.startswith(("688", "689")):
        return "000688.SH", "科创50"
    if code.startswith(("8", "4", "92")):
        return "899050.BJ", "北证50"
    return "000300.SH", "沪深300"


def t0_of(first: datetime, trading_days: list[datetime]) -> tuple[datetime | None, str]:
    """首次披露日：15:00 之后披露的顺延到下一个交易日；只有日期（00:00）的按当日盘前处理。"""
    date_only = (first.hour, first.minute, first.second) == (0, 0, 0)
    after_close = not date_only and (first.hour, first.minute) >= (15, 0)
    day = first.replace(hour=0, minute=0, second=0, microsecond=0)
    if after_close:
        rule = f"{first:%m-%d %H:%M} 收盘后披露，t0 顺延到下一个交易日"
        cands = [d for d in trading_days if d > day]
    else:
        rule = f"{first:%m-%d} 披露（{'只有日期，按盘前处理' if date_only else '盘中/盘前'}），t0 取当日或之后首个交易日"
        cands = [d for d in trading_days if d >= day]
    return (cands[0] if cands else None), rule


def market_window(stock: Stock, first: datetime, now: datetime) -> MarketWindow | None:
    bcode, bname = benchmark(stock.thscode)
    start, end = first - timedelta(days=20), min(now, first + timedelta(days=20))
    px = fuyao.daily_prices(stock.thscode, start, end)
    bx = {x["date_ms"]: x["close_price"] for x in fuyao.daily_prices(bcode, start, end, index=True)}
    px = [x for x in px if x["date_ms"] in bx]
    days = [datetime.fromtimestamp(x["date_ms"] / 1000) for x in px]
    t0, rule = t0_of(first, days)
    if t0 is None:
        return None
    i0 = days.index(t0)
    lo, hi = max(1, i0 - WINDOW), min(len(px) - 1, i0 + WINDOW)
    base_p, base_b = px[lo - 1]["close_price"], bx[px[lo - 1]["date_ms"]]
    dates, s_cum, b_cum, car = [], [], [], 0.0
    for i in range(lo, hi + 1):
        p, pp = px[i]["close_price"], px[i - 1]["close_price"]
        b, bp = bx[px[i]["date_ms"]], bx[px[i - 1]["date_ms"]]
        car += (p / pp - 1) - (b / bp - 1)
        dates.append(f"{days[i]:%m-%d}")
        s_cum.append(round((p / base_p - 1) * 100, 2))
        b_cum.append(round((b / base_b - 1) * 100, 2))
    note = "只描述数据，不代表因果"
    if hi - i0 < WINDOW:
        note += f"；事件后只有 {hi - i0} 个交易日数据，窗口未满"
    return MarketWindow(thscode=stock.thscode, benchmark=bcode, benchmark_name=bname, t0=f"{t0:%Y-%m-%d}",
                        t0_rule=rule, dates=dates, stock_cum=s_cum, bench_cum=b_cum,
                        car=round(car * 100, 2), note=note)


def relate(ev: Event, q: Query, now: datetime | None = None) -> tuple[Relation, list[str]]:
    now = now or datetime.now()
    rel, gaps = Relation(), []
    watch = set(q.watchlist)
    if ev.stock:
        rel.stocks.append(RelatedStock(stock=ev.stock, role="事件主体", basis="用户问题中的公司，经代码检索接口确认",
                                       in_watchlist=ev.stock.code in watch or ev.stock.name in watch))
    # 交易对手：只取一手来源中写明的名称，并经接口确认代码；确认不了就不展示
    seen = {ev.stock.thscode} if ev.stock else set()
    for n in ev.nodes:
        for c in n.claims:
            if c.source_type not in FIRST_HAND or c.attributed_to:
                continue
            for name in c.counterparties:
                try:
                    hits = [s for s in fuyao.search(name) if s.name == name]
                except SourceError:
                    continue
                if hits and hits[0].thscode not in seen:
                    seen.add(hits[0].thscode)
                    rel.stocks.append(RelatedStock(stock=hits[0], role="交易对手", basis=f"公告写明：{c.quote[:40]}",
                                                   in_watchlist=hits[0].code in watch or hits[0].name in watch))
    if ev.stock and ev.nodes:
        first = min(n.time for n in ev.nodes)
        try:
            rel.market = market_window(ev.stock, first, now)
        except SourceError as e:
            gaps.append("行情数据暂时无法获取，未展示事件前后走势")
        try:
            rel.valuation = fuyao.valuation(ev.stock.thscode)
        except SourceError:
            pass
    gaps.append("估值只显示当前值，不做事件前后对比；暂不展示产业链上下游公司")
    return rel, gaps
