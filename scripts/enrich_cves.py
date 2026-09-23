"""按重要性富化 CVE。无 API key 时 NVD 限 5 次/30 秒,不可能全量。

优先级:出现在 relevant 帖里的 > 已被实际利用(KEV)且被提到的 > 被多帖提到的。
这个顺序就是「对分析有用」的顺序,不是「容易拿」的顺序。
"""
import argparse, logging, sys
from ccint import db
from ccint.osint import cve

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
ap = argparse.ArgumentParser()
ap.add_argument("--min-posts", type=int, default=5)
ap.add_argument("--api-key", default=None)
a = ap.parse_args()

SEL = """
SELECT c.cve_id, count(DISTINCT c.post_id) AS posts,
       bool_or(coalesce(r.known_exploited,false)) AS kev,
       bool_or(l.is_relevant) AS in_relevant
FROM post_cves c
LEFT JOIN cve_records r USING (cve_id)
LEFT JOIN post_labels l ON l.post_id = c.post_id AND l.label_version = 'llm_v2c'
GROUP BY 1
HAVING bool_or(l.is_relevant) OR bool_or(coalesce(r.known_exploited,false))
    OR count(DISTINCT c.post_id) >= %(m)s
ORDER BY 4 DESC NULLS LAST, 3 DESC, 2 DESC
"""
with db.connect(autocommit=True) as c:
    rows = c.execute(SEL, {"m": a.min_posts}).fetchall()
    have = {r["cve_id"] for r in c.execute(
        "select cve_id from cve_records where source='nvd'")}
todo = [r["cve_id"] for r in rows if r["cve_id"] not in have]
print(f"候选 {len(rows)}，待富化 {len(todo)}", flush=True)
if todo:
    print(cve.enrich(api_key=a.api_key, limit=len(todo)), flush=True)
