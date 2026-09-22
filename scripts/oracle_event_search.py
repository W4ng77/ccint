"""逐事件定向搜索:把「平台上没有」和「我们的查询没抓到」分开。

现有语料只能回答「我们采到了多少」。它无法区分两种失败:
  (a) 这个事件在 Bluesky 上根本没人讨论;
  (b) 有人讨论,但 34 个安全词一个都没命中,所以物理上不可达。
两者对系统的含义完全相反 —— (a) 是平台的性质,改进采集无用;(b) 是采集边界的
缺陷,是可修的。

做法:对登记表里每个加拿大事件,用**受害组织名本身**(不带任何安全词)在
Bluesky 定向搜一次,窗口为事件公布日 [-2, +30] 天。这条查询不是生产采集流的
一部分,只用于测量 —— 它是一次 oracle,回答「如果我们早知道要找什么,能找到
多少」。

[MUST] 写 collection_runs(P5)。任何触达采集 API 的路径都要留痕,否则日后
无法区分「那天量少」是社会现象还是我们在跑别的东西。
[MUST] 不入库 posts。把 oracle 的结果灌进语料会改变语料定义,而这份语料正在
被用来评估采集边界本身。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time

from ccint import db
from ccint.config import Settings
from ccint.collectors.bluesky import BlueskyCollector

logging.basicConfig(level=logging.WARNING, format="%(message)s")
WIN_BEFORE, WIN_AFTER = 2, 30


def main() -> int:
    with db.connect(autocommit=True) as c:
        events = [dict(r) for r in c.execute("""
            SELECT v.event_id, v.title, v.published_at_utc,
                   array_agg(e.entity_text) AS ents
            FROM external_events v
            JOIN external_event_entities e USING (event_id)
            WHERE v.country='CA' AND v.published_at_utc >= '2026-08-22'
              AND v.published_at_utc < '2026-09-23'
            GROUP BY 1,2,3 ORDER BY 3""")]
    print(f"加拿大事件 {len(events)} 起", flush=True)

    col = BlueskyCollector([" "], query_version="oracle_v1")
    t0 = dt.datetime.now(dt.timezone.utc)
    out, n_calls = [], 0
    try:
        for i, ev in enumerate(events, 1):
            name = ev["title"]
            since = ev["published_at_utc"] - dt.timedelta(days=WIN_BEFORE)
            until = ev["published_at_utc"] + dt.timedelta(days=WIN_AFTER)
            try:
                page = col.fetch_page(since=since, until=until, cursor=None,
                                      limit=100, term=f'"{name}"')
                n_calls += 1
                hits = page.posts
            except Exception as e:                                # noqa: BLE001
                print(f"  !! {name[:40]}: {type(e).__name__}", flush=True)
                hits = None
            out.append({
                "event_id": ev["event_id"], "title": name,
                "published": ev["published_at_utc"].isoformat(),
                "oracle_hits": None if hits is None else len(hits),
                "handles": None if hits is None
                           else sorted({p.author_handle for p in hits})[:10],
            })
            if i % 10 == 0:
                print(f"  {i}/{len(events)}", flush=True)
            time.sleep(0.4)
    finally:
        col.close()

    with db.connect(autocommit=True) as c:
        c.execute("""INSERT INTO collection_runs
            (source_key, mode, query_version, query_spec, window_start, window_end,
             started_at, ended_at, status, n_fetched, n_inserted, n_duplicate, n_error)
            VALUES ('bluesky','oracle','oracle_v1',%s,%s,%s,%s,now(),'success',
                    %s,0,0,%s)""",
            (json.dumps({"kind": "per-event victim-name search",
                         "n_events": len(events),
                         "window_days": [-WIN_BEFORE, WIN_AFTER]}),
             events[0]["published_at_utc"], events[-1]["published_at_utc"], t0,
             sum(o["oracle_hits"] or 0 for o in out),
             sum(1 for o in out if o["oracle_hits"] is None)))

    path = "reports/oracle_events.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    ok = [o for o in out if o["oracle_hits"] is not None]
    withpost = [o for o in ok if o["oracle_hits"] > 0]
    print(f"\noracle 搜索成功 {len(ok)}/{len(out)},API 调用 {n_calls}")
    print(f"平台上**存在**讨论的事件: {len(withpost)}/{len(ok)} = "
          f"{len(withpost)/max(1,len(ok)):.1%}")
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
