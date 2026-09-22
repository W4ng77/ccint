"""把机械校验应用到已存的实体上,派生一个新的 label_version。

不需要再调一次模型:校验用的两条规则(实体是否出现在原文、域名是否加拿大顶级域)
都是确定性的,输入已经在库里。
"""
from __future__ import annotations

import argparse

from psycopg.types.json import Jsonb

from ccint import db
from ccint.labeling import _UPSERT
from ccint.labelers.llm_v2 import validate_entities

ap = argparse.ArgumentParser()
ap.add_argument("--src", default="llm_v2b")
ap.add_argument("--dst", default="llm_v2c")
a = ap.parse_args()

with db.connect(autocommit=True) as c:
    rows = [dict(r) for r in c.execute("""
        select l.post_id, l.is_cyber, l.matched_terms, p.text
        from post_labels l join posts p using(post_id)
        where l.label_version=%s""", (a.src,))]
print(f"{a.src}: {len(rows)} 行")

n_rel = n_drop = n_lost = 0
with db.connect(autocommit=False) as conn, conn.cursor() as cur:
    cur.execute("delete from post_labels where label_version=%s", (a.dst,))
    cur.execute("delete from post_entities where label_version=%s", (a.dst,))
    buf = []
    for r in rows:
        mt = r["matched_terms"] or {}
        ents = mt.get("canada_entities") or []
        keep, drop = validate_entities(r["text"] or "", ents)
        n_drop += len(drop)
        was = bool(r["is_cyber"]) and bool(ents)
        now = bool(r["is_cyber"]) and bool(keep)
        n_rel += now
        n_lost += (was and not now)
        buf.append({"post_id": r["post_id"], "label_version": a.dst,
                    "is_cyber": r["is_cyber"], "is_canada": bool(keep),
                    "is_relevant": now, "topic_key": None,
                    "is_low_information": False,
                    "matched_terms": Jsonb({**mt, "canada_entities": keep,
                                            "dropped_entities": drop,
                                            "derived_from": a.src,
                                            "validation": "mechanical_v1"}),
                    "confidence": None})
        if len(buf) >= 2000:
            cur.executemany(_UPSERT, buf); buf.clear()
    if buf:
        cur.executemany(_UPSERT, buf)
    conn.commit()
print(f"{a.dst}: relevant {n_rel}  丢弃实体 {n_drop}  因校验失去 relevant 的帖 {n_lost}")
