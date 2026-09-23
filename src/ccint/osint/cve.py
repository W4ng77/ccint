"""CVE 抽取与富化。

分三步,刻意分开:

1. **抽取** —— 纯正则,确定性。``CVE-YYYY-NNNN+`` 的格式由 MITRE 规定,不需要
   模型参与。这一步的产物 (``post_cves``) 因此可以在标注质量完全未知的情况下
   被信任,这在本项目里是稀缺性质。
2. **富化** —— 向 NVD 查这个编号是什么、多严重;向 CISA KEV 查它是否已被实际
   利用。两者都是外部权威源,本系统无法影响。
3. **关联** —— 供门户按 CVE 反查帖子、按帖子展开 CVE。

[MUST] ``known_exploited`` 与 CVSS 分数正交,不要合成单一「风险分」。
CVSS 是理论严重性,KEV 是「有人真的在用它」。把外部事实和我们的权重揉在一起
之后,读者就无法判断哪部分是观测、哪部分是我们的假设。

[MUST] NVD 无 key 时限速 5 次/30 秒。超限会被 403,而一次被 ban 造成的富化
缺口在时间序列上与「这段时间没人谈 CVE」不可区分。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

import httpx
from psycopg.types.json import Jsonb

from .. import db

log = logging.getLogger(__name__)

# MITRE 规定:CVE-<4 位年>-<至少 4 位序号>。\b 两端避免匹配到更长的串。
CVE_RE = re.compile(r"\bCVE-(\d{4})-(\d{4,7})\b", re.I)

NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
KEV_URL = ("https://www.cisa.gov/sites/default/files/feeds/"
           "known_exploited_vulnerabilities.json")
UA = "ccint-research/0.1"


def extract(text: str) -> list[tuple[str, int]]:
    """返回 ``[(CVE-ID 规范大写, 字符偏移), ...]``,按出现顺序,去重保留首次。"""
    seen, out = set(), []
    for m in CVE_RE.finditer(text or ""):
        cid = f"CVE-{m.group(1)}-{m.group(2)}"
        if cid not in seen:
            seen.add(cid)
            out.append((cid, m.start()))
    return out


# --------------------------------------------------------------------------
# 抽取 → post_cves
# --------------------------------------------------------------------------
_LINK = """
INSERT INTO post_cves (post_id, cve_id, char_start)
VALUES (%(post_id)s, %(cve_id)s, %(char_start)s)
ON CONFLICT DO NOTHING
"""


def link_corpus(*, only_new: bool = True, limit: int | None = None) -> dict:
    """在全语料上跑 CVE 抽取。纯本地,无网络。"""
    sql = """
    SELECT p.post_id, p.text FROM posts p
    WHERE p.text ~* 'CVE-[0-9]{4}-[0-9]{4,7}'
      AND (%(only_new)s = false OR NOT EXISTS (
            SELECT 1 FROM post_cves c WHERE c.post_id = p.post_id))
    ORDER BY p.post_id
    """
    with db.connect(autocommit=True) as c:
        rows = [dict(r) for r in c.execute(sql, {"only_new": only_new}).fetchall()]
    if limit:
        rows = rows[:limit]
    n_links = 0
    ids: set[str] = set()
    with db.connect(autocommit=False) as conn, conn.cursor() as cur:
        for r in rows:
            for cid, pos in extract(r["text"]):
                cur.execute(_LINK, {"post_id": r["post_id"], "cve_id": cid,
                                    "char_start": pos})
                n_links += cur.rowcount
                ids.add(cid)
        conn.commit()
    return {"posts_scanned": len(rows), "links_written": n_links,
            "distinct_cves": len(ids)}


# --------------------------------------------------------------------------
# 富化 → cve_records
# --------------------------------------------------------------------------
_UPSERT = """
INSERT INTO cve_records (cve_id, published_at, last_modified_at, description,
                         cvss_score, cvss_severity, cvss_version, source, raw_payload)
VALUES (%(cve_id)s, %(published_at)s, %(last_modified_at)s, %(description)s,
        %(cvss_score)s, %(cvss_severity)s, %(cvss_version)s, %(source)s,
        %(raw_payload)s)
ON CONFLICT (cve_id) DO UPDATE SET
  published_at = EXCLUDED.published_at,
  last_modified_at = EXCLUDED.last_modified_at,
  description = EXCLUDED.description,
  cvss_score = EXCLUDED.cvss_score,
  cvss_severity = EXCLUDED.cvss_severity,
  cvss_version = EXCLUDED.cvss_version,
  source = EXCLUDED.source,
  raw_payload = EXCLUDED.raw_payload,
  fetched_at = now()
"""


def _best_cvss(metrics: dict) -> tuple[float | None, str | None, str | None]:
    """取可用的最高 CVSS 版本。v3.1 > v3.0 > v2 —— 版本要一起记,分数不可跨版本比。"""
    for key, ver in (("cvssMetricV31", "3.1"), ("cvssMetricV30", "3.0"),
                     ("cvssMetricV2", "2.0")):
        arr = metrics.get(key) or []
        if arr:
            d = arr[0].get("cvssData") or {}
            sev = (d.get("baseSeverity")
                   or arr[0].get("baseSeverity"))          # v2 把它放在外层
            return d.get("baseScore"), sev, ver
    return None, None, None


def _parse(s: str | None):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def enrich(*, api_key: str | None = None, limit: int | None = None,
           sleep_s: float | None = None) -> dict:
    """对 ``post_cves`` 里尚未富化的编号查 NVD。"""
    gap = sleep_s if sleep_s is not None else (0.7 if api_key else 6.5)
    with db.connect(autocommit=True) as c:
        todo = [r["cve_id"] for r in c.execute("""
            SELECT DISTINCT c.cve_id FROM post_cves c
            WHERE NOT EXISTS (SELECT 1 FROM cve_records r WHERE r.cve_id = c.cve_id)
            ORDER BY 1""")]
    if limit:
        todo = todo[:limit]
    log.warning("NVD 富化 %d 个 CVE(间隔 %.1fs)", len(todo), gap)
    hdr = {"user-agent": UA} | ({"apiKey": api_key} if api_key else {})
    ok = miss = err = 0
    with httpx.Client(headers=hdr, timeout=60) as http, \
            db.connect(autocommit=False) as conn, conn.cursor() as cur:
        for i, cid in enumerate(todo, 1):
            time.sleep(gap)
            try:
                r = http.get(NVD_URL, params={"cveId": cid})
                r.raise_for_status()
                items = r.json().get("vulnerabilities") or []
            except Exception as e:                            # noqa: BLE001
                log.warning("  %s -> %s", cid, type(e).__name__)
                err += 1
                continue
            if not items:
                # 不存在的编号也要落一行:否则每次富化都会重查它。
                cur.execute(_UPSERT, {
                    "cve_id": cid, "published_at": None, "last_modified_at": None,
                    "description": None, "cvss_score": None, "cvss_severity": None,
                    "cvss_version": None, "source": "unresolved",
                    "raw_payload": Jsonb({})})
                miss += 1
                continue
            v = items[0]["cve"]
            score, sev, ver = _best_cvss(v.get("metrics") or {})
            desc = next((d["value"] for d in (v.get("descriptions") or [])
                         if d.get("lang") == "en"), None)
            cur.execute(_UPSERT, {
                "cve_id": cid, "published_at": _parse(v.get("published")),
                "last_modified_at": _parse(v.get("lastModified")),
                "description": desc, "cvss_score": score, "cvss_severity": sev,
                "cvss_version": ver, "source": "nvd", "raw_payload": Jsonb(v)})
            ok += 1
            if i % 25 == 0:
                conn.commit()
                log.warning("  %d/%d", i, len(todo))
        conn.commit()
    return {"requested": len(todo), "resolved": ok, "unresolved": miss, "errors": err}


def sync_kev() -> dict:
    """同步 CISA Known Exploited Vulnerabilities。一次请求,覆盖全表。"""
    with httpx.Client(headers={"user-agent": UA}, timeout=90) as http:
        data = http.get(KEV_URL).json()
    rows = data.get("vulnerabilities") or []
    n = 0
    with db.connect(autocommit=False) as conn, conn.cursor() as cur:
        for v in rows:
            cid = (v.get("cveID") or "").upper()
            if not cid:
                continue
            cur.execute("""
                INSERT INTO cve_records (cve_id, known_exploited, kev_date_added,
                                         description, source, raw_payload)
                VALUES (%s, true, %s, %s, 'kev', %s)
                ON CONFLICT (cve_id) DO UPDATE SET
                  known_exploited = true,
                  kev_date_added = EXCLUDED.kev_date_added""",
                (cid, v.get("dateAdded"), v.get("shortDescription"), Jsonb(v)))
            n += cur.rowcount
        conn.commit()
    return {"kev_catalogue": len(rows), "rows_touched": n,
            "catalogue_version": data.get("catalogVersion")}
