"""M7 Report 单测。"""
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from ccint.models import TrendCandidate
from ccint.report import LIMITATIONS, data_health, persist_analysis_run, render_report

UTC = timezone.utc
AS_OF = datetime(2026, 9, 20, tzinfo=UTC)

C1 = TrendCandidate("data_breach", 28, 14, 24, 13, 14, 1.0, 0.30, 0.20, 0.10)
C2 = TrendCandidate("ransomware", 18, 34, 7, 8, -16, -0.47, 0.20, 0.30, -0.10)

CLEAN_HEALTH = {"runs": [], "bad_runs": [], "daily": [], "gaps": [],
                "modes": [{"mode": "incremental", "n_posts": 500}], "has_problem": False}


def _render(**kw):
    base = dict(analysis_id=1, as_of=AS_OF, window_days=7, label_version="rules_v1",
                query_version="cyber_v1", agent_model=None, prompt_version=None,
                candidates=[C1, C2], health=CLEAN_HEALTH, agent_result=None)
    base.update(kw)
    return render_report(**base)


# -- provenance -----------------------------------------------------------

def test_provenance_header_complete():
    md = _render()
    for k in ("analysis_id", "as_of", "label_version", "query_version",
              "trend_method", "agent_model", "prompt_version", "window_days"):
        assert k in md, k
    assert "rules_v1" in md and "cyber_v1" in md and "window_compare_v1" in md


def test_windows_are_explicit_and_adjacent():
    md = _render()
    assert "2026-09-13T00:00:00+00:00" in md   # cur_start
    assert "2026-09-06T00:00:00+00:00" in md   # prev_start


# -- 数据健康警告 [MUST 置顶] -------------------------------------------

def test_failed_run_produces_top_warning():
    h = dict(CLEAN_HEALTH, has_problem=True, bad_runs=[
        {"run_id": 4, "mode": "backfill", "status": "failed", "n_error": 3,
         "n_fetched": 100, "error_detail": "boom"}])
    md = _render(health=h)
    head = md.split("## Provenance")[0]
    assert "⚠️" in head and "数据健康警告" in head, "警告必须在 Provenance 之前"
    assert "run #4" in head and "boom" in head


def test_collection_gap_produces_top_warning():
    h = dict(CLEAN_HEALTH, has_problem=True, gaps=["2026-09-17", "2026-09-18"])
    md = _render(health=h)
    head = md.split("## Provenance")[0]
    assert "采集空洞" in head and "2026-09-17" in head


def test_no_warning_when_healthy():
    assert "数据健康警告" not in _render()


# -- backfill 混入说明 [MUST] -------------------------------------------

def test_backfill_presence_is_stated():
    h = dict(CLEAN_HEALTH, modes=[{"mode": "backfill", "n_posts": 900}])
    md = _render(health=h)
    assert "本窗口包含 backfill 数据" in md
    assert "采样性质不同" in md


def test_pure_incremental_has_no_backfill_note():
    assert "本窗口包含 backfill 数据" not in _render()


# -- top-N 表格 -----------------------------------------------------------

def test_topn_table_includes_unique_authors():
    """[MUST] 报告中必须给出 unique_authors。"""
    md = _render()
    assert "cur authors" in md and "prev authors" in md
    assert "| 24 |" in md and "posts/author" in md


def test_prev_zero_renders_na_not_inf():
    c = TrendCandidate("x", 10, 0, 8, 0, 10, None, 1.0, 0.0, 1.0)
    md = _render(candidates=[c])
    assert "| n/a |" in md
    import re
    assert not re.search(r"\binf\b|\bInfinity\b", md)


def test_empty_candidates_explains_rather_than_blank():
    md = _render(candidates=[])
    assert "min_posts" in md
    assert "数据量不足" in md
    assert "没有趋势" in md


# -- agent 段落 -----------------------------------------------------------

class _FakeAgentResult:
    def __init__(self, analysis, warnings, served, urls=None):
        self.analysis = analysis
        self.warnings = warnings
        self.served_post_ids = served
        self.post_urls = urls or {}
        self.tool_calls = [{"tool": "get_representative_posts", "args": {}, "ok": True}]


def _analysis(evidence):
    return {"topic_key": "data_breach", "what_is_happening": "A breach occurred.",
            "is_single_event_or_multiple": "single", "key_entities": ["Acme"],
            "evidence": evidence, "alternative_explanation": "Could be one story.",
            "confidence": "medium", "confidence_reason": "few posts"}


def test_agent_section_rendered():
    ar = _FakeAgentResult(_analysis([{"post_id": 7, "why": "names victim"}]), [], {7},
                          {7: "https://bsky.app/p/7"})
    md = _render(agent_result=ar, agent_model="claude-opus-5", prompt_version="analyst_v1")
    assert "A breach occurred." in md
    assert "替代解释" in md and "Could be one story." in md
    assert "https://bsky.app/p/7" in md
    assert "claude-opus-5" in md


def test_hallucination_warning_surfaces_in_report():
    """[MUST] 幻觉记入报告 warning 区，不静默丢弃。"""
    ar = _FakeAgentResult(_analysis([{"post_id": 999, "why": "made up"}]),
                          ["HALLUCINATED post_id=999：不在工具返回中"], {7})
    md = _render(agent_result=ar, agent_model="m", prompt_version="p")
    assert "Agent 输出警告" in md
    assert "HALLUCINATED post_id=999" in md
    assert "未经核实" in md, "该条证据本身也要标记"
    assert "999" in md, "不得静默丢弃"


def test_agent_absent_is_explained():
    md = _render()
    assert "未运行 agent 分析" in md


# -- 局限性固定段落 [MUST] ----------------------------------------------

def test_limitations_is_template_constant():
    md = _render()
    assert LIMITATIONS in md
    for k in ("单一 source", "固定规则词表", "无 baseline 模型", "样本量小"):
        assert k in md, k
    assert "不由 agent 生成" in md


def test_limitations_present_even_with_agent():
    ar = _FakeAgentResult(_analysis([]), [], set())
    assert LIMITATIONS in _render(agent_result=ar, agent_model="m", prompt_version="p")


# -- 持久化 ---------------------------------------------------------------

def test_analysis_run_persisted(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        aid = persist_analysis_run(
            conn, as_of=AS_OF, label_version="rules_v1", window_days=7,
            filters={"min_posts": 5}, agent_model="claude-opus-5",
            prompt_version="analyst_v1", report_path="/tmp/r.md",
            candidates=[C1, C2], agent_result=None)
        row = conn.execute("SELECT * FROM analysis_runs WHERE analysis_id=%s",
                           (aid,)).fetchone()
    assert row["label_version"] == "rules_v1"
    assert row["trend_method"] == "window_compare_v1"
    assert row["report_path"] == "/tmp/r.md"
    # trend 快照必须落库，供事后复核
    assert len(row["result"]["candidates"]) == 2
    assert row["result"]["candidates"][0]["topic_key"] == "data_breach"


def test_data_health_detects_gaps(clean_db):
    with clean_db.connect(autocommit=True) as conn:
        h = data_health(conn, window_start=AS_OF - timedelta(days=3), window_end=AS_OF)
    assert len(h["gaps"]) == 3, "空库应报出每一天都是空洞"
    assert h["has_problem"] is True


# -- 空库友好报错 [验收] --------------------------------------------------

def test_analyze_on_empty_db_errors_friendly(clean_db, monkeypatch):
    from ccint import cli
    from ccint.config import settings
    monkeypatch.setattr(settings, "db_name", "ccint_test")
    res = CliRunner().invoke(cli.app, ["analyze", "--as-of", "2026-09-20"])
    assert res.exit_code == 1, "空库应友好退出而非崩溃"
    assert "collect backfill" in res.output
    assert "Traceback" not in res.output
