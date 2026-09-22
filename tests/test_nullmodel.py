"""零模型回归测试。

重点覆盖两件曾经把我坑过的事：
1. p 值不得为 0 —— m 次置换给不出比 1/(m+1) 更强的证据；
2. author 单位必须比 post 单位保守（零分布更宽），否则 cluster 校正是假的。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ccint.analytics.nullmodel import NULL_METHOD, permutation_test

AS_OF = datetime(2026, 9, 21, tzinfo=timezone.utc)
WINDOW = 7


def rec(author: str, day_offset: float, topic: str) -> dict:
    """day_offset 从 prev_start 起算：<7 落在 prev，>=7 落在 cur。"""
    return {"author_id": author,
            "published_at": AS_OF - timedelta(days=2 * WINDOW) + timedelta(days=day_offset),
            "topic_key": topic}


def test_as_of_must_be_tz_aware():
    from ccint.analytics.nullmodel import fetch_records

    class _C:
        def execute(self, *a, **k):  # pragma: no cover - 不该走到
            raise AssertionError("不应发起查询")

    with pytest.raises(ValueError, match="tz-aware"):
        fetch_records(_C(), label_version="rules_v2",
                      as_of=datetime(2026, 9, 21), window_days=7)


def test_empty_records_returns_empty_result():
    r = permutation_test([], as_of=AS_OF, window_days=WINDOW)
    assert r.topics == [] and r.n_cur == 0 and r.method == NULL_METHOD


def test_bad_unit_rejected():
    with pytest.raises(ValueError, match="unit must be"):
        permutation_test([rec("a", 1, "x")], as_of=AS_OF, window_days=WINDOW,
                         unit="day")


def test_observed_counts_match_window_split():
    recs = [rec("a", 1.0, "t"), rec("b", 6.9, "t"), rec("c", 7.0, "t"),
            rec("d", 13.9, "t")]
    r = permutation_test(recs, as_of=AS_OF, window_days=WINDOW, n_iter=100)
    t = r.by_topic()["t"]
    # 7.0 是 cur 的左闭边界
    assert (t.prev_posts, t.cur_posts, t.abs_delta) == (2, 2, 0)


def test_p_value_never_zero_even_for_extreme_effect():
    """效应极端到零分布无法覆盖时，p 仍须等于下界 1/(m+1)，不得为 0。"""
    recs = ([rec(f"s{i}", 7.0 + i * 0.1, "spike") for i in range(50)]
            + [rec(f"b{i}", i * 0.1, "base") for i in range(50)])
    r = permutation_test(recs, as_of=AS_OF, window_days=WINDOW,
                         unit="post", n_iter=200)
    t = r.by_topic()["spike"]
    assert t.p_raw == pytest.approx(1 / 201)
    assert t.p_raw > 0


def test_post_null_is_blind_to_single_topic_corpus():
    """post 单位条件于 n_cur/n_prev —— 只有一个 topic 时它什么都测不出（p≡1）。

    这不是 bug，是这个零模型的定义：它检验的是**构成**是否变了，
    不是**总量**是否变了。总量变化更可能是采集侧的事（见报告的数据健康段），
    不该由 topic 趋势检验来回答。author 单位不条件于总量，因此不受此限制。
    """
    recs = [rec(f"a{i}", 7.0 + i * 0.1, "only") for i in range(40)]
    t = permutation_test(recs, as_of=AS_OF, window_days=WINDOW,
                         unit="post", n_iter=200).by_topic()["only"]
    assert t.p_raw == 1.0
    assert t.null_sd == 0.0


def test_fwer_p_is_never_smaller_than_raw_p():
    """max-statistic 校正只能让 p 变大或持平，变小说明实现错了。"""
    recs = ([rec(f"a{i}", 7.5, "hot") for i in range(30)]
            + [rec(f"b{i}", 1.5, "cold") for i in range(30)]
            + [rec(f"c{i}", i % 14, "flat") for i in range(40)])
    r = permutation_test(recs, as_of=AS_OF, window_days=WINDOW, n_iter=400)
    for t in r.topics:
        assert t.p_fwer >= t.p_raw - 1e-12, t.topic_key


def test_author_null_is_wider_than_post_null():
    """每个作者连发一串同 topic 的帖（over-dispersion）。

    post 单位假装这些是独立观测 → 零分布偏窄 → p 偏小；
    author 单位把它们作为整体平移 → 零分布更宽。这是 cluster 校正的全部意义。
    """
    recs = []
    for i in range(20):                      # 20 个作者，每人 8 条同 topic
        base = (i * 0.7) % 14
        for j in range(8):
            recs.append(rec(f"burst{i}", (base + j * 0.01) % 14, "bursty"))
    post = permutation_test(recs, as_of=AS_OF, window_days=WINDOW,
                            unit="post", n_iter=800).by_topic()["bursty"]
    auth = permutation_test(recs, as_of=AS_OF, window_days=WINDOW,
                            unit="author", n_iter=800).by_topic()["bursty"]
    assert auth.null_sd > post.null_sd * 1.5
    assert auth.abs_delta == post.abs_delta      # 观测值与零模型无关


def test_author_shift_preserves_topic_composition():
    """循环平移只动时间，不动 topic —— 任何置换下 cur+prev 总数必须守恒。"""
    recs = ([rec(f"a{i}", i % 14, "x") for i in range(25)]
            + [rec(f"b{i}", i % 14, "y") for i in range(15)])
    r = permutation_test(recs, as_of=AS_OF, window_days=WINDOW,
                         unit="author", n_iter=200)
    by = r.by_topic()
    assert by["x"].cur_posts + by["x"].prev_posts == 25
    assert by["y"].cur_posts + by["y"].prev_posts == 15


def test_seed_is_deterministic():
    recs = [rec(f"a{i}", (i * 1.3) % 14, "t") for i in range(40)]
    kw = dict(as_of=AS_OF, window_days=WINDOW, n_iter=300, seed=7)
    a = permutation_test(recs, **kw).by_topic()["t"]
    b = permutation_test(recs, **kw).by_topic()["t"]
    assert (a.p_raw, a.null_sd) == (b.p_raw, b.null_sd)


def test_verdict_thresholds():
    from ccint.analytics.nullmodel import TopicNull

    def mk(p_raw, p_fwer):
        return TopicNull("t", 1, 1, 0, 0.0, 0.0, 1.0, 1.0, p_raw, p_fwer, 100)

    assert mk(0.001, 0.02).verdict == "signal"
    assert mk(0.01, 0.30).verdict == "weak"     # 自己显著但扛不住多重比较
    assert mk(0.40, 0.90).verdict == "noise"


def test_pick_prompt_switches_on_noise():
    from ccint.cli import _pick_prompt
    from ccint.models import TrendCandidate

    cand = TrendCandidate(topic_key="t", cur_posts=10, prev_posts=5,
                          cur_authors=9, prev_authors=5, abs_delta=5,
                          rel_growth=1.0, cur_share=0.1, prev_share=0.05,
                          share_delta=0.05)
    recs = [rec(f"a{i}", (i * 1.7) % 14, "t") for i in range(30)]
    noisy = permutation_test(recs, as_of=AS_OF, window_days=WINDOW, n_iter=200)
    assert _pick_prompt(noisy, cand) == "analyst_v2"
    assert _pick_prompt(None, cand) != "analyst_v2"
