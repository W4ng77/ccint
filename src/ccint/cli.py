"""ccint CLI（typer）。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from . import db
from .config import settings
from .logging_setup import setup as setup_logging

app = typer.Typer(add_completion=False, help="Canada Cyber Social Intelligence (v0)")
db_app = typer.Typer(help="数据库生命周期与 migration")
collect_app = typer.Typer(help="采集")
app.add_typer(db_app, name="db")
app.add_typer(collect_app, name="collect")


def _parse_when(s: str) -> datetime:
    """接受 'now' / 'YYYY-MM-DD' / ISO8601。[MUST] 一律返回 tz-aware UTC。"""
    if s.lower() == "now":
        return datetime.now(timezone.utc)
    raw = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")):
    setup_logging(logging.DEBUG if verbose else logging.INFO)
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------- db
@db_app.command("migrate")
def db_migrate():
    """建表 / 顺序应用 migration。幂等。"""
    applied = db.migrate()
    if applied:
        typer.echo(f"applied: {', '.join(applied)}")
    else:
        typer.echo("already up to date")
    typer.echo("tables: " + ", ".join(db.table_names()))


@db_app.command("start")
def db_start():
    """启动用户态 Postgres 实例。"""
    db.get_server()
    typer.echo(f"postgres running at {settings.pgdata}")


@db_app.command("stop")
def db_stop():
    db.stop_server()
    typer.echo("stopped")


@db_app.command("psql")
def db_psql():
    """打印连接串，便于外部工具接入。"""
    typer.echo(db.dsn())


# ---------------------------------------------------------------- collect
def _make_collector():
    from .collectors.bluesky import BlueskyCollector, load_cyber_search_terms

    terms, ver = load_cyber_search_terms()
    if ver != settings.query_version:
        typer.secho(
            f"WARNING: cyber.yaml version={ver} 与 config.query_version="
            f"{settings.query_version} 不一致。[MUST] query_version 变更必须显式递增。",
            fg="yellow",
        )
    return BlueskyCollector(terms, query_version=ver)


@collect_app.command("backfill")
def collect_backfill(
    since: str = typer.Option(..., "--since"),
    until: str = typer.Option("now", "--until"),
    max_pages: int = typer.Option(30, "--max-pages", help="每个 (天, term) 的翻页上限"),
    chunk_days: int = typer.Option(1, "--chunk-days", help="[MUST] 按天分块，避免倒序分页截断老日期"),
):
    """一次性 seed 历史语料。"""
    from .ingest import reap_stale_runs, run_collection

    reap_stale_runs()
    s, u = _parse_when(since), _parse_when(until)
    if s >= u:
        raise typer.BadParameter("--since 必须早于 --until")
    with _make_collector() as c:
        res = run_collection(c, mode="backfill", since=s, until=u,
                             max_pages_per_term=max_pages, chunk_days=chunk_days)
    typer.echo(str(res))


@collect_app.command("incremental")
def collect_incremental(
    max_pages: int = typer.Option(50, "--max-pages"),
):
    """从上次成功 checkpoint 继续。供 systemd timer 调用。"""
    from .ingest import reap_stale_runs, resolve_incremental_window, run_collection

    reap_stale_runs()
    with _make_collector() as c:
        s, u = resolve_incremental_window(c.source_key)
        res = run_collection(c, mode="incremental", since=s, until=u,
                             max_pages_per_term=max_pages)
    typer.echo(str(res))


# ---------------------------------------------------------------- label
@app.command("label")
def label_cmd(
    version: str = typer.Option("rules_v1", "--version"),
    all_: bool = typer.Option(False, "--all", help="重标全部（换词表后用）"),
    unlabeled: bool = typer.Option(True, "--unlabeled/--no-unlabeled"),
):
    """对 posts 跑标注，写入 post_labels。幂等。"""
    from .labeling import label_counts, label_posts

    stats = label_posts(version, only_unlabeled=(not all_) and unlabeled)
    typer.echo(str(stats))
    typer.echo("totals: " + str(label_counts(version)))


# ---------------------------------------------------------------- analytics
@app.command("pilot-stats")
def pilot_stats_cmd(
    label_version: str = typer.Option("rules_v2", "--label-version"),
    json_out: bool = typer.Option(False, "--json"),
):
    """输出决定后续架构的那个数字：日均 relevant posts/day。"""
    import json as _json

    from .analytics.descriptive import format_pilot_stats, pilot_stats

    with db.connect(autocommit=True) as conn:
        st = pilot_stats(conn, label_version)
    typer.echo(_json.dumps(st, indent=2, default=str) if json_out
               else format_pilot_stats(st))


@app.command("trend")
def trend_cmd(
    as_of: str = typer.Option("now", "--as-of"),
    window: int = typer.Option(7, "--window"),
    label_version: str = typer.Option("rules_v2", "--label-version"),
    min_posts: int = typer.Option(5, "--min-posts"),
    actor_mode: str = typer.Option(
        "all", "--actor-mode",
        help="attention | events | ecosystem | all —— 见 analytics/actor.py"),
):
    """相邻窗口对比，产出 candidate trend 排名。"""
    from .analytics.trend import detect_trends

    t = _parse_when(as_of)
    with db.connect(autocommit=True) as conn:
        excl = _actor_exclusion(conn, actor_mode)
        cands = detect_trends(conn, label_version=label_version, as_of=t,
                              window_days=window, min_posts=min_posts,
                              exclude_authors=excl)
    if not cands:
        typer.secho(f"无候选（min_posts={min_posts}）。数据量可能不足以支撑 "
                    f"{window} 天窗口对比 —— 见 README「数据量冲突」一节。", fg="yellow")
        raise typer.Exit(0)
    typer.echo(f"{'topic':26s} {'cur':>5s} {'prev':>5s} {'Δ':>5s} {'growth':>8s} "
               f"{'cur_au':>7s} {'prev_au':>8s} {'share Δ':>9s}")
    for c in cands:
        g = "n/a" if c.rel_growth is None else f"{c.rel_growth:+.0%}"
        typer.echo(f"{c.topic_key:26s} {c.cur_posts:5d} {c.prev_posts:5d} "
                   f"{c.abs_delta:+5d} {g:>8s} {c.cur_authors:7d} {c.prev_authors:8d} "
                   f"{c.share_delta:+9.3f}")


def _actor_exclusion(conn, mode: str, profile_version: str = "actor_v1"):
    """按分析目的取排除集。mode='all' 时连画像都不用加载。"""
    if mode == "all":
        return None
    from .analytics.actor import exclusion_set, load_profiles
    try:
        profs = load_profiles(conn, profile_version)
    except LookupError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1) from e
    ids = exclusion_set(profs, mode)
    typer.secho(f"actor-mode={mode}：排除 {len(ids)} 位作者", fg="yellow")
    return ids


def _broadcast_detect(conn, label_version: str):
    from .analytics.broadcast import detect_broadcast_authors
    return detect_broadcast_authors(conn, label_version=label_version)


def _broadcast_ids(conn, label_version: str, enabled: bool):
    return _broadcast_detect(conn, label_version).author_ids if enabled else None


def _pick_prompt(null_result, rank1) -> str:
    """零模型判定为 noise / weak 时切到 analyst_v2。

    analyst_v1 的开头写着「统计层已判定这是 top candidate trend，你的任务是解释它」——
    在一个统计上不可区分于噪声的变化上，这句话本身就是在诱导 agent 编故事。
    analyst_v2 把任务改成「抛开 volume，这些帖子里到底有没有一件事」，
    并允许「没有」作为正确答案。
    """
    if null_result is None:
        return settings.prompt_version
    t = null_result.by_topic().get(rank1.topic_key)
    if t is None or t.verdict == "signal":
        return settings.prompt_version
    return "analyst_v2"


# ---------------------------------------------------------------- analyze
@app.command("analyze")
def analyze_cmd(
    as_of: str = typer.Option("now", "--as-of"),
    window: int = typer.Option(7, "--window"),
    label_version: str = typer.Option("rules_v2", "--label-version"),
    top: int = typer.Option(3, "--top"),
    min_posts: int = typer.Option(5, "--min-posts"),
    no_agent: bool = typer.Option(False, "--no-agent", help="跳过 agent，只出统计报告"),
    agent_backend: str = typer.Option(
        "anthropic", "--agent-backend",
        help="anthropic（默认，需 API key）| local（OpenAI 兼容端点，如 vLLM）"),
    local_base_url: str = typer.Option(
        "http://127.0.0.1:8000/v1", "--local-base-url"),
    local_model: str = typer.Option("user", "--local-model"),
    out: str = typer.Option("", "--out", help="报告路径，默认 reports/<ts>.md"),
    null_iter: int = typer.Option(
        5000, "--null-iter",
        help="置换检验次数。0 = 跳过显著性检验（不推荐：报告将无法区分信号与噪声）"),
    null_unit: str = typer.Option(
        "author", "--null-unit",
        help="零模型单位：author（cluster-robust，默认）| post（忽略作者聚集，偏乐观）"),
    exclude_broadcast: bool = typer.Option(
        False, "--exclude-broadcast",
        help="剔除广播型账号（自动化情报 feed / 会议宣传号）。见 analytics/broadcast.py"),
):
    """全流程：detect_trends → top-N → 对 rank 1 跑 agent → 渲染报告 → 写 analysis_runs。"""
    from .agent.analyst import run_analyst
    from .analytics.trend import detect_trends
    from .report import data_health, persist_analysis_run, render_report

    t = _parse_when(as_of)
    cur_start = t - timedelta(days=window)

    with db.connect(autocommit=True) as conn:
        n_posts = conn.execute("SELECT count(*) AS n FROM posts").fetchone()["n"]
        if n_posts == 0:
            typer.secho("库里没有数据。先跑 `ccint collect backfill --since <date>`。",
                        fg="red")
            raise typer.Exit(1)
        n_lab = conn.execute(
            "SELECT count(*) AS n FROM post_labels WHERE label_version=%s",
            (label_version,)).fetchone()["n"]
        if n_lab == 0:
            typer.secho(f"没有 label_version={label_version} 的标注。"
                        f"先跑 `ccint label --version {label_version}`。", fg="red")
            raise typer.Exit(1)

        bcast = _broadcast_detect(conn, label_version)
        excl = bcast.author_ids if exclude_broadcast else None
        cands = detect_trends(conn, label_version=label_version, as_of=t,
                              window_days=window, min_posts=min_posts,
                              exclude_authors=excl)
        health = data_health(conn, window_start=cur_start, window_end=t)

        # [MUST] 显著性检验必须跑在 agent 之前 —— agent 的 prompt 依赖它的判定。
        # 否则 agent 会把一个不可区分于噪声的波动写成「某主题正在上升」，
        # 而这正是 HANDOFF §1「不得制造虚假信心」要禁止的事。
        null_result = None
        if null_iter > 0 and cands:
            from .analytics.nullmodel import fetch_records, permutation_test
            typer.echo(f"跑零模型（{null_iter:,} 次置换，unit={null_unit}）...")
            recs = fetch_records(conn, label_version=label_version, as_of=t,
                                 window_days=window, exclude_authors=excl)
            null_result = permutation_test(
                recs, as_of=t, window_days=window, label_version=label_version,
                unit=null_unit, n_iter=null_iter)
            _v = null_result.by_topic().get(cands[0].topic_key)
            if _v is not None:
                typer.secho(
                    f"  rank1 `{cands[0].topic_key}` Δ={_v.abs_delta:+d} "
                    f"p_fwer={_v.p_fwer:.3f} → {_v.verdict}",
                    fg=("green" if _v.verdict == "signal" else "yellow"))

        agent_result = None
        if cands and not no_agent:
            if agent_backend == "local":
                # [偏离 HANDOFF §M6] 见 agent/local_analyst.py 顶部的决策记录。
                from .agent.local_analyst import run_analyst_local

                typer.secho(
                    f"使用本地模型后端（{local_model} @ {local_base_url}）。"
                    "HANDOFF §M6 将本地模型列为 OUT OF SCOPE —— 小模型在多步 tool use 与"
                    "拒绝编造证据上弱于前沿模型。报告 provenance 会如实标注。",
                    fg="yellow")
                typer.echo(f"对 rank 1 ({cands[0].topic_key}) 跑 agent...")
                try:
                    agent_result = run_analyst_local(
                        conn, cands[0], as_of=t.isoformat(), window_days=window,
                        label_version=label_version, model=local_model,
                        base_url=local_base_url,
                        prompt_version=_pick_prompt(null_result, cands[0]),
                        null_result=null_result)
                except Exception as e:  # noqa: BLE001
                    typer.secho(f"agent 失败（报告继续产出）: {e}", fg="yellow")
            else:
                key = settings.anthropic_api_key
                if not key:
                    typer.secho("ANTHROPIC_API_KEY 未配置，跳过 agent 分析。"
                                "报告仍会产出统计部分。"
                                "（无预算可用 --agent-backend local）", fg="yellow")
                else:
                    typer.echo(f"对 rank 1 ({cands[0].topic_key}) 跑 agent...")
                    try:
                        agent_result = run_analyst(
                            conn, cands[0], as_of=t.isoformat(), window_days=window,
                            label_version=label_version, model=settings.agent_model,
                            api_key=key,
                            prompt_version=_pick_prompt(null_result, cands[0]),
                            null_result=null_result)
                    except Exception as e:  # noqa: BLE001
                        typer.secho(f"agent 失败（报告继续产出）: {e}", fg="yellow")

        top_cands = cands[:top]
        report_path = Path(out) if out else (
            settings.report_dir / f"report_{t:%Y%m%dT%H%M%SZ}.md")
        report_path.parent.mkdir(parents=True, exist_ok=True)

        analysis_id = persist_analysis_run(
            conn, as_of=t, label_version=label_version, window_days=window,
            filters={"min_posts": min_posts, "top": top},
            agent_model=(agent_result.model if agent_result else None),
            prompt_version=(agent_result.prompt_version if agent_result else None),
            report_path=report_path, candidates=top_cands, agent_result=agent_result)

        md = render_report(
            analysis_id=analysis_id, as_of=t, window_days=window,
            label_version=label_version, query_version=settings.query_version,
            agent_model=(agent_result.model if agent_result else None),
            prompt_version=(agent_result.prompt_version if agent_result else None),
            candidates=top_cands, health=health, agent_result=agent_result,
            min_posts=min_posts, null_result=null_result,
            broadcast=bcast, broadcast_excluded=exclude_broadcast)

    report_path.write_text(md, encoding="utf-8")
    typer.echo(f"analysis_id={analysis_id}  report={report_path}")
    if health["has_problem"]:
        typer.secho("注意：本窗口存在采集故障或数据空洞，报告顶部有警告。", fg="yellow")


# ---------------------------------------------------------------- health
@app.command("health")
def health_cmd():
    """采集健康检查（HANDOFF §M8）。一条 SQL 覆盖 v0 阶段 90% 的监控需求。"""
    from .health import run_health_check

    typer.echo(run_health_check())


# ---------------------------------------------------------------- eval
eval_app = typer.Typer(help="标注器评估（v1 Phase 0：量化召回与精度）")
app.add_typer(eval_app, name="eval")


@eval_app.command("sample")
def eval_sample(
    label_version: str = typer.Option("rules_v2", "--label-version"),
    per_stratum: int = typer.Option(50, "--per-stratum"),
    out: str = typer.Option("eval/sample.tsv", "--out"),
    seed: str = typer.Option("ccint", "--seed"),
):
    """分层抽样导出待人工标注的 TSV。填 TRUE_cyber / TRUE_canada 两列（y/n）。"""
    from .evaluation import draw_sample, export_tsv, stratum_sizes

    rows = draw_sample(label_version, per_stratum=per_stratum, seed=seed)
    path = export_tsv(rows, Path(out))
    typer.echo(f"抽样 {len(rows)} 条 → {path}")
    typer.echo(f"各层总体大小: {stratum_sizes(label_version)}")
    typer.echo("填好 TRUE_cyber / TRUE_canada（y/n）后跑 `ccint eval score`。")


@eval_app.command("score")
def eval_score(
    truth: str = typer.Option("eval/sample.tsv", "--truth"),
    label_version: str = typer.Option("rules_v2", "--label-version"),
    compare_with: str = typer.Option("", "--compare", help="逗号分隔的其它 version"),
):
    """在人工标注集上计分。"""
    from .evaluation import compare, format_score, load_tsv, score

    rows = load_tsv(Path(truth))
    if not rows:
        typer.secho(f"{truth} 里没有已填写的行（需要 TRUE_cyber / TRUE_canada 都是 y/n）。",
                    fg="red")
        raise typer.Exit(1)
    if compare_with:
        vs = [label_version] + [v.strip() for v in compare_with.split(",") if v.strip()]
        typer.echo(compare(rows, vs))
    else:
        typer.echo(format_score(score(rows, label_version)))


if __name__ == "__main__":
    app()


# ---------------------------------------------------------------- viz
@app.command("viz")
def viz_cmd(
    label_version: str = typer.Option("rules_v2", "--label-version"),
    since: str = typer.Option("", "--since", help="留空 = 全语料"),
    until: str = typer.Option("", "--until"),
    out: str = typer.Option("figures", "--out", help="输出目录"),
    top_n: int = typer.Option(6, "--top-n", help="trajectory 小倍数的 topic 数"),
    roll: int = typer.Option(7, "--roll", help="trajectory 滚动平均天数"),
):
    """产出论文级图表（PDF + PNG）。

    [MUST] 每张涉及 volume 的图都强制画出 raw / non-broadcast / unique authors
    三条序列 —— 只画 raw 会掩盖「表面热度由少数 feed 驱动」这一实测事实。
    """
    from .analytics.broadcast import detect_broadcast_authors
    from .viz import charts
    from .viz import data as D

    outdir = Path(out)
    s = _parse_when(since) if since else None
    u = _parse_when(until) if until else None

    with db.connect(autocommit=True) as conn:
        b = detect_broadcast_authors(conn, label_version=label_version)
        typer.secho(f"广播型账号 {len(b.authors)}/{b.n_total_authors} "
                    f"({b.author_share:.1%})，贡献 {b.post_share:.1%} 的语料",
                    fg="yellow")
        ctx = D.VizContext(label_version, s, u, sorted(b.author_ids))
        made: list = []
        made += charts.raw_vs_organic(D.topic_split(conn, ctx), outdir)
        made += charts.topic_trajectory(D.trajectory(conn, ctx), outdir,
                                        top_n=top_n, roll=roll)
        made += charts.topic_week_heatmap(D.heatmap(conn, ctx), outdir)
        made += charts.actor_topic_network(D.actor_topic(conn, ctx), outdir)
        made += charts.concentration_curve(D.concentration(conn, ctx), outdir,
                                           gini_fn=D.gini)
        made += charts.source_uptake(D.coshare(conn, ctx), outdir)

    for p in sorted(str(x) for x in made if str(x).endswith(".pdf")):
        typer.echo(f"  {p}")
    typer.secho(f"{len([x for x in made if str(x).endswith('.pdf')])} 张图 -> {outdir}/",
                fg="green")


@app.command("viz-compact")
def viz_compact_cmd(
    label_version: str = typer.Option("rules_v2", "--label-version"),
    out: str = typer.Option("figures", "--out"),
):
    """两页摘要用的紧凑版图。

    与 `ccint viz` 出自同一份数据和同一套设计令牌，只是尺寸与取材收紧 ——
    2 页的篇幅放不下全尺寸图，但不能因此换一套口径。
    """
    from .analytics.broadcast import detect_broadcast_authors
    from .viz import charts
    from .viz import data as D

    outdir = Path(out)
    with db.connect(autocommit=True) as conn:
        b = detect_broadcast_authors(conn, label_version=label_version)
        ctx = D.VizContext(label_version, None, None, sorted(b.author_ids))
        made = charts.raw_vs_organic(D.topic_split(conn, ctx), outdir,
                                     name="fig_tldr_raw_vs_organic",
                                     top_n=7, compact=True)
        # [MUST] 这三个是并排对照，不是「最大的三个」：
        # ransomware 84% feed-driven / data_breach 混合 / fraud_financial 1%。
        made += charts.topic_trajectory(
            D.trajectory(conn, ctx), outdir, name="fig_tldr_trajectory",
            topics=["ransomware", "data_breach", "fraud_financial"],
            ncol=3, panel_h=1.32, roll=7)
    for x in made:
        typer.echo(f"  {x}")


# ---------------------------------------------------------------- label-llm
@app.command("label-llm")
def label_llm_cmd(
    base_version: str = typer.Option("rules_v2", "--base-version",
                                     help="继承相关性判定的来源 version"),
    version: str = typer.Option("llm_v1", "--version"),
    base_url: str = typer.Option("http://127.0.0.1:8000/v1", "--base-url"),
    model: str = typer.Option("user", "--model"),
    workers: int = typer.Option(6, "--workers"),
    seed: int = typer.Option(20260921, "--seed"),
    all_: bool = typer.Option(False, "--all", help="重判已判过的"),
    limit: int = typer.Option(0, "--limit", help="0 = 不限"),
):
    """用 LLM 重判主题，写入新的 label_version。

    [MUST] 只重判 topic_key。is_cyber / is_canada / is_relevant 原样继承
    base_version —— 一次只动一个变量，否则 rules_v2 与 llm_v1 的差异无法归因。
    """
    from .labelers import llm_runner

    s = llm_runner.run(base_url=base_url, model=model, base_version=base_version,
                       version=version, seed=seed, workers=workers,
                       only_new=not all_, limit=limit or None)
    typer.echo(str(s))
    if s["n"]:
        typer.secho(f"从 other 召回 {s['recovered_from_other']} 条 "
                    f"({100*s['recovered_from_other']/s['n']:.0f}% of 本批)",
                    fg="green")
    typer.echo("评估：python scripts/eval_llm_topics.py " + version)


# ---------------------------------------------------------------- actors
@app.command("actors")
def actors_cmd(
    label_version: str = typer.Option("rules_v2", "--label-version"),
    profile_version: str = typer.Option("actor_v1", "--profile-version"),
    base_url: str = typer.Option("http://127.0.0.1:8000/v1", "--base-url"),
    model: str = typer.Option("user", "--model"),
    workers: int = typer.Option(8, "--workers"),
    show: bool = typer.Option(False, "--show", help="只展示已有画像，不重跑"),
):
    """作者画像：受众（实测）× 账号性质（内容判定）两个轴。

    [MUST] 两轴分开。旧的 is_broadcast 把「自动化情报 feed」和「会议宣传号」
    压成同一个判断一起剔除 —— 前者是事件 ground truth，后者是生态指标，
    研究价值完全不同。
    """
    from .analytics.actor import MODES, coverage, load_profiles

    if not show:
        from .analytics import actor_runner
        s = actor_runner.run(base_url=base_url, model=model,
                             label_version=label_version,
                             profile_version=profile_version, workers=workers)
        typer.echo(str(s))

    with db.connect(autocommit=True) as conn:
        profs = load_profiles(conn, profile_version)
        cov = coverage(profs)
        rows = conn.execute(
            """SELECT actor_type, count(*) a, sum(n_posts) p
               FROM author_profiles WHERE profile_version=%s
               GROUP BY 1 ORDER BY 3 DESC""", (profile_version,)).fetchall()

    typer.echo(f"\n{cov['n_authors']} 位作者 / {cov['n_posts']} 条 relevant")
    typer.echo(f"{'actor_type':24s} {'作者':>5s} {'帖子':>5s} {'占比':>7s}")
    for r in rows:
        typer.echo(f"{r['actor_type']:24s} {r['a']:5d} {int(r['p']):5d} "
                   f"{100*int(r['p'])/cov['n_posts']:6.1f}%")
    typer.secho(f"\n已归类 {100*cov['classified_share']:.1f}% 的帖子；"
                f"其余样本量不足以判定（不等于无关，故不剔除）", fg="yellow")
    typer.echo("按样本量分层: " + "  ".join(
        f"{k}={v}" for k, v in sorted(cov["posts_by_tier"].items())))
    typer.echo("\n可用的分析 mode：")
    for k, v in MODES.items():
        typer.echo(f"  --actor-mode {k:10s} {v}")
