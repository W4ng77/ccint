"""统一数据模型。[MUST] RawPost / Page / Collector 签名保持稳定（HANDOFF §M2）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class RawPost:
    source_key: str
    source_post_id: str
    author_id: str
    author_handle: str | None
    published_at: datetime          # tz-aware UTC
    text: str
    lang: str | None
    url: str | None
    in_reply_to: str | None
    reply_count: int | None
    like_count: int | None
    repost_count: int | None
    raw_payload: dict


@dataclass(frozen=True)
class Page:
    posts: list[RawPost]
    next_cursor: str | None


@dataclass(frozen=True)
class StoredPost:
    """已入库的 post（labeler / agent 读取用）。"""
    post_id: int
    source_key: str
    source_post_id: str
    author_id: str
    author_handle: str | None
    published_at: datetime
    text: str
    lang: str | None
    url: str | None


@dataclass(frozen=True)
class Label:
    is_cyber: bool
    is_canada: bool
    is_relevant: bool
    topic_key: str | None
    is_low_information: bool = False
    matched_terms: dict = field(default_factory=dict)
    confidence: float | None = None


@dataclass(frozen=True)
class TrendCandidate:
    topic_key: str
    cur_posts: int
    prev_posts: int
    cur_authors: int
    prev_authors: int
    abs_delta: int
    rel_growth: float | None     # prev=0 时为 None，不写 inf
    cur_share: float
    prev_share: float
    share_delta: float

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)
