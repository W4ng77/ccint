"""rules_v2 单测：Phase 0 修的每个假阳性源都要有回归断言。

[MUST] HANDOFF §1.2：v1 与 v2 并存，旧 version 保留用于并排比较。
"""
import pytest

from ccint.labeling import get_labeler

V1 = get_labeler("rules_v1")
V2 = get_labeler("rules_v2")


# -- 假阳性修复（实测来源见 canada.yaml v2 注释）--------------------------

@pytest.mark.parametrize("text,note", [
    ("GTA 6 ISO fake leak full of malware", "GTA=Grand Theft Auto，非 Greater Toronto Area"),
    ("Fake GTA VI ISO circulates, 113GB hiding a virus", "GTA VI"),
    ("New post from #Cyberleek : Gta 6: Plane Video #Ransomware", "游戏泄露站"),
    ("Mastering GRC: Governance, Risk and Compliance for cybersecurity",
     "GRC=治理风险合规，非 Gendarmerie royale du Canada"),
    ("CRA Compliance Gap Assessment: NIS2 and GDPR for European firms",
     "CRA=EU Cyber Resilience Act，非 Canada Revenue Agency"),
    ("ai.csis.org director argues for cybersecurity expectations on frontier labs",
     "CSIS=美国智库，非加拿大安全情报局"),
])
def test_v2_removes_false_positives(text, note):
    assert V2.label(text).is_relevant is False, note


def test_gta_was_the_single_largest_fp_source():
    """裸 GTA 曾使 153/846 (18.1%) 的 relevant 成为假阳性。"""
    t = "GTA 6 ISO fake leak full of malware"
    assert V1.label(t).is_relevant is True, "v1 的已知缺陷（保留为对照基准）"
    assert V2.label(t).is_relevant is False


# -- 召回修复 -------------------------------------------------------------

@pytest.mark.parametrize("text,note", [
    ("dark web service selling US and Canadian drivers licenses", "dark web"),
    ("FBI investigating licenses leaked on a Russian cybercrime forum, Canada affected",
     "cybercrime + leaked on forum"),
    ("New breach: Golf Canada had 569k unique email addresses", "HIBP 式通报"),
    ("Class Action in Alberta over the breach of their personal data",
     "breach of their personal data"),
    ("RCMP monitoring reports of massive North American drivers licence hack",
     "裸 hack 名词形式 + RCMP"),
])
def test_v2_recovers_missed_posts(text, note):
    assert V2.label(text).is_relevant is True, note


@pytest.mark.parametrize("text", [
    "This productivity life hack changed my morning routine",
    "Growth hack: post at 9am in Toronto for more engagement",
])
def test_bare_hack_does_not_overmatch(text):
    """裸 hacks? 会命中 life hack / growth hack，故只用「<资产> hack」搭配。"""
    assert V2.label(text).is_cyber is False


# -- 缩写佐证机制 ---------------------------------------------------------

def test_ambiguous_abbrev_needs_corroboration():
    """歧义缩写的另一含义**本身就是网安术语**，requires_cyber_context 挡不住，
    必须要求加拿大侧另有信号。"""
    alone = V2.label("Our GRC program covers phishing and ransomware readiness")
    assert alone.is_canada is False
    assert "_uncorroborated" in alone.matched_terms["canada"], "未采信的缩写必须留痕"

    corroborated = V2.label(
        "CRA warns Canadians in Ontario of a phishing scam targeting taxpayers")
    assert corroborated.is_canada is True
    assert corroborated.is_relevant is True


def test_rcmp_is_strong_enough_alone():
    r = V2.label("RCMP investigating a ransomware attack on a hospital network")
    assert r.is_relevant is True


# -- 版本并存 -------------------------------------------------------------

def test_versions_are_independent():
    assert V1.label_version == "rules_v1" and V2.label_version == "rules_v2"
    assert V1.lexicon_dir != V2.lexicon_dir
    assert V1.lex["versions"]["canada"] == "canada_v1"
    assert V2.lex["versions"]["canada"] == "canada_v2"


def test_unknown_version_rejected():
    with pytest.raises(ValueError, match="unknown label_version"):
        get_labeler("rules_v99")


# -- YAML 陷阱回归 --------------------------------------------------------

def test_lexicon_entries_are_all_strings():
    """带冒号的正则若忘加引号，YAML 会静默解析成 mapping，
    报错 'unhashable type: dict' 完全指不到根因。"""
    from ccint.labelers.rules_v1 import LEXICON_DIRS, _load
    for version, d in LEXICON_DIRS.items():
        for f in ("cyber.yaml", "canada.yaml", "topics.yaml"):
            _load(f, d)   # 内部断言全部为 str，否则抛 TypeError


def test_compile_handles_punctuation_suffix():
    """\\b 是零宽断言：以标点结尾的词条若追加 \\b 会永不命中。"""
    from ccint.labelers.rules_v1 import _compile
    assert _compile(r"(?:new |another )?breach:").search("New breach: Golf Canada")
