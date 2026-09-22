"""llm_v1 主题分类器的回归测试。

重点锁住两件实测踩过的坑：
1. vLLM 的 guided decoding **不约束 JSON 之间的空白**，4B 模型写完长 rationale
   后会陷入 "\\r\\n" 死循环直到耗尽 max_tokens，对象永远收不了尾。
2. 相关性三元组必须原样继承 base version —— llm_v1 只许动 topic_key，
   否则 rules_v2 与 llm_v1 的差异无法归因。
"""
from __future__ import annotations

import json

import pytest

from ccint.labelers.llm_v1 import (BUCKET_KEYS, OUTPUT_SCHEMA, SYSTEM_PROMPT,
                                   TopicVerdict, parse_verdict_json, to_label)


# ------------------------------------------------------------------ 分类体系
def test_bucket_order_comes_from_taxonomy():
    """[MUST] 顺序必须与分类法真值源一致。

    两个 version 若用不同的优先级裁决多主题帖,它们的差异里就混进了排序口径的
    差异,归因立刻失效。T1 之前这里断言的是「两份列表彼此相等」;现在只有一份
    列表,断言变成「消费者没有自带副本」。
    """
    from ccint import taxonomy
    assert BUCKET_KEYS == taxonomy.keys()
    assert BUCKET_KEYS[-1] == taxonomy.residual()


def test_schema_puts_free_text_last():
    """自由文本必须排在枚举/布尔之后 —— 见模块 docstring 的死循环问题。"""
    props = list(OUTPUT_SCHEMA["properties"])
    assert props[-1] == "rationale"
    assert props.index("topic_key") < props.index("rationale")
    assert OUTPUT_SCHEMA["properties"]["rationale"].get("maxLength")


def test_schema_enum_is_closed():
    assert OUTPUT_SCHEMA["properties"]["topic_key"]["enum"] == BUCKET_KEYS
    assert OUTPUT_SCHEMA["additionalProperties"] is False


def test_prompt_states_the_substance_rule():
    """「不要求关键词出现」是这次换机制的全部理由，prompt 里不能丢。"""
    flat = " ".join(SYSTEM_PROMPT.split())      # prompt 有硬换行，先归一空白
    assert "does not have to use a technical term" in flat
    assert "Judge the substance, not the vocabulary" in flat
    assert "priority" in flat.lower()


def test_policy_regulation_has_explicit_negative_guard():
    """实测：不加反向约束时它会变成「凡涉及政府/规则」的垃圾桶（克制 79%）。"""
    desc = dict((k, d) for k, d in
                __import__("ccint.labelers.llm_v1", fromlist=["BUCKETS"]).BUCKETS)
    p = desc["policy_regulation"]
    assert "NOT this bucket" in p
    for excluded in ("alliance", "procurement", "funding", "sovereignty"):
        assert excluded in p.lower(), excluded


# -------------------------------------------------------- 残缺 JSON 的抢救
def test_parses_normal_json():
    d = parse_verdict_json('{"topic_key":"data_breach","confidence":"high",'
                           '"off_topic":false,"rationale":"records exposed"}')
    assert d["topic_key"] == "data_breach" and d["off_topic"] is False


def test_recovers_from_whitespace_loop_truncation():
    """真实故障形态：字段都写完了，尾部陷入 \\r\\n 死循环，对象没有闭合。"""
    broken = ('{\n  "topic_key": "policy_regulation",\n  "confidence": "medium",\n'
              '  "off_topic": false,\n  "rationale": "Bill C-26 amendment"\n'
              + '  \r\n' * 200)
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    d = parse_verdict_json(broken)
    assert d["topic_key"] == "policy_regulation"
    assert d["confidence"] == "medium"
    assert d["off_topic"] is False


def test_recovers_when_rationale_never_closed():
    """更狠的一种：rationale 还没写完就断了 —— 结构化字段仍应可用。"""
    broken = ('{"topic_key": "ransomware", "confidence": "high", '
              '"off_topic": true, "rationale": "the org was encrypted and')
    d = parse_verdict_json(broken)
    assert d["topic_key"] == "ransomware"
    assert d["off_topic"] is True
    assert d["rationale"] == ""          # 拿不到就留空，不猜


def test_refuses_to_guess_when_topic_missing():
    """[MUST] topic_key 抢救不出来时必须报错。

    猜一个主题会静默污染整个 label_version，而且事后无法分辨哪些是猜的。
    """
    with pytest.raises(ValueError, match="unparseable"):
        parse_verdict_json('{"confidence": "high", "off_topic": false')


# ------------------------------------------------------ 只动 topic 的约束
def _base(**kw):
    d = {"is_cyber": True, "is_canada": True, "is_relevant": True,
         "is_low_information": False}
    d.update(kw)
    return d


def test_relevance_triplet_is_inherited_verbatim():
    v = TopicVerdict("data_breach", "why", "high", False, {})
    lab = to_label(_base(is_cyber=True, is_canada=False, is_relevant=False,
                         is_low_information=True), v, model="m", seed=1)
    assert (lab.is_cyber, lab.is_canada, lab.is_relevant, lab.is_low_information) \
        == (True, False, False, True)
    assert lab.matched_terms["inherited_relevance_from"] == "rules_v2"


def test_other_is_stored_as_null_like_the_rules_labeler():
    """'other' 必须存 NULL。存字符串会在 GROUP BY 里多出一个假桶，
    与 rules_v2 的分布不可直接比较。"""
    lab = to_label(_base(), TopicVerdict("other", "", "low", False, {}),
                   model="m", seed=1)
    assert lab.topic_key is None


def test_off_topic_recorded_but_does_not_change_relevance():
    """模型说「这根本不是 Canada×cyber」只作旁证，不得改写 is_relevant ——
    相关性有独立的验证路径，混在一起就没法归因。"""
    lab = to_label(_base(is_relevant=True),
                   TopicVerdict("other", "", "low", True, {}), model="m", seed=1)
    assert lab.is_relevant is True
    assert lab.matched_terms["off_topic"] is True


def test_provenance_is_recorded_per_row():
    lab = to_label(_base(), TopicVerdict("ransomware", "r", "high", False, {}),
                   model="qwen", seed=42)
    mt = lab.matched_terms
    assert mt["method"] == "llm" and mt["model"] == "qwen" and mt["seed"] == 42
    assert lab.confidence == 0.9


def test_confidence_maps_all_enum_values():
    for c, expect in (("high", 0.9), ("medium", 0.6), ("low", 0.3)):
        lab = to_label(_base(), TopicVerdict("other", "", c, False, {}),
                       model="m", seed=1)
        assert lab.confidence == expect
