"""作者画像的批量执行器：测受众 + 判性质 + 写 author_profiles。"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from psycopg.types.json import Jsonb

from .. import db
from .actor import PROFILE_VERSION, AuthorProfile, measure_audience
from .actor_llm import classify_author

log = logging.getLogger(__name__)

_SAMPLE_SQL = """
SELECT p.author_id, p.text
FROM posts p JOIN post_labels l ON l.post_id = p.post_id
WHERE l.label_version = %(lv)s AND l.is_relevant
  AND p.author_id = ANY(%(ids)s)
ORDER BY p.author_id, length(p.text) DESC
"""

_UPSERT = """
INSERT INTO author_profiles
  (author_id, profile_version, author_handle, n_posts, n_settled,
   mean_engagement, zero_rate, audience, function, function_conf, rationale,
   actor_type, evidence)
VALUES (%(author_id)s, %(pv)s, %(handle)s, %(n_posts)s, %(n_settled)s,
        %(mean_engagement)s, %(zero_rate)s, %(audience)s, %(function)s,
        %(function_conf)s, %(rationale)s, %(actor_type)s, %(evidence)s)
ON CONFLICT (author_id, profile_version) DO UPDATE SET
  author_handle=EXCLUDED.author_handle, n_posts=EXCLUDED.n_posts,
  n_settled=EXCLUDED.n_settled, mean_engagement=EXCLUDED.mean_engagement,
  zero_rate=EXCLUDED.zero_rate, audience=EXCLUDED.audience,
  function=EXCLUDED.function, function_conf=EXCLUDED.function_conf,
  rationale=EXCLUDED.rationale, actor_type=EXCLUDED.actor_type,
  evidence=EXCLUDED.evidence, profiled_at=now()
"""

_CONF = {"high": 0.9, "medium": 0.6, "low": 0.3}


def run(*, base_url: str, model: str, label_version: str = "rules_v2",
        profile_version: str = PROFILE_VERSION, seed: int = 20260921,
        workers: int = 8, max_samples: int = 8) -> dict:
    with db.connect(autocommit=True) as conn:
        profiles: list[AuthorProfile] = measure_audience(
            conn, label_version=label_version)
        ids = [p.author_id for p in profiles]
        texts: dict[str, list[str]] = {}
        for r in conn.execute(_SAMPLE_SQL, {"lv": label_version, "ids": ids}).fetchall():
            texts.setdefault(r["author_id"], []).append(r["text"] or "")

    stats = {"n": len(profiles), "n_written": 0, "n_error": 0}
    limits = httpx.Limits(max_connections=workers + 2,
                          max_keepalive_connections=workers + 2)
    with httpx.Client(limits=limits) as client, \
            ThreadPoolExecutor(max_workers=workers) as pool, \
            db.connect(autocommit=False) as wconn:
        futs = {pool.submit(classify_author, client, base_url, model,
                            handle=p.author_handle,
                            texts=texts.get(p.author_id, []),
                            n_total=p.n_posts, seed=seed,
                            max_samples=max_samples): p
                for p in profiles}
        done = 0
        for fut in as_completed(futs):
            p = futs[fut]
            done += 1
            try:
                d = fut.result()
                p.function = d["function"]
                p.function_conf = _CONF.get(d["confidence"])
                p.rationale = d["rationale"][:400]
            except Exception as e:                        # noqa: BLE001
                stats["n_error"] += 1
                log.warning("author %s failed: %s", p.author_handle, e)
                p.function = "unknown"
            p.evidence.update({"model": model, "seed": seed,
                               "n_samples_shown": min(max_samples, p.n_posts)})
            wconn.execute(_UPSERT, {
                "author_id": p.author_id, "pv": profile_version,
                "handle": p.author_handle, "n_posts": p.n_posts,
                "n_settled": p.n_settled, "mean_engagement": p.mean_engagement,
                "zero_rate": p.zero_rate, "audience": p.audience,
                "function": p.function, "function_conf": p.function_conf,
                "rationale": p.rationale, "actor_type": p.actor_type,
                "evidence": Jsonb(p.evidence)})
            stats["n_written"] += 1
            if done % 100 == 0:
                wconn.commit()
                log.info("profiled %d/%d", done, len(profiles))
        wconn.commit()
    log.info("author profiling done: %s", stats)
    return stats
