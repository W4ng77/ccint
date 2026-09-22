"""M6 Agent 单测。用 mock 的工具返回，不打网络、不调真实模型。"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ccint.agent.analyst import (OUTPUT_SCHEMA, AnalysisResult, render_prompt,
                                 run_analyst, verify_evidence)
from ccint.agent.tools import TOOL_SCHEMAS, TopicTools
from ccint.models import TrendCandidate

UTC = timezone.utc
AS_OF = datetime(2026, 9, 20, tzinfo=UTC)

CAND = TrendCandidate(
    topic_key="ransomware", cur_posts=40, prev_posts=12, cur_authors=31,
    prev_authors=10, abs_delta=28, rel_growth=2.333, cur_share=0.4,
    prev_share=0.2, share_delta=0.2,
)


# -- prompt ---------------------------------------------------------------

def test_prompt_renders_with_stats():
    p = render_prompt(CAND, AS_OF.isoformat(), 7)
    assert "ransomware" in p
    assert "40 posts from 31 distinct authors" in p
    assert "alternative_explanation" in p
    assert "You do not decide what counts as a trend" in p


def test_prompt_handles_prev_zero():
    c = TrendCandidate("x", 10, 0, 8, 0, 10, None, 1.0, 0.0, 1.0)
    p = render_prompt(c, AS_OF.isoformat(), 7)
    assert "n/a (previous window had no posts)" in p
    # [MUST] prev=0 不得渲染成 inf / Infinity / nan
    import re as _re
    assert not _re.search(r"\b(inf|Infinity|-?nan)\b", p, _re.I)


# -- 输出 schema ----------------------------------------------------------

def test_output_schema_requires_alternative_explanation():
    """[MUST] alternative_explanation 是必填字段。"""
    assert "alternative_explanation" in OUTPUT_SCHEMA["required"]
    assert OUTPUT_SCHEMA["additionalProperties"] is False


def test_output_schema_shape_matches_handoff():
    for k in ("topic_key", "what_is_happening", "is_single_event_or_multiple",
              "key_entities", "evidence", "alternative_explanation",
              "confidence", "confidence_reason"):
        assert k in OUTPUT_SCHEMA["properties"], k
    assert OUTPUT_SCHEMA["properties"]["confidence"]["enum"] == ["high", "medium", "low"]
    ev = OUTPUT_SCHEMA["properties"]["evidence"]["items"]
    assert ev["required"] == ["post_id", "why"]


def test_tool_schemas_are_strict():
    for t in TOOL_SCHEMAS:
        assert t["strict"] is True
        assert t["input_schema"]["additionalProperties"] is False


# -- post_id 幻觉校验 [MUST] ---------------------------------------------

def test_hallucinated_post_id_is_caught():
    """[MUST] 故意注入一个不存在的 id，断言被捕获且不静默丢弃。"""
    analysis = {
        "evidence": [{"post_id": 101, "why": "real"},
                     {"post_id": 999999, "why": "fabricated"}],
        "alternative_explanation": "could be one news story",
    }
    w = verify_evidence(analysis, served={101, 102})
    assert len(w) == 1
    assert "HALLUCINATED post_id=999999" in w[0]
    # 不静默丢弃：原 analysis 未被修改
    assert len(analysis["evidence"]) == 2


def test_all_real_post_ids_pass():
    analysis = {"evidence": [{"post_id": 101, "why": "x"}],
                "alternative_explanation": "y"}
    assert verify_evidence(analysis, served={101, 102}) == []


def test_empty_evidence_warns():
    analysis = {"evidence": [], "alternative_explanation": "y"}
    assert any("未提供任何 evidence" in x for x in verify_evidence(analysis, {1}))


def test_missing_alternative_explanation_warns():
    analysis = {"evidence": [{"post_id": 1, "why": "x"}], "alternative_explanation": "  "}
    assert any("alternative_explanation" in x for x in verify_evidence(analysis, {1}))


# -- 工具 -----------------------------------------------------------------

def _seed_topic(conn, topic, n_by_author: dict, day_offset=-3):
    conn.execute("""INSERT INTO collection_runs (run_id, source_key, mode,
                    query_version, query_spec, status)
                    VALUES (1,'test','backfill','v','{}','success')
                    ON CONFLICT DO NOTHING""")
    pid = 0
    for author, n in n_by_author.items():
        for i in range(n):
            pid += 1
            conn.execute(
                """INSERT INTO posts (source_key, source_post_id, author_id,
                    author_handle, published_at, first_seen_run, text, text_hash,
                    like_count, repost_count, reply_count, url, raw_payload)
                   VALUES ('test',%s,%s,%s,%s,1,%s,'h',%s,0,0,%s,'{}')""",
                (f"p{pid}", author, f"{author}.bsky.social",
                 AS_OF + timedelta(days=day_offset), f"post text {pid}", i, f"https://x/{pid}"))
            conn.execute(
                """INSERT INTO post_labels (post_id,label_version,is_cyber,is_canada,
                    is_relevant,topic_key,is_low_information)
                   SELECT post_id,'rules_v1',true,true,true,%s,false FROM posts
                   WHERE source_post_id=%s""", (topic, f"p{pid}"))


def test_representative_posts_enforce_author_diversity(clean_db):
    """[MUST] 强制覆盖至少 3 个不同 author，避免全部来自同一人。"""
    with clean_db.connect(autocommit=True) as conn:
        # 一个高 engagement 刷量者 + 三个低 engagement 的真实作者
        _seed_topic(conn, "ransomware", {"did:spam": 20, "did:a": 1, "did:b": 1, "did:c": 1})
        t = TopicTools(conn, "rules_v1")
        posts = t.get_representative_posts("ransomware", 7, AS_OF.isoformat(), k=5)
    assert len({p["author_handle"] for p in posts}) >= 3


def test_representative_posts_exclude_low_information(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        _seed_topic(conn, "ransomware", {"did:a": 3, "did:b": 3, "did:c": 3})
        conn.execute("UPDATE post_labels SET is_low_information=true WHERE post_id<=3")
        t = TopicTools(conn, "rules_v1")
        posts = t.get_representative_posts("ransomware", 7, AS_OF.isoformat(), k=20)
    assert all(p["post_id"] > 3 for p in posts)


def test_representative_posts_have_required_fields(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        _seed_topic(conn, "ransomware", {"did:a": 2, "did:b": 2, "did:c": 2})
        t = TopicTools(conn, "rules_v1")
        posts = t.get_representative_posts("ransomware", 7, AS_OF.isoformat(), k=5)
    for p in posts:
        for f in ("post_id", "url", "published_at", "author_handle", "text"):
            assert f in p, f


def test_served_post_ids_tracked_for_verification(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        _seed_topic(conn, "ransomware", {"did:a": 2, "did:b": 2, "did:c": 2})
        t = TopicTools(conn, "rules_v1")
        posts = t.get_representative_posts("ransomware", 7, AS_OF.isoformat(), k=4)
    assert t.served_post_ids == {p["post_id"] for p in posts}


def test_compare_periods_prev_zero_is_none(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        _seed_topic(conn, "ransomware", {"did:a": 5}, day_offset=-3)
        t = TopicTools(conn, "rules_v1")
        r = t.compare_periods("ransomware", AS_OF.isoformat(), 7)
    assert r["previous"]["n_posts"] == 0
    assert r["rel_growth"] is None


def test_topic_stats_exposes_posts_per_author(clean_db):
    """刷量指标：agent 需要它来识别假爆发。"""
    with clean_db.connect(autocommit=True) as conn:
        _seed_topic(conn, "ransomware", {"did:spam": 10})
        t = TopicTools(conn, "rules_v1")
        s = t.get_topic_stats("ransomware", 7, AS_OF.isoformat())
    assert s["n_posts"] == 10 and s["n_authors"] == 1
    assert s["posts_per_author"] == 10.0


# -- agent loop（mock 模型）----------------------------------------------

class _FakeClient:
    """按预设脚本回应，不打网络。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def _tool_use_resp(name, args, tid="t1"):
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", id=tid, name=name, input=args)],
    )


def _final_resp(payload):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )


def _payload(evidence):
    return {
        "topic_key": "ransomware",
        "what_is_happening": "A Canadian hospital network was hit.",
        "is_single_event_or_multiple": "single",
        "key_entities": ["LockBit"],
        "evidence": evidence,
        "alternative_explanation": "Could be one news story being widely shared.",
        "confidence": "medium",
        "confidence_reason": "few posts",
    }


def test_agent_loop_runs_tools_then_returns_structured_output(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        _seed_topic(conn, "ransomware", {"did:a": 2, "did:b": 2, "did:c": 2})
        t = TopicTools(conn, "rules_v1")
        ids = [p["post_id"] for p in
               t.get_representative_posts("ransomware", 7, AS_OF.isoformat(), k=3)]

        fake = _FakeClient([
            _tool_use_resp("get_representative_posts",
                           {"topic_key": "ransomware", "window_days": 7,
                            "as_of": AS_OF.isoformat(), "k": 10}),
            _final_resp(_payload([{"post_id": ids[0], "why": "names the victim"}])),
        ])
        res = run_analyst(conn, CAND, as_of=AS_OF.isoformat(), window_days=7,
                          label_version="rules_v1", model="claude-opus-5",
                          api_key="x", client=fake)
    assert isinstance(res, AnalysisResult)
    assert res.warnings == []
    assert res.analysis["topic_key"] == "ransomware"
    assert res.tool_calls[0]["tool"] == "get_representative_posts"
    # 结构化输出必须被声明
    assert fake.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_agent_hallucination_flows_into_warnings(clean_db):
    """[MUST] 端到端：故意让 agent 返回不存在的 post_id，断言进入 warnings。"""
    with clean_db.connect(autocommit=True) as conn:
        _seed_topic(conn, "ransomware", {"did:a": 2, "did:b": 2, "did:c": 2})
        fake = _FakeClient([
            _tool_use_resp("get_representative_posts",
                           {"topic_key": "ransomware", "window_days": 7,
                            "as_of": AS_OF.isoformat(), "k": 10}),
            _final_resp(_payload([{"post_id": 424242, "why": "invented"}])),
        ])
        res = run_analyst(conn, CAND, as_of=AS_OF.isoformat(), window_days=7,
                          label_version="rules_v1", model="claude-opus-5",
                          api_key="x", client=fake)
    assert any("HALLUCINATED post_id=424242" in w for w in res.warnings)
    # 不静默丢弃：原始 evidence 仍在结果里，供报告读者判断
    assert res.analysis["evidence"][0]["post_id"] == 424242


def test_agent_tool_error_is_returned_not_raised(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        fake = _FakeClient([
            _tool_use_resp("get_topic_stats", {"bogus_arg": 1}),
            _final_resp(_payload([])),
        ])
        res = run_analyst(conn, CAND, as_of=AS_OF.isoformat(), window_days=7,
                          label_version="rules_v1", model="claude-opus-5",
                          api_key="x", client=fake)
    assert res.tool_calls[0]["ok"] is False
    assert res.warnings, "空 evidence 应产生 warning"


def test_agent_refusal_raises(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        fake = _FakeClient([SimpleNamespace(stop_reason="refusal", content=[],
                                            stop_details=None)])
        with pytest.raises(RuntimeError, match="refused"):
            run_analyst(conn, CAND, as_of=AS_OF.isoformat(), window_days=7,
                        label_version="rules_v1", model="claude-opus-5",
                        api_key="x", client=fake)


def test_agent_gives_up_after_max_turns(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        fake = _FakeClient([_tool_use_resp("get_topic_stats",
                            {"topic_key": "ransomware", "window_days": 7,
                             "as_of": AS_OF.isoformat()}, tid=f"t{i}")
                            for i in range(5)])
        with pytest.raises(RuntimeError, match="未收敛"):
            run_analyst(conn, CAND, as_of=AS_OF.isoformat(), window_days=7,
                        label_version="rules_v1", model="claude-opus-5",
                        api_key="x", client=fake, max_turns=3)


def test_agent_cannot_change_ranking(clean_db):
    """[MUST] agent 只有三个只读工具，没有任何能改排名的能力。"""
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {"get_topic_stats", "get_representative_posts", "compare_periods"}
    for t in TOOL_SCHEMAS:
        assert not any(w in t["name"] for w in ("set", "update", "rank", "write"))
