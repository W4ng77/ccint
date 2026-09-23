"""Agent 工具层与编排的测试。

重点不是「函数能跑」，而是三件事：
  1. 工具层不含分析逻辑 —— 它必须转发到既有模块，否则同一个统计会出现两份实现；
  2. 口径随返回体一起走 —— agent 的回答要能追到源/窗口/工具结果；
  3. 护栏在 prompt 里是硬性的 —— 四条既有测量结论不得被绕过。
"""
import inspect

import pytest

from ccint.agent import assistant, toolset


# ---------------- 工具契约 ----------------
def test_registry_matches_declared_schemas():
    """schema 与实现必须一一对应，否则 agent 会调用不存在的工具。"""
    declared = {t["function"]["name"] for t in assistant._tool_schemas()}
    assert declared == set(toolset.REGISTRY)


def test_schema_parameters_match_function_signatures():
    """每个 schema 参数都必须是实现函数真实接受的参数。"""
    for t in assistant._tool_schemas():
        name = t["function"]["name"]
        params = set(t["function"]["parameters"]["properties"])
        sig = set(inspect.signature(toolset.REGISTRY[name]).parameters)
        assert params <= sig, f"{name}: schema 有实现不接受的参数 {params - sig}"


def test_tools_delegate_rather_than_reimplement():
    """[MUST] 工具层不得自带分析实现。

    判据：actor composition 必须走既有的 broadcast 判据，时间序列必须用它算出
    的 feed 名单。若这里出现第二套阈值，两处会各自演化而没人发现。
    """
    src = inspect.getsource(toolset)
    assert "detect_broadcast_authors" in src
    assert "from ..portal import queries" in src
    assert "from .tools import TopicTools" in src
    # 不得在工具层里重新定义广播阈值
    assert "zero_rate =" not in src and "min_posts =" not in src


# ---------------- 口径必须随数字一起返回 ----------------
def test_latest_topics_carries_window_and_caveat(seeded_db):
    r = toolset.get_latest_topics(days=14, top_k=3)
    assert r["window_days"] == 14 and r["window"]
    assert r["label_version"] and r["topic_version"]
    assert "noise" in r["caveat"]


def test_activity_labels_are_neutral_not_trend_claims(seeded_db):
    """[MUST] 不得把涨幅叫 trend —— 置换检验已证明当前样本量上不可检出。"""
    r = toolset.get_latest_topics(days=14, top_k=10)
    allowed = {"most_discussed", "increasing_activity", "decreasing_activity",
               "newly_observed", "unchanged"}
    for t in r["topics"]:
        parts = {p.strip() for p in t["activity_label"].replace(";", " ").split()}
        assert parts <= allowed, t["activity_label"]
        assert "trend" not in t["activity_label"].lower()


def test_timeseries_gives_three_series_not_just_volume(seeded_db):
    """[MUST] 原始发帖量会被自动 feed 主导，单条曲线会把转载读成关注。"""
    r = toolset.get_topic_timeseries(days=14)
    assert r["series"], "窗口内应有数据"
    row = r["series"][0]
    assert {"posts", "non_broadcast_posts", "authors"} <= set(row)


def test_actor_composition_reports_feed_share_and_method(seeded_db):
    r = toolset.get_actor_composition(days=14)
    assert 0.0 <= r["broadcast_post_share"] <= 1.0
    assert "behavioral_v1" in r["method"]
    assert r["reading"]


def test_cve_severity_and_exploitation_stay_separate(seeded_db):
    """[MUST] CVSS 与 KEV 不得合成风险分。"""
    r = toolset.get_related_cves(days=30)
    assert "not" in r["note"] and "combined" in r["note"]
    # 种子数据里的 CVE 还没抽取，cves 可能为空；契约在 note 与字段上，不在行数上。
    for c in r["cves"]:
        assert "known_exploited" in c
        assert not any("risk_score" in k or "combined" in k for k in c)


# ---------------- 采集边界不得被 agent 绕过 ----------------
def test_collect_rejects_sources_outside_config():
    """[MUST] agent 不能采集任意站点 —— 那会让采集边界脱离版本控制。"""
    r = toolset.collect_sources(["https://example.org/feed.xml"])
    assert "error" in r and "未配置" in r["error"]


def test_list_sources_exposes_status_fields_the_demo_needs(seeded_db):
    r = toolset.list_sources()
    need = {"name", "type", "enabled", "posts", "relevant",
            "last_successful_collection", "collection_status"}
    assert r["sources"] and need <= set(r["sources"][0])


# ---------------- 护栏 ----------------
@pytest.mark.parametrize("phrase", [
    "Post volume is not public attention",
    "does not represent Canadian public opinion",
    "Never call it a trend",
    "Every statement must be traceable",
    "never merge them into one risk score",
])
def test_system_prompt_states_each_guardrail(phrase):
    # 归一化空白：prompt 里这些句子会跨硬换行，直接子串匹配会假阴性。
    flat = " ".join(assistant.SYSTEM.split())
    assert phrase in flat


def test_agent_refuses_to_answer_from_memory_when_backend_is_down():
    """后端不可用时必须明说，不得凭常识作答。"""
    a = assistant.ask("what is happening?", base_url="http://127.0.0.1:1/v1")
    assert a.error and "rather than one from memory" in a.answer
