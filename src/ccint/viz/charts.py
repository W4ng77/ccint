"""图表渲染。

约定
----
* 全部输出矢量 PDF（进 LaTeX）+ PNG（预览）。
* 宽度默认 6.5in = letterpaper 1in 页边距下的 \\textwidth。
* 分类编码只用 palette 的 slot 1–3；需要第四个序列时用小倍数，不新增颜色。
* [MUST] aqua 在浅底上对比度 <3:1 → 每条序列都直接标注，不靠颜色单独承载身份。
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates         # noqa: E402
import matplotlib.pyplot as plt          # noqa: E402
import numpy as np                        # noqa: E402
from matplotlib.lines import Line2D       # noqa: E402

from .palette import AUTHORS, ORGANIC, PALETTE, RAW, rcparams  # noqa: E402

log = logging.getLogger(__name__)
plt.rcParams.update(rcparams())

TEXTWIDTH_IN = 6.5


def _save(fig, out: Path, name: str) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in ("pdf", "png"):
        p = out / f"{name}.{ext}"
        fig.savefig(p)
        paths.append(p)
    plt.close(fig)
    log.info("wrote %s.{pdf,png}", name)
    return paths


def _note(fig, text: str, width: int = 112) -> None:
    """图下方的说明。图必须自解释 —— 读者不一定读正文。

    自己折行而不用 wrap=True：后者按 figure 宽度折，但 bbox_inches="tight"
    会把超出的文字连同画布一起撑大，结果是说明文字跑出图的右边界。
    """
    import textwrap
    wrapped = "\n".join(textwrap.wrap(" ".join(text.split()), width=width))
    fig.text(0.0, -0.02, wrapped, ha="left", va="top", fontsize=7.2,
             linespacing=1.45, color=PALETTE["text_secondary"])


# ==========================================================================
# 1. 招牌图：raw vs organic
# ==========================================================================
def raw_vs_organic(rows, out: Path, name="fig_raw_vs_organic", min_posts=4,
                   top_n=None, compact=False):
    """每个 topic 三条：全部 post / 剔除广播号后 / 独立作者数。

    这张图的全部目的是让「表面热度」与「真实参与」的落差不可忽视。
    """
    rows = [r for r in rows if r["raw_posts"] >= min_posts]
    rows = sorted(rows, key=lambda r: r["raw_posts"])
    if top_n:
        rows = rows[-top_n:]          # 已升序，取尾部 = 量最大的 N 个
    topics = [r["topic_key"] for r in rows]
    raw = np.array([r["raw_posts"] for r in rows], float)
    org = np.array([r["organic_posts"] for r in rows], float)
    aut = np.array([r["organic_authors"] for r in rows], float)

    y = np.arange(len(rows))
    h = 0.26
    gap = 0.02   # 2px surface gap between fills

    per = 0.26 if compact else 0.42
    fig, ax = plt.subplots(figsize=(TEXTWIDTH_IN, per * len(rows) + 1.15))
    ax.barh(y + (h + gap), raw, h, color=RAW, label="All posts", zorder=3)
    ax.barh(y,             org, h, color=ORGANIC, label="Non-broadcast posts", zorder=3)
    ax.barh(y - (h + gap), aut, h, color=AUTHORS, label="Unique authors", zorder=3)

    # 直接标注（relief 规则：aqua 不能只靠颜色）
    for yi, v in zip(y + (h + gap), raw):
        ax.text(v + max(raw) * 0.012, yi, f"{int(v)}", va="center", fontsize=7.5,
                color=PALETTE["text_secondary"])
    for yi, v in zip(y, org):
        ax.text(v + max(raw) * 0.012, yi, f"{int(v)}", va="center", fontsize=7.5,
                color=PALETTE["text_secondary"])
    for yi, v in zip(y - (h + gap), aut):
        ax.text(v + max(raw) * 0.012, yi, f"{int(v)}", va="center", fontsize=7.5,
                color=PALETTE["text_secondary"])

    # feed 驱动比例：只在显著时标出，不是每根都标
    for yi, r0, o0 in zip(y, raw, org):
        frac = 1 - o0 / r0 if r0 else 0
        if frac >= 0.4:
            ax.text(max(raw) * 1.005, yi, f"{frac:.0%} feed-driven",
                    va="center", fontsize=7.5, fontweight="bold",
                    color=PALETTE["critical"])

    ax.set_yticks(y)
    ax.set_yticklabels(topics, fontsize=8.5)
    ax.set_xlabel("posts / authors in window")
    ax.set_xlim(0, max(raw) * 1.30)
    ax.grid(axis="y", visible=False)
    ax.set_title("Surface activity vs organic participation, by topic",
                 loc="left", pad=26, fontweight="bold")
    # 图例横排置于标题下方：右下角会与 "feed-driven" 标注碰撞
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.005), ncol=3,
              handlelength=1.1, columnspacing=1.4)
    if compact:
        _note(fig, "Broadcast accounts are automated threat-intel feeds and "
                   "conference-promotion accounts, identified behaviourally "
                   f"(≥5 posts, ≥4 active days, ≥60% zero-engagement). "
                   f"Top {len(rows)} topics by volume.")
    else:
        _note(fig, "Broadcast accounts are automated threat-intel feeds and conference "
                   "promotion, identified behaviourally (≥5 posts, ≥4 active days, "
                   "≥60% zero-engagement among settled posts).")
    return _save(fig, out, name)


# ==========================================================================
# 2. Topic trajectory（小倍数）
# ==========================================================================
def topic_trajectory(rows, out: Path, name="fig_topic_trajectory",
                     top_n=6, roll=7, topics=None, ncol=2, panel_h=1.55):
    """每个 topic 一格，三条线。滚动平均因为日量太小（中位数个位数）。"""
    by_topic = defaultdict(list)
    for r in rows:
        by_topic[r["topic_key"]].append(r)
    totals = {t: sum(x["raw_posts"] for x in v) for t, v in by_topic.items()}
    if topics:
        # 显式指定顺序：紧凑版要的是「feed 驱动 vs 有机」的并排对照，
        # 按量排序反而会把对比最强的两个分开。
        keep = [t for t in topics if t in by_topic]
    else:
        keep = [t for t, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:top_n]]

    all_days = sorted({r["d"] for r in rows})
    idx = {d: i for i, d in enumerate(all_days)}

    def series(recs, key):
        """[MUST] 居中滚动平均，窗口不完整处返回 NaN。

        原先用 np.convolve(mode="same")，它在两端**补零**，于是首尾各 roll//2 天
        被人为压低 —— 图上表现为一段并不存在的「下跌」。这正是本项目一直在
        防的那类伪信号，不能出现在自己的图里。NaN 让 matplotlib 直接不画。
        """
        a = np.zeros(len(all_days))
        for r in recs:
            a[idx[r["d"]]] = r[key]
        if roll <= 1:
            return a
        half = roll // 2
        out = np.full(len(a), np.nan)
        for i in range(half, len(a) - half):
            out[i] = a[i - half:i + half + 1].mean()
        return out

    nrow = math.ceil(len(keep) / ncol)
    fig, axes = plt.subplots(nrow, ncol,
                             figsize=(TEXTWIDTH_IN, panel_h * nrow + 0.9),
                             sharex=True)
    axes = np.atleast_1d(axes).ravel()

    for ax, topic in zip(axes, keep):
        recs = by_topic[topic]
        ax.plot(all_days, series(recs, "raw_posts"), color=RAW, lw=2.0, zorder=4)
        ax.plot(all_days, series(recs, "organic_posts"), color=ORGANIC, lw=2.0, zorder=5)
        ax.plot(all_days, series(recs, "authors"), color=AUTHORS, lw=1.6,
                ls=(0, (4, 2)), zorder=6)   # 线型作为第二重编码
        ax.set_title(topic, loc="left", fontsize=9, fontweight="bold", pad=4)
        ax.margins(x=0.01)
        # 日期轴：默认的等距刻度会把 10 个 ISO 日期挤在一起
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.tick_params(axis="x", labelsize=7.5)
    for ax in axes[len(keep):]:
        ax.set_visible(False)

    handles = [Line2D([], [], color=RAW, lw=2, label="All posts"),
               Line2D([], [], color=ORGANIC, lw=2, label="Non-broadcast posts"),
               Line2D([], [], color=AUTHORS, lw=1.6, ls=(0, (4, 2)),
                      label="Unique authors")]
    fig.legend(handles=handles, loc="upper right", ncol=3,
               bbox_to_anchor=(1.0, 1.02))
    fig.suptitle(f"Topic trajectories ({roll}-day rolling mean)",
                 x=0.0, ha="left", fontsize=10, fontweight="bold", y=1.04)
    fig.tight_layout()
    _note(fig, f"Centred {roll}-day rolling mean; lines stop {roll // 2} days short of "
               "each end, where the window is incomplete. Daily counts are in the "
               "single digits for most topics, so raw daily values are not plotted. "
               "Where the blue and orange lines diverge, volume is feed-driven.")
    return _save(fig, out, name)


# ==========================================================================
# 3. topic × 周 热力图
# ==========================================================================
def topic_week_heatmap(rows, out: Path, name="fig_topic_week_heatmap",
                       min_total=4, drop_partial_weeks=True):
    """行 = topic，列 = 周，颜色 = 该周内的 share（已剔除广播号）。

    [MUST] 归一化到「周内占比」而不是原始计数 —— 否则读者看到的是
    采集量的涨落，不是注意力的重新分配。
    """
    weeks = sorted({r["wk"] for r in rows})
    if drop_partial_weeks and len(weeks) > 2:
        # [MUST] 首尾周被语料边界截断，其「周内占比」不可比 ——
        # 实测最后一周只有 1 天数据，却因此拿到了全图最深的一格。
        dropped = [weeks[0], weeks[-1]]
        weeks = weeks[1:-1]
        rows = [r for r in rows if r["wk"] not in dropped]
        log.info("heatmap: dropped partial boundary weeks %s", dropped)
    totals = defaultdict(int)
    for r in rows:
        totals[r["topic_key"]] += r["n"]
    topics = [t for t, n in sorted(totals.items(), key=lambda kv: -kv[1])
              if n >= min_total]

    M = np.full((len(topics), len(weeks)), np.nan)
    N = np.zeros_like(M)
    ti = {t: i for i, t in enumerate(topics)}
    wi = {w: i for i, w in enumerate(weeks)}
    for r in rows:
        if r["topic_key"] in ti:
            M[ti[r["topic_key"]], wi[r["wk"]]] = r["share"]
            N[ti[r["topic_key"]], wi[r["wk"]]] = r["n"]

    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list("seq_blue", PALETTE["seq"])
    cmap = cmap.with_extremes(bad=PALETTE["surface"])

    fig, ax = plt.subplots(figsize=(TEXTWIDTH_IN, 0.34 * len(topics) + 1.6))
    im = ax.imshow(M, cmap=cmap, aspect="auto", vmin=0,
                   vmax=float(np.nanmax(M)))
    ax.set_xticks(range(len(weeks)))
    ax.set_xticklabels([w.strftime("%b %d") for w in weeks], fontsize=8)
    ax.set_yticks(range(len(topics)))
    ax.set_yticklabels(topics, fontsize=8.5)
    ax.grid(False)
    ax.set_xlabel("week beginning")

    # [MUST] 每格标注原始计数 —— 颜色说的是比例，但比例在 n=2 时毫无意义
    mid = float(np.nanmax(M)) * 0.55
    for i in range(len(topics)):
        for j in range(len(weeks)):
            if not np.isnan(M[i, j]):
                ax.text(j, i, f"{int(N[i, j])}", ha="center", va="center",
                        fontsize=7,
                        color="white" if M[i, j] > mid else PALETTE["text"])

    cb = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.015)
    cb.set_label("share of that week's relevant posts", fontsize=8)
    cb.outline.set_visible(False)
    cb.ax.tick_params(labelsize=7.5)
    ax.set_title("Topic composition by week (broadcast accounts excluded)",
                 loc="left", pad=10, fontweight="bold")
    _note(fig, "Cell colour is the topic's share of that week; the number is the raw "
               "post count; a blank cell means no posts. Partial weeks at the corpus "
               "boundary are excluded — their shares are not comparable. Most cells "
               "are single digits: at this corpus size the shares are descriptive, "
               "not evidence of change.")
    return _save(fig, out, name)


# ==========================================================================
# 4. actor–topic 二部网络
# ==========================================================================
def actor_topic_network(rows, out: Path, name="fig_actor_topic_network",
                        min_posts=1):
    """二部图：topic 钉死在圆周上，作者由弹簧布局收敛。

    不用纯 spring_layout：大多数作者只碰一个 topic，图几乎是不相连的星形，
    自由布局会把它们甩成互不相干的小疙瘩，还会让 topic 标签互相压住。
    把 topic 固定在圆周上，作者自然落到自己的 topic 附近，
    跨 topic 的作者被拉到中间 —— 那正是要看的东西。
    """
    import networkx as nx

    edges = [r for r in rows if r["n_posts"] >= min_posts]
    G = nx.Graph()
    topics = sorted({r["topic_key"] for r in edges})
    for t in topics:
        G.add_node(("t", t), kind="topic")
    for r in edges:
        a = ("a", r["author_id"])
        if a not in G:
            G.add_node(a, kind="author", bcast=r["is_bcast"],
                       handle=r["author_handle"], total=0, ntopics=0)
        G.nodes[a]["total"] += r["n_posts"]
        G.nodes[a]["ntopics"] += 1
        G.add_edge(a, ("t", r["topic_key"]), w=r["n_posts"])

    # topic 按语料量排序后均匀放上圆周，相邻的量级相近，视觉上不会一边倒
    order = sorted(topics, key=lambda t: -sum(
        d["w"] for _, _, d in G.edges(("t", t), data=True)))
    fixed_pos = {}
    for i, t in enumerate(order):
        ang = 2 * math.pi * i / len(order) + math.pi / 2
        fixed_pos[("t", t)] = np.array([math.cos(ang), math.sin(ang)])
    pos = nx.spring_layout(G, pos=fixed_pos, fixed=list(fixed_pos),
                           k=0.12, iterations=300, seed=20260921, weight="w")

    fig, ax = plt.subplots(figsize=(TEXTWIDTH_IN, 4.9))
    for u, v, d in G.edges(data=True):
        ax.plot([pos[u][0], pos[v][0]], [pos[u][1], pos[v][1]],
                color=PALETTE["grid"], lw=0.3 + 0.14 * d["w"], zorder=1,
                alpha=0.75, solid_capstyle="round")

    auth = [n for n, d in G.nodes(data=True) if d["kind"] == "author"]
    bc = [n for n in auth if G.nodes[n]["bcast"]]
    org = [n for n in auth if not G.nodes[n]["bcast"]]
    for grp, color, lbl, z, alpha in ((org, AUTHORS, "Organic account", 2, 0.75),
                                      (bc, PALETTE["critical"], "Broadcast account", 3, 1.0)):
        if not grp:
            continue
        ax.scatter([pos[n][0] for n in grp], [pos[n][1] for n in grp],
                   s=[10 + 7 * G.nodes[n]["total"] for n in grp],
                   c=color, edgecolors=PALETTE["surface"], linewidths=0.6,
                   zorder=z, label=lbl, alpha=alpha)

    tn = [("t", t) for t in topics]
    ax.scatter([pos[n][0] for n in tn], [pos[n][1] for n in tn],
               s=150, c=RAW, marker="s", edgecolors=PALETTE["surface"],
               linewidths=1.4, zorder=5, label="Topic")
    # 标签沿半径向外推，彼此不会重叠
    # 圆周上相邻的两个标签会相撞 —— 交替推远/推近，错开半径
    for i, t in enumerate(order):
        x, y = pos[("t", t)]
        r = math.hypot(x, y) or 1.0
        off = 30 if i % 2 else 15
        ax.annotate(t, (x, y), fontsize=7.2, fontweight="bold",
                    ha="center", va="center", zorder=6,
                    xytext=(off * x / r, off * y / r), textcoords="offset points",
                    color=PALETTE["text"],
                    bbox=dict(boxstyle="round,pad=0.2", fc=PALETTE["surface"],
                              ec=PALETTE["grid"], lw=0.5, alpha=0.92))
    # 不标 handle：红点自身已经足够显眼，点名会与 topic 标签互相压住，
    # 而具体是哪几个账号在 fig_raw_vs_organic 与正文里已经给全了。

    ax.set_axis_off()
    ax.set_aspect("equal")
    ax.margins(0.16)
    ax.legend(loc="upper left", markerscale=0.6, bbox_to_anchor=(-0.02, 1.02))
    ax.set_title("Actor-topic network: who posts into which topic",
                 loc="left", pad=8, fontweight="bold")
    multi = sum(1 for n in auth if G.nodes[n]["ntopics"] > 1)
    _note(fig, "Topics are pinned on a circle; accounts settle beside the topics they "
               "post into. Node size = posts contributed. A topic ringed by many small "
               f"teal nodes is distributed discussion; one dominated by a few large red "
               f"nodes is feed-driven. {multi} of {len(auth)} accounts post into more "
               "than one topic, so most of the graph is star-shaped by construction.")
    return _save(fig, out, name)


# ==========================================================================
# 5. 作者集中度（Lorenz）
# ==========================================================================
def concentration_curve(rows, out: Path, name="fig_concentration", gini_fn=None):
    allc = sorted((r["n_posts"] for r in rows), reverse=True)
    orgc = sorted((r["n_posts"] for r in rows if not r["is_bcast"]), reverse=True)

    def lorenz(counts):
        a = np.array(sorted(counts))
        return (np.arange(1, len(a) + 1) / len(a),
                np.cumsum(a) / a.sum())

    fig, ax = plt.subplots(figsize=(TEXTWIDTH_IN * 0.62, 3.5))
    ax.plot([0, 1], [0, 1], color=PALETTE["muted"], lw=1.0, ls=(0, (3, 3)),
            zorder=2)
    ax.annotate("perfect equality", (0.62, 0.66), fontsize=7.5, rotation=34,
                color=PALETTE["muted"])
    for counts, color, lbl in ((allc, RAW, "All accounts"),
                               (orgc, ORGANIC, "Broadcast excluded")):
        x, y = lorenz(counts)
        g = gini_fn(counts) if gini_fn else float("nan")
        ax.plot(x, y, color=color, lw=2.0, zorder=3, label=f"{lbl}  (Gini {g:.2f})")
        ax.fill_between(x, y, x, color=color, alpha=0.07, zorder=1)
    ax.set_xlabel("cumulative share of accounts (least active first)")
    ax.set_ylabel("cumulative share of posts")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.legend(loc="upper left")
    ax.set_title("Posting concentration", loc="left", pad=8, fontweight="bold")
    _note(fig, "The gap between the two curves is exactly what the broadcast filter "
               "removes.")
    return _save(fig, out, name)


# ==========================================================================
# 6. co-sharing 网络（diffusion 的可行替代）
# ==========================================================================
def source_uptake(rows, out: Path, name="fig_source_uptake", min_authors=3,
                  top_n=18):
    """外部信源的采纳广度：每个域名被多少个**不同**账号引用。

    为什么不是网络图
    ----------------
    最初画的是作者–作者共享网络（两人引用过同一域名则连边）。实测否决：
    **121 个参与作者中有 115 个只出现在一个共享域名里**，传递性 0.907 ——
    这张图在结构上就是一堆互不相连的完全子图，最大连通分量也只占 40%。
    把它画成 node-link 会暗示一种并不存在的网络结构，这恰恰是本项目
    一直在防的「制造虚假信心」。

    真正有信息量的问题是：**哪些信源被很多独立账号各自引用，哪些只是
    单个账号在反复自推。** 这正是 broadcast 与 organic 的分界，用排序条形
    比网络图清楚得多，且不夸大证据强度。

    真正的传播级联（谁转发了谁）需要 `app.bsky.feed.getRepostedBy`，
    本项目尚未采集该端点 —— 见 README「下一步」。
    """
    bydom = defaultdict(lambda: {"org": set(), "bc": set(), "posts": 0})
    for r in rows:
        d = bydom[r["domain"]]
        (d["bc"] if r["is_bcast"] else d["org"]).add(r["author_id"])
        d["posts"] += r["n"]

    items = [(dom, v) for dom, v in bydom.items()
             if len(v["org"]) + len(v["bc"]) >= min_authors]
    items.sort(key=lambda kv: (len(kv[1]["org"]) + len(kv[1]["bc"]), kv[1]["posts"]))
    items = items[-top_n:]
    if not items:
        log.warning("source_uptake: nothing above min_authors=%d", min_authors)
        return []

    doms = [d for d, _ in items]
    org = np.array([len(v["org"]) for _, v in items], float)
    bc = np.array([len(v["bc"]) for _, v in items], float)
    y = np.arange(len(items))

    fig, ax = plt.subplots(figsize=(TEXTWIDTH_IN, 0.30 * len(items) + 1.7))
    ax.barh(y, org, 0.62, color=AUTHORS, label="Organic accounts", zorder=3)
    # 2px surface gap between stacked segments
    ax.barh(y, bc, 0.62, left=org + 0.06, color=PALETTE["critical"],
            label="Broadcast accounts", zorder=3)
    for yi, o, b in zip(y, org, bc):
        ax.text(o + b + 0.28, yi, f"{int(o + b)}", va="center", fontsize=7.5,
                color=PALETTE["text_secondary"])
    ax.set_yticks(y)
    ax.set_yticklabels(doms, fontsize=8)
    ax.set_xlabel("number of distinct accounts citing this domain")
    ax.set_xlim(0, (org + bc).max() * 1.12)
    ax.grid(axis="y", visible=False)
    ax.set_title("External source uptake: broad citation vs single-account repeat",
                 loc="left", pad=26, fontweight="bold")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.005), ncol=2,
              handlelength=1.1, columnspacing=1.4)

    n_single = sum(1 for _, v in bydom.items()
                   if len(v["org"]) + len(v["bc"]) == 1 and v["posts"] >= 3)
    _note(fig, "A long teal bar means many accounts independently cited the same "
               "source. A short red bar is one feed reposting its own domain. "
               f"{n_single} further domains were cited 3+ times by a single account "
               "each and are not shown. Platform and URL-shortener domains are "
               "excluded. This is source overlap, NOT a repost cascade: propagation "
               "(who reposted whom) needs an endpoint this project does not yet "
               "collect.")
    return _save(fig, out, name)
