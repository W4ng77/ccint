"""广播型账号识别的回归测试。

核心风险：`like_count` 是采集瞬间的快照。若零互动率不按采集时滞过滤，
刚采到的正常帖子会被误判为广播号 —— 这会把 incremental 抓的所有新帖打成 bot。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ccint.analytics.broadcast import DEFAULTS, detect_broadcast_authors

NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


class FakeConn:
    """按 broadcast.SQL 的输出契约返回行，不触碰真实数据库。"""

    def __init__(self, rows):
        self._rows = rows
        self.params = None

    def execute(self, sql, params):
        self.params = params
        return self

    def fetchall(self):
        return self._rows


def row(handle, n_posts, n_days, zero_rate, eng=0.0, n_settled=None):
    settled = n_posts if n_settled is None else n_settled
    return {"author_id": f"did:{handle}", "author_handle": handle,
            "n_posts": n_posts, "n_days": n_days, "avg_engagement": eng,
            "n_settled": settled,
            "n_settled_zero": int(round(settled * zero_rate)),
            "zero_rate": zero_rate}


def test_all_three_conditions_required():
    """三个条件缺一不可 —— 任一单独满足都不算广播号。"""
    rows = [
        row("bot", 10, 8, 0.9),            # 全中 → 命中
        row("few_posts", 3, 8, 1.0),       # 发帖太少
        row("one_day_burst", 20, 1, 1.0),  # 单日刷屏：是另一种问题，不是广播号
        row("engaged", 10, 8, 0.1, eng=6), # 有受众
    ]
    res = detect_broadcast_authors(FakeConn(rows), label_version="rules_v2")
    assert [a.author_handle for a in res.authors] == ["bot"]


def test_zero_rate_none_is_not_broadcast():
    """所有帖子都太新（无一条沉淀）→ zero_rate 为 NULL，必须判为「不知道」而非「是」。

    这是最危险的误判方向：incremental 刚抓到的帖子全都还没有互动。
    """
    rows = [row("all_fresh", 30, 10, 0.0, n_settled=0)]
    rows[0]["zero_rate"] = None
    res = detect_broadcast_authors(FakeConn(rows), label_version="rules_v2")
    assert res.authors == []


def test_min_lag_hours_is_passed_to_sql():
    """守卫必须真的传进 SQL —— 忘了传等于没有守卫。"""
    conn = FakeConn([])
    detect_broadcast_authors(conn, label_version="rules_v2", min_lag_hours=48)
    assert conn.params["min_lag_hours"] == 48
    conn2 = FakeConn([])
    detect_broadcast_authors(conn2, label_version="rules_v2")
    assert conn2.params["min_lag_hours"] == DEFAULTS["min_lag_hours"]


def test_shares_computed_over_all_authors_not_just_hits():
    rows = [row("bot1", 30, 10, 0.9), row("bot2", 10, 5, 0.8)] + [
        row(f"human{i}", 2, 2, 0.5, eng=4) for i in range(8)]
    res = detect_broadcast_authors(FakeConn(rows), label_version="rules_v2")
    assert len(res.authors) == 2
    assert res.n_total_authors == 10
    assert res.n_total_posts == 30 + 10 + 8 * 2
    assert res.n_posts == 40
    assert res.author_share == pytest.approx(0.2)
    assert res.post_share == pytest.approx(40 / 56)


def test_empty_corpus_does_not_divide_by_zero():
    res = detect_broadcast_authors(FakeConn([]), label_version="rules_v2")
    assert res.author_share == 0.0 and res.post_share == 0.0
    assert res.author_ids == set()


def test_sorted_by_volume_then_handle():
    rows = [row("b", 10, 5, 0.9), row("a", 10, 5, 0.9), row("big", 20, 5, 0.9)]
    res = detect_broadcast_authors(FakeConn(rows), label_version="rules_v2")
    assert [a.author_handle for a in res.authors] == ["big", "a", "b"]


def test_thresholds_are_inclusive_boundaries():
    """>= 而非 > —— 边界值必须命中，否则默认参数的含义会悄悄偏移一格。"""
    d = DEFAULTS
    rows = [row("edge", d["min_posts"], d["min_days"], d["zero_rate"])]
    res = detect_broadcast_authors(FakeConn(rows), label_version="rules_v2")
    assert len(res.authors) == 1


def test_exclusion_threads_into_trend_sql():
    """exclude_authors 必须真的进到 SQL 参数里。"""
    from ccint.analytics.trend import detect_trends

    captured = {}

    class C:
        def execute(self, sql, params):
            captured.update(params)
            return self

        def fetchall(self):
            return []

    detect_trends(C(), label_version="rules_v2", as_of=NOW,
                  exclude_authors={"did:b", "did:a"})
    assert captured["exclude_authors"] == ["did:a", "did:b"]   # 排序保证确定性

    detect_trends(C(), label_version="rules_v2", as_of=NOW)
    assert captured["exclude_authors"] is None

    detect_trends(C(), label_version="rules_v2", as_of=NOW, exclude_authors=set())
    assert captured["exclude_authors"] is None      # 空集 = 不剔除，不是剔除所有
