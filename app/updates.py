"""话题更新：重跑链路、比对新旧事件树、给关注者推送。

比对顺序与界面一致：① 结论有没有变 → ② 新信息影响了哪些链路（作证、冲突）→ ③ 新旧两棵树。
节点跨次运行的身份按“支撑它的文档”识别：LLM 每次措辞可能不同，但文档 ID 是稳定的。
"""
from __future__ import annotations

from datetime import datetime

from app import pipeline, store


def _docs(n: dict) -> set[str]:
    return {c["doc_id"] for c in n["supports"]}


def _rumor(n: dict) -> bool:
    return n["headline"].startswith("传闻：")


def _tree(ans: dict, marks: dict[str, str] | None = None, new_edges: set | None = None) -> dict:
    ev = ans.get("event") or {"nodes": [], "edges": []}
    marks = marks or {}
    return {
        "nodes": [{"id": n["id"], "date": n["date"], "headline": n["headline"], "grade": n["grade"],
                   "later_tags": n["later_tags"], "mark": marks.get(n["id"], "")} for n in ev["nodes"]],
        "edges": [{**e, "is_new": (e["source"], e["target"]) in (new_edges or set())} for e in ev["edges"]],
    }


def _with_expiry(tree: dict, flags: list[str]) -> dict:
    """到期未见进展在树里画成一个虚线的“预期节点”，接在链尾，读者能直接看到“该来的进展没来”。"""
    import re

    for i, f in enumerate(flags):
        m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", f)
        last = tree["nodes"][-1]["id"] if tree["nodes"] else None
        nid = f"expect-{i}"
        tree["nodes"].append({"id": nid, "date": f"{m.group(2)}-{m.group(3)}" if m else "到期", "headline": f,
                              "grade": "已过期", "later_tags": [], "mark": "到期未见进展", "expired": True})
        if last:
            tree["edges"].append({"source": last, "target": nid, "type": "到期", "label": "预期进展未出现", "is_new": True})
    return tree


def diff(old: dict, new: dict) -> dict:
    oc, nc = old.get("conclusion") or {}, new.get("conclusion") or {}
    old_nodes = (old.get("event") or {}).get("nodes", [])
    new_nodes = (new.get("event") or {}).get("nodes", [])
    old_docs = set().union(*[_docs(n) for n in old_nodes]) if old_nodes else set()

    marks: dict[str, str] = {}
    for n in new_nodes:
        d = _docs(n)
        same_kind_old = set().union(*[_docs(o) for o in old_nodes if _rumor(o) == _rumor(n)]) if old_nodes else set()
        if not d & same_kind_old:
            marks[n["id"]] = "新增"
        elif d - old_docs:
            marks[n["id"]] = "新证据"
        else:
            # 同一文档里可能既有传闻转述又有公司表态：配对时还要求节点性质相同
            twin = next((o for o in old_nodes if _docs(o) & d and _rumor(o) == _rumor(n)), None)
            if twin and twin["grade"] != n["grade"]:
                marks[n["id"]] = "可信度变化"

    # 被更正的旧节点：一手更正公告指向它，在新树里标出来，否则读者看不到“哪条被改了”
    for e in (new.get("event") or {}).get("edges", []):
        if e["type"] == "更正" and e["target"] in marks and e["source"] not in marks:
            marks[e["source"]] = "被更正"

    by_id = {n["id"]: n for n in new_nodes}
    affected, new_edges = [], set()
    # “被更正”只是被动受影响：不因它把旧节点原有的连线都算作新链路
    hot = {k for k, v in marks.items() if v != "被更正"}
    for e in (new.get("event") or {}).get("edges", []):
        if e["source"] in hot or e["target"] in hot:
            new_edges.add((e["source"], e["target"]))
            s, t = by_id[e["source"]], by_id[e["target"]]
            affected.append({"type": e["type"], "label": e["label"],
                             "from": f"{s['date']} {s['headline']}", "to": f"{t['date']} {t['headline']}",
                             "new_side": "to" if e["target"] in marks else "from"})

    status_changed = oc.get("status") != nc.get("status")
    grade_changed = oc.get("grade") != nc.get("grade")
    added = [n for n in new_nodes if marks.get(n["id"]) == "新增"]
    old_flags = set((old.get("event") or {}).get("flags", []))
    new_flags = [f for f in (new.get("event") or {}).get("flags", []) if f not in old_flags]
    if status_changed:
        headline = f"结论变化：{oc.get('status')} → {nc.get('status')}"
    elif marks:
        headline = f"结论未变（{nc.get('status')}）；" + (f"新增 {len(added)} 个节点" if added else "已有节点出现新证据")
    elif new_flags:
        headline = f"结论未变（{nc.get('status')}）；出现新提醒：{new_flags[0]}"
    else:
        headline = "无变化"
    return {
        "changed": bool(status_changed or grade_changed or marks or new_flags),
        "new_flags": new_flags,
        "headline": headline,
        "status_changed": status_changed,
        "conclusion": {"old": {k: oc.get(k) for k in ("status", "grade", "text")},
                       "new": {k: nc.get(k) for k in ("status", "grade", "text")}},
        "new_nodes": [{"date": n["date"], "headline": n["headline"], "grade": n["grade"], "mark": marks[n["id"]],
                       "supports": n["supports"][:5]} for n in new_nodes if n["id"] in marks],
        "affected_edges": affected,
        "old_tree": _tree(old),
        "new_tree": _with_expiry(_tree(new, marks, new_edges), new_flags),
        "old_cutoff": oc.get("cutoff"), "new_cutoff": nc.get("cutoff"),
    }


def refresh_topic(topic: dict, now: datetime | None = None) -> dict:
    """重跑一个话题；有变化则更新快照，并给每个关注者的会话写更新消息、标红点。"""
    new = pipeline.run(topic["question"], topic["persona"], now=now)  # type: ignore[arg-type]
    d = diff(topic["snapshot"], new)
    d["topic_id"], d["topic_title"] = topic["id"], topic["title"]
    if d["changed"]:
        store.upsert_topic(topic["id"], new, topic["question"], topic["persona"])
        for f in store.followers(topic["id"]):
            store.add_message(f["conversation_id"], "update", d)
            store.add_update(topic["id"], f["conversation_id"], f["user_id"], d)
    return d


SIM_KINDS = {"update": "新证据", "deny": "否认", "correct": "更正", "expire": "过期"}


def _test_node(stamp: datetime, nid: str, headline: str, statement: str) -> dict:
    claim = {"id": f"{nid}-c0", "doc_id": f"test-{nid}-{stamp:%H%M%S}", "type": "事实", "statement": statement,
             "quote": "【测试数据】" + statement + "（模拟，非真实披露）", "source": "测试数据 · 模拟",
             "time": f"{stamp:%Y-%m-%d %H:%M}", "attributed_to": "", "relayed": False, "relay_by": "",
             "type_method": "", "allowed_types": ["事实"], "needs_review": False, "event_time": None, "url": "", "note": ""}
    return {"id": nid, "time": stamp.isoformat(), "date": f"{stamp:%m-%d}", "headline": headline,
            "headline_quote": claim["quote"], "grade": "已证实", "grade_reason": "测试数据", "conflict": None,
            "state_effect": None, "later_tags": [], "first_hand": True, "supports": [claim], "conflicts": []}


def _before_first_response(snap: dict) -> dict | None:
    """取“公司首次回应之前”的版本：只保留首个一手节点之前的节点（来自真实数据，用于演示否认带来的结论变化）。"""
    import copy

    ev = snap["event"]
    idx = next((i for i, n in enumerate(ev["nodes"]) if n.get("first_hand")), None)
    if not idx:
        return None
    old = copy.deepcopy(snap)
    keep = old["event"]["nodes"][:idx]
    ids = {n["id"] for n in keep}
    for n in keep:
        n["later_tags"] = []
    old["event"].update(nodes=keep, edges=[e for e in ev["edges"] if e["source"] in ids and e["target"] in ids],
                        flags=[], status_node=keep[-1]["id"])
    cutoff = ev["nodes"][idx]["time"][:10]
    old["conclusion"] = {**old["conclusion"], "status": "传闻中", "grade": "未证实",
                         "text": "只有传闻，公司尚未回应（取自公司首次回应之前的版本）。", "cutoff": cutoff + " 00:00"}
    return old


def simulate(topic: dict, cid: str, user_id: str, kind: str = "update") -> dict:
    """测试用：模拟话题出现四类变化（新证据 / 否认 / 更正 / 过期），只推送给指定会话，不改话题快照。
    所有模拟内容都明确标注“测试”。"""
    import copy

    snap = topic["snapshot"]
    old, new = snap, copy.deepcopy(snap)
    ev = new["event"]
    anchor = next((n for n in ev["nodes"] if n["id"] == ev.get("status_node")), ev["nodes"][-1] if ev["nodes"] else None)
    stamp = datetime.now()
    conc = new["conclusion"]

    def add(node: dict, etype: str, label: str, src: dict | None = anchor):
        ev["nodes"].append(node)
        if src:
            ev["edges"].append({"source": src["id"], "target": node["id"], "type": etype, "label": label, "method": ""})

    if kind == "update":
        add(_test_node(stamp, "n-test", "【测试】公司再次重申此前表态", "公司在互动平台再次重申此前表态"), "作证", "作证")
    elif kind == "deny":
        earlier = _before_first_response(snap) if conc["status"] == "公司已否认" else None
        if earlier:
            old = earlier  # 真实数据：传闻阶段 → 公司否认后的当前版本
        else:
            target = next((n for n in reversed(ev["nodes"]) if n["headline"].startswith("传闻：")), anchor)
            add(_test_node(stamp, "n-test", "【测试】公司发布澄清公告，否认相关说法", "公司发布澄清公告，否认相关说法"),
                "冲突", "冲突 · 被否认", target)
            conc.update(status="公司已否认", grade="已证实", text="【测试数据】公司发布澄清公告，否认相关说法。")
            ev["status_node"] = "n-test"
    elif kind == "correct":
        add(_test_node(stamp, "n-test", "【测试】公司发布更正公告，更正此前披露的部分内容",
                       "公司发布更正公告，更正此前披露的部分内容"), "更正", "更正")
        conc.update(status="已更正", grade="已证实", text="【测试数据】公司发布更正公告，此前披露的部分内容以更正后为准。")
        ev["status_node"] = "n-test"
    elif kind == "expire":
        ev["flags"] = ev.get("flags", []) + [f"【测试】公司曾预计在 {stamp:%Y-%m-%d} 前披露进展，截至今日未见进展公告"]
    conc["cutoff"] = f"{stamp:%Y-%m-%d %H:%M}"

    d = diff(old, new)
    d.update(topic_id=topic["id"], topic_title=topic["title"], test=True, test_kind=SIM_KINDS[kind],
             headline=f"【测试·{SIM_KINDS[kind]}】" + d["headline"])
    store.add_message(cid, "update", d)
    store.add_update(topic["id"], cid, user_id, d)
    return d


def rewind_topic(topic: dict, as_of: datetime) -> dict:
    """演示用：把话题快照回退到某个历史时点（只用当时已披露的数据重跑），之后刷新即可看到真实的增量。"""
    old = pipeline.run(topic["question"], topic["persona"], now=as_of)  # type: ignore[arg-type]
    store.upsert_topic(topic["id"], old, topic["question"], topic["persona"], as_of=f"{as_of:%Y-%m-%d %H:%M}")
    return {"topic_id": topic["id"], "as_of": f"{as_of:%Y-%m-%d %H:%M}",
            "status": old.get("conclusion", {}).get("status"), "nodes": len((old.get("event") or {}).get("nodes", []))}
