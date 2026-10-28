"""主链路规则测试：不访问网络、不调用 LLM，覆盖设计文档中的确定性规则。"""
from datetime import datetime

import pytest

from app.layers import event, fetch, generate, rules
from app.layers.relate import t0_of
from app.models import Claim, Query, Relation, Stock
from app.updates import diff
from app.sources.http import SourceError

STOCK = Stock(thscode="300209.SZ", name="行云科技")


def claim(cid, doc, src, quote, t, *, stance="support", attributed="", ctype=None, resp="无", aspect=None):
    c = Claim(id=cid, doc_id=doc, source_type=src, publisher="行云科技" if src != "selfmedia" else "某自媒体",
              quote=quote, statement=quote, aspect=aspect or quote[:12], stance=stance, response_type=resp,
              attributed_to=attributed, disclosure_time=t)
    c.allowed_types = rules.allowed_types(src, quote, attributed)
    c.type = ctype or c.allowed_types[0]
    return c


def query(keywords=("算力卡", "海关")):
    return Query(question="网传行云科技的算力卡被卡在海关，是真的吗？", intents=["B"], stocks=[STOCK],
                 keywords=list(keywords), start=datetime(2026, 7, 1), end=datetime(2026, 10, 28))


# ---------------- ③ 证据层：硬规则 ----------------
@pytest.mark.parametrize("src,quote,attr,expected", [
    ("selfmedia", "X 将收购 Y", "", ["传闻"]),
    ("news", "据知情人士透露，X 将收购 Y", "", ["传闻"]),
    ("announcement", "“算力卡被卡在海关”", "市场传闻（公告转述）", ["传闻"]),
    ("announcement", "网传算力卡被卡在海关，经核查均不属实", "", ["事实", "推测"]),  # 说话人是公司
    ("irm_reply", "公司在手订单交付正常", "", ["事实", "推测"]),
    ("news", "公司公告称已签署合同", "", ["事实", "观点", "推测"]),
    ("research", "我们预计营收增长 30%", "", ["观点", "推测"]),
])
def test_allowed_types(src, quote, attr, expected):
    assert rules.allowed_types(src, quote, attr) == expected


def test_soft_judge_fallback_stays_inside_allowed():
    assert rules.cue_type(["事实", "推测"], "公司拟于 2026 年底前完成交割") == "推测"
    assert rules.cue_type(["事实", "推测"], "公司已完成交割") == "事实"
    assert rules.cue_type(["传闻"], "已完成") == "传闻"
    assert rules.most_conservative(["事实", "观点", "推测"]) == "推测"


def test_sentence_split_removes_header_and_boilerplate():
    text = ("证券代码：300209 证券简称：行云科技 公告编号：2026-115行云科技股份有限公司关于进展公告"
            "行云科技股份有限公司（以下简称“公司”）于 2026 年 8 月 12 日披露了澄清公告。"
            "二、澄清声明经核查，相关传言均不属实。敬请广大投资者理性投资，注意投资风险。")
    sents = [s for s in rules.split_sentences(text) if not __import__("re").search(rules.BOILER, s)]
    assert sents[0].startswith("于 2026 年 8 月 12 日")
    assert sents[1].startswith("经核查")
    assert len(sents) == 2


# ---------------- ④ 事件层 ----------------
def test_late_rumor_is_inserted_before_denial_and_graded_as_of_its_time():
    deny = claim("a1", "ann1", "announcement", "“行云科技的算力卡被卡在海关”等传言均不属实", datetime(2026, 8, 12),
                 stance="refute", resp="否认", ctype="事实")
    # 晚到的传闻：抓取晚，但披露时间更早
    rumor = claim("s1", "sm1", "selfmedia", "行云科技的算力卡被卡在海关", datetime(2026, 8, 11, 9))
    ev, _ = event.build([deny, rumor], query(), now=datetime(2026, 10, 28))
    assert [n.claims[0].id for n in ev.nodes] == ["s1", "a1"]
    rumor_node, deny_node = ev.nodes
    assert rumor_node.grade == "未证实" and deny_node.grade == "已证实"
    assert ev.edges[0].type == "冲突" and ev.edges[0].label == "冲突 · 被否认"
    assert rumor_node.later_tags == ["→ 08-12 被公司否认"]
    assert ev.status == "公司已否认" and ev.grade == "已证实"


def test_repeated_rumor_merges_into_first_rumor_node():
    r1 = claim("r1", "ann1", "announcement", "行云科技的算力卡被卡在海关", datetime(2026, 8, 12), attributed="市场传闻")
    d1 = claim("d1", "ann1", "announcement", "“行云科技的算力卡被卡在海关”等传言均不属实", datetime(2026, 8, 12),
               stance="refute", resp="否认", ctype="事实")
    r2 = claim("r2", "ann2", "announcement", "行云科技的算力卡被卡在海关", datetime(2026, 9, 8), attributed="市场传闻")
    nodes = event.build_nodes([r1, d1, r2])
    rumor_nodes = [n for n in nodes if all(c.type == "传闻" for c in n.claims)]
    assert len(rumor_nodes) == 1 and len(rumor_nodes[0].claims) == 2


def test_rumor_remention_with_different_wording_merges_by_quoted_text():
    r1 = claim("r1", "ann1", "announcement", "网络平台出现“行云科技的算力卡被卡在海关”的言论", datetime(2026, 8, 12),
               attributed="网络平台")
    r2 = claim("r2", "ann2", "announcement", "网传“行云科技的算力卡被卡在海关”", datetime(2026, 9, 8), attributed="社交平台")
    nodes = event.build_nodes([r1, r2])
    assert len(nodes) == 1


def test_topic_diff_pairs_nodes_of_same_kind_only():
    old = _answer("公司已否认", [("n1", "08-11", "传闻：X", "未证实", "ann1"), ("n2", "08-12", "公司否认", "已证实", "ann1")],
                  [("n1", "n2", "冲突")])
    d = diff(old, old)
    assert d["changed"] is False


def test_status_cites_first_denial_not_later_restatement():
    d1 = claim("d1", "ann1", "announcement", "“算力卡被卡在海关”等传言均不属实", datetime(2026, 8, 12),
               stance="refute", resp="否认", ctype="事实")
    d2 = claim("d2", "ann2", "announcement", "对造谣、传谣主体依法追究，相关算力卡传言不实", datetime(2026, 9, 8),
               stance="refute", resp="否认", ctype="事实")
    ev, _ = event.build([d1, d2], query(), now=datetime(2026, 10, 28))
    assert ev.status == "公司已否认"
    assert ev.status_node == ev.nodes[0].id
    assert ev.edges[0].type == "作证"


def test_unrelated_first_hand_progress_does_not_change_core_state():
    d1 = claim("d1", "ann1", "announcement", "“算力卡被卡在海关”等传言均不属实", datetime(2026, 8, 12),
               stance="refute", resp="否认", ctype="事实")
    police = claim("p1", "ann2", "announcement", "公安分局决定对相关违法行为人实施行政处罚。", datetime(2026, 9, 8),
                   ctype="事实", aspect="行政处罚", stance="neutral")
    q = query()
    ev, _ = event.build([d1, police], q, now=datetime(2026, 10, 28))
    assert ev.status == "公司已否认"  # “实施行政处罚”不是事件本身的推进


def test_consistent_company_statements_are_not_conflict():
    """“传言不属实”（否认）与“清关合规”（同样否认）、“已报警”（中性）出自同一公告：同一节点，无冲突连线。"""
    t = datetime(2026, 8, 12)
    d1 = claim("d1", "ann1", "announcement", "算力卡被卡在海关的传言不属实", t, stance="refute", resp="否认", ctype="事实")
    d2 = claim("d2", "ann1", "announcement", "进口清关合规、手续完备", t, stance="refute", ctype="事实", aspect="清关合规")
    d3 = claim("d3", "ann1", "announcement", "公司已向公安机关报警", t, stance="neutral", ctype="事实", aspect="报警")
    ev, _ = event.build([d1, d2, d3], query(), now=datetime(2026, 10, 28))
    assert len(ev.nodes) == 1 and not ev.edges
    assert ev.status == "公司已否认"


def test_neutral_node_never_forms_conflict():
    r = claim("r1", "sm1", "selfmedia", "行云科技的算力卡被卡在海关", datetime(2026, 8, 11))
    n = claim("p1", "ann2", "announcement", "相关算力卡传言已由公安机关处理", datetime(2026, 9, 8),
              stance="neutral", ctype="事实")
    ev, _ = event.build([r, n], query(), now=datetime(2026, 10, 28))
    assert all(e.type != "冲突" for e in ev.edges)


def test_overclaim_conclusion_falls_back_to_template(monkeypatch):
    d1 = claim("d1", "ann1", "announcement", "“算力卡被卡在海关”等传言均不属实", datetime(2026, 8, 12),
               stance="refute", resp="否认", ctype="事实")
    ev, _ = event.build([d1], query(), now=datetime(2026, 10, 28))
    monkeypatch.setattr(generate, "_llm_conclusion", lambda *a: "公司已否认。不是真的。")
    ans, checks = generate.generate(query(), ev, Relation(), [], [], datetime(2026, 10, 28))
    assert "不是真的" not in ans["conclusion"]["text"] and checks["writer"].startswith("模板")


def test_legal_opinion_wording_is_not_company_denial():
    c = claim("l1", "ann1", "announcement", "本次合同签署不存在违反法律法规的情形", datetime(2026, 10, 19),
              stance="neutral", ctype="事实", aspect="合同合规")
    ev, _ = event.build([c], query(keywords=("重大合同",)), now=datetime(2026, 10, 28))
    assert ev.status != "公司已否认"


def test_speculation_does_not_create_conflict():
    fact = claim("f1", "irm1", "irm_reply", "公司在手订单规模约150亿元", datetime(2026, 8, 13), ctype="事实", aspect="订单")
    spec = claim("s1", "irm2", "irm_reply", "若订单未能顺利交付，可能影响现金流", datetime(2026, 8, 14),
                 stance="refute", ctype="推测", aspect="订单")
    ev, _ = event.build([fact, spec], query(keywords=("订单",)), now=datetime(2026, 10, 28))
    assert all(e.type != "冲突" for e in ev.edges) and ev.status != "公司已否认"


def test_progress_states_do_not_regress():
    done = claim("a1", "ann1", "announcement", "重大合同已完成签署并生效", datetime(2026, 10, 19), ctype="事实")
    plan = claim("a2", "irm1", "irm_reply", "合同框架下的后续安排正在规划", datetime(2026, 10, 27), ctype="事实",
                 aspect="后续安排")
    done.stage, plan.stage = "已完成", "已披露预案"
    ev, _ = event.build([done, plan], query(keywords=("合同",)), now=datetime(2026, 10, 28))
    assert ev.status == "已完成"


def test_two_independent_sources_without_first_hand_is_yellow():
    a = claim("n1", "news1", "news", "X 公司签下 50 亿元订单", datetime(2026, 9, 1), ctype="事实")
    b = claim("n2", "news2", "news", "X 公司签下 50 亿元订单", datetime(2026, 9, 1, 12), ctype="事实")
    b.publisher = "另一家媒体"
    nodes = event.build_nodes([a, b])
    event.grade_node(nodes[0])
    assert len(nodes) == 1 and nodes[0].grade == "有依据"


def test_numeric_conflict_inside_node_sets_badge():
    a = claim("n1", "news1", "news", "订单约 50 亿元", datetime(2026, 9, 1), ctype="事实", aspect="订单金额")
    b = claim("n2", "news2", "news", "订单约 30 亿元", datetime(2026, 9, 1), ctype="事实", aspect="订单金额")
    a.numbers, b.numbers, b.publisher = ["50亿元"], ["30亿元"], "另一家媒体"
    nodes = event.build_nodes([a, b])
    event.grade_node(nodes[0])
    assert nodes[0].conflict and "50亿元" in nodes[0].conflict


def test_different_numbers_in_one_document_are_not_conflict():
    a = claim("a1", "ann1", "announcement", "公告编号 2026-110，订单约 150 亿元", datetime(2026, 8, 12), ctype="事实", aspect="订单")
    b = claim("a2", "ann1", "announcement", "首批交付 20 亿元", datetime(2026, 8, 12), ctype="事实", aspect="订单")
    a.numbers, b.numbers = ["2026", "150亿元"], ["20亿元"]
    nodes = event.build_nodes([a, b])
    event.grade_node(nodes[0])
    assert nodes[0].conflict is None


# ---------------- ⑤ 关联层 ----------------
def test_t0_after_close_moves_to_next_trading_day():
    days = [datetime(2026, 10, 22), datetime(2026, 10, 23), datetime(2026, 10, 26)]
    t0, rule = t0_of(datetime(2026, 10, 22, 19, 30), days)
    assert t0 == datetime(2026, 10, 23) and "顺延" in rule
    t0, _ = t0_of(datetime(2026, 10, 22, 0, 0), days)  # 只有日期：按盘前处理
    assert t0 == datetime(2026, 10, 22)
    t0, _ = t0_of(datetime(2026, 10, 24, 10, 0), days)  # 周末披露：下一个交易日
    assert t0 == datetime(2026, 10, 26)


# ---------------- ⑥ 生成层 ----------------
def test_citation_check_drops_sentence_with_unsourced_number():
    d1 = claim("d1", "ann1", "announcement", "在手订单规模约150.82亿元", datetime(2026, 8, 12), ctype="事实")
    ev, _ = event.build([d1], query(keywords=("订单",)), now=datetime(2026, 10, 28))
    text, removed = generate.cite_check("公司在手订单约150.82亿元。预计明年增长到300亿元。", ev, Relation())
    assert "150.82" in text and "300亿" not in text and len(removed) == 1


def test_banned_words_and_g_intent():
    d1 = claim("d1", "ann1", "announcement", "“算力卡被卡在海关”等传言均不属实", datetime(2026, 8, 12),
               stance="refute", resp="否认", ctype="事实")
    q = query()
    q.intents = ["G", "B"]
    ev, _ = event.build([d1], q, now=datetime(2026, 10, 28))
    ans, checks = generate.generate(q, ev, Relation(), [], [], datetime(2026, 10, 28))
    notice = ans["conclusion"]["prefix"]
    assert "理性判断" in notice and "公开事实与证据链供你参考" in notice
    assert "不代你做买卖决定" in notice and "无法给出目标价或保证收益" in notice
    assert not any(word in notice for word in ("不合规", "越界", "意图识别"))
    assert ans["conclusion"]["text"].startswith("公司已否认")
    assert "利好" not in ans["conclusion"]["text"]


# ---------------- 订阅通知 ----------------
def _answer(status, nodes, edges):
    return {"kind": "event", "conclusion": {"status": status, "grade": "已证实", "text": "", "cutoff": "x"},
            "event": {"nodes": [{"id": i, "date": d, "headline": h, "grade": g, "later_tags": [],
                                 "supports": [{"doc_id": doc, "statement": h}]} for i, d, h, g, doc in nodes],
                      "edges": [{"source": s, "target": t, "type": ty, "label": ty} for s, t, ty in edges]}}


def test_topic_diff_marks_new_node_and_affected_chain():
    old = _answer("传闻中", [("n1", "08-11", "传闻：X", "未证实", "sm1")], [])
    new = _answer("公司已否认", [("n1", "08-11", "传闻：X", "未证实", "sm1"), ("n2", "08-12", "公司否认", "已证实", "ann1")],
                  [("n1", "n2", "冲突")])
    d = diff(old, new)
    assert d["changed"] and d["status_changed"] and d["headline"] == "结论变化：传闻中 → 公司已否认"
    assert [n["mark"] for n in d["new_tree"]["nodes"]] == ["", "新增"]
    assert d["affected_edges"][0]["type"] == "冲突" and d["new_tree"]["edges"][0]["is_new"]
    assert len(d["old_tree"]["nodes"]) == 1


def test_topic_diff_same_docs_is_unchanged_even_if_wording_differs():
    old = _answer("公司已否认", [("n1", "08-12", "公司否认传闻", "已证实", "ann1")], [])
    new = _answer("公司已否认", [("n1", "08-12", "公司经核查否认传闻", "已证实", "ann1")], [])
    assert diff(old, new)["changed"] is False


# ---------------- 数据 / 接口异常 ----------------
def test_source_failure_becomes_gap_not_crash(monkeypatch):
    def boom(*a, **k):
        raise SourceError("巨潮资讯", "ConnectTimeout")
    monkeypatch.setattr(fetch.cninfo, "announcements", boom)
    monkeypatch.setattr(fetch.cninfo, "irm_questions", boom)
    docs, stats, gaps = fetch.fetch(query())
    assert docs == []
    assert any("公司公告暂时无法获取" in g for g in gaps) and any("董秘问答暂时无法获取" in g for g in gaps)


def test_empty_result_wording():
    ev, _ = event.build([], query(), now=datetime(2026, 10, 28))
    ans, _ = generate.generate(query(), ev, Relation(), [], [], datetime(2026, 10, 28))
    assert "未检索到" in ans["conclusion"]["text"] and "不存在" not in ans["conclusion"]["text"]


# ---------------- 身份视角 ----------------
def test_persona_views_differ_and_are_grounded():
    from app.layers import views

    rumor = claim("s1", "sm1", "selfmedia", "行云科技的算力卡被卡在海关", datetime(2026, 8, 11, 9))
    deny = claim("a1", "ann1", "announcement", "“行云科技的算力卡被卡在海关”等传言均不属实", datetime(2026, 8, 12),
                 stance="refute", resp="否认", ctype="事实")
    q = query()
    q.watchlist = ["300209"]
    ev, _ = event.build([rumor, deny], q, now=datetime(2026, 10, 28))
    rel = Relation()
    from app.models import RelatedStock
    rel.stocks.append(RelatedStock(stock=STOCK, role="事件主体", basis="x", in_watchlist=True))
    v = views.build(ev, rel, q, [])
    assert "否认" in v["retail"]["meaning"] and v["retail"]["watch_hit"]
    assert v["researcher"]["claim_types"] == {"传闻": 1, "事实": 1} and "methods" not in v["researcher"]
    assert v["pm"]["stage"] == "公司已否认" and v["pm"]["watch_points"]
    assert v["compliance"]["consistency"].startswith("口径一致")
    assert "间隔约 1 天" in v["compliance"]["response_lag"]["text"]
    assert [s["quote"] for s in v["compliance"]["statements"]] == [deny.quote]  # 只列公司官方表态


# ---------------- 测试推送四类变化 ----------------
def _snapshot():
    def node(i, d, h, g, doc, fh):
        return {"id": i, "time": f"2026-{d}T00:00:00", "date": d, "headline": h, "grade": g, "later_tags": [],
                "first_hand": fh, "supports": [{"doc_id": doc, "statement": h}], "conflicts": []}
    return {"kind": "event",
            "conclusion": {"status": "公司已否认", "grade": "已证实", "text": "公司已否认", "cutoff": "2026-10-28 01:00"},
            "event": {"nodes": [node("n1", "08-11", "传闻：X", "未证实", "ann1", False),
                                node("n2", "08-12", "公司否认", "已证实", "ann1", True)],
                      "edges": [{"source": "n1", "target": "n2", "type": "冲突", "label": "冲突 · 被否认"}],
                      "flags": [], "status_node": "n2"}}


@pytest.mark.parametrize("kind,check", [
    ("update", lambda d: d["new_nodes"][0]["mark"] == "新增" and not d["status_changed"]),
    ("deny", lambda d: d["conclusion"]["old"]["status"] == "传闻中" and d["conclusion"]["new"]["status"] == "公司已否认"),
    ("correct", lambda d: d["conclusion"]["new"]["status"] == "已更正" and d["affected_edges"][0]["type"] == "更正"
     and {n["id"]: n["mark"] for n in d["new_tree"]["nodes"]}["n2"] == "被更正"),   # 被更正的旧节点在树里标出
    ("expire", lambda d: d["new_flags"] and "过期" in d["test_kind"] and not d["new_nodes"]
     and d["new_tree"]["nodes"][-1].get("expired") and d["new_tree"]["edges"][-1]["type"] == "到期"),  # 树里出现虚线预期节点
])
def test_simulated_updates(monkeypatch, kind, check):
    from app import updates
    monkeypatch.setattr(updates.store, "add_message", lambda *a: None)
    monkeypatch.setattr(updates.store, "add_update", lambda *a: None)
    topic = {"id": "t", "title": "话题", "snapshot": _snapshot()}
    d = updates.simulate(topic, "c1", "u1", kind)
    assert d["changed"] and d["test"] and d["headline"].startswith("【测试") and check(d)
    assert topic["snapshot"] == _snapshot()  # 测试推送不改真实快照


def test_compliance_boilerplate_is_not_denial():
    c = claim("b1", "ann1", "announcement", "本次募投项目延期不存在损害公司和股东利益的情况", datetime(2026, 10, 28),
              stance="refute", resp="否认", ctype="事实", aspect="募投项目延期")
    c.stage = "已延期"
    ev, _ = event.build([c], query(keywords=("募投项目", "延期")), now=datetime(2026, 10, 28))
    assert ev.status == "已延期"


def test_keyword_synonyms_reach_formal_titles():
    from app.layers.understand import SYNONYMS
    assert "第三季度报告" in SYNONYMS["三季报"] and "募集资金投资项目" in SYNONYMS["募投项目"]


def test_correction_announcement_rebuilds_original_disclosure_and_correction_edge():
    """原文件已被替换时，更正公告回溯写明的原披露排在它自己的日期上，并与更正节点连成“更正”。"""
    orig = claim("o1", "corr", "announcement", "公司于2026年10月27日披露了《2026年第三季度报告》", datetime(2026, 10, 28),
                 ctype="事实", aspect="第三季度报告披露", stance="neutral")
    orig.event_time = "2026-10-27"
    fix = claim("f1", "corr", "announcement", "第三季度报告前10名无限售条件股东信息列示有误，现予以更正", datetime(2026, 10, 28),
                ctype="事实", aspect="第三季度报告更正", stance="neutral")
    fix.stage = "已更正"
    ev, _ = event.build([orig, fix], query(keywords=("三季报", "第三季度报告", "更正")), now=datetime(2026, 10, 28))
    assert [f"{n.time:%m-%d}" for n in ev.nodes] == ["10-27", "10-28"]
    assert ev.edges[0].type == "更正" and ev.status == "已更正"


def test_back_reference_not_used_when_original_present_or_not_a_disclosure():
    deny = claim("d1", "ann1", "announcement", "相关传言均不属实", datetime(2026, 8, 12), stance="refute", resp="否认", ctype="事实")
    police = claim("p1", "ann2", "announcement", "经公安机关查明，2026年8月11日相关人员在微信群转发上述信息", datetime(2026, 9, 8),
                   ctype="事实", stance="neutral", aspect="公安查明")
    police.event_time = "2026-08-11"
    recall = claim("p2", "ann2", "announcement", "公司于2026年8月12日披露了《关于市场传闻的澄清公告》", datetime(2026, 9, 8),
                   ctype="事实", stance="neutral", aspect="回顾澄清")
    recall.event_time = "2026-08-12"
    nodes = event.build_nodes([deny, police, recall], datetime(2026, 7, 1))
    assert {f"{n.time:%m-%d}" for n in nodes} == {"08-12", "09-08"}  # 公安查明不是披露；08-12 原文件在库里
