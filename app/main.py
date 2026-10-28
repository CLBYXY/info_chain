"""Web 服务：会话式问答（后台任务 + 逐层进度）、话题订阅与更新推送、管理端刷新。"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import pipeline, store, updates
from app.config import ROOT

app = FastAPI(title="EventLens 投资事件证据链")
WEB = ROOT / "web"
LAYERS = ["① 理解层", "② 取数层", "③ 证据层", "④ 事件层", "⑤ 关联层", "⑥ 生成层"]
PERSONAS = ("retail", "researcher", "pm", "compliance")

store.init()
_jobs: dict[str, dict] = {}
_answers: dict = {}  # 同一问题 10 分钟内复用结果，避免重复调用 LLM


class UserReq(BaseModel):
    user: str = Field(min_length=4, max_length=64)


class AskReq(UserReq):
    question: str = Field(min_length=2, max_length=200)
    persona: str = "retail"
    watchlist: list[str] = Field(default_factory=list)


@app.get("/api/meta")
def meta():
    from app.config import LLM_ENABLED, LLM_MODEL, LLM_PROVIDER

    return {"llm_enabled": LLM_ENABLED, "model": f"{LLM_PROVIDER}:{LLM_MODEL}"}


# ---------------- 会话 ----------------
@app.get("/api/conversations")
def conversations(user: str):
    return store.list_conversations(user)


@app.post("/api/conversations")
def new_conversation(req: UserReq):
    return store.create_conversation(req.user, "新对话")


@app.get("/api/conversations/{cid}")
def conversation(cid: str, user: str):
    conv = store.get_conversation(cid, user)
    if not conv:
        raise HTTPException(404, "会话不存在")
    store.mark_read(cid)
    # 正在生成的任务：前端切换回来或刷新页面后据此恢复进度显示
    conv["running_job"] = next((jid for jid, j in _jobs.items()
                                if j["conversation_id"] == cid and j["status"] == "running"), None)
    return conv


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str, user: str):
    store.delete_conversation(cid, user)
    return {"ok": True}


# ---------------- 提问：后台任务 + 逐层进度 ----------------
def _register_topic(ans: dict, req: AskReq, cid: str) -> None:
    """记录“谁问过这个话题”。话题已存在且这次拿到了新信息时，顺带通知其他关注者。"""
    tid = store.topic_key(ans)
    if not tid:
        return
    existing = next((t for t in store.topics() if t["id"] == tid), None)
    if existing:
        d = updates.diff(existing["snapshot"], ans)
        if d["changed"]:
            d["topic_id"], d["topic_title"] = tid, existing["title"]
            for f in store.followers(tid):
                if f["conversation_id"] != cid:
                    store.add_message(f["conversation_id"], "update", d)
                    store.add_update(tid, f["conversation_id"], f["user_id"], d)
            store.upsert_topic(tid, ans, req.question, req.persona)
    else:
        store.upsert_topic(tid, ans, req.question, req.persona)
    store.follow(tid, cid, req.user)
    ans["topic_id"] = tid


def _run_job(jid: str, cid: str, req: AskReq) -> None:
    job = _jobs[jid]

    def on_step(layer, state, summary):
        job["layers"][layer] = {"state": state, "summary": summary}

    try:
        key = (req.question.strip(), req.persona, tuple(req.watchlist))
        hit = _answers.get(key)
        if hit and time.time() - hit[0] < 600:
            ans = {**hit[1], "cached_at": hit[2]}
            for layer in LAYERS:
                on_step(layer, "done", "复用 10 分钟内的结果")
        else:
            ans = pipeline.run(req.question, req.persona, req.watchlist, on_step=on_step)  # type: ignore[arg-type]
            _answers[key] = (time.time(), ans, f"{datetime.now():%H:%M}")
        _register_topic(ans, req, cid)
        msg = store.add_message(cid, "assistant", ans)
        conv = store.get_conversation(cid, req.user)
        if conv and conv["title"] in ("新对话", req.question[:24]) and ans.get("kind") == "event":
            store.rename_conversation(cid, ans["event"]["title"])
        job.update(status="done", message=msg)
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=f"{type(e).__name__}: {e}")
        store.add_message(cid, "assistant", {"kind": "error", "text": "处理失败，请稍后重试。", "detail": str(e)[:300]})


@app.post("/api/conversations/{cid}/ask")
def ask(cid: str, req: AskReq):
    if req.persona not in PERSONAS:
        raise HTTPException(400, "persona 取值无效")
    conv = store.get_conversation(cid, req.user)
    if not conv:
        raise HTTPException(404, "会话不存在")
    if conv["title"] == "新对话":
        store.rename_conversation(cid, req.question[:24])
    user_msg = store.add_message(cid, "user", {"text": req.question, "persona": req.persona})
    jid = store.new_id()
    _jobs[jid] = {"status": "running", "layers": {l: {"state": "pending", "summary": ""} for l in LAYERS},
                  "conversation_id": cid, "started": time.time()}
    threading.Thread(target=_run_job, args=(jid, cid, req), daemon=True).start()
    return {"job_id": jid, "user_message": user_msg}


@app.get("/api/jobs/{jid}")
def job(jid: str):
    j = _jobs.get(jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    return {**j, "elapsed": int(time.time() - j["started"])}


# ---------------- 更新推送 ----------------
@app.get("/api/updates")
def unread(user: str):
    return store.unread_updates(user)


class RewindReq(BaseModel):
    topic_id: str
    as_of: str  # YYYY-MM-DD


@app.get("/api/admin/topics")
def admin_topics():
    return [{"id": t["id"], "title": t["title"], "followers": t["followers"], "as_of": t["as_of"],
             "status": t["snapshot"].get("conclusion", {}).get("status"),
             "nodes": len((t["snapshot"].get("event") or {}).get("nodes", []))} for t in store.topics()]


@app.post("/api/admin/refresh")
def admin_refresh(topic_id: Optional[str] = None):
    out = []
    for t in store.topics():
        if topic_id and t["id"] != topic_id:
            continue
        d = updates.refresh_topic(t)
        out.append({"topic_id": t["id"], "changed": d["changed"], "headline": d["headline"],
                    "followers": t["followers"] if d["changed"] else 0})
    return out


@app.post("/api/admin/rewind")
def admin_rewind(req: RewindReq):
    t = next((x for x in store.topics() if x["id"] == req.topic_id), None)
    if not t:
        raise HTTPException(404, "话题不存在")
    return updates.rewind_topic(t, datetime.fromisoformat(req.as_of).replace(hour=18))


class SimReq(UserReq):
    kind: str = "update"
    conversation_id: Optional[str] = None


@app.post("/api/test/simulate-update")
def simulate_update(req: SimReq):
    """测试按钮：模拟新证据 / 否认 / 更正 / 过期四类变化，推送给当前（或最近一个）关注了话题的会话。"""
    if req.kind not in updates.SIM_KINDS:
        raise HTTPException(400, "kind 取值无效")
    convs = {c["id"]: c for c in store.list_conversations(req.user)}
    best = None
    for t in store.topics():
        for f in store.followers(t["id"]):
            if f["conversation_id"] not in convs:
                continue
            score = (f["conversation_id"] == req.conversation_id, convs[f["conversation_id"]]["updated"])
            if best is None or score > best[0]:
                best = (score, t, f)
    if not best:
        raise HTTPException(404, "还没有问过任何事件话题，先提一个问题再测试")
    _, t, f = best
    d = updates.simulate(t, f["conversation_id"], req.user, req.kind)
    return {"ok": True, "conversation_id": f["conversation_id"], "headline": d["headline"]}


def _auto_refresh():
    minutes = float(os.getenv("EVENTLENS_REFRESH_MINUTES", "0"))
    while minutes > 0:
        time.sleep(minutes * 60)
        try:
            admin_refresh()
        except Exception:  # noqa: BLE001 - 定时任务失败不影响服务
            pass


threading.Thread(target=_auto_refresh, daemon=True).start()

app.mount("/static", StaticFiles(directory=WEB), name="static")


NO_CACHE = {"Cache-Control": "no-cache"}  # 页面本身不缓存，静态资源靠 ?v= 版本号更新


@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers=NO_CACHE)


@app.get("/admin")
def admin_page():
    return FileResponse(WEB / "admin.html", headers=NO_CACHE)
