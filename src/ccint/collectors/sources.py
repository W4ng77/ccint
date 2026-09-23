"""用户指定源的注册表(``config/sources.yaml``)。

这一层存在的理由是让「采集哪些论坛」变成配置而不是代码。下游(ingest、
labeler、analytics)已经是 source-agnostic 的 —— ``posts.source_key`` 是
唯一的区分,所有分析按它分组即可。

[MUST] 每个源必须带 ``authorised`` 说明。自动抓取第三方论坛涉及该站的服务
条款;这个字段的作用不是技术性的,是强迫添加源的人先去确认过。
"""
from __future__ import annotations

import functools
import pathlib
from dataclasses import dataclass, field

import yaml

CONFIG = pathlib.Path(__file__).resolve().parents[3] / "config" / "sources.yaml"
KINDS = {"bluesky", "rss", "discourse"}


@dataclass(frozen=True)
class SourceSpec:
    key: str
    kind: str
    enabled: bool
    authorised: str
    url: str | None = None
    base_url: str | None = None
    rate_limit_s: float = 2.0
    notes: str | None = None
    extra: dict = field(default_factory=dict)


@functools.lru_cache(maxsize=1)
def load(path: pathlib.Path | None = None) -> tuple[str, list[SourceSpec]]:
    d = yaml.safe_load((path or CONFIG).read_text(encoding="utf-8"))
    out: list[SourceSpec] = []
    seen: set[str] = set()
    for s in d.get("sources") or []:
        key = s["key"]
        if key in seen:
            raise ValueError(f"sources.yaml 有重复 key: {key!r}")
        seen.add(key)
        if s["kind"] not in KINDS:
            raise ValueError(f"{key}: 未知 kind {s['kind']!r};支持 {sorted(KINDS)}")
        if not s.get("authorised"):
            raise ValueError(
                f"{key}: 缺 authorised。每个源必须写明依据什么认为可以自动采集 —— "
                "robots.txt 允许不等于服务条款允许。")
        if s["kind"] == "rss" and not s.get("url"):
            raise ValueError(f"{key}: kind=rss 必须有 url")
        if s["kind"] == "discourse" and not s.get("base_url"):
            raise ValueError(f"{key}: kind=discourse 必须有 base_url")
        out.append(SourceSpec(
            key=key, kind=s["kind"], enabled=bool(s.get("enabled", True)),
            authorised=s["authorised"], url=s.get("url"),
            base_url=s.get("base_url"),
            rate_limit_s=float(s.get("rate_limit_s", 2.0)),
            notes=s.get("notes"),
            extra={k: v for k, v in s.items()
                   if k not in {"key", "kind", "enabled", "authorised", "url",
                                "base_url", "rate_limit_s", "notes"}}))
    return d["version"], out


def enabled() -> list[SourceSpec]:
    return [s for s in load()[1] if s.enabled]


def get(key: str) -> SourceSpec:
    for s in load()[1]:
        if s.key == key:
            return s
    raise KeyError(f"sources.yaml 里没有 {key!r};可用:{[s.key for s in load()[1]]}")
