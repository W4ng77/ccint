"""通用 Web 源采集器:RSS/Atom 与 Discourse。

这两种覆盖了绝大多数「论坛」的可达形式 —— 多数安全社区要么有 RSS,要么跑在
Discourse 上并开放 ``/latest.json``。两者都只读公开内容,不登录、不绕过任何
访问控制。

[MUST] 抓任何第三方站点之前先读 robots.txt,并按源配置限速。这不是礼貌问题:
一个被 ban 掉的源会造成采集缺口,而采集缺口在时间序列上与「讨论减少」不可区分
—— 正是本项目最核心的那个混淆。robots 检查失败时**拒绝采集并记录原因**,
不做「检查不到就当允许」的默认。
"""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import logging
import re
import time
import urllib.parse
import urllib.robotparser as robotparser
from xml.etree import ElementTree as ET

import httpx

from ..models import Page, RawPost
from .sources import SourceSpec

log = logging.getLogger(__name__)

UA = "ccint-research/0.1 (+academic study of cybersecurity discussion)"
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NS = {"atom": "http://www.w3.org/2005/Atom",
       "dc": "http://purl.org/dc/elements/1.1/"}


def _clean(html: str | None) -> str:
    return _WS.sub(" ", _TAG.sub(" ", html or "")).strip()


def _parse_date(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:                                     # RFC 822(RSS)
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:                                 # ISO 8601(Atom / Discourse)
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def robots_allows(url: str, *, client: httpx.Client) -> tuple[bool, str]:
    """返回 ``(allowed, reason)``。取不到 robots.txt 视为**不允许**。"""
    p = urllib.parse.urlparse(url)
    robots = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        r = client.get(robots, timeout=20, headers={"user-agent": UA})
    except Exception as e:                                        # noqa: BLE001
        return False, f"robots.txt 取不到({type(e).__name__})"
    if r.status_code == 404:
        return True, "robots.txt 不存在(404),视为未设限"
    if r.status_code != 200:
        return False, f"robots.txt 返回 {r.status_code}"
    rp = robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    ok = rp.can_fetch(UA, url)
    return ok, "robots.txt allows" if ok else "robots.txt disallows this path"


class _Base:
    """公共部分:限速、robots 闸门、query_spec。"""

    def __init__(self, spec: SourceSpec, query_version: str):
        self.spec = spec
        self.source_key = spec.key
        self.query_version = query_version
        self.client = httpx.Client(headers={"user-agent": UA},
                                   follow_redirects=True, timeout=45)
        self._last = 0.0
        self._robots_checked: dict[str, tuple[bool, str]] = {}

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _wait(self) -> None:
        gap = self.spec.rate_limit_s - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def _get(self, url: str) -> httpx.Response:
        host = urllib.parse.urlparse(url).netloc
        if host not in self._robots_checked:
            self._robots_checked[host] = robots_allows(url, client=self.client)
            log.info("robots %s -> %s", host, self._robots_checked[host])
        ok, why = self._robots_checked[host]
        if not ok:
            raise PermissionError(f"{self.source_key}: 拒绝采集 {host} —— {why}")
        self._wait()
        r = self.client.get(url)
        r.raise_for_status()
        return r

    # run_collection 是按 term 驱动的(Bluesky 的形态)。web 源没有「查询词」这个
    # 维度 —— 一个 feed 就是一个流。用单元素的 search_terms 适配,好处是所有源
    # 共用同一条 ingest / run bookkeeping 路径:多一条入库路径就多一处可能绕过
    # collection_runs,而那张表的全部价值就是让「这天量少」可归因。
    search_terms = [None]

    def iter_term_pages(self, term, *, since=None, until=None, max_pages: int = 20):
        cursor = None
        for _ in range(max_pages):
            page = self.fetch_page(since=since, until=until, cursor=cursor,
                                   limit=200)
            yield page
            cursor = page.next_cursor
            if not cursor or not page.posts:
                return

    def query_spec(self) -> dict:
        return {"source": self.spec.key, "kind": self.spec.kind,
                "url": self.spec.url or self.spec.base_url,
                "rate_limit_s": self.spec.rate_limit_s,
                "authorised": self.spec.authorised}


class RSSCollector(_Base):
    """RSS 2.0 / Atom。feed 只给最近若干条,故 ``since``/``until`` 是过滤而非查询。"""

    def fetch_page(self, *, since=None, until=None, cursor=None,
                   limit: int = 200) -> Page:
        if cursor:                       # feed 无分页
            return Page(posts=[], next_cursor=None)
        root = ET.fromstring(self._get(self.spec.url).content)
        items = root.findall(".//item") or root.findall("atom:entry", _NS)
        posts = []
        for it in items[:limit]:
            p = self._to_post(it)
            if p is None:
                continue
            if since and p.published_at < since:
                continue
            if until and p.published_at > until:
                continue
            posts.append(p)
        return Page(posts=posts, next_cursor=None)

    def _to_post(self, it) -> RawPost | None:
        def txt(*names):
            for n in names:
                e = it.find(n) if not n.startswith("atom:") else it.find(n, _NS)
                if e is not None:
                    return (e.text or "").strip() or (e.get("href") or "").strip()
            return None

        link = txt("link", "atom:link") or ""
        title = txt("title", "atom:title") or ""
        body = _clean(txt("description", "atom:summary", "atom:content", "content"))
        pub = _parse_date(txt("pubDate", "atom:published", "atom:updated",
                              "dc:date"))
        if not (title or body) or pub is None:
            return None
        gid = txt("guid", "atom:id") or link or title
        author = txt("author", "dc:creator") or self.spec.key
        text = f"{title}. {body}".strip() if body else title
        return RawPost(
            source_key=self.spec.key,
            source_post_id=hashlib.sha1(f"{self.spec.key}:{gid}".encode()).hexdigest(),
            author_id=f"{self.spec.key}:{hashlib.sha1(author.encode()).hexdigest()[:12]}",
            author_handle=author[:120],
            published_at=pub, text=text[:8000], lang=None, url=link or None,
            in_reply_to=None, reply_count=None, like_count=None, repost_count=None,
            raw_payload={"title": title, "link": link, "author": author,
                         "summary": body[:4000], "source_kind": "rss"})


class DiscourseCollector(_Base):
    """Discourse 公开 JSON。抓话题列表,正文取每个话题的首帖。"""

    def fetch_page(self, *, since=None, until=None, cursor=None,
                   limit: int = 50) -> Page:
        page = int(cursor or 0)
        url = f"{self.spec.base_url.rstrip('/')}/latest.json?page={page}"
        data = self._get(url).json()
        topics = (data.get("topic_list") or {}).get("topics") or []
        if not topics:
            return Page(posts=[], next_cursor=None)
        posts, stop = [], False
        for t in topics[:limit]:
            pub = _parse_date(t.get("created_at"))
            if pub is None:
                continue
            if since and pub < since:
                stop = True                  # latest.json 按时间倒序
                continue
            if until and pub > until:
                continue
            posts.append(self._to_post(t, pub))
        nxt = None if (stop or page >= 20) else str(page + 1)
        return Page(posts=posts, next_cursor=nxt)

    def _to_post(self, t: dict, pub: dt.datetime) -> RawPost:
        base = self.spec.base_url.rstrip("/")
        title = (t.get("title") or "").strip()
        excerpt = _clean(t.get("excerpt"))
        return RawPost(
            source_key=self.spec.key,
            source_post_id=f"{self.spec.key}:{t.get('id')}",
            author_id=f"{self.spec.key}:{t.get('posters', [{}])[0].get('user_id', '?')}",
            author_handle=None,
            published_at=pub,
            text=(f"{title}. {excerpt}" if excerpt else title)[:8000],
            lang=None, url=f"{base}/t/{t.get('slug')}/{t.get('id')}",
            in_reply_to=None, reply_count=t.get("posts_count"),
            like_count=t.get("like_count"), repost_count=None,
            raw_payload={**t, "source_kind": "discourse"})


def build(spec: SourceSpec, query_version: str):
    if spec.kind == "rss":
        return RSSCollector(spec, query_version)
    if spec.kind == "discourse":
        return DiscourseCollector(spec, query_version)
    raise ValueError(f"{spec.key}: web.build 不处理 kind={spec.kind!r}")
