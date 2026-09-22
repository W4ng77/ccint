"""M4 Labeler 单测。20 条手写 fixture，含法语与 HANDOFF §M4 点名的误判陷阱。"""
import pytest

from ccint.labelers.rules_v1 import RulesV1Labeler, effective_tokens

L = RulesV1Labeler()

# (text, is_cyber, is_canada, topic_key_or_None(=不断言), note)
CASES = [
    # --- 真阳性：英语 ---
    ("The Canada Revenue Agency warns of a phishing scam targeting taxpayers this tax season.",
     True, True, "phishing_scam", "CRA + phishing"),
    ("LockBit ransomware gang claims attack on a Toronto hospital network.",
     True, True, "ransomware", "ransomware 优先级高于 critical_infrastructure"),
    ("Data breach at Desjardins exposed records of millions of members.",
     True, True, "data_breach", "加拿大金融实体 + breach"),
    ("The Canadian Centre for Cyber Security issued an advisory about CVE-2026-1234.",
     True, True, "vulnerability_cve", "CVE 优先于 gov_advisory（顺序即优先级）"),
    ("Rogers Communications reported a DDoS attack that took services offline in Ontario.",
     True, True, "ddos_outage", "电信 + DDoS"),
    ("RCMP investigating credential stuffing against Canadian bank accounts.",
     True, True, None, "RCMP 缩写 + cyber 上下文"),
    ("Bill C-26 passed in Ottawa; PIPEDA compliance deadlines tighten for breach reporting.",
     True, True, "policy_regulation", "立法（不含 critical infrastructure，避免与更高优先级桶重叠）"),
    ("Bill C-26 gives Ottawa new powers over critical infrastructure cybersecurity.",
     True, True, "critical_infrastructure", "优先级验证：critical_infrastructure(6) 先于 policy_regulation(10)"),
    ("Infostealer malware harvested passwords from users in Vancouver and Calgary.",
     True, True, "malware_infostealer", "infostealer"),
    ("Hydro-Québec confirmed a cyberattack on its systems.",
     True, True, None, "关键基础设施实体"),
    ("Phishing emails impersonating Canada Post are circulating again.",
     True, True, "phishing_scam", "Canada Post 冒充"),

    # --- 真阳性：法语 ---
    ("Une cyberattaque a frappé plusieurs ministères du gouvernement canadien.",
     True, True, None, "FR: cyberattaque + canadien"),
    ("Fuite de données majeure chez Desjardins au Québec.",
     True, True, "data_breach", "FR: fuite de données"),
    ("Le rançongiciel a paralysé un hôpital de Montréal.",
     True, True, "ransomware", "FR: rançongiciel"),
    ("Hameçonnage : de faux courriels de l'Agence du revenu du Canada circulent.",
     True, True, "phishing_scam", "FR: hameçonnage + ARC 全称"),

    # --- 误判陷阱（HANDOFF §M4 点名）---
    ("My Canada Goose jacket zipper broke, this is a disaster",
     False, False, None, "TRAP: Canada Goose 是品牌"),
    ("Someone hacked my Canada Goose account on the resale site",
     True, False, None, "TRAP: 有 cyber 但 Canada Goose 不构成 Canada relevance"),
    ("The CRA downgraded the issuer rating after weak earnings guidance.",
     False, False, None, "TRAP: CRA=信用评级机构，且无 cyber 上下文"),
    ("Ransomware hit a hospital in Berlin; no other details yet.",
     True, False, None, "cyber 但非加拿大"),
    ("Great weather in Toronto today, going for a walk.",
     False, True, None, "加拿大但非 cyber → 不 relevant"),

    # --- 低信息量 ---
    ("wow this is insane",
     False, False, None, "HANDOFF 点名的低信息量例子"),
]


@pytest.mark.parametrize("text,is_cyber,is_canada,topic,note", CASES)
def test_rules_v1_cases(text, is_cyber, is_canada, topic, note):
    r = L.label(text, None, {})
    assert r.is_cyber == is_cyber, f"{note}: is_cyber | hits={r.matched_terms}"
    assert r.is_canada == is_canada, f"{note}: is_canada | hits={r.matched_terms}"
    assert r.is_relevant == (is_cyber and is_canada), f"{note}: is_relevant"
    if topic is not None:
        assert r.topic_key == topic, f"{note}: topic | hits={r.matched_terms.get('topic')}"


def test_case_count():
    assert len(CASES) >= 20, "HANDOFF §M4 要求至少 20 条 fixture"


def test_french_cases_present():
    fr = [c for c in CASES if any(ch in c[0] for ch in "éèêàçô")]
    assert len(fr) >= 4, "[MUST] 必须含法语条目"


def test_low_information_flag_only_never_deletes():
    """[MUST] is_low_information 只打 flag。"""
    r = L.label("wow this is insane", None, {})
    assert r.is_low_information is True
    # 仍然产出完整 Label（未被丢弃）
    assert r.topic_key is not None


@pytest.mark.parametrize("text,expect_low", [
    ("wow this is insane", True),
    ("omg", True),
    ("🔥🔥🔥", True),
    ("RT", True),
    ("Rogers outage hits Ontario", False),
    ("LockBit claims Toronto hospital breach", False),
])
def test_low_information_detection(text, expect_low):
    assert L.label(text, None, {}).is_low_information is expect_low


def test_matched_terms_recorded():
    """[MUST] matched_terms 必须记录命中词，否则人工无法检查误判。"""
    r = L.label("CRA phishing scam in Toronto", None, {})
    assert r.matched_terms["cyber"], "cyber 命中词必须记录"
    assert r.matched_terms["canada"], "canada 命中词必须记录"
    assert "geo" in r.matched_terms["canada"]


def test_negative_guard_recorded():
    r = L.label("Canada Goose account hacked", None, {})
    assert "_guards_triggered" in r.matched_terms["canada"]


def test_topic_priority_order_is_deterministic():
    """同时命中多桶时，靠前的桶胜出。"""
    t = "Ransomware gang exploited CVE-2026-9999 to cause a DDoS outage in Ottawa"
    assert L.label(t, None, {}).topic_key == "ransomware"


def test_topic_other_fallback():
    r = L.label("Someone hacked my account in Ottawa yesterday", None, {})
    assert r.topic_key in {"other", "malware_infostealer", "fraud_financial",
                           "data_breach", "phishing_scam"}


def test_effective_tokens_strips_urls_mentions_emoji():
    assert effective_tokens("@alice https://x.com/a 🔥 breach") == ["breach"]


def test_labeler_is_pure():
    """同一输入多次调用结果一致（幂等的前提）。"""
    t = "CRA phishing scam targeting Canadians"
    a, b = L.label(t, None, {}), L.label(t, None, {})
    assert (a.is_cyber, a.is_canada, a.topic_key) == (b.is_cyber, b.is_canada, b.topic_key)
