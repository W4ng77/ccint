"""门户的数据层。所有 SQL 集中在这里,前端只消费 JSON。

[MUST] 每个返回体都带 ``label_version`` / ``as_of`` / 口径说明。门户是这个项目
第一个真正有读者的输出,而读者看不到 provenance 表 —— 如果界面上的数字不自带
口径,它们就会被当成无条件的事实,而这个项目的全部要点是数字必须带边界。
"""
from __future__ import annotations

import datetime as dt

from .. import db

LABEL_VERSION = "llm_v2c"       # 当前判定门
TOPIC_VERSION = "llm_v1"        # 当前主题标注


def _rows(sql: str, **p) -> list[dict]:
    with db.connect(autocommit=True) as c:
        return [dict(r) for r in c.execute(sql, p).fetchall()]


def _one(sql: str, **p) -> dict:
    r = _rows(sql, **p)
    return r[0] if r else {}


def overview() -> dict:
    """顶部概览。"""
    s = _one("""
        SELECT count(*) AS posts,
               count(DISTINCT author_id) AS authors,
               min(published_at)::date AS first_day,
               max(published_at)::date AS last_day,
               count(DISTINCT source_key) AS sources
        FROM posts""")
    rel = _one("""
        SELECT count(*) FILTER (WHERE is_relevant) AS relevant
        FROM post_labels WHERE label_version = %(v)s""", v=LABEL_VERSION)
    cve = _one("""
        SELECT count(DISTINCT cve_id) AS cves,
               count(DISTINCT cve_id) FILTER (WHERE kev) AS kev
        FROM (SELECT c.cve_id, coalesce(r.known_exploited,false) AS kev
              FROM post_cves c LEFT JOIN cve_records r USING (cve_id)) t""")
    return {**s, **rel, **cve, "label_version": LABEL_VERSION,
            "as_of": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


def sources() -> list[dict]:
    """每个源的产出。让「我们在看哪些论坛」本身可见。"""
    return _rows("""
        SELECT p.source_key,
               count(*) AS posts,
               count(*) FILTER (WHERE l.is_relevant) AS relevant,
               min(p.published_at)::date AS first_day,
               max(p.published_at)::date AS last_day
        FROM posts p
        LEFT JOIN post_labels l
               ON l.post_id = p.post_id AND l.label_version = %(v)s
        GROUP BY 1 ORDER BY 2 DESC""", v=LABEL_VERSION)


def topics_over_time(days: int = 60, relevant_only: bool = True) -> dict:
    """主题 × 日期。返回长表,前端做小倍数。"""
    rows = _rows("""
        SELECT coalesce(t.topic_key, 'other') AS topic,
               p.published_at::date AS day,
               count(*) AS posts
        FROM posts p
        JOIN post_labels t ON t.post_id = p.post_id AND t.label_version = %(tv)s
        LEFT JOIN post_labels g ON g.post_id = p.post_id AND g.label_version = %(gv)s
        WHERE p.published_at >= now() - make_interval(days => %(d)s)
          AND (%(ro)s = false OR g.is_relevant)
        GROUP BY 1, 2 ORDER BY 2, 1""",
        tv=TOPIC_VERSION, gv=LABEL_VERSION, d=days, ro=relevant_only)
    return {"series": rows, "days": days, "relevant_only": relevant_only,
            "topic_version": TOPIC_VERSION, "label_version": LABEL_VERSION}


def recent_topics(days: int = 14) -> dict:
    """近 N 天的主题排名,并与前一个等长窗口对比。

    [MUST] 变化量只作**描述**呈现,不标显著性。这个项目已经用置换检验测过:
    在当前样本量上,没有任何主题变化能与噪声区分。界面上出现一个未加限定的
    「↑ 56%」会直接制造出统计上不存在的结论。
    """
    cur = _rows("""
        SELECT coalesce(t.topic_key,'other') AS topic, count(*) AS posts,
               count(DISTINCT p.author_id) AS authors
        FROM posts p
        JOIN post_labels t ON t.post_id=p.post_id AND t.label_version=%(tv)s
        JOIN post_labels g ON g.post_id=p.post_id AND g.label_version=%(gv)s
        WHERE g.is_relevant AND p.published_at >= now() - make_interval(days => %(d)s)
        GROUP BY 1""", tv=TOPIC_VERSION, gv=LABEL_VERSION, d=days)
    prev = _rows("""
        SELECT coalesce(t.topic_key,'other') AS topic, count(*) AS posts
        FROM posts p
        JOIN post_labels t ON t.post_id=p.post_id AND t.label_version=%(tv)s
        JOIN post_labels g ON g.post_id=p.post_id AND g.label_version=%(gv)s
        WHERE g.is_relevant
          AND p.published_at >= now() - make_interval(days => %(d2)s)
          AND p.published_at <  now() - make_interval(days => %(d)s)
        GROUP BY 1""", tv=TOPIC_VERSION, gv=LABEL_VERSION, d=days, d2=days * 2)
    pm = {r["topic"]: r["posts"] for r in prev}
    out = [{**r, "prev_posts": pm.get(r["topic"], 0),
            "delta": r["posts"] - pm.get(r["topic"], 0)} for r in cur]
    out.sort(key=lambda r: -r["posts"])
    return {"window_days": days, "topics": out,
            "caveat": "Differences are descriptive only. Permutation testing on "
                      "this corpus found no topic change distinguishable from "
                      "noise; treat movement as unexplained variation."}


def recent_posts(days: int = 14, topic: str | None = None,
                 limit: int = 40) -> list[dict]:
    return _rows("""
        SELECT p.post_id, p.source_key, p.author_handle, p.published_at, p.url,
               left(p.text, 320) AS text,
               coalesce(t.topic_key,'other') AS topic,
               coalesce(array_agg(DISTINCT c.cve_id)
                        FILTER (WHERE c.cve_id IS NOT NULL), '{}') AS cves
        FROM posts p
        JOIN post_labels g ON g.post_id=p.post_id AND g.label_version=%(gv)s
        LEFT JOIN post_labels t ON t.post_id=p.post_id AND t.label_version=%(tv)s
        LEFT JOIN post_cves c ON c.post_id = p.post_id
        WHERE g.is_relevant
          AND p.published_at >= now() - make_interval(days => %(d)s)
          AND (%(topic)s::text IS NULL OR coalesce(t.topic_key,'other') = %(topic)s::text)
        GROUP BY p.post_id, t.topic_key
        ORDER BY p.published_at DESC LIMIT %(lim)s""",
        gv=LABEL_VERSION, tv=TOPIC_VERSION, d=days, topic=topic, lim=limit)


def top_cves(days: int = 14, limit: int = 25) -> dict:
    """被讨论最多的 CVE,挂上 NVD 严重性与 CISA KEV 状态。

    [MUST] CVSS 与 KEV 分开显示,不合成风险分。前者是理论严重性,后者是
    「有人真的在用它」—— 合成之后读者就无法判断哪部分是外部观测。
    """
    rows = _rows("""
        SELECT c.cve_id,
               count(DISTINCT c.post_id) AS posts,
               count(DISTINCT p.source_key) AS sources,
               max(p.published_at) AS last_seen,
               r.cvss_score, r.cvss_severity, r.cvss_version,
               coalesce(r.known_exploited,false) AS known_exploited,
               r.kev_date_added, r.description, r.source AS enrichment
        FROM post_cves c
        JOIN posts p USING (post_id)
        LEFT JOIN cve_records r ON r.cve_id = c.cve_id
        WHERE p.published_at >= now() - make_interval(days => %(d)s)
        GROUP BY c.cve_id, r.cvss_score, r.cvss_severity, r.cvss_version,
                 r.known_exploited, r.kev_date_added, r.description, r.source
        ORDER BY 2 DESC, 1 LIMIT %(lim)s""", d=days, lim=limit)
    return {"window_days": days, "cves": rows,
            "note": "CVSS is theoretical severity from NVD; KEV means CISA has "
                    "observed exploitation. They are independent signals and are "
                    "deliberately not combined into one score."}


def cve_posts(cve_id: str, limit: int = 30) -> list[dict]:
    return _rows("""
        SELECT p.post_id, p.source_key, p.author_handle, p.published_at, p.url,
               left(p.text, 320) AS text
        FROM post_cves c JOIN posts p USING (post_id)
        WHERE c.cve_id = %(cid)s
        ORDER BY p.published_at DESC LIMIT %(lim)s""",
        cid=cve_id.upper(), lim=limit)


def collection_health(days: int = 14) -> list[dict]:
    """采集健康。系统故障必须与讨论减少可区分,所以它和内容并列展示。"""
    return _rows("""
        SELECT source_key, mode, status, count(*) AS runs,
               max(ended_at) AS last_run, sum(n_inserted) AS inserted
        FROM collection_runs
        WHERE started_at >= now() - make_interval(days => %(d)s)
        GROUP BY 1,2,3 ORDER BY 1,2,3""", d=days)
