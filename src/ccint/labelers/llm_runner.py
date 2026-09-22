"""llm_v1 的批量执行器。

与规则 labeler 分开的原因：它有网络 I/O、需要并发、只作用于 relevant 子集，
且必须在失败时保留已完成的部分（650 条重跑一次不贵，但没必要浪费）。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from psycopg.types.json import Jsonb

from .. import db
from ..labeling import _UPSERT
from .llm_v1 import LABEL_VERSION, classify, to_label

log = logging.getLogger(__name__)

# 只取 rules_v2 已判 relevant 的帖子，并带上它的相关性三元组供继承
_SELECT = """
SELECT p.post_id, p.text, p.lang,
       l.is_cyber, l.is_canada, l.is_relevant, l.is_low_information,
       COALESCE(l.topic_key,'other') AS rules_topic
FROM posts p
JOIN post_labels l ON l.post_id = p.post_id
WHERE l.label_version = %(base)s AND l.is_relevant
  AND (%(only_new)s = false OR NOT EXISTS (
        SELECT 1 FROM post_labels x
        WHERE x.post_id = p.post_id AND x.label_version = %(version)s))
ORDER BY p.post_id
"""


def run(*, base_url: str, model: str, base_version: str = "rules_v2",
        version: str = LABEL_VERSION, seed: int = 20260921,
        workers: int = 6, only_new: bool = True, limit: int | None = None,
        post_ids: list[int] | None = None) -> dict:
    with db.connect(autocommit=True) as conn:
        rows = [dict(r) for r in conn.execute(
            _SELECT, {"base": base_version, "version": version,
                      "only_new": only_new}).fetchall()]
    if post_ids:
        keep = set(post_ids)
        rows = [r for r in rows if r["post_id"] in keep]
    if limit:
        rows = rows[:limit]
    if not rows:
        return {"n": 0, "n_written": 0, "n_error": 0, "n_changed": 0}

    stats = {"n": len(rows), "n_written": 0, "n_error": 0, "n_changed": 0,
             "n_off_topic": 0, "recovered_from_other": 0}
    limits = httpx.Limits(max_connections=workers + 2,
                          max_keepalive_connections=workers + 2)

    with httpx.Client(limits=limits) as client, \
            ThreadPoolExecutor(max_workers=workers) as pool, \
            db.connect(autocommit=False) as wconn:
        futs = {pool.submit(classify, client, base_url, model,
                            r["text"] or "", lang=r["lang"], seed=seed): r
                for r in rows}
        done = 0
        for fut in as_completed(futs):
            r = futs[fut]
            done += 1
            try:
                v = fut.result()
            except Exception as e:                       # noqa: BLE001
                stats["n_error"] += 1
                log.warning("post %s failed: %s", r["post_id"], e)
                continue
            lab = to_label(r, v, model=model, seed=seed)
            wconn.execute(_UPSERT, {
                "post_id": r["post_id"], "label_version": version,
                "is_cyber": lab.is_cyber, "is_canada": lab.is_canada,
                "is_relevant": lab.is_relevant, "topic_key": lab.topic_key,
                "is_low_information": lab.is_low_information,
                "matched_terms": Jsonb(lab.matched_terms),
                "confidence": lab.confidence})
            stats["n_written"] += 1
            if v.off_topic:
                stats["n_off_topic"] += 1
            if v.topic_key != r["rules_topic"]:
                stats["n_changed"] += 1
                if r["rules_topic"] == "other" and v.topic_key != "other":
                    stats["recovered_from_other"] += 1
            if done % 100 == 0:
                wconn.commit()
                log.info("llm labelling %d/%d", done, len(rows))
        wconn.commit()
    log.info("llm labelling done: %s", stats)
    return stats
