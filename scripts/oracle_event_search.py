"""逐事件定向搜索:把「平台上没有」和「我们的查询没抓到」分开。

现有语料只能回答「我们采到了多少」。它无法区分两种失败:
  (a) 这个事件在 Bluesky 上根本没人讨论;
  (b) 有人讨论,但 34 个安全词一个都没命中,所以物理上不可达。
两者对系统的含义完全相反 —— (a) 是平台的性质,改进采集无用;(b) 是采集边界的
缺陷,是可修的。

做法:对登记表里每个加拿大事件,用**受害组织名本身**(不带任何安全词)定向搜,
窗口为事件公布日 [-2, +30] 天。

[MUST] 光按组织名搜会被同名噪声淹没 —— 实测 "MEQ" 搜出的是医学单位 mEq,
"Air Canada" 搜出的是机票优惠和航班动态。所以每条命中必须再过一次 cyber 判定
才计入。不加这一步,「平台上存在讨论」这个数会被虚报数倍。

[MUST] 写 collection_runs(P5);不入库 posts —— 把 oracle 结果灌进语料会改变
语料定义,而这份语料正在被用来评估采集边界本身。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from ccint import db
from ccint.collectors.bluesky import BlueskyCollector
from ccint.labelers.llm_v2 import CYBER_SCHEMA
from ccint.llm import client as llm

logging.basicConfig(level=logging.WARNING, format="%(message)s")
WIN_BEFORE, WIN_AFTER = 2, 30
BASE_URL, MODEL = "http://127.0.0.1:8000/v1", "user"


def main() -> int:
    with db.connect(autocommit=True) as c:
        events = [dict(r) for r in c.execute("""
            SELECT event_id, title, published_at_utc FROM external_events
            WHERE country='CA' AND published_at_utc >= '2026-08-22'
              AND published_at_utc < '2026-09-23' ORDER BY published_at_utc""")]
        have = {r["sid"] for r in c.execute("SELECT source_post_id sid FROM posts")}
    print(f"加拿大事件 {len(events)} 起", flush=True)

    col = BlueskyCollector([" "], query_version="oracle_v1")
    t0 = dt.datetime.now(dt.timezone.utc)
    out = []
    try:
        for i, ev in enumerate(events, 1):
            since = ev["published_at_utc"] - dt.timedelta(days=WIN_BEFORE)
            until = ev["published_at_utc"] + dt.timedelta(days=WIN_AFTER)
            try:
                pg = col.fetch_page(since=since, until=until, cursor=None,
                                    limit=100, term=f'"{ev["title"]}"')
                hits = [{"uri": p.source_post_id, "handle": p.author_handle,
                         "text": p.text, "likes": p.like_count or 0,
                         "in_corpus": p.source_post_id in have} for p in pg.posts]
            except Exception as e:                                # noqa: BLE001
                print(f"  !! {ev['title'][:40]}: {type(e).__name__}", flush=True)
                hits = None
            out.append({"event_id": ev["event_id"], "title": ev["title"],
                        "published": ev["published_at_utc"].isoformat(),
                        "hits": hits})
            if i % 10 == 0:
                print(f"  搜索 {i}/{len(events)}", flush=True)
            time.sleep(0.4)
    finally:
        col.close()

    # ---- 每条命中再过一次 cyber 判定,滤掉同名噪声 ----
    p_cy = llm.load_prompt("gate/is_cyber", 1)
    flat = [(o, h) for o in out if o["hits"] for h in o["hits"]]
    print(f"\n对 {len(flat)} 条命中做 cyber 判定(滤同名噪声)…", flush=True)
    lim = httpx.Limits(max_connections=36, max_keepalive_connections=36)
    with httpx.Client(limits=lim) as http, ThreadPoolExecutor(32) as pool:
        def f(x):
            return x, llm.call(http, base_url=BASE_URL, model=MODEL, prompt=p_cy,
                               user_text=x[1]["text"] or "", schema=CYBER_SCHEMA,
                               max_tokens=96)
        for (o, h), r in pool.map(f, flat):
            h["is_cyber"] = None if r.data is None else bool(r.data.get("is_cyber"))

    with db.connect(autocommit=True) as c:
        c.execute("""INSERT INTO collection_runs
            (source_key, mode, query_version, query_spec, window_start, window_end,
             started_at, ended_at, status, n_fetched, n_inserted, n_duplicate, n_error)
            VALUES ('bluesky','oracle','oracle_v1',%s,%s,%s,%s,now(),'success',%s,0,0,%s)""",
            (json.dumps({"kind": "per-event victim-name search + cyber filter",
                         "n_events": len(events),
                         "window_days": [-WIN_BEFORE, WIN_AFTER]}),
             events[0]["published_at_utc"], events[-1]["published_at_utc"], t0,
             len(flat), sum(1 for o in out if o["hits"] is None)))

    with open("reports/oracle_events.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    cy = sum(1 for _, h in flat if h.get("is_cyber"))
    print(f"\n命中 {len(flat)} 条,其中 cyber {cy} 条 ({cy/max(1,len(flat)):.1%})")
    print("→ reports/oracle_events.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
