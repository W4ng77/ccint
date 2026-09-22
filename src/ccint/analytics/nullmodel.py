"""零模型：候选 trend 与噪声的区分（v1 Phase 2）。

为什么需要这一层
----------------
v0 的 detect_trends 只回答「哪个 topic 变化最大」，不回答「这个变化是否
超出随机波动」。在 n≈150 relevant/窗口、单 topic 十几条的量级上，
±50% 的波动是**常态**而非信号 —— 直接把 rel_growth 写进报告等于制造虚假信心。

TREND_AGENT.md §4 要求用 z-score 解决这件事。实测否决：
    真实语料 max|z| ∈ [0.2, 0.9]，而纯 Poisson 噪声的 |z| 中位数就有 1.4~1.7、
    95 分位 3.0~4.0。也就是说 z-score 在这个数据量下**反信息** ——
    它给噪声打的分比给真实信号还高。原因是 Poisson 方差假设不成立：
    社交语料是 over-dispersed 的（一个作者连发 8 条、一条新闻被集中转发）。

因此改用 **permutation test**：不假设任何分布，直接从数据本身构造零分布。

两种零模型
----------
post-level  （宽松）：在 14 天内随机重排每条 post 的窗口归属。
    零假设 = topic 与时间无关，且每条 post 独立。
    ⚠ 忽略作者聚集 → p 值偏小（偏向宣称显著）。
    ⚠ 它条件于 n_cur / n_prev，因此检验的是**构成**变化而非**总量**变化。
      语料只有一个 topic 时它恒返回 p=1 —— 这是定义使然，不是 bug。
      总量的升降更可能来自采集侧（run 失败、token 过期、平台限流），
      应由报告的「数据健康」段落回答，而不是由 topic 趋势检验回答。

author-shift（保守，默认）：对每个作者，把其**全部** post 在 14 天跨度上
    整体循环平移 δ ∈ {0..13} 天。
    保留：每个作者的发帖数、topic 组成、内部 burst 结构（帖间间隔）。
    破坏：作者活动与日历时间的对齐。
    零假设 = 「某 topic 的上升只是几个高产作者恰好扎堆」。
    这是 cluster-robust 的版本，也是我们真正想排除的解释。

为什么平移取整天
----------------
窗口跨度固定为 2*window_days。当 window_days=7 时总跨度 14 天 = 整 2 周，
两个窗口各含每种星期几恰好一次 —— 周末效应（实测周末 volume ≈ 工作日 45%）
在 delta 中天然抵消。整天平移保持这一性质；任意秒级平移会破坏它。

多重比较
--------
我们在 ~11 个 topic 里挑最大的那个来讲故事，这是典型的 look-elsewhere effect。
因此每次置换额外记录**全 topic 的最大统计量**，用它构造 max-statistic 零分布，
得到 family-wise 校正后的 p_fwer。只有 p_fwer 才能用来下「这个 topic 显著」的结论。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

log = logging.getLogger(__name__)

NULL_METHOD = "permutation_v1"

FETCH_SQL = """
WITH bounds AS (
    SELECT %(as_of)s::timestamptz                                              AS as_of,
           %(as_of)s::timestamptz - make_interval(days => 2 * %(window_days)s) AS prev_start
)
SELECT p.author_id,
       p.published_at,
       COALESCE(l.topic_key, 'other') AS topic_key
FROM posts p
JOIN post_labels l ON l.post_id = p.post_id
CROSS JOIN bounds b
WHERE l.label_version = %(label_version)s
  AND l.is_relevant
  AND (%(include_low_information)s OR NOT l.is_low_information)
  AND p.published_at >= b.prev_start
  AND p.published_at <  b.as_of
  AND (%(exclude_authors)s::text[] IS NULL
       OR NOT (p.author_id = ANY(%(exclude_authors)s::text[])))
"""


@dataclass(frozen=True)
class TopicNull:
    """单个 topic 的零模型检验结果。"""
    topic_key: str
    cur_posts: int
    prev_posts: int
    abs_delta: int
    observed: float          # 被检验的统计量（默认 = abs_delta）
    null_mean: float
    null_sd: float
    null_p95_abs: float      # 零分布 |统计量| 的 95 分位 —— 「噪声有多大」的直观刻度
    p_raw: float             # 双侧，未校正
    p_fwer: float            # max-statistic 校正后
    n_iter: int

    @property
    def verdict(self) -> str:
        if self.p_fwer <= 0.05:
            return "signal"
        if self.p_raw <= 0.05:
            return "weak"      # 单看自己显著，但扛不住多重比较
        return "noise"


@dataclass
class NullResult:
    method: str
    unit: str
    n_iter: int
    seed: int
    as_of: datetime
    window_days: int
    label_version: str
    n_cur: int
    n_prev: int
    n_authors: int
    topics: list[TopicNull] = field(default_factory=list)
    max_null_p95: float = 0.0   # 零分布下「最大 topic delta」的 95 分位

    def by_topic(self) -> dict[str, TopicNull]:
        return {t.topic_key: t for t in self.topics}


def fetch_records(conn, *, label_version: str, as_of: datetime,
                  window_days: int = 7,
                  include_low_information: bool = True,
                  exclude_authors: set[str] | None = None) -> list[dict]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be tz-aware (HANDOFF §1: 时间一律 UTC)")
    return conn.execute(FETCH_SQL, {
        "as_of": as_of, "window_days": window_days,
        "label_version": label_version,
        "include_low_information": include_low_information,
        "exclude_authors": sorted(exclude_authors) if exclude_authors else None,
    }).fetchall()


def permutation_test(
    records,
    *,
    as_of: datetime,
    window_days: int = 7,
    label_version: str = "rules_v2",
    unit: str = "author",
    n_iter: int = 2000,
    seed: int = 20260921,
) -> NullResult:
    """对每个 topic 检验 abs_delta 是否超出零分布。

    unit="author" → 作者整体循环平移（默认，cluster-robust）
    unit="post"   → 逐 post 重排窗口归属（宽松对照）
    """
    if unit not in ("author", "post"):
        raise ValueError(f"unit must be 'author' or 'post', got {unit!r}")

    span = timedelta(days=2 * window_days)
    prev_start = as_of - span
    cur_start = as_of - timedelta(days=window_days)

    if not records:
        return NullResult(NULL_METHOD, unit, n_iter, seed, as_of, window_days,
                          label_version, 0, 0, 0)

    authors = np.array([r["author_id"] for r in records])
    topics = np.array([r["topic_key"] for r in records])
    # 以「距 prev_start 的天数（浮点）」表示时间，循环平移只需对 2*window_days 取模
    offs = np.array([(r["published_at"] - prev_start).total_seconds() / 86400.0
                     for r in records])

    uniq_topics, topic_idx = np.unique(topics, return_inverse=True)
    uniq_authors, author_idx = np.unique(authors, return_inverse=True)
    n_topics, n_authors = len(uniq_topics), len(uniq_authors)
    total_days = 2 * window_days

    def deltas_from_offsets(o: np.ndarray) -> np.ndarray:
        """给定每条 post 的时间偏移，算出每个 topic 的 (cur - prev)。"""
        is_cur = o >= window_days
        cur = np.bincount(topic_idx[is_cur], minlength=n_topics)
        prev = np.bincount(topic_idx[~is_cur], minlength=n_topics)
        return (cur - prev).astype(np.float64)

    observed_cur = np.bincount(topic_idx[offs >= window_days], minlength=n_topics)
    observed_prev = np.bincount(topic_idx[offs < window_days], minlength=n_topics)
    observed = (observed_cur - observed_prev).astype(np.float64)

    rng = np.random.default_rng(seed)
    null = np.empty((n_iter, n_topics), dtype=np.float64)

    if unit == "post":
        # 保持 n_cur / n_prev 不变，只重排归属
        n_cur = int((offs >= window_days).sum())
        n = len(offs)
        for i in range(n_iter):
            perm = rng.permutation(n)
            is_cur = np.zeros(n, dtype=bool)
            is_cur[perm[:n_cur]] = True
            cur = np.bincount(topic_idx[is_cur], minlength=n_topics)
            prev = np.bincount(topic_idx[~is_cur], minlength=n_topics)
            null[i] = cur - prev
    else:
        # 每个作者整体循环平移整数天
        for i in range(n_iter):
            shift = rng.integers(0, total_days, size=n_authors).astype(np.float64)
            null[i] = deltas_from_offsets((offs + shift[author_idx]) % total_days)

    # max-statistic：每次置换取全 topic 的最大 |delta|，用于 FWER 校正
    max_abs = np.abs(null).max(axis=1)
    max_null_p95 = float(np.quantile(max_abs, 0.95))

    out: list[TopicNull] = []
    for j, t in enumerate(uniq_topics):
        col = null[:, j]
        obs = float(observed[j])
        # 双侧 p，(b+1)/(m+1) 修正：永不报告 p=0（m 次置换给不出比 1/(m+1) 更小的证据）
        p_raw = (np.sum(np.abs(col) >= abs(obs)) + 1) / (n_iter + 1)
        p_fwer = (np.sum(max_abs >= abs(obs)) + 1) / (n_iter + 1)
        out.append(TopicNull(
            topic_key=str(t),
            cur_posts=int(observed_cur[j]),
            prev_posts=int(observed_prev[j]),
            abs_delta=int(obs),
            observed=obs,
            null_mean=float(col.mean()),
            null_sd=float(col.std(ddof=1)),
            null_p95_abs=float(np.quantile(np.abs(col), 0.95)),
            p_raw=float(p_raw),
            p_fwer=float(p_fwer),
            n_iter=n_iter,
        ))

    out.sort(key=lambda t: (-abs(t.observed), t.topic_key))
    res = NullResult(
        method=NULL_METHOD, unit=unit, n_iter=n_iter, seed=seed, as_of=as_of,
        window_days=window_days, label_version=label_version,
        n_cur=int(observed_cur.sum()), n_prev=int(observed_prev.sum()),
        n_authors=n_authors, topics=out, max_null_p95=max_null_p95,
    )
    log.info("permutation_test unit=%s n_iter=%d: %d topic(s), "
             "max-null p95=%.1f, signals=%d",
             unit, n_iter, len(out), max_null_p95,
             sum(1 for t in out if t.verdict == "signal"))
    return res
