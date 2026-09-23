"""多源采集、CVE 抽取与门户数据层的回归测试。"""
import pytest

from ccint.collectors import sources
from ccint.osint.cve import CVE_RE, extract


# ---------------- CVE 抽取:确定性,不依赖模型 ----------------
def test_extract_normalises_case_and_dedupes():
    t = "cve-2025-25249 then CVE-2025-25249 again, plus CVE-2026-67279."
    assert extract(t) == [("CVE-2025-25249", 0), ("CVE-2026-67279", t.index("CVE-2026-67279"))]


def test_extract_rejects_malformed_ids():
    """序号少于 4 位不是合法 CVE；不要因为看起来像就收进来。"""
    assert extract("CVE-12-3 CVE-2025-1 CVE-20255-1234") == []


def test_extract_requires_word_boundary():
    """XCVE-2025-1234 与 CVE-2025-12345678 都不该匹配成 CVE-2025-1234。"""
    assert extract("XCVE-2025-1234") == []
    got = extract("CVE-2025-1234567")
    assert got == [("CVE-2025-1234567", 0)]


def test_offsets_point_at_the_match():
    t = "padding CVE-2026-00001"
    cid, pos = extract(t)[0]
    assert t[pos:pos + len(cid)].upper() == cid


# ---------------- 源配置:约束必须真的生效 ----------------
def test_every_source_declares_authorisation(tmp_path):
    """[MUST] 少了 authorised 必须报错。自动抓第三方站点不能靠默认放行。"""
    bad = tmp_path / "s.yaml"
    bad.write_text("version: t\nsources:\n  - key: x\n    kind: rss\n"
                   "    url: https://e.org/f.xml\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authorised"):
        sources.load(bad)


def test_duplicate_source_keys_rejected(tmp_path):
    bad = tmp_path / "s.yaml"
    bad.write_text("version: t\nsources:\n"
                   "  - {key: x, kind: rss, url: 'https://a/f', authorised: ok}\n"
                   "  - {key: x, kind: rss, url: 'https://b/f', authorised: ok}\n",
                   encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        sources.load(bad)


def test_shipped_sources_config_is_valid():
    version, specs = sources.load()
    assert version and specs
    assert all(s.authorised for s in specs)


# ---------------- 门户:口径必须随数字一起返回 ----------------
def test_portal_payloads_carry_their_caveats():
    """界面上的裸数字会被当成无条件事实，所以口径必须在返回体里。"""
    from ccint.portal import queries as q
    assert "label_version" in q.overview()
    r = q.recent_topics(days=14)
    assert r["caveat"] and "noise" in r["caveat"]
    c = q.top_cves(days=14, limit=1)
    assert "not combined" in c["note"]


def test_portal_is_read_only():
    """[MUST] 门户不得有任何写路径 —— 从界面触发的采集会在 collection_runs 里
    产生没有对应计划的运行，而那张表的价值就是让「这天量少」可归因。"""
    from ccint.portal.app import app
    methods = {m for r in app.routes for m in getattr(r, "methods", set())}
    assert methods <= {"GET", "HEAD"}, f"门户出现了写方法: {methods}"
