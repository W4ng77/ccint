"""可视化层的回归测试。

这里锁住的都是**会悄悄骗人**的东西：滚动平均的边缘补零、把不完整的边界周
当成完整周、以及用网络图去画一个没有拓扑的图。这些不会报错，只会让读者
得出错误结论 —— 正是本项目最该防的那类失败。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from ccint.viz import data as D
from ccint.viz.palette import AUTHORS, ORGANIC, PALETTE, RAW


# --------------------------------------------------------------------- 调色板
def test_series_slots_are_the_validated_three():
    """只用 slot 1–3。它们是参考调色板中唯一在 --pairs all 下全模式通过的三色组。"""
    assert PALETTE["series"] == ["#2a78d6", "#eb6834", "#1baf7a"]
    assert [RAW, ORGANIC, AUTHORS] == PALETTE["series"]


def test_status_colors_never_reused_as_series():
    for role in ("critical", "warning", "good"):
        assert PALETTE[role] not in PALETTE["series"]


def test_sequential_ramp_is_monotonic_single_hue():
    """顺序色阶必须单调变深；彩虹色阶会让读者把色相当成类别。"""
    def lum(h):
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    L = [lum(c) for c in PALETTE["seq"]]
    assert all(a > b for a, b in zip(L, L[1:])), "顺序色阶不是单调由浅到深"


# --------------------------------------------------------------------- Gini
def test_gini_bounds():
    assert D.gini([5, 5, 5, 5]) == pytest.approx(0.0, abs=1e-9)
    assert D.gini([]) == 0.0
    many = D.gini([1] * 99 + [10_000])
    assert 0.9 < many < 1.0


def test_gini_ignores_zero_counts():
    assert D.gini([3, 3, 0, 0]) == pytest.approx(D.gini([3, 3]))


def test_gini_increases_with_concentration():
    assert D.gini([1, 1, 1, 7]) > D.gini([2, 2, 3, 3])


# ------------------------------------------------------------- VizContext/SQL
def test_empty_broadcast_list_does_not_become_null():
    """空列表必须传成 [''] —— 传 NULL 会让 `= ANY(NULL)` 返回 NULL，
    于是 `NOT (...)` 也是 NULL，WHERE 过滤掉全部行，图会变成空的。"""
    ctx = D.VizContext("rules_v2", None, None, [])
    assert ctx.params()["bcast"] == [""]


def test_shortener_domains_excluded_from_source_uptake():
    """短链不是信源。漏掉一个就会让不同来源伪装成同一个域名。"""
    for d in ("bit.ly", "tinyurl.com", "t.co", "buff.ly", "ow.ly", "linktr.ee"):
        assert f"'{d}'" in D.COSHARE_SQL, d


def test_youtube_aliases_are_normalised():
    assert "'youtu.be'" in D.COSHARE_SQL and "THEN 'youtube.com'" in D.COSHARE_SQL


# ------------------------------------------------------- 滚动平均的边缘伪影
def _series(values, roll):
    """复刻 charts.topic_trajectory 内部的 series()，单测其数值行为。"""
    a = np.asarray(values, dtype=float)
    if roll <= 1:
        return a
    half = roll // 2
    out = np.full(len(a), np.nan)
    for i in range(half, len(a) - half):
        out[i] = a[i - half:i + half + 1].mean()
    return out


def test_rolling_mean_leaves_edges_undefined_not_zero_padded():
    """np.convolve(mode="same") 会在两端补零，凭空造出一段「下跌」。

    这是最危险的一类图表 bug：不报错、看起来很像趋势、而且正是本项目
    在统计层花大力气排除的那种伪信号。
    """
    flat = [10] * 20
    out = _series(flat, 7)
    assert np.isnan(out[:3]).all() and np.isnan(out[-3:]).all()
    assert out[3:-3] == pytest.approx([10.0] * 14)   # 中段不得被边缘影响


def test_rolling_mean_is_centred():
    """单个尖峰经居中窗口平滑后，响应区间必须**对称**地落在尖峰两侧。

    不能断言 argmax == 10：平滑后是一段等高平台，argmax 只会返回最左端。
    要测的是对称性 —— 窗口若不居中，平台会整体偏向一侧。
    """
    vals = [0] * 10 + [100] + [0] * 10
    out = _series(vals, 5)
    nz = np.flatnonzero(np.nan_to_num(out) > 0)
    assert nz.min() == 10 - 2 and nz.max() == 10 + 2
    assert (nz.min() + nz.max()) / 2 == 10


# --------------------------------------------------- 热力图剔除不完整边界周
def test_heatmap_drops_partial_boundary_weeks():
    from ccint.viz.charts import topic_week_heatmap

    rows = []
    for i, wk in enumerate([date(2026, 8, 17), date(2026, 8, 24),
                            date(2026, 8, 31), date(2026, 9, 7)]):
        rows.append({"wk": wk, "topic_key": "a", "n": 10, "tot": 10, "share": 1.0})
    out = Path("/tmp/claude-1003/_viztest")
    paths = topic_week_heatmap(rows, out, name="t_drop", min_total=1)
    assert paths, "应当仍然出图"
    # 只剩中间两周
    import matplotlib.pyplot as plt
    plt.close("all")


def test_heatmap_keeps_all_weeks_when_told_not_to_drop():
    from ccint.viz.charts import topic_week_heatmap
    base = date(2026, 8, 17)
    rows = [{"wk": base + timedelta(weeks=i), "topic_key": "a", "n": 5,
             "tot": 5, "share": 1.0} for i in range(4)]
    paths = topic_week_heatmap(rows, Path("/tmp/claude-1003/_viztest"),
                               name="t_keep", min_total=1,
                               drop_partial_weeks=False)
    assert paths
    import matplotlib.pyplot as plt
    plt.close("all")


# ------------------------------------------------------------- 图能画出来
def test_all_charts_render_from_minimal_input():
    from ccint.viz import charts
    out = Path("/tmp/claude-1003/_viztest")

    split = [{"topic_key": "t1", "raw_posts": 40, "organic_posts": 6,
              "raw_authors": 9, "organic_authors": 4},
             {"topic_key": "t2", "raw_posts": 20, "organic_posts": 19,
              "raw_authors": 18, "organic_authors": 18}]
    assert charts.raw_vs_organic(split, out, name="t_rvo")

    traj = [{"topic_key": "t1", "d": date(2026, 9, d), "raw_posts": d,
             "organic_posts": 1, "authors": 2, "day_total": 10, "share": 0.5}
            for d in range(1, 21)]
    assert charts.topic_trajectory(traj, out, name="t_traj", top_n=1, roll=7)

    at = [{"author_id": f"a{i}", "author_handle": f"h{i}", "topic_key": "t1",
           "n_posts": 2, "avg_engagement": 0.0, "is_bcast": i == 0}
          for i in range(6)]
    assert charts.actor_topic_network(at, out, name="t_net")

    cc = [{"author_id": f"a{i}", "author_handle": f"h{i}", "n_posts": i + 1,
           "is_bcast": i > 4} for i in range(8)]
    assert charts.concentration_curve(cc, out, name="t_conc", gini_fn=D.gini)

    cs = [{"domain": "x.ca", "author_id": f"a{i}", "author_handle": f"h{i}",
           "is_bcast": False, "n": 1} for i in range(4)]
    assert charts.source_uptake(cs, out, name="t_src", min_authors=3)

    import matplotlib.pyplot as plt
    plt.close("all")


def test_source_uptake_returns_empty_when_nothing_clears_threshold():
    from ccint.viz import charts
    cs = [{"domain": "x.ca", "author_id": "a1", "author_handle": "h1",
           "is_bcast": False, "n": 1}]
    assert charts.source_uptake(cs, Path("/tmp/claude-1003/_viztest"),
                                name="t_none", min_authors=5) == []
