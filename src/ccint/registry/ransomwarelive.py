"""ransomware.live 公开 API 接入。

选它作为第一个登记源的理由:它逐条记录**受害组织名 + 发现日期 + 勒索团伙**,
带 country 字段,公开无需鉴权。受害组织名是可以直接在语料里做文本匹配的实体,
这让「事件 → 帖子」的链接不需要任何本系统的判断参与。

[MUST] 这个源有已知的强偏差,报告里必须写明:它只覆盖**勒索软件泄露站**上公开
点名的受害者。数据泄露、钓鱼、政策类事件它完全看不到;被勒索但付了赎金、
没被挂上泄露站的组织它也看不到。用它当分母只能回答「勒索软件受害事件的
社媒覆盖率」,不能回答「加拿大网安事件的社媒覆盖率」。
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

BASE = "https://api.ransomware.live/v2"
SOURCE = "ransomware.live"

# 法人后缀不参与匹配:"Woodlore International Inc." 在帖子里常写成 "Woodlore
# International"。去掉后缀提高召回,但**不去掉核心词**,否则 "Storm" 这类
# 短名会把整个语料匹爆。
_SUFFIX = re.compile(
    r"\b(inc|inc\.|incorporated|ltd|ltd\.|limited|ltée|ltee|llc|llp|corp|corp\.|"
    r"corporation|co|co\.|company|group|holdings|enterprises|s\.a\.|sa|gmbh)\b\.?",
    re.I)
_PUNCT = re.compile(r"[^\w\sÀ-ɏ]+")
_WS = re.compile(r"\s+")


_DOMAINISH = re.compile(r"^(?:https?://)?(?:www\.)?[\w-]+(?:\.[\w-]+)+/?$", re.I)


def canonical(name: str, kind: str = "org") -> str:
    """归一化。org 去标点去法人后缀;domain 保留点号,只去协议和 www 前缀。

    域名不能走去标点那条路 —— "alphaplantes.com" 被打成 "alphaplantes com"
    之后既不匹配原文也不匹配任何东西。
    """
    raw = (name or "").strip()
    if kind == "domain" or _DOMAINISH.match(raw):
        d = re.sub(r"^https?://", "", raw.lower()).split("/")[0]
        return d[4:] if d.startswith("www.") else d
    s = _PUNCT.sub(" ", raw.lower())
    s = _SUFFIX.sub(" ", s)
    return _WS.sub(" ", s).strip()


def fetch_country_victims(country: str, *, retries: int = 6,
                          backoff: float = 25.0) -> list[dict]:
    """拉某国的全部受害者记录。API 有速率限制,429 时退避重试。"""
    url = f"{BASE}/countryvictims/{country.upper()}"
    for attempt in range(retries):
        r = httpx.get(url, timeout=60, headers={
            "accept": "application/json", "user-agent": "ccint-research/0.1"})
        if r.status_code == 200:
            return r.json()
        log.warning("ransomware.live %s -> %s (attempt %d)",
                    country, r.status_code, attempt)
        time.sleep(backoff * (attempt + 1) / 2)
    raise RuntimeError(f"ransomware.live {country} 拉取失败(速率限制)")


def _parse_ts(x: dict) -> tuple[dt.datetime | None, dt.datetime | None]:
    def p(k):
        s = x.get(k)
        if not s:
            return None
        try:
            d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    return p("published"), p("discovered")


def _victim_name(x: dict) -> str:
    return (x.get("victim") or x.get("post_title") or "").strip()


_UPSERT_EV = """
INSERT INTO external_events (event_id, source, published_at_utc, discovered_at_utc,
                             title, country, event_type, url, raw_payload)
VALUES (%(event_id)s, %(source)s, %(published_at_utc)s, %(discovered_at_utc)s,
        %(title)s, %(country)s, %(event_type)s, %(url)s, %(raw_payload)s)
ON CONFLICT (event_id) DO NOTHING
"""
_UPSERT_ENT = """
INSERT INTO external_event_entities (event_id, entity_text, entity_type, canonical)
VALUES (%(event_id)s, %(entity_text)s, %(entity_type)s, %(canonical)s)
ON CONFLICT (event_id, entity_text) DO NOTHING
"""


def ingest(country: str, *, since: dt.date | None = None,
           until: dt.date | None = None) -> dict:
    """拉取并写入。返回计数。窗口过滤用 published/discovered 里较早的那个。"""
    rows = fetch_country_victims(country)
    n_ev = n_ent = n_skip = 0
    with db.connect(autocommit=False) as conn, conn.cursor() as cur:
        for x in rows:
            pub, disc = _parse_ts(x)
            ts = pub or disc
            if ts is None:
                n_skip += 1
                continue
            if since and ts.date() < since:
                continue
            if until and ts.date() > until:
                continue
            name = _victim_name(x)
            if not name:
                n_skip += 1
                continue
            gid = (x.get("group_name") or "?").strip()
            eid = f"rl:{country.upper()}:{gid}:{canonical(name)[:60]}:{ts.date()}"
            cur.execute(_UPSERT_EV, {
                "event_id": eid, "source": SOURCE,
                "published_at_utc": ts, "discovered_at_utc": disc,
                "title": name, "country": country.upper(),
                "event_type": "ransomware_victim",
                "url": x.get("post_url") or x.get("website"),
                "raw_payload": Jsonb(x)})
            n_ev += cur.rowcount
            for text, etype in _entities(name, x):
                cur.execute(_UPSERT_ENT, {
                    "event_id": eid, "entity_text": text,
                    "entity_type": etype, "canonical": canonical(text, etype)})
                n_ent += cur.rowcount
        conn.commit()
    return {"country": country.upper(), "fetched": len(rows),
            "events": n_ev, "entities": n_ent, "skipped": n_skip}


def _entities(name: str, x: dict) -> list[tuple[str, str]]:
    out = [(name, "domain" if _DOMAINISH.match(name) else "org")]
    site = (x.get("website") or "").strip()
    if site:
        d = re.sub(r"^https?://", "", site).split("/")[0].lower()
        if d.startswith("www."):
            d = d[4:]
        if "." in d:
            out.append((d, "domain"))
    return out
