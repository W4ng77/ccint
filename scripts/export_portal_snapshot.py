"""把门户需要的数据导出成一个 JSON 快照,供 claude.ai 上的 Signal Desk artifact 使用。

artifact 在浏览器沙箱里运行,连不到本机的 pgserver,所以门户读的是这份快照。
[MUST] 快照自带口径:label_version、topic_version、覆盖到哪天、每个窗口的起止。
判定门(llm_v2c)只覆盖到 ``labelled_through``;之后采到的帖只计入原始采集量,
不进入主题排名 —— 不能把「还没判」当成「不相关」。

CVE 链接是纯正则,不依赖判定门,所以 CVE 面板用真实的最近 14 个日历日。

用法:
    .venv/bin/python scripts/export_portal_snapshot.py --out outputs/portal_artifact/data.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib

import yaml

from ccint import db
from ccint.collectors import sources as srcs

GATE = "llm_v2c"
TOPIC = "topic_v2"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def rows(c, sql, **p):
    return [dict(r) for r in c.execute(sql, p).fetchall()]


def iso(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return v


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if hasattr(o, "is_finite"):          # Decimal
        return float(o)
    return iso(o)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="outputs/portal_artifact/data.json")
    ap.add_argument("--window", type=int, default=14)
    ap.add_argument("--top-cves", type=int, default=200)
    a = ap.parse_args()
    W = a.window

    tax = yaml.safe_load((ROOT / "config/taxonomy.yaml").read_text())
    topics_meta = [{"key": t["key"], "description": t.get("llm_description", "")}
                   for t in tax["topics"]]
    src_version, specs = srcs.load()

    with db.connect(autocommit=True) as c:
        daily = rows(c, """
            SELECT p.published_at::date AS day, p.source_key AS source,
                   count(*) AS posts, count(g.post_id) AS gated,
                   count(*) FILTER (WHERE g.is_relevant) AS relevant
            FROM posts p
            LEFT JOIN post_labels g ON g.post_id=p.post_id AND g.label_version=%(g)s
            GROUP BY 1,2 ORDER BY 1,2""", g=GATE)
        by_day: dict = {}
        for r in daily:
            d = by_day.setdefault(r["day"], {"posts": 0, "gated": 0})
            d["posts"] += r["posts"]; d["gated"] += r["gated"]
        days = sorted(by_day)
        collected_through = days[-1]
        # 判定门覆盖率 >= 90% 的最后一天
        labelled_through = max(d for d in days if by_day[d]["gated"] >= 0.9 * by_day[d]["posts"])
        first_day = days[0]

        w_end = labelled_through
        w_start = w_end - dt.timedelta(days=W - 1)
        p_end = w_start - dt.timedelta(days=1)
        p_start = p_end - dt.timedelta(days=W - 1)
        c_end = collected_through
        c_start = c_end - dt.timedelta(days=W - 1)
        cp_end = c_start - dt.timedelta(days=1)
        cp_start = cp_end - dt.timedelta(days=W - 1)

        topic_daily = rows(c, """
            SELECT coalesce(t.topic_key,'other') AS topic, p.published_at::date AS day,
                   count(*) AS posts, count(DISTINCT p.author_id) AS authors
            FROM posts p
            JOIN post_labels g ON g.post_id=p.post_id AND g.label_version=%(g)s AND g.is_relevant
            JOIN post_labels t ON t.post_id=p.post_id AND t.label_version=%(t)s
            WHERE p.published_at::date <= %(e)s
            GROUP BY 1,2 ORDER BY 2,1""", g=GATE, t=TOPIC, e=labelled_through)

        profiles = {r["author_id"]: r for r in rows(c, """
            SELECT author_id, actor_type, function, audience FROM author_profiles
            WHERE profile_version='actor_v1'""")}

        posts = rows(c, """
            SELECT p.post_id AS id, p.source_key AS src, p.author_id, p.author_handle AS handle,
                   p.published_at AS ts, p.url, left(p.text, 420) AS text,
                   coalesce(t.topic_key,'other') AS topic,
                   p.like_count AS likes, p.repost_count AS reposts, p.reply_count AS replies,
                   coalesce(array_agg(DISTINCT c.cve_id) FILTER (WHERE c.cve_id IS NOT NULL),'{}') AS cves
            FROM posts p
            JOIN post_labels g ON g.post_id=p.post_id AND g.label_version=%(g)s AND g.is_relevant
            JOIN post_labels t ON t.post_id=p.post_id AND t.label_version=%(t)s
            LEFT JOIN post_cves c ON c.post_id=p.post_id
            GROUP BY p.post_id, t.topic_key
            ORDER BY p.published_at DESC""", g=GATE, t=TOPIC)
        for p in posts:
            prof = profiles.get(p.pop("author_id"))
            p["actor"] = prof["function"] if prof else None
            p["actor_type"] = prof["actor_type"] if prof else None

        # 窗口内每个主题的作者集中度
        conc = {}
        for p in posts:
            d = dt.date.fromisoformat(str(p["ts"])[:10]) if isinstance(p["ts"], str) else p["ts"].date()
            if not (w_start <= d <= w_end):
                continue
            conc.setdefault(p["topic"], {}).setdefault(p["handle"], 0)
            conc[p["topic"]][p["handle"]] += 1
        feed_share = {}
        for p in posts:
            d = p["ts"].date()
            if w_start <= d <= w_end:
                fs = feed_share.setdefault(p["topic"], [0, 0])
                fs[1] += 1
                if p["actor"] == "incident_feed":
                    fs[0] += 1

        cve_top = rows(c, """
            WITH w AS (
              SELECT c.cve_id, c.post_id, p.published_at::date AS day, p.source_key
              FROM post_cves c JOIN posts p USING (post_id)
              WHERE p.published_at::date BETWEEN %(s)s AND %(e)s)
            SELECT w.cve_id, count(DISTINCT w.post_id) AS posts,
                   count(DISTINCT w.source_key) AS sources, max(w.day) AS last_seen,
                   r.cvss_score, r.cvss_severity, r.cvss_version, r.published_at AS nvd_published,
                   coalesce(r.known_exploited,false) AS kev, r.kev_date_added,
                   left(r.description, 600) AS description, r.source AS enrichment
            FROM w LEFT JOIN cve_records r USING (cve_id)
            GROUP BY w.cve_id, r.cvss_score, r.cvss_severity, r.cvss_version, r.published_at,
                     r.known_exploited, r.kev_date_added, r.description, r.source
            ORDER BY posts DESC, w.cve_id LIMIT %(n)s""",
            s=c_start, e=c_end, n=a.top_cves)
        ids = [r["cve_id"] for r in cve_top]
        cdaily = rows(c, """
            SELECT c.cve_id, p.published_at::date AS day, count(DISTINCT c.post_id) AS n
            FROM post_cves c JOIN posts p USING (post_id)
            WHERE c.cve_id = ANY(%(ids)s) AND p.published_at::date BETWEEN %(s)s AND %(e)s
            GROUP BY 1,2""", ids=ids, s=cp_start, e=c_end)
        cmentions = rows(c, """
            SELECT * FROM (
              SELECT c.cve_id, p.post_id AS id, p.source_key AS src, p.author_handle AS handle,
                     p.published_at AS ts, p.url, left(p.text, 300) AS text,
                     coalesce(g.is_relevant,false) AS canada,
                     row_number() OVER (PARTITION BY c.cve_id
                        ORDER BY coalesce(g.is_relevant,false) DESC, p.published_at DESC) AS rk
              FROM post_cves c JOIN posts p USING (post_id)
              LEFT JOIN post_labels g ON g.post_id=p.post_id AND g.label_version=%(g)s
              WHERE c.cve_id = ANY(%(ids)s) AND p.published_at::date BETWEEN %(s)s AND %(e)s) x
            WHERE rk <= 4""", ids=ids, s=c_start, e=c_end, g=GATE)
        for r in cve_top:
            dm = {x["day"]: x["n"] for x in cdaily if x["cve_id"] == r["cve_id"]}
            r["daily"] = [dm.get(c_start + dt.timedelta(days=i), 0) for i in range(W)]
            r["prev_posts"] = sum(dm.get(cp_start + dt.timedelta(days=i), 0) for i in range(W))
            r["mentions"] = [{k: v for k, v in m.items() if k not in ("cve_id", "rk")}
                             for m in cmentions if m["cve_id"] == r["cve_id"]]
        cve_totals = rows(c, """
            SELECT count(DISTINCT c.post_id) AS posts, count(DISTINCT c.cve_id) AS cves,
                   count(DISTINCT c.cve_id) FILTER (WHERE r.known_exploited) AS kev
            FROM post_cves c JOIN posts p USING (post_id)
            LEFT JOIN cve_records r USING (cve_id)
            WHERE p.published_at::date BETWEEN %(s)s AND %(e)s""", s=c_start, e=c_end)[0]

        # ransomware.live 加拿大受害者 × 帖子(机构名或域名实体匹配)
        events = rows(c, """
            SELECT e.event_id, e.published_at_utc AS ts, e.title AS victim,
                   split_part(e.event_id, ':', 3) AS grp,
                   (SELECT string_agg(DISTINCT x.entity_text, ' ') FROM external_event_entities x
                     WHERE x.event_id=e.event_id AND x.entity_type='domain') AS domain
            FROM external_events e WHERE e.country='CA' ORDER BY e.published_at_utc DESC""")
        ev_posts = rows(c, """
            SELECT DISTINCT ON (x.event_id, p.post_id) x.event_id, p.post_id AS id,
                   p.source_key AS src, p.author_handle AS handle, p.published_at AS ts, p.url,
                   left(p.text, 300) AS text, x.entity_type AS matched_on
            FROM external_events e
            JOIN external_event_entities x ON x.event_id=e.event_id
            JOIN posts p ON p.published_at >= e.published_at_utc - interval '14 days'
                        AND p.published_at <= e.published_at_utc + interval '30 days'
                        AND (lower(p.text) LIKE '%%' || lower(x.entity_text) || '%%')
            WHERE e.country='CA' AND length(x.entity_text) >= 6""")
        for e in events:
            e["posts"] = [{k: v for k, v in p.items() if k != "event_id"}
                          for p in ev_posts if p["event_id"] == e["event_id"]]

        runs = rows(c, """
            SELECT source_key AS source, started_at::date AS day, status,
                   count(*) AS runs, coalesce(sum(n_inserted),0) AS inserted,
                   max(ended_at) AS last_run
            FROM collection_runs WHERE started_at::date >= %(s)s
            GROUP BY 1,2,3 ORDER BY 2,1""", s=cp_start)
        last_run = {r["source_key"]: r for r in rows(c, """
            SELECT DISTINCT ON (source_key) source_key, ended_at, status, n_inserted
            FROM collection_runs ORDER BY source_key, started_at DESC""")}

    per_source = {}
    for r in daily:
        s = per_source.setdefault(r["source"], {"posts": 0, "relevant": 0, "first": r["day"], "last": r["day"]})
        s["posts"] += r["posts"]; s["relevant"] += r["relevant"]
        s["first"] = min(s["first"], r["day"]); s["last"] = max(s["last"], r["day"])
    sources = []
    for sp in specs:
        sources.append({"key": sp.key, "kind": sp.kind, "enabled": sp.enabled,
                        "endpoint": sp.url or sp.base_url, "authorised": sp.authorised,
                        "stats": per_source.get(sp.key), "last_run": last_run.get(sp.key)})

    out = {
        "meta": {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "gate_version": GATE, "topic_version": TOPIC, "sources_version": src_version,
            "first_day": first_day, "collected_through": collected_through,
            "labelled_through": labelled_through,
            "topic_window": [w_start, w_end], "topic_prev_window": [p_start, p_end],
            "cve_window": [c_start, c_end], "cve_prev_window": [cp_start, cp_end],
            "window_days": W,
        },
        "topics": topics_meta,
        "daily": daily,
        "topic_daily": topic_daily,
        "topic_concentration": {t: sorted(h.items(), key=lambda x: -x[1])[:8] for t, h in conc.items()},
        "topic_feed_share": feed_share,
        "posts": posts,
        "cves": cve_top, "cve_totals": cve_totals,
        "ransomware_events": events,
        "sources": sources, "runs": runs,
    }
    path = ROOT / a.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean(out), ensure_ascii=False, separators=(",", ":")))
    print(f"wrote {path} ({path.stat().st_size/1e6:.2f} MB): {len(posts)} relevant posts, "
          f"{len(cve_top)} CVEs, {len(events)} CA ransomware events; "
          f"topic window {w_start}..{w_end}, cve window {c_start}..{c_end}")


if __name__ == "__main__":
    main()
