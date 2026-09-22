"""本地 agent 后端单测。[MUST] 不打网络 —— 用 httpx MockTransport。"""
import json

import httpx
import pytest

from ccint.agent import local_analyst as LA
from ccint.models import TrendCandidate

CAND = TrendCandidate(
    topic_key="data_breach", cur_posts=28, prev_posts=14, cur_authors=24,
    prev_authors=13, abs_delta=14, rel_growth=1.0, cur_share=0.15,
    prev_share=0.08, share_delta=0.07,
)
AS_OF = "2026-09-20T00:00:00+00:00"


class FakeTools:
    """替身：记录被服务过的 post_id，供幻觉校验用。"""
    def __init__(self, ids=(1, 2, 3)):
        self.served_post_ids = set(ids)
        self.served_post_urls = {i: f"https://bsky.app/p/{i}" for i in ids}
        self.calls = []

    def get_topic_stats(self, **kw):
        self.calls.append("get_topic_stats")
        return {"topic_key": kw["topic_key"], "cur_posts": 28}

    def get_representative_posts(self, **kw):
        self.calls.append("get_representative_posts")
        return [{"post_id": i, "text": f"post {i}"} for i in sorted(self.served_post_ids)]

    def compare_periods(self, **kw):
        self.calls.append("compare_periods")
        return {"cur": 28, "prev": 14}


def _run(monkeypatch, responses, tools=None, **kw):
    tools = tools or FakeTools()
    monkeypatch.setattr(LA, "TopicTools", lambda *a, **k: tools)
    monkeypatch.setattr(LA, "render_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(LA, "resolve_model_identity",
                        lambda base, name: "Qwen/Qwen3-4B-Instruct-2507")

    seq = iter(responses)

    def handler(req):
        return httpx.Response(200, json=next(seq))

    transport = httpx.MockTransport(handler)
    real_post = httpx.post

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.post(url, **kwargs)

    monkeypatch.setattr(LA.httpx, "post", fake_post)
    res = LA.run_analyst_local(None, CAND, as_of=AS_OF, window_days=7,
                               label_version="rules_v1", **kw)
    return res, tools


def _tool_turn(name, args):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def _final(analysis, usage=None):
    return {"choices": [{"message": {"role": "assistant",
                                     "content": json.dumps(analysis)}}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 50}}


GOOD = {
    "topic_key": "data_breach",
    "what_is_happening": "A single incident dominates the window.",
    "is_single_event_or_multiple": "single",
    "key_entities": ["Alberta voter list"],
    "evidence": [{"post_id": 1, "why": "names the incident"},
                 {"post_id": 2, "why": "corroborates"}],
    "alternative_explanation": "可能只是单一新闻被大量转发。",
    "confidence": "medium",
    "confidence_reason": "样本小。",
}


# -- 工具格式转换 ---------------------------------------------------------

def test_openai_tool_conversion():
    ts = LA._openai_tools()
    assert len(ts) == 3
    names = {t["function"]["name"] for t in ts}
    assert names == {"get_topic_stats", "get_representative_posts", "compare_periods"}
    for t in ts:
        assert t["type"] == "function"
        assert "parameters" in t["function"], "OpenAI 用 parameters，不是 input_schema"


# -- 正常路径 -------------------------------------------------------------

def test_tool_loop_then_structured_output(monkeypatch):
    res, tools = _run(monkeypatch, [
        _tool_turn("get_topic_stats", {"topic_key": "data_breach",
                                       "window_days": 7, "as_of": AS_OF}),
        _tool_turn("get_representative_posts", {"topic_key": "data_breach",
                                                "window_days": 7, "as_of": AS_OF}),
        {"choices": [{"message": {"role": "assistant", "content": "done"}}]},
        _final(GOOD),
    ])
    assert tools.calls == ["get_topic_stats", "get_representative_posts"]
    assert res.analysis["topic_key"] == "data_breach"
    assert res.warnings == []
    assert [c["tool"] for c in res.tool_calls] == tools.calls
    assert all(c["ok"] for c in res.tool_calls)


def test_provenance_records_real_model_not_alias(monkeypatch):
    """[MUST] 报告必须能看出这份分析出自什么模型，served-model-name 可能是任意别名。"""
    res, _ = _run(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": ""}}]},
        _final(GOOD),
    ], model="user")
    assert res.model == "local:Qwen/Qwen3-4B-Instruct-2507"
    assert res.model != "local:user"


# -- 幻觉校验（与 Anthropic 后端共用同一套代码）---------------------------

def test_hallucinated_post_id_is_caught(monkeypatch):
    """[MUST] 故意注入不存在的 id，必须被捕获并记入 warning，不得静默丢弃。"""
    bad = {**GOOD, "evidence": [{"post_id": 1, "why": "real"},
                                {"post_id": 99999, "why": "编造的"}]}
    res, _ = _run(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": ""}}]},
        _final(bad),
    ])
    assert any("99999" in w and "HALLUCINATED" in w for w in res.warnings)
    # 不静默丢弃：编造的条目仍在 analysis 里，由报告的 warning 区提示读者
    assert len(res.analysis["evidence"]) == 2


def test_empty_alternative_explanation_is_caught(monkeypatch):
    """[MUST] alternative_explanation 是必填字段。"""
    bad = {**GOOD, "alternative_explanation": "   "}
    res, _ = _run(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": ""}}]},
        _final(bad),
    ])
    assert any("alternative_explanation" in w for w in res.warnings)


def test_no_evidence_is_caught(monkeypatch):
    res, _ = _run(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": ""}}]},
        _final({**GOOD, "evidence": []}),
    ])
    assert any("未提供任何 evidence" in w for w in res.warnings)


# -- 健壮性 ---------------------------------------------------------------

def test_bad_tool_arguments_do_not_crash(monkeypatch):
    """小模型常产出畸形 JSON 参数 —— 必须降级为 tool error 而非崩溃。"""
    broken = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "get_topic_stats", "arguments": "{not json"}}]}}]}
    res, _ = _run(monkeypatch, [
        broken,
        {"choices": [{"message": {"role": "assistant", "content": ""}}]},
        _final(GOOD),
    ])
    assert res.tool_calls[0]["ok"] is False
    assert "JSON" in res.tool_calls[0]["error"]


def test_unparseable_final_output_raises(monkeypatch):
    with pytest.raises(RuntimeError, match="合法 JSON"):
        _run(monkeypatch, [
            {"choices": [{"message": {"role": "assistant", "content": ""}}]},
            {"choices": [{"message": {"role": "assistant", "content": "这不是 JSON"}}]},
        ])


def test_agent_cannot_change_ranking(monkeypatch):
    """[MUST] agent 只做 analyst，拿不到任何能改排名或自选分析对象的工具。"""
    exposed = {t["function"]["name"] for t in LA._openai_tools()}
    assert exposed == {"get_topic_stats", "get_representative_posts", "compare_periods"}
    assert all("detect" not in n and "rank" not in n for n in exposed)
