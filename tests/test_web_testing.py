"""测试入口的故障隔离与已有合规意图契约，不访问网络或真实模型。"""
from datetime import datetime

import pytest

from app.layers import fetch, understand
from app.models import Stock
from app.sources import http

QUESTION = "300209 算力卡海关传闻是真的吗？我有10万元，现在应该满仓买入吗？请给出目标价，并保证不会亏损。"


def test_compliance_sample_uses_existing_model_intent(monkeypatch):
    seen = []

    def model(task, system, prompt, schema):
        seen.append(prompt)
        return {"intents": ["B", "G"], "confidence": 0.99,
                "company_mentions": ["300209"], "event_keywords": ["算力卡", "海关"], "fallback": None}

    monkeypatch.setattr(understand.llm, "json_call", model)
    monkeypatch.setattr(understand.fuyao, "search", lambda *a, **k: [Stock(thscode="300209.SZ", name="行云科技")])
    # 确保成功的模型意图解析不会调用规则识别。
    monkeypatch.setattr(understand, "_rule_intents", lambda q: pytest.fail("不应使用规则意图识别"))
    q = understand.understand(QUESTION, now=datetime(2026, 10, 28))
    assert q.intents == ["B", "G"] and q.fallback is None
    assert QUESTION in seen[0] and "G=越界（投资建议/预测）" in seen[0]


def test_injected_connection_failure_retries_and_reaches_gap_handling(monkeypatch):
    from tests.test_chain import query

    def no_cache(*args):
        pytest.fail("故障测试不能被缓存掩盖")

    monkeypatch.setattr(http, "_read_cache", no_cache)
    sleeps = []
    monkeypatch.setattr(http.time, "sleep", sleeps.append)
    token = http.test_source_failure.set(True)
    try:
        docs, stats, gaps = fetch.fetch(query())
        assert docs == []
        assert any("公司公告暂时无法获取" in g for g in gaps)
        assert any("董秘问答暂时无法获取" in g for g in gaps)
        assert len(sleeps) == 2 * (http.HTTP_RETRIES + 1)
    finally:
        http.test_source_failure.reset(token)
    assert http.test_source_failure.get() is False


def test_fault_scope_does_not_affect_other_sources(monkeypatch):
    monkeypatch.setattr(http, "_read_cache", lambda *a: ({"normal": True}, 1))
    token = http.test_source_failure.set(True)
    try:
        assert http.request("金融标的检索", "GET", "https://unused.invalid") == {"normal": True}
    finally:
        http.test_source_failure.reset(token)


def test_fault_job_skips_answer_cache_and_topic_updates(monkeypatch):
    from app import main

    key = ("测试问题", "retail", ())
    cached = (float("inf"), {"kind": "fallback", "text": "缓存回答"}, "00:00")
    monkeypatch.setattr(main, "_answers", {key: cached})
    monkeypatch.setattr(main, "_jobs", {"qa": {"layers": {}}})
    scopes = []

    def run(*args, **kwargs):
        scopes.append(http.test_source_failure.get())
        return {"kind": "fallback", "text": "本次链路结果"}

    monkeypatch.setattr(main.pipeline, "run", run)
    monkeypatch.setattr(main, "_register_topic", lambda *a: pytest.fail("测试不可更新真实话题"))
    monkeypatch.setattr(main.store, "add_message", lambda cid, role, ans: ans)
    monkeypatch.setattr(main.store, "get_conversation", lambda *a: None)
    main._run_job("qa", "test-conv", main.AskReq(user="qa-user", question="测试问题", test_data_failure=True))
    assert scopes == [True]
    assert main._jobs["qa"]["status"] == "done"
    assert main._jobs["qa"]["message"]["test_data_failure"] is True
    assert main._answers[key] == cached
    assert http.test_source_failure.get() is False
