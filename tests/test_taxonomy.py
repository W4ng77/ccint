"""分类法单一真值源的回归测试。

这组测试防的不是崩溃,是**静默漂移**:两份 topic 列表各自演化,谁也不报错,
而两个 label_version 之间的逐行 diff 里从此混进一个与模型能力无关的成分。
"""
import yaml

from ccint import taxonomy
from ccint.labelers import llm_v1, rules_v1


def test_llm_enum_is_taxonomy():
    assert llm_v1.BUCKET_KEYS == taxonomy.keys()


def test_rules_v2_buckets_are_taxonomy_minus_residual():
    lex = rules_v1._lexicons(rules_v1.LEXICON_DIRS["rules_v2"])
    keys = [k for k, _ in lex["topic_buckets"]]
    assert keys == [t["key"] for t in taxonomy.substantive()]
    assert taxonomy.residual() not in keys, "兜底桶不能进规则匹配序列"


def test_residual_is_last():
    assert taxonomy.keys()[-1] == taxonomy.residual()


def test_rules_v2_topic_terms_unchanged_by_t1():
    """T1 只搬家不改内容:rules_v2 的词必须和迁移前的历史词表逐条相同。

    若这条失败,说明重算 rules_v2 会产生与库里已有标签不同的结果 —— 那是 P2
    意义上的标签覆写,即使没有真的写库。
    """
    hist = yaml.safe_load(
        open("src/ccint/labelers/lexicons_v2/topics.yaml.frozen", encoding="utf-8"))
    now = {t["key"]: t["terms"] for t in taxonomy.substantive()}
    assert {b["key"]: b["terms"] for b in hist["buckets"]} == now


def test_low_information_config_preserved():
    hist = yaml.safe_load(
        open("src/ccint/labelers/lexicons_v2/topics.yaml.frozen", encoding="utf-8"))
    assert hist["low_information"] == taxonomy.low_information()


def test_validate_entities_drops_hallucinations_and_foreign_domains():
    """两条机械校验:原文里没有的实体、非加拿大顶级域的域名。"""
    from ccint.labelers.llm_v2 import validate_entities
    text = ("Cyberattaque dans l'Éducation nationale — see trustedsec.com "
            "and cyber.gc.ca and ia.cr")
    keep, drop = validate_entities(text, [
        {"type": "place", "text": "Canada"},          # 原文没有 → 幻觉
        {"type": "domain", "text": "trustedsec.com"},  # 非 .ca
        {"type": "domain", "text": "ia.cr"},           # 哥斯达黎加
        {"type": "domain", "text": "cyber.gc.ca"},     # 保留
    ])
    assert [e["text"] for e in keep] == ["cyber.gc.ca"]
    assert {e["reason"] for e in drop} == {"not_in_text", "non_ca_tld"}


def test_validate_entities_is_case_insensitive_but_requires_presence():
    from ccint.labelers.llm_v2 import validate_entities
    keep, drop = validate_entities("The RCMP said so", [
        {"type": "gov", "text": "rcmp"}])
    assert len(keep) == 1 and not drop
