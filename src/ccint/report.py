"""报告渲染与编排（HANDOFF §M7）。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from .analytics.trend import TREND_METHOD
from .models import TrendCandidate

log = logging.getLogger(__name__)

# [MUST] 固定段落，模板常量，不由 agent 生成 —— 避免 agent 自我美化局限性。
LIMITATIONS = """\
## 局限性

本节为模板常量，不由 agent 生成。

- **单一 source。** 全部数据来自 Bluesky，选它的唯一理由是 API 零审批延迟，
  不是因为它最能代表加拿大网络安全讨论。Bluesky 用户群体有明显偏斜，
  这里看到的不等于公众讨论。正式的 source 可行性比较（Reddit / X）尚未进行。
- **Topic 来自固定规则词表。** `topic_key` 由关键词规则判定，首个命中的桶胜出。
  误分类必然存在，跨桶重叠的帖子会被归给优先级更高的桶。
- **无 baseline 模型。** 未对周末效应、平台整体 volume 漂移、单链接 mass repost
  做任何建模。因此「上升」可能来自这些因素而非讨论本身的变化。
- **样本量小。** 相关帖子量级为每天数十条，绝对数的小幅变动会造成很大的百分比波动。
  `min_posts` 阈值挡掉了最小样本的噪声，但没有消除它。
- **Canada relevance 由关键词判定。** 召回优先的词表会带入误报（例如帖子只是
  提到某个加拿大地名），也会漏掉未明示加拿大关联的相关讨论。
"""


def _fmt_growth(g: float | None) -> str:
    return "n/a" if g is None else f"{g:+.0%}"


def data_health(conn, *, window_start: datetime, window_end: datetime) -> dict:
    """本窗口内的采集健康。用于报告顶部的醒目警告。"""
    runs = [dict(r) for r in conn.execute(
        """SELECT run_id, mode, status, n_fetched, n_inserted, n_error,
                  started_at, ended_at, left(error_detail, 200) AS error_detail
           FROM collection_runs
           WHERE started_at >= %s AND started_at < %s + interval '1 day'
           ORDER BY run_id""",
        (window_start - timedelta(days=30), window_end),
    ).fetchall()]

    days = [dict(r) for r in conn.execute(
        """SELECT d::date AS day, COALESCE(c.n, 0) AS n_posts
           FROM generate_series(%s::date, (%s::date - 1), interval '1 day') d
           LEFT JOIN (
               SELECT (published_at AT TIME ZONE 'UTC')::date AS day, count(*) AS n
               FROM posts GROUP BY 1
           ) c ON c.day = d::date
           ORDER BY 1""",
        (window_start, window_end),
    ).fetchall()]

    bad = [r for r in runs if r["status"] in ("failed", "partial")]
    gaps = [d["day"] for d in days if d["n_posts"] == 0]
    # [MUST] backfill 与 incremental 的采样性质不同，窗口内是否混入 backfill 必须说明
    modes = [dict(r) for r in conn.execute(
        """SELECT r.mode, count(*) AS n_posts
           FROM posts p LEFT JOIN collection_runs r ON r.run_id = p.first_seen_run
           WHERE p.published_at >= %s AND p.published_at < %s
           GROUP BY 1 ORDER BY 1""",
        (window_start, window_end),
    ).fetchall()]

    return {"runs": runs, "bad_runs": bad, "daily": days, "gaps": gaps,
            "modes": modes, "has_problem": bool(bad or gaps)}


def render_report(
    *,
    analysis_id: int | None,
    as_of: datetime,
    window_days: int,
    label_version: str,
    query_version: str,
    agent_model: str | None,
    prompt_version: str | None,
    candidates: list[TrendCandidate],
    health: dict,
    agent_result=None,
    min_posts: int = 5,
    null_result=None,
    broadcast=None,
    broadcast_excluded: bool = False,
) -> str:
    L: list[str] = []
    A = L.append
    cur_start = as_of - timedelta(days=window_days)
    prev_start = as_of - timedelta(days=2 * window_days)

    A("# Canada Cyber Social Intelligence — Trend Report")
    A("")

    # ---- 2. 数据健康警告（[MUST] 置顶醒目）-----------------------------
    if health["has_problem"]:
        A("> ## ⚠️ 数据健康警告")
        A(">")
        A("> **本窗口的采集不完整。下面的趋势可能反映采集故障而非社会讨论的变化。**")
        A(">")
        # [MUST] 同质故障必须折叠。10 小时的 token 过期事故会产生 40+ 条一模一样的
        # partial run —— 逐条列出会把真正独特的故障（如 NUL 字符崩溃）淹没，
        # 读者反而看不见问题。按 (mode, status, 首行错误) 分组，每组只举例一条。
        def _sig(r):
            d = (r.get("error_detail") or "").strip().splitlines()
            return (r["mode"], r["status"], d[0][:120] if d else "")

        groups: dict[tuple, list] = {}
        for r in health["bad_runs"]:
            groups.setdefault(_sig(r), []).append(r)
        for (mode, status, detail), rs in sorted(
                groups.items(), key=lambda kv: -len(kv[1])):
            ids = [x["run_id"] for x in rs]
            span = (f"run #{ids[0]}" if len(ids) == 1
                    else f"**{len(ids)} 次** run #{min(ids)}–#{max(ids)}")
            tot_err = sum(x["n_error"] for x in rs)
            tot_fetch = sum(x["n_fetched"] for x in rs)
            A(f"> - {span} ({mode}) 状态 `{status}`，"
              f"n_error 合计={tot_err:,}，fetched 合计={tot_fetch:,}"
              + (f" — {detail}" if detail else ""))
        if health["gaps"]:
            g = ", ".join(str(d) for d in health["gaps"])
            A(f"> - **采集空洞**：以下日期没有任何帖子入库 — {g}")
        A(">")
        A("> 解读这些日期的数字前，请先确认是采集问题还是真实变化。")
        A("")

    # ---- 1. Provenance -------------------------------------------------
    A("## Provenance")
    A("")
    A("| 字段 | 值 |")
    A("|---|---|")
    A(f"| analysis_id | {analysis_id if analysis_id is not None else '(未持久化)'} |")
    A(f"| as_of | `{as_of.isoformat()}` |")
    A(f"| current window | `[{cur_start.isoformat()}, {as_of.isoformat()})` |")
    A(f"| previous window | `[{prev_start.isoformat()}, {cur_start.isoformat()})` |")
    A(f"| window_days | {window_days} |")
    A(f"| min_posts | {min_posts} |")
    A(f"| label_version | `{label_version}` |")
    A(f"| query_version | `{query_version}` |")
    A(f"| trend_method | `{TREND_METHOD}` |")
    if null_result is not None:
        A(f"| null_method | `{null_result.method}` (unit=`{null_result.unit}`, "
          f"n_iter={null_result.n_iter:,}, seed={null_result.seed}) |")
    A(f"| exclude_broadcast | `{broadcast_excluded}` |")
    A(f"| agent_model | `{agent_model or '(未运行)'}` |")
    A(f"| prompt_version | `{prompt_version or '(未运行)'}` |")
    A(f"| generated_at | `{datetime.now(timezone.utc).isoformat()}` |")
    A("")

    # ---- 3. 采集 mode 说明 ---------------------------------------------
    A("## 采集构成")
    A("")
    modes = {m["mode"]: m["n_posts"] for m in health["modes"]}
    for k, v in modes.items():
        A(f"- `{k or 'unknown'}`: {v:,} posts")
    if modes.get("backfill"):
        A("")
        A("> **本窗口包含 backfill 数据。** backfill 与 incremental 的采样性质不同 —— "
          "search API 的历史覆盖不完整且可能有偏，与持续采集不可等同。"
          "跨这两种数据的窗口对比应据此打折看待。")
    A("")
    if health["runs"]:
        A("<details><summary>本窗口相关的 collection runs</summary>")
        A("")
        A("| run | mode | status | fetched | inserted | errors |")
        A("|---|---|---|---:|---:|---:|")
        for r in health["runs"][-20:]:
            A(f"| {r['run_id']} | {r['mode']} | `{r['status']}` | "
              f"{r['n_fetched']:,} | {r['n_inserted']:,} | {r['n_error']} |")
        A("")
        A("</details>")
        A("")

    # ---- 4. Top-N 表格 -------------------------------------------------
    A(f"## Top-{len(candidates)} candidate trends")
    A("")
    if not candidates:
        A(f"本窗口没有任何 topic 的 `cur_posts` 达到 `min_posts={min_posts}`。")
        A("")
        A("这通常意味着数据量不足以支撑该窗口长度的对比，而不是「没有趋势」。"
          "见 README 的「数据量冲突」一节。")
    else:
        A("| # | topic | cur posts | prev posts | Δ | growth | "
          "cur authors | prev authors | posts/author | cur share | share Δ |")
        A("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for i, c in enumerate(candidates, 1):
            ppa = f"{c.cur_posts / c.cur_authors:.1f}" if c.cur_authors else "n/a"
            A(f"| {i} | `{c.topic_key}` | {c.cur_posts} | {c.prev_posts} | "
              f"{c.abs_delta:+d} | {_fmt_growth(c.rel_growth)} | {c.cur_authors} | "
              f"{c.prev_authors} | {ppa} | {c.cur_share:.1%} | {c.share_delta:+.1%} |")
        A("")
        A("> `posts/author` 接近 1 表示讨论面广；显著大于 1 表示少数账号贡献了大部分帖子，"
          "此时的「上升」可能是发帖行为而非公众讨论。")
    A("")

    # ---- 4b. 显著性检验（v1 Phase 2）-----------------------------------
    if null_result is not None and null_result.topics:
        nr = null_result
        by = nr.by_topic()
        A("## 显著性检验 — 这些变化能否与噪声区分")
        A("")
        A(f"零模型 `{nr.method}` / unit=`{nr.unit}` / {nr.n_iter:,} 次置换 / seed={nr.seed}。")
        A("")
        A("| topic | Δ | 零分布 sd | 零分布 \\|Δ\\| 95% | p (raw) | p (FWER) | 判定 |")
        A("|---|---:|---:|---:|---:|---:|---|")
        _V = {"signal": "**signal**", "weak": "weak", "noise": "noise"}
        for c in candidates:
            t = by.get(c.topic_key)
            if t is None:
                continue
            A(f"| `{t.topic_key}` | {t.abs_delta:+d} | {t.null_sd:.2f} | "
              f"±{t.null_p95_abs:.0f} | {t.p_raw:.3f} | {t.p_fwer:.3f} | {_V[t.verdict]} |")
        A("")
        A(f"> **判读刻度：零模型下「全 topic 中最大的 |Δ|」95 分位 = ±{nr.max_null_p95:.0f}。**")
        A("> 换句话说，即使 topic 与时间完全无关，光靠随机波动就能在某个 topic 上")
        A(f"> 看到 ±{nr.max_null_p95:.0f} 的变化。任何 |Δ| 小于这个数的 topic，")
        A("> 都不足以支撑「该主题正在上升／下降」的结论。")
        A(">")
        A("> `p (FWER)` 已对「我们在 %d 个 topic 里挑最大的那个来讲故事」做了校正"
          % len(nr.topics))
        A("> （max-statistic 法）。**只有 FWER ≤ 0.05 才可下显著结论。**")
        A(">")
        if nr.unit == "author":
            A("> unit=`author`：每个作者的全部 post 作为一个整体在时间轴上循环平移，")
            A("> 保留其发帖数、topic 组成与 burst 结构。这排除的是")
            A("> 「某主题的上升只是少数高产作者恰好扎堆」这一解释。")
        else:
            A("> unit=`post`：逐条重排窗口归属。**忽略作者聚集，p 值偏乐观**，仅作对照。")
        A("")
        sig = [t for t in nr.topics if t.verdict == "signal"]
        if not sig:
            A("### 本窗口结论：无统计可检出的趋势")
            A("")
            A(f"{len(nr.topics)} 个 topic 全部落在噪声带内。排名第一的 "
              f"`{candidates[0].topic_key}`（{candidates[0].abs_delta:+d}，"
              f"{_fmt_growth(candidates[0].rel_growth)}）"
              f"的 FWER 校正 p = {by[candidates[0].topic_key].p_fwer:.2f}，"
              "与「什么都没发生」不可区分。")
            A("")
            A("**上面的 Top-N 表格仍然有意义 —— 但它是「本窗口谈得最多的主题」，"
              "不是「正在上升的主题」。** 后一种说法本报告无法支持。")
        else:
            A("### 通过检验的 topic")
            A("")
            for t in sig:
                A(f"- `{t.topic_key}`：Δ={t.abs_delta:+d}，FWER p={t.p_fwer:.4f}")
        A("")

    # ---- 4c. 广播型账号（v1 Phase 3）-----------------------------------
    if broadcast is not None and broadcast.authors:
        b = broadcast
        A("## 广播型账号 — 谁在发，有没有人在听")
        A("")
        state = ("**已从上述所有数字中剔除。**" if broadcast_excluded
                 else "**未剔除 —— 上述数字包含它们。**")
        A(f"行为判定 `{b.method}`（{b.params}）。{state}")
        A("")
        A(f"- **{len(b.authors)} 个账号（占全部作者 {b.author_share:.1%}）"
          f"贡献了 {b.n_posts} 条 relevant（{b.post_share:.1%}）。**")
        A("")
        A("| handle | 条数 | 活跃天 | 零互动率 | 平均互动 |")
        A("|---|---:|---:|---:|---:|")
        for a in b.authors:
            A(f"| `{a.author_handle}` | {a.n_posts} | {a.n_days} | "
              f"{a.zero_rate:.0%} | {a.avg_engagement:.2f} |")
        A("")
        A("> 这些账号同时满足：发帖量高、跨多日持续、且**已充分沉淀的帖子几乎无人点赞转发**。")
        A("> 实际构成是自动化威胁情报 feed（勒索软件泄露站监控机器人）与安全会议宣传号。")
        A(">")
        A("> **它们不是公众讨论。** 把它们算进 topic volume，等于用机器人的发帖频率")
        A("> 去度量社会关注度。若某 topic 的量主要由这些账号构成，该 topic 的")
        A("> 「上升／下降」反映的是 feed 的运行状态，而不是任何社会现象。")
        A(">")
        A(f"> 零互动率只统计采集滞后 ≥ {b.params['min_lag_hours']}h 的帖子 —— "
          "`like_count` 是采集瞬间的快照，")
        A("> 刚抓到的帖子还没来得及获得互动，不加这个守卫会把新帖误判成广播号。")
        A("")

    # ---- 5. Rank 1 的 agent 分析 ---------------------------------------
    if candidates:
        A(f"## Rank 1 分析 — `{candidates[0].topic_key}`")
        A("")
    if agent_result is None:
        A("_未运行 agent 分析。_ 若因缺少 `ANTHROPIC_API_KEY`，"
          "在 `.env` 中配置后重跑 `ccint analyze`。")
        A("")
    else:
        a = agent_result.analysis
        if agent_result.warnings:
            A("> ### ⚠️ Agent 输出警告")
            A(">")
            A("> 以下问题未被静默处理，请据此判断本节结论的可靠程度：")
            A(">")
            for w in agent_result.warnings:
                A(f"> - {w}")
            A("")
        # analyst_v2 的两个判定字段 —— 它们回答的是两个不同的问题，
        # 必须分开呈现：「量的上升成不成立」与「内容里有没有一件事」。
        tcs, coh = a.get("trend_claim_supported"), a.get("coherence")
        if tcs or coh:
            _T = {"supported": "✅ 支持 —— 内容层证据足以独立成立",
                  "not_supported": "❌ 不支持 —— 不得写成「该主题正在上升」",
                  "undetermined": "⚠️ 无法判定"}
            _C = {"one_story": "🎯 同一件事",
                  "few_stories": "◐ 几件事",
                  "unrelated": "✖ 互不相关，只是共享同一个关键词标签"}
            A("> | 判定 | 结论 |")
            A("> |---|---|")
            if tcs:
                A(f"> | **趋势主张是否成立** | {_T.get(tcs, tcs)} |")
            if coh:
                A(f"> | **内容是否连贯** | {_C.get(coh, coh)} |")
            A("")
            if tcs == "not_supported" and coh == "one_story":
                A("> 这两行**不矛盾**：帖子里确实有一件具体的事，"
                  "但它在本窗口造成的量级变化与随机波动不可区分。"
                  "可以报道这件事，不能报道「它在升温」。")
                A("")
        A("### 发生了什么")
        A("")
        A(a.get("what_is_happening", "(空)"))
        A("")
        A(f"- **单一事件还是多起**：`{a.get('is_single_event_or_multiple', '?')}`")
        ents = a.get("key_entities") or []
        A(f"- **关键实体**：{', '.join(f'`{e}`' for e in ents) if ents else '(无)'}")
        A(f"- **置信度**：`{a.get('confidence', '?')}` — {a.get('confidence_reason', '')}")
        A("")
        A("### 替代解释（强制字段）")
        A("")
        A(a.get("alternative_explanation") or "_(agent 未填写 —— 见上方警告)_")
        A("")
        A("### 证据")
        A("")
        ev = a.get("evidence") or []
        if not ev:
            A("_agent 未提供证据。_")
        else:
            served = agent_result.served_post_ids
            A("| post_id | 为什么相关 | 链接 |")
            A("|---|---|---|")
            for e in ev:
                pid = e.get("post_id")
                ok = pid in served
                mark = "" if ok else " ⚠️**未经核实**"
                url = (agent_result.post_urls or {}).get(pid) if hasattr(
                    agent_result, "post_urls") else None
                link = f"[post]({url})" if url else "—"
                A(f"| `{pid}`{mark} | {e.get('why', '')} | {link} |")
        A("")
        A("<details><summary>Agent 调用的工具</summary>")
        A("")
        for tc in agent_result.tool_calls:
            status = "ok" if tc.get("ok") else f"ERROR: {tc.get('error')}"
            A(f"- `{tc['tool']}({json.dumps(tc['args'], ensure_ascii=False)})` — {status}")
        A("")
        A("</details>")
        A("")

    # ---- 6. 局限性（固定段落）------------------------------------------
    A(LIMITATIONS)
    return "\n".join(L)


def persist_analysis_run(conn, *, as_of, label_version, window_days, filters,
                         agent_model, prompt_version, report_path,
                         candidates, agent_result) -> int:
    result = {
        "candidates": [c.to_dict() for c in candidates],
        "agent": (agent_result.analysis if agent_result else None),
        "agent_warnings": (agent_result.warnings if agent_result else []),
    }
    row = conn.execute(
        """INSERT INTO analysis_runs
           (as_of, label_version, trend_method, window_days, filters,
            agent_model, prompt_version, report_path, result)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING analysis_id""",
        (as_of, label_version, TREND_METHOD, window_days, Jsonb(filters),
         agent_model, prompt_version, str(report_path) if report_path else None,
         Jsonb(result)),
    ).fetchone()
    return row["analysis_id"]
