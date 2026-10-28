"""SQLite 存储：用户、会话、消息、话题、话题关注者、更新通知。

“话题”是同一事件在库里的唯一记录（按股票 + 事件关键词归一）。谁问过这个话题，
就成为它的关注者；话题有新信息时，给每个关注者的会话写一条更新并标红点。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from app.config import DATA_DIR

DB = DATA_DIR / "eventlens.db"
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
  created TEXT NOT NULL, updated TEXT NOT NULL, unread INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_conv_user ON conversations(user_id, updated);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON messages(conversation_id, created);
CREATE TABLE IF NOT EXISTS topics (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, question TEXT NOT NULL, persona TEXT NOT NULL,
  snapshot TEXT NOT NULL, as_of TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topic_followers (
  topic_id TEXT NOT NULL, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL, since TEXT NOT NULL,
  PRIMARY KEY (topic_id, conversation_id));
CREATE TABLE IF NOT EXISTS updates (
  id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
  diff TEXT NOT NULL, created TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_upd_user ON updates(user_id, read);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@contextmanager
def db():
    with _lock:
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init() -> None:
    with db() as c:
        c.executescript(SCHEMA)


# ---------------- 会话与消息 ----------------
def create_conversation(user_id: str, title: str) -> dict:
    cid, t = new_id(), now()
    with db() as c:
        c.execute("INSERT INTO conversations VALUES (?,?,?,?,?,0)", (cid, user_id, title, t, t))
    return {"id": cid, "title": title, "created": t, "updated": t, "unread": 0}


def list_conversations(user_id: str) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM conversations WHERE user_id=? ORDER BY updated DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_conversation(cid: str, user_id: str) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM conversations WHERE id=? AND user_id=?", (cid, user_id)).fetchone()
        if not r:
            return None
        msgs = c.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY created, rowid", (cid,)).fetchall()
    conv = dict(r)
    conv["messages"] = [{**dict(m), "content": json.loads(m["content"])} for m in msgs]
    return conv


def rename_conversation(cid: str, title: str) -> None:
    with db() as c:
        c.execute("UPDATE conversations SET title=? WHERE id=?", (title, cid))


def delete_conversation(cid: str, user_id: str) -> None:
    with db() as c:
        c.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        c.execute("DELETE FROM topic_followers WHERE conversation_id=?", (cid,))
        c.execute("DELETE FROM updates WHERE conversation_id=?", (cid,))
        c.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (cid, user_id))


def add_message(cid: str, role: str, content: Any) -> dict:
    mid, t = new_id(), now()
    with db() as c:
        c.execute("INSERT INTO messages VALUES (?,?,?,?,?)", (mid, cid, role, json.dumps(content, ensure_ascii=False, default=str), t))
        c.execute("UPDATE conversations SET updated=? WHERE id=?", (t, cid))
    return {"id": mid, "role": role, "content": content, "created": t}


def mark_read(cid: str) -> None:
    with db() as c:
        c.execute("UPDATE conversations SET unread=0 WHERE id=?", (cid,))
        c.execute("UPDATE updates SET read=1 WHERE conversation_id=?", (cid,))


# ---------------- 话题与关注 ----------------
def topic_key(answer: dict) -> str | None:
    q = answer.get("query") or {}
    if answer.get("kind") != "event" or not q.get("stocks"):
        return None
    return q["stocks"][0]["thscode"] + "|" + "、".join(sorted(q.get("keywords") or []))


def upsert_topic(tid: str, answer: dict, question: str, persona: str, as_of: str | None = None) -> None:
    title = answer["event"]["title"]
    snap = json.dumps(answer, ensure_ascii=False, default=str)
    with db() as c:
        c.execute("""INSERT INTO topics VALUES (?,?,?,?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET title=excluded.title, snapshot=excluded.snapshot,
                     as_of=excluded.as_of, updated=excluded.updated""",
                  (tid, title, question, persona, snap, as_of or now(), now()))


def follow(tid: str, cid: str, user_id: str) -> None:
    with db() as c:
        c.execute("INSERT OR IGNORE INTO topic_followers VALUES (?,?,?,?)", (tid, cid, user_id, now()))


def topics() -> list[dict]:
    with db() as c:
        rows = c.execute("""SELECT t.*, COUNT(f.conversation_id) AS followers FROM topics t
                            LEFT JOIN topic_followers f ON f.topic_id=t.id GROUP BY t.id ORDER BY t.updated DESC""").fetchall()
    return [{**dict(r), "snapshot": json.loads(r["snapshot"])} for r in rows]


def followers(tid: str) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM topic_followers WHERE topic_id=?", (tid,)).fetchall()]


def add_update(tid: str, cid: str, user_id: str, diff: dict) -> str:
    uid, t = new_id(), now()
    with db() as c:
        c.execute("INSERT INTO updates VALUES (?,?,?,?,?,?,0)", (uid, tid, cid, user_id, json.dumps(diff, ensure_ascii=False, default=str), t))
        c.execute("UPDATE conversations SET unread=1, updated=? WHERE id=?", (t, cid))
    return uid


def unread_updates(user_id: str) -> list[dict]:
    with db() as c:
        rows = c.execute("""SELECT u.id, u.topic_id, u.conversation_id, u.created, u.diff, c.title FROM updates u
                            JOIN conversations c ON c.id=u.conversation_id
                            WHERE u.user_id=? AND u.read=0 ORDER BY u.created DESC""", (user_id,)).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["diff"])
        out.append({"id": r["id"], "conversation_id": r["conversation_id"], "title": r["title"],
                    "created": r["created"], "headline": d["headline"]})
    return out
