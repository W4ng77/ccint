"""作者画像的回归测试（二维 actor type）。

锁住的是三件会悄悄出错的事：
1. 「样本量不足」被当成「判定为无关」而剔除 —— 会系统性压低低产账号，
   而那恰好是最像有机讨论的一批；
2. 情报 feed 被当成噪声一并丢掉 —— 它是事件 ground truth；
3. 「无沉淀帖」被当成「无受众」—— 那是采集时滞，不是没人看。
"""
from __future__ import annotations

import pytest

from ccint.analytics.actor import (ENGAGED_MIN_MEAN, MIN_POSTS_FOR_PATTERN,
                                   MODES, AuthorProfile, coverage, derive_type,
                                   exclusion_set, profile_tier)


def prof(**kw):
    d = dict(author_id="a", author_handle="h", n_posts=10, n_settled=10,
             mean_engagement=0.0, zero_rate=1.0, audience="unheard",
             function="individual")
    d.update(kw)
    p = AuthorProfile(**d)
    return {"author_id": p.author_id, "n_posts": p.n_posts,
            "actor_type": p.actor_type, "tier": p.tier}


# ------------------------------------------------------ 模式类别的样本量下限
def test_pattern_categories_need_enough_posts():
    """incident_feed / promotion 描述的是反复出现的行为。

    实测：只发过 1 条的账号里 38 个被判成 incident_feed，逐条看全是
    转发驾照泄露新闻的普通人。一条帖子在定义上就确立不了模式。
    """
    for fn in ("incident_feed", "promotion"):
        assert derive_type(fn, "unheard", n_posts=1) == "unclassified"
        assert derive_type(fn, "unheard", n_posts=MIN_POSTS_FOR_PATTERN - 1) \
            == "unclassified"
        assert derive_type(fn, "unheard", n_posts=MIN_POSTS_FOR_PATTERN) \
            != "unclassified"


def test_non_pattern_categories_have_no_floor():
    """individual / news_media / org_other 单条也能判 —— 它们描述的是这条像什么。"""
    assert derive_type("individual", "engaged", n_posts=1) == "organic_attention"
    assert derive_type("news_media", "unheard", n_posts=1) == "news_syndicated"


def test_tiers():
    assert profile_tier(1) == profile_tier(2) == "single"
    assert profile_tier(3) == profile_tier(4) == "provisional"
    assert profile_tier(5) == profile_tier(99) == "established"


# ---------------------------------------------------- 两轴不得互相污染
def test_intel_feed_without_audience_is_not_noise():
    """[MUST] 情报 feed 即使无人点赞也不是噪声 —— 它报的是真实事件。

    旧的 is_broadcast 把它和会议宣传号压成同一个判断一起剔除，
    于是语料里唯一系统性的事件流也被丢掉了。
    """
    assert derive_type("incident_feed", "unheard") == "intel_feed_raw"
    assert derive_type("promotion", "unheard") == "promotion_unheard"
    assert derive_type("incident_feed", "unheard") != derive_type("promotion", "unheard")


def test_unknown_on_either_axis_yields_unclassified():
    assert derive_type("unknown", "engaged") == "unclassified"
    assert derive_type("individual", "unknown") == "unclassified"


def test_audience_threshold_is_a_stated_operational_definition():
    p = AuthorProfile("a", "h", 10, 10, ENGAGED_MIN_MEAN, 0.0, "engaged",
                      function="individual")
    assert p.actor_type == "organic_attention"


# -------------------------------------------------------------- 排除集语义
def test_unclassified_is_never_excluded():
    """[MUST]「判不了」不等于「判为无关」。

    把不确定当阳性会系统性压低低产账号的语料 —— 而低产账号正是最像
    有机讨论的那一批，压低的方向与结论方向一致，是最危险的一类偏差。
    """
    profs = [prof(author_id="u", function="unknown")]
    for mode in ("attention", "events"):
        assert exclusion_set(profs, mode) == set()


def test_attention_mode_drops_both_feeds_and_promotion():
    profs = [prof(author_id="f", function="incident_feed"),
             prof(author_id="p", function="promotion"),
             prof(author_id="i", function="individual")]
    assert exclusion_set(profs, "attention") == {"f", "p"}


def test_events_mode_keeps_intel_feeds():
    """度量「发生了哪些事件」时，情报 feed 是最好的来源，必须保留。"""
    profs = [prof(author_id="f", function="incident_feed"),
             prof(author_id="p", function="promotion")]
    assert exclusion_set(profs, "events") == {"p"}
    assert "f" not in exclusion_set(profs, "events")


def test_ecosystem_mode_is_the_complement():
    profs = [prof(author_id="p", function="promotion"),
             prof(author_id="i", function="individual"),
             prof(author_id="f", function="incident_feed")]
    excl = exclusion_set(profs, "ecosystem")
    assert "p" not in excl and {"i", "f"} <= excl


def test_all_mode_excludes_nothing():
    profs = [prof(author_id=x, function=f) for x, f in
             (("f", "incident_feed"), ("p", "promotion"), ("i", "individual"))]
    assert exclusion_set(profs, "all") == set()


def test_unknown_mode_rejected():
    with pytest.raises(ValueError, match="unknown mode"):
        exclusion_set([], "whatever")


def test_every_mode_is_documented():
    from ccint.analytics.actor import _EXCLUDE, _KEEP_ONLY
    assert set(MODES) == set(_EXCLUDE) | set(_KEEP_ONLY)


# ------------------------------------------------------------------ 覆盖率
def test_coverage_reports_unclassified_share():
    profs = [prof(author_id="a", n_posts=10, function="individual"),
             prof(author_id="b", n_posts=5, function="unknown")]
    cov = coverage(profs)
    assert cov["n_posts"] == 15
    assert cov["n_unclassified_posts"] == 5
    assert cov["classified_share"] == pytest.approx(10/15)


def test_coverage_handles_empty():
    cov = coverage([])
    assert cov["n_posts"] == 0 and cov["classified_share"] == 0.0


# ------------------------------------------- 受众轴：无沉淀帖 ≠ 无受众
def test_no_settled_posts_means_unknown_not_unheard():
    """[MUST]「还没来得及被看见」与「没人看」是两回事。

    incremental 在发帖后约 4h 就抓走，那时互动数必然是 0。
    把它判成 unheard 会把所有新采的帖子打成没受众。
    """
    p = AuthorProfile("a", "h", n_posts=3, n_settled=0, mean_engagement=None,
                      zero_rate=None, audience="unknown", function="individual")
    assert p.actor_type == "unclassified"
