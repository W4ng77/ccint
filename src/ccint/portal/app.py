"""ccint 门户:演示用的只读 Web 界面。

这是这个项目第一个有真实读者的输出。在它之前分析结果落在 ``reports/*.md``,
等价于没有消费者。

[MUST] 只读。门户不触发采集、不写标签、不改配置。一个能从界面触发采集的
按钮会让 ``collection_runs`` 里出现没有对应计划的运行,而那张表的全部价值
就是让「这天量少」可归因。

[MUST] 每个面板都显示它的口径(label_version / 窗口 / 是否只含 relevant)。
界面上的裸数字会被当成无条件事实。
"""
from __future__ import annotations

import pathlib

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import queries as q

STATIC = pathlib.Path(__file__).resolve().parent / "static"

app = FastAPI(title="ccint portal", version="0.1",
              description="Read-only demonstration portal for the ccint corpus.")


@app.get("/api/overview")
def api_overview():
    return q.overview()


@app.get("/api/sources")
def api_sources():
    from ..collectors import sources as srcs
    version, specs = srcs.load()
    stats = {r["source_key"]: r for r in q.sources()}
    out = []
    for s in specs:
        out.append({"key": s.key, "kind": s.kind, "enabled": s.enabled,
                    "endpoint": s.url or s.base_url, "authorised": s.authorised,
                    **{k: v for k, v in (stats.get(s.key) or {}).items()
                       if k != "source_key"}})
    for k, r in stats.items():                       # 库里有、配置里没有的历史源
        if not any(o["key"] == k for o in out):
            out.append({"key": k, "kind": "(not in sources.yaml)", "enabled": False,
                        "endpoint": None, "authorised": None,
                        **{kk: vv for kk, vv in r.items() if kk != "source_key"}})
    return {"sources_version": version, "sources": out}


@app.get("/api/topics/timeseries")
def api_timeseries(days: int = Query(60, ge=7, le=365),
                   relevant_only: bool = True):
    return q.topics_over_time(days=days, relevant_only=relevant_only)


@app.get("/api/topics/recent")
def api_recent(days: int = Query(14, ge=1, le=90)):
    return q.recent_topics(days=days)


@app.get("/api/posts/recent")
def api_posts(days: int = Query(14, ge=1, le=90),
              topic: str | None = None, limit: int = Query(40, ge=1, le=200)):
    return {"posts": q.recent_posts(days=days, topic=topic, limit=limit)}


@app.get("/api/cves")
def api_cves(days: int = Query(14, ge=1, le=365), limit: int = Query(25, ge=1, le=200)):
    return q.top_cves(days=days, limit=limit)


@app.get("/api/cves/{cve_id}/posts")
def api_cve_posts(cve_id: str, limit: int = Query(30, ge=1, le=200)):
    return {"cve_id": cve_id.upper(), "posts": q.cve_posts(cve_id, limit=limit)}


@app.get("/api/health")
def api_health(days: int = Query(14, ge=1, le=90)):
    return {"runs": q.collection_health(days=days)}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(Exception)
def on_error(request, exc):                                   # noqa: ANN001
    return JSONResponse({"error": type(exc).__name__, "detail": str(exc)[:400]},
                        status_code=500)
