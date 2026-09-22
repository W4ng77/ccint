"""作者画像：把「广播号」拆成两个正交的轴（v1 Phase 6）。

为什么要拆
----------
原先的 ``is_broadcast``（发帖多 + 跨多日 + 零互动）把两件不相干的事压成了
一个布尔，于是这两类账号被当成同一种东西剔除：

* ``ecrime.ch`` / ``ransomlook`` —— 勒索泄露站监控，**每条都在报一个真实受害者**。
  它是事件 ground truth，是情报信号。
* ``bsidesedmonton`` / ``hackfest`` —— 会议宣传，反复推自己的活动。
  它是生态指标，不是事件。

两者的共同点只有「没人点赞」。把它们一起丢掉，等于把语料里唯一系统性的
事件流也丢掉了。因此改成两个轴。

轴 1：audience（实测）
---------------------
互动量，且**只统计已沉淀的帖子**。``like_count`` 是采集瞬间的快照，
incremental 在发帖后约 4h 就抓走、backfill 平均滞后 354h ——
不加沉淀期守卫，「零互动」会把所有刚采到的帖子误判。

轴 2：function（内容判定，非行为推断）
------------------------------------
**这里刻意不做「是不是机器人」的行为推断。** 实测否决：

* 时段熵能分开已知 bot（0.67–0.74，全天候）与会议号（0.38–0.52，工作时间），
  但在 n=5 时熵的上限只有 log(5)/log(24)=0.506 —— ``ransomlook``（真 bot）
  因此只测到 0.42。本语料 285/372 的作者只发过 1 条，该特征大面积不可用。
* 域名集中度 100% 同时命中 ``ransomlook``（bot）与 ``journodale``（真人记者，
  只转自己供稿的媒体）。模板化程度同理：journodale 的模板化 0.83，比多数 bot 还高。

也就是说**从行为推断「是不是机器」在这个样本量下不可靠**。
可靠的是读内容判断**这个账号在做什么**，而这恰好也是研究上更有意义的问题。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

PROFILE_VERSION = "actor_v1"
MIN_SETTLED_LAG_H = 24

# --- 轴 1 阈值 -----------------------------------------------------------
# 「有受众」= 已沉淀帖子的平均互动 >= 1.0，即平均每帖至少有一个人点赞或转发。
# 取 1.0 不是统计导出的，是一个可陈述的操作定义：低于它，该账号在这个语料里
# 事实上没有被回应。阈值写进 evidence，换阈值可重算。
ENGAGED_MIN_MEAN = 1.0

# [MUST] incident_feed 与 promotion 是**模式**类别 —— 它们描述的是反复出现的
# 行为，一两条帖子在定义上就无法确立。实测：只发过 1 条的账号里，38 个被判成
# incident_feed，逐条看全是转发驾照泄露新闻的普通人（elainebg、matthewbennell、
# saucetweet…），只有 haveibeenpwned.com 是真 feed。
#
# 这不只是可靠性问题，也是语义问题：在本语料里只出现 1 条的账号，
# 无论它在别处是什么，**在这里都不构成一个 feed**。
MIN_POSTS_FOR_PATTERN = 3
PATTERN_FUNCTIONS = {"incident_feed", "promotion"}

# 画像可信度分层，按样本量。下游分析可据此筛选。
def profile_tier(n_posts: int) -> str:
    if n_posts >= 5:
        return "established"      # 实测此层的 function 判定全部正确
    if n_posts >= MIN_POSTS_FOR_PATTERN:
        return "provisional"
    return "single"               # 1-2 条：只能判「这条像什么」，不能判账号是什么


FUNCTIONS = {
    "incident_feed": "系统性报告外部安全事件/受害者/通报的账号；每条对应一个新事件",
    "news_media":    "新闻机构或记者，报道新闻",
    "promotion":     "推广自己的活动/产品/服务/招聘",
    "individual":    "以个人身份发表评论与讨论",
    "org_other":     "机构账号，但不以上述方式为主（政府、大学、行业组织）",
    "unknown":       "证据不足",
}

# 派生类型：两轴的组合。命名按**研究含义**，不按判定过程。
def derive_type(function: str, audience: str, n_posts: int = 99) -> str:
    # 样本量不足以确立模式时，宁可标 unclassified 也不宣称它是 feed/promotion。
    if function in PATTERN_FUNCTIONS and n_posts < MIN_POSTS_FOR_PATTERN:
        return "unclassified"
    if function == "unknown" or audience == "unknown":
        return "unclassified"
    if function == "incident_feed":
        # [MUST] 情报信号即使无人点赞也**不是噪声** —— 它是事件 ground truth。
        return "intel_feed_amplified" if audience == "engaged" else "intel_feed_raw"
    if function == "promotion":
        return "promotion_reaching" if audience == "engaged" else "promotion_unheard"
    if function == "news_media":
        return "news_with_reach" if audience == "engaged" else "news_syndicated"
    if function == "individual":
        return "organic_attention" if audience == "engaged" else "individual_unheard"
    return "org_reaching" if audience == "engaged" else "org_unheard"


# 哪些类型算「有机社会注意力」—— 趋势分析真正想度量的东西
ORGANIC_TYPES = {"organic_attention", "individual_unheard"}
# 哪些类型在度量「社会注意力」时应剔除，但**必须单独保留**用于事件覆盖
NON_ATTENTION_TYPES = {"intel_feed_raw", "intel_feed_amplified",
                       "promotion_unheard", "promotion_reaching"}


@dataclass
class AuthorProfile:
    author_id: str
    author_handle: str
    n_posts: int
    n_settled: int
    mean_engagement: float | None
    zero_rate: float | None
    audience: str
    function: str = "unknown"
    function_conf: float | None = None
    rationale: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def tier(self) -> str:
        return profile_tier(self.n_posts)

    @property
    def actor_type(self) -> str:
        return derive_type(self.function, self.audience, self.n_posts)


METRICS_SQL = """
WITH rel AS (
    SELECT p.author_id, max(p.author_handle) AS author_handle,
           p.post_id, p.like_count, p.repost_count,
           extract(epoch FROM (p.collected_at - p.published_at)) / 3600.0 AS lag_h
    FROM posts p
    JOIN post_labels l ON l.post_id = p.post_id
    WHERE l.label_version = %(label_version)s AND l.is_relevant
    GROUP BY p.author_id, p.post_id, p.like_count, p.repost_count, lag_h
)
SELECT author_id,
       max(author_handle)                                        AS author_handle,
       count(*)                                                  AS n_posts,
       count(*) FILTER (WHERE lag_h >= %(min_lag)s)              AS n_settled,
       avg(like_count + repost_count)
         FILTER (WHERE lag_h >= %(min_lag)s)                     AS mean_engagement,
       (count(*) FILTER (WHERE lag_h >= %(min_lag)s
                           AND like_count = 0 AND repost_count = 0))::float8
         / NULLIF(count(*) FILTER (WHERE lag_h >= %(min_lag)s), 0) AS zero_rate
FROM rel
GROUP BY author_id
ORDER BY n_posts DESC
"""


def measure_audience(conn, *, label_version: str,
                     min_lag_hours: int = MIN_SETTLED_LAG_H) -> list[AuthorProfile]:
    """轴 1：只用实测互动，不做任何推断。"""
    out = []
    for r in conn.execute(METRICS_SQL, {"label_version": label_version,
                                        "min_lag": min_lag_hours}).fetchall():
        me = float(r["mean_engagement"]) if r["mean_engagement"] is not None else None
        # [MUST] 没有沉淀帖子时判 unknown，不判 unheard ——
        # 「还没来得及被看见」与「没人看」是两回事。
        aud = ("unknown" if me is None
               else ("engaged" if me >= ENGAGED_MIN_MEAN else "unheard"))
        out.append(AuthorProfile(
            author_id=r["author_id"], author_handle=r["author_handle"] or "",
            n_posts=int(r["n_posts"]), n_settled=int(r["n_settled"]),
            mean_engagement=me,
            zero_rate=float(r["zero_rate"]) if r["zero_rate"] is not None else None,
            audience=aud,
            evidence={"min_lag_hours": min_lag_hours,
                      "engaged_min_mean": ENGAGED_MIN_MEAN},
        ))
    log.info("measured audience for %d authors", len(out))
    return out


# --------------------------------------------------------------------------
# 供分析层使用：不同的问题需要不同的过滤器
# --------------------------------------------------------------------------
#
# 这是二维拆分的全部回报。旧的 is_broadcast 只能回答「剔不剔」，
# 于是 ecrime.ch（事件 ground truth）和 bsidesedmonton（会议宣传）
# 被迫接受同一种处置。实际上：
#
#   问「社会有多关注这个主题」 → 情报 feed 与宣传号都要剔（它们不是注意力）
#   问「这个窗口发生了哪些事件」 → 情报 feed 是**最好的**来源，必须留；
#                                  宣传号仍要剔（它不报事件）
#   问「生态里在办什么活动」     → 反过来，只看宣传号
#
MODES = {
    "attention": "度量社会注意力：剔除情报 feed 与机构宣传",
    "events":    "度量事件覆盖：保留情报 feed，仅剔除宣传",
    "ecosystem": "度量社群生态：只保留宣传与机构账号",
    "all":       "不做任何剔除",
}

_EXCLUDE = {
    "attention": {"intel_feed_raw", "intel_feed_amplified",
                  "promotion_unheard", "promotion_reaching"},
    "events":    {"promotion_unheard", "promotion_reaching"},
    "all":       set(),
}
_KEEP_ONLY = {
    "ecosystem": {"promotion_unheard", "promotion_reaching",
                  "org_reaching", "org_unheard"},
}

LOAD_SQL = """
SELECT author_id, author_handle, n_posts, n_settled, mean_engagement,
       zero_rate, audience, function, function_conf, actor_type,
       evidence->>'tier' AS tier
FROM author_profiles WHERE profile_version = %s
"""


def load_profiles(conn, profile_version: str = PROFILE_VERSION) -> list[dict]:
    rows = [dict(r) for r in conn.execute(LOAD_SQL, (profile_version,)).fetchall()]
    if not rows:
        raise LookupError(
            f"没有 profile_version={profile_version!r} 的作者画像。"
            f"先跑 `ccint actors`。")
    return rows


def exclusion_set(profiles: list[dict], mode: str = "attention") -> set[str]:
    """返回该 mode 下应**排除**的 author_id 集合。

    [MUST] ``unclassified`` 永远不排除。它的含义是「样本量不足以判断」，
    不是「判断为无关」—— 把不确定当成阳性会系统性地压低语料量，
    而且压低的方向恰好是低产账号，即最像有机讨论的那一批。
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; 可选：{sorted(MODES)}")
    if mode in _KEEP_ONLY:
        keep = _KEEP_ONLY[mode]
        return {p["author_id"] for p in profiles if p["actor_type"] not in keep}
    drop = _EXCLUDE[mode]
    return {p["author_id"] for p in profiles if p["actor_type"] in drop}


def coverage(profiles: list[dict]) -> dict:
    """画像覆盖率。报告里必须写明有多少语料是「判不了」的。"""
    tot = sum(p["n_posts"] for p in profiles)
    unc = sum(p["n_posts"] for p in profiles if p["actor_type"] == "unclassified")
    by_tier: dict[str, int] = {}
    for p in profiles:
        by_tier[p["tier"] or "?"] = by_tier.get(p["tier"] or "?", 0) + p["n_posts"]
    return {"n_authors": len(profiles), "n_posts": tot,
            "n_unclassified_posts": unc,
            "classified_share": (tot - unc) / tot if tot else 0.0,
            "posts_by_tier": by_tier}
