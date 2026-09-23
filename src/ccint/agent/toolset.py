"""Agent 可调用的工具集合。

[MUST] 这一层**不含任何分析逻辑**。每个工具都是对既有模块的转发:

  list_sources / collect_sources      → collectors.sources · collectors.web · ingest
  get_latest_topics / timeseries      → portal.queries
  get_representative_posts / stats    → agent.tools.TopicTools(既有,签名不动)
  get_actor_composition               → analytics.broadcast.detect_broadcast_authors
  get_related_cves / lookup_cve       → osint.cve · portal.queries
  search_external_intelligence        → external_events(registry)

把分析搬进 agent prompt 会让结论不可复现:同一个问题两次问可能走不同的统计口径。
既有代码是确定性的、单独可测的,agent 只负责决定**调用哪个**,不负责**怎么算**。

[MUST] 每个返回体都带口径字段(window / label_version / 过滤条件)。agent 的回答
要能追溯到源、帖子、窗口、工具结果,这四样缺一不可。
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .. import db
from ..analytics.broadcast import detect_broadcast_authors
from ..portal import queries as pq
from .tools import TopicTools

log = logging.getLogger(__name__)

LABEL_VERSION = pq.LABEL_VERSION
TOPIC_VERSION = pq.TOPIC_VERSION


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _src_clause(sources: list[str] | None) -> tuple[str, list[str] | None]:
    """源过滤片段。None = 全部源;空列表也按全部处理(UI 未选 = 不限制)。"""
    return ("" if not sources else " AND p.source_key = ANY(%(sources)s)"), \
           (sources or None)


# --------------------------------------------------------------------------
# 1. 源
# --------------------------------------------------------------------------
def list_sources() -> dict:
    """列出已配置的源及其采集状态。"""
    from ..collectors import sources as srcs

    version, specs = srcs.load()
    stats = {r["source_key"]: r for r in pq.sources()}
    health = {}
    for r in pq.collection_health(days=30):
        h = health.setdefault(r["source_key"], {"last_run": None, "statuses": {}})
        h["statuses"][r["status"]] = h["statuses"].get(r["status"], 0) + r["runs"]
        if r["last_run"] and (h["last_run"] is None or r["last_run"] > h["last_run"]):
            h["last_run"] = r["last_run"]
    out = []
    for s in specs:
        st, hl = stats.get(s.key, {}), health.get(s.key, {})
        out.append({
            "name": s.key, "type": s.kind, "enabled": s.enabled,
            "endpoint": s.url or s.base_url,
            "authorised": s.authorised,
            "posts": st.get("posts", 0), "relevant": st.get("relevant", 0),
            "first_day": st.get("first_day"), "last_day": st.get("last_day"),
            "last_successful_collection": hl.get("last_run"),
            "collection_status": hl.get("statuses") or {},
        })
    return {"sources_version": version, "sources": out}


def collect_sources(sources: list[str], start_date: str | None = None,
                    end_date: str | None = None) -> dict:
    """对指定的已配置源跑一次采集。只接受 sources.yaml 里已存在的 key。

    [MUST] 不接受任意 URL。让 agent(或界面)能采集任意站点,等于让采集边界
    脱离版本控制,而整个项目的前提是边界必须可追溯。
    """
    from ..collectors import sources as srcs
    from ..collectors import web
    from ..ingest import reap_stale_runs, run_collection

    version, specs = srcs.load()
    known = {s.key: s for s in specs}
    unknown = [s for s in sources if s not in known]
    if unknown:
        return {"error": f"未配置的源: {unknown}。可用: {sorted(known)}",
                "hint": "源必须先写进 config/sources.yaml 并声明 authorised"}
    since = (dt.datetime.fromisoformat(start_date).replace(tzinfo=dt.timezone.utc)
             if start_date else _now() - dt.timedelta(days=7))
    until = (dt.datetime.fromisoformat(end_date).replace(tzinfo=dt.timezone.utc)
             if end_date else _now())
    reap_stale_runs()
    results = []
    for key in sources:
        spec = known[key]
        if spec.kind == "bluesky":
            results.append({"source": key, "skipped":
                            "Bluesky 走独立的 backfill/incremental 流程,不从这里触发"})
            continue
        try:
            with web.build(spec, query_version=version) as c:
                r = run_collection(c, mode="incremental", since=since, until=until,
                                   max_pages_per_term=10)
            results.append({"source": key, **r})
        except PermissionError as e:
            results.append({"source": key, "refused": str(e)})
        except Exception as e:                                    # noqa: BLE001
            results.append({"source": key, "error": f"{type(e).__name__}: {e}"})
    return {"window": [since.isoformat(), until.isoformat()], "results": results}


# --------------------------------------------------------------------------
# 2. 主题
# --------------------------------------------------------------------------
_TOPICS_SQL = """
SELECT CASE WHEN t.post_id IS NULL THEN '(unlabelled)' ELSE coalesce(t.topic_key,'other') END AS topic,
       count(*) AS posts,
       count(DISTINCT p.author_id) AS authors,
       count(DISTINCT p.source_key) AS sources
FROM posts p
JOIN post_labels g ON g.post_id=p.post_id AND g.label_version=%(gv)s AND g.is_relevant
LEFT JOIN post_labels t ON t.post_id=p.post_id AND t.label_version=%(tv)s
WHERE p.published_at >= %(since)s AND p.published_at < %(until)s{src}
GROUP BY 1
"""


def _topic_counts(since, until, sources) -> list[dict]:
    clause, arr = _src_clause(sources)
    with db.connect(autocommit=True) as c:
        return [dict(r) for r in c.execute(
            _TOPICS_SQL.format(src=clause),
            {"gv": LABEL_VERSION, "tv": TOPIC_VERSION,
             "since": since, "until": until, "sources": arr}).fetchall()]


def get_latest_topics(days: int = 14, sources: list[str] | None = None,
                      top_k: int = 10) -> dict:
    """近 N 天最活跃的主题,并与前一个等长窗口对比。

    [MUST] 返回的是中性标签(most_discussed / increasing / decreasing / newly_observed),
    不是「趋势」。这个语料上的置换检验已经测出:没有任何主题变化能与噪声区分。
    把增长直接叫 trend 会制造统计上不存在的结论。
    """
    until = _now()
    since = until - dt.timedelta(days=days)
    prev_since = since - dt.timedelta(days=days)
    cur = {r["topic"]: r for r in _topic_counts(since, until, sources)}
    prev = {r["topic"]: r for r in _topic_counts(prev_since, since, sources)}

    rows = []
    for t, r in cur.items():
        p = prev.get(t, {}).get("posts", 0)
        if p == 0 and r["posts"] > 0:
            label = "newly_observed"
        elif r["posts"] > p:
            label = "increasing_activity"
        elif r["posts"] < p:
            label = "decreasing_activity"
        else:
            label = "unchanged"
        rows.append({**r, "prev_posts": p, "change": r["posts"] - p,
                     "activity_label": label})
    rows.sort(key=lambda r: -r["posts"])
    rows = rows[:top_k]
    for r in rows:                       # 排名第一的额外标注
        r.setdefault("activity_label", "most_discussed")
    if rows:
        rows[0]["activity_label"] = f"most_discussed; {rows[0]['activity_label']}"
    return {
        "window_days": days, "window": [since.isoformat(), until.isoformat()],
        "sources": sources or "all", "topics": rows,
        "label_version": LABEL_VERSION, "topic_version": TOPIC_VERSION,
        "caveat": "Labels describe observed activity only. Permutation testing on "
                  "this corpus found no topic change distinguishable from noise, "
                  "so an increase here is not evidence of a trend.",
    }


_TS_SQL = """
SELECT p.published_at::date AS day,
       count(*) AS posts,
       count(DISTINCT p.author_id) AS authors,
       count(*) FILTER (WHERE p.author_id <> ALL(%(feeds)s)) AS non_broadcast_posts
FROM posts p
JOIN post_labels g ON g.post_id=p.post_id AND g.label_version=%(gv)s AND g.is_relevant
LEFT JOIN post_labels t ON t.post_id=p.post_id AND t.label_version=%(tv)s
WHERE p.published_at >= %(since)s AND p.published_at < %(until)s
  AND (%(topic)s::text IS NULL OR CASE WHEN t.post_id IS NULL THEN '(unlabelled)' ELSE coalesce(t.topic_key,'other') END = %(topic)s::text){src}
GROUP BY 1 ORDER BY 1
"""


def _feed_authors(since, until) -> list[str]:
    """窗口内的广播型作者。直接复用既有的行为式判据,不另立标准。"""
    with db.connect(autocommit=True) as c:
        res = detect_broadcast_authors(c, label_version=LABEL_VERSION,
                                       since=since, until=until)
    return [a.author_id for a in res.authors]


def get_topic_timeseries(topic: str | None = None, days: int = 30,
                         sources: list[str] | None = None) -> dict:
    """主题的逐日活动。同时给三条序列 —— 全部帖 / 非广播帖 / 独立作者。

    [MUST] 三条一起给,因为原始发帖量会被自动情报 feed 主导:本项目实测 3% 的
    账号产出 24.3% 的语料。只看一条曲线会把转载读成关注。
    """
    until = _now()
    since = until - dt.timedelta(days=days)
    feeds = _feed_authors(since, until)
    clause, arr = _src_clause(sources)
    with db.connect(autocommit=True) as c:
        rows = [dict(r) for r in c.execute(
            _TS_SQL.format(src=clause),
            {"gv": LABEL_VERSION, "tv": TOPIC_VERSION, "topic": topic,
             "since": since, "until": until, "sources": arr,
             "feeds": feeds or [""]}).fetchall()]
    return {"topic": topic or "(all topics)", "days": days,
            "sources": sources or "all", "series": rows,
            "n_broadcast_authors_in_window": len(feeds),
            "series_note": "posts = everything; non_broadcast_posts excludes "
                           "behaviourally-detected feed accounts; authors = distinct "
                           "posters. Raw volume is dominated by automated feeds."}


def get_actor_composition(topic: str | None = None, days: int = 14) -> dict:
    """某主题的作者构成:广播/feed 型占多少。复用既有 broadcast 判据。"""
    until = _now()
    since = until - dt.timedelta(days=days)
    with db.connect(autocommit=True) as c:
        res = detect_broadcast_authors(c, label_version=LABEL_VERSION,
                                       since=since, until=until)
        feeds = [a.author_id for a in res.authors]
        clause, arr = _src_clause(None)
        r = dict(c.execute(f"""
            SELECT count(*) AS posts,
                   count(DISTINCT p.author_id) AS authors,
                   count(*) FILTER (WHERE p.author_id = ANY(%(feeds)s)) AS broadcast_posts,
                   count(DISTINCT p.author_id) FILTER (WHERE p.author_id = ANY(%(feeds)s))
                       AS broadcast_authors
            FROM posts p
            JOIN post_labels g ON g.post_id=p.post_id AND g.label_version=%(gv)s
                 AND g.is_relevant
            LEFT JOIN post_labels t ON t.post_id=p.post_id AND t.label_version=%(tv)s
            WHERE p.published_at >= %(since)s AND p.published_at < %(until)s
              AND (%(topic)s::text IS NULL
                   OR CASE WHEN t.post_id IS NULL THEN '(unlabelled)' ELSE coalesce(t.topic_key,'other') END = %(topic)s::text){clause}""",
            {"gv": LABEL_VERSION, "tv": TOPIC_VERSION, "topic": topic,
             "since": since, "until": until, "feeds": feeds or [""],
             "sources": arr}).fetchall()[0])
    share = (r["broadcast_posts"] / r["posts"]) if r["posts"] else 0.0
    return {
        "topic": topic or "(all topics)", "window_days": days, **r,
        "broadcast_post_share": round(share, 4),
        "method": "behavioral_v1: n_posts>=5 and n_active_days>=4 and "
                  "zero-engagement rate>=0.6 over posts with >=24h settling time",
        "reading": ("most volume is produced by feed-like accounts"
                    if share >= 0.5 else
                    "volume is mixed" if share >= 0.2 else
                    "volume is mostly from non-feed accounts"),
    }


def inspect_topic(topic: str, days: int = 14,
                  sources: list[str] | None = None) -> dict:
    """一个主题的完整画像:统计 + 构成 + 时间序列 + 代表帖 + 实体 + CVE。"""
    until = _now()
    since = until - dt.timedelta(days=days)
    clause, arr = _src_clause(sources)
    with db.connect(autocommit=True) as c:
        by_src = [dict(r) for r in c.execute(f"""
            SELECT p.source_key, count(*) AS posts,
                   count(DISTINCT p.author_id) AS authors
            FROM posts p
            JOIN post_labels g ON g.post_id=p.post_id AND g.label_version=%(gv)s
                 AND g.is_relevant
            LEFT JOIN post_labels t ON t.post_id=p.post_id AND t.label_version=%(tv)s
            WHERE p.published_at >= %(since)s AND p.published_at < %(until)s
              AND coalesce(t.topic_key,'other') = %(topic)s{clause}
            GROUP BY 1 ORDER BY 2 DESC""",
            {"gv": LABEL_VERSION, "tv": TOPIC_VERSION, "topic": topic,
             "since": since, "until": until, "sources": arr}).fetchall()]
        ents = [dict(r) for r in c.execute(f"""
            SELECT e.entity_type, e.entity_text, count(DISTINCT e.post_id) AS posts
            FROM post_entities e
            JOIN posts p USING (post_id)
            JOIN post_labels t ON t.post_id=p.post_id AND t.label_version=%(tv)s
            WHERE p.published_at >= %(since)s AND p.published_at < %(until)s
              AND CASE WHEN t.post_id IS NULL THEN '(unlabelled)' ELSE coalesce(t.topic_key,'other') END = %(topic)s
            GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20""",
            {"tv": TOPIC_VERSION, "topic": topic,
             "since": since, "until": until}).fetchall()]
    return {
        "topic": topic, "window_days": days, "sources": sources or "all",
        "by_source": by_src,
        "actor_composition": get_actor_composition(topic, days=days),
        "timeseries": get_topic_timeseries(topic, days=max(days, 30), sources=sources),
        "representative_posts": get_representative_posts(topic, days=days, limit=8),
        "entities": ents,
        "related_cves": get_related_cves(topic=topic, days=days).get("cves", [])[:10],
    }


def get_representative_posts(topic: str, days: int = 14, limit: int = 10) -> dict:
    """代表性帖子。复用既有 TopicTools 的确定性选取(engagement 排序 + 作者去重)。"""
    with db.connect(autocommit=True) as c:
        tt = TopicTools(c, LABEL_VERSION)
        try:
            out = tt.get_representative_posts(topic, window_days=days,
                                              as_of=_now().isoformat(), limit=limit)
        except Exception as e:                                    # noqa: BLE001
            return {"topic": topic, "error": f"{type(e).__name__}: {e}", "posts": []}
    posts = out.get("posts", out) if isinstance(out, dict) else out
    ids = [p["post_id"] for p in posts] if posts else []
    if ids:
        with db.connect(autocommit=True) as c:
            cmap: dict[int, list[str]] = {}
            for r in c.execute("select post_id, cve_id from post_cves "
                               "where post_id = any(%s)", (ids,)):
                cmap.setdefault(r["post_id"], []).append(r["cve_id"])
        for p in posts:
            p["cves"] = cmap.get(p["post_id"], [])
    return {"topic": topic, "window_days": days, "posts": posts,
            "selection": "ranked by engagement, one post per author, "
                         "low-information posts excluded"}


# --------------------------------------------------------------------------
# 3. CVE / OSINT
# --------------------------------------------------------------------------
def get_related_cves(topic: str | None = None, post_id: int | None = None,
                     days: int = 14) -> dict:
    """某主题或某帖关联的 CVE,带 NVD 严重性与 CISA KEV 状态。"""
    if post_id is not None:
        with db.connect(autocommit=True) as c:
            rows = [dict(r) for r in c.execute("""
                SELECT c.cve_id, r.cvss_score, r.cvss_severity, r.cvss_version,
                       coalesce(r.known_exploited,false) AS known_exploited,
                       r.kev_date_added, r.description
                FROM post_cves c LEFT JOIN cve_records r USING (cve_id)
                WHERE c.post_id = %s ORDER BY 1""", (post_id,)).fetchall()]
        return {"post_id": post_id, "cves": rows, "note": pq.top_cves(days=1, limit=1)["note"]}
    until = _now()
    since = until - dt.timedelta(days=days)
    with db.connect(autocommit=True) as c:
        rows = [dict(r) for r in c.execute("""
            SELECT c.cve_id, count(DISTINCT c.post_id) AS posts,
                   r.cvss_score, r.cvss_severity, r.cvss_version,
                   coalesce(r.known_exploited,false) AS known_exploited,
                   r.kev_date_added, r.description
            FROM post_cves c
            JOIN posts p USING (post_id)
            LEFT JOIN post_labels t ON t.post_id=p.post_id AND t.label_version=%(tv)s
            LEFT JOIN cve_records r ON r.cve_id = c.cve_id
            WHERE p.published_at >= %(since)s AND p.published_at < %(until)s
              AND (%(topic)s::text IS NULL
                   OR CASE WHEN t.post_id IS NULL THEN '(unlabelled)' ELSE coalesce(t.topic_key,'other') END = %(topic)s::text)
            GROUP BY c.cve_id, r.cvss_score, r.cvss_severity, r.cvss_version,
                     r.known_exploited, r.kev_date_added, r.description
            ORDER BY 2 DESC LIMIT 25""",
            {"tv": TOPIC_VERSION, "topic": topic,
             "since": since, "until": until}).fetchall()]
    return {"topic": topic or "(all)", "window_days": days, "cves": rows,
            "note": "CVSS is theoretical severity from NVD; KEV means CISA has "
                    "observed exploitation. They are independent and are not "
                    "combined into a single score."}


def lookup_cve(cve_id: str) -> dict:
    """单个 CVE 的详情 + 语料里提到它的帖子。"""
    cid = cve_id.upper().strip()
    with db.connect(autocommit=True) as c:
        rec = c.execute("select * from cve_records where cve_id=%s", (cid,)).fetchall()
        n = c.execute("select count(distinct post_id) n from post_cves where cve_id=%s",
                      (cid,)).fetchall()[0]["n"]
    return {"cve_id": cid,
            "record": dict(rec[0]) if rec else None,
            "enriched": bool(rec and rec[0]["source"] == "nvd"),
            "corpus_mentions": n,
            "posts": pq.cve_posts(cid, limit=10),
            "note": None if rec else "尚未富化 —— 语料里提到过但还没向 NVD 查询"}


def search_external_intelligence(entity_or_topic: str) -> dict:
    """在外部事件登记表里按实体名或关键词检索。"""
    q = f"%{entity_or_topic.strip()}%"
    with db.connect(autocommit=True) as c:
        events = [dict(r) for r in c.execute("""
            SELECT v.event_id, v.source, v.published_at_utc, v.title, v.country,
                   v.event_type, v.url
            FROM external_events v
            WHERE v.title ILIKE %(q)s OR EXISTS (
                SELECT 1 FROM external_event_entities e
                WHERE e.event_id = v.event_id AND e.entity_text ILIKE %(q)s)
            ORDER BY v.published_at_utc DESC LIMIT 15""", {"q": q}).fetchall()]
        cves = [dict(r) for r in c.execute("""
            SELECT cve_id, cvss_score, cvss_severity, known_exploited, description
            FROM cve_records
            WHERE description ILIKE %(q)s OR cve_id ILIKE %(q)s
            ORDER BY known_exploited DESC, cvss_score DESC NULLS LAST LIMIT 10""",
            {"q": q}).fetchall()]
    return {"query": entity_or_topic, "registry_events": events,
            "cve_records": cves,
            "sources": "ransomware.live incident registry · NVD · CISA KEV",
            "note": "The registry covers ransomware leak-site victims only; "
                    "absence here is not evidence that no incident occurred."}


# --------------------------------------------------------------------------
# 分发表
# --------------------------------------------------------------------------
REGISTRY: dict[str, Any] = {
    "list_sources": list_sources,
    "collect_sources": collect_sources,
    "get_latest_topics": get_latest_topics,
    "get_topic_timeseries": get_topic_timeseries,
    "inspect_topic": inspect_topic,
    "get_representative_posts": get_representative_posts,
    "get_actor_composition": get_actor_composition,
    "get_related_cves": get_related_cves,
    "lookup_cve": lookup_cve,
    "search_external_intelligence": search_external_intelligence,
}
