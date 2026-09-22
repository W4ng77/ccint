"""轴 2：用 LLM 从账号的实际发帖判断它在做什么。

与 labelers/llm_v1 的区别：那个判**单条帖子的主题**，这个判**账号的性质**，
输入是该作者的一组帖子。同样走 guided decoding，同样把字段顺序排成
「枚举在前、自由文本殿后」—— 见 llm_v1 模块 docstring 里的空白死循环问题。
"""
from __future__ import annotations

import json
import logging
import re

import httpx

log = logging.getLogger(__name__)

FUNCTION_KEYS = ["incident_feed", "news_media", "promotion", "individual",
                 "org_other", "unknown"]

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "function": {"type": "string", "enum": FUNCTION_KEYS},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "rationale": {"type": "string", "maxLength": 200,
                      "description": "ONE short sentence. What pattern decided it."},
    },
    "required": ["function", "confidence", "rationale"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are characterising what a social-media account *does*, \
from a sample of its posts, for a study of Canada-related cybersecurity discussion.

You are NOT asked whether the account is a bot. Do not guess at automation —
judge only what the account is posting.

## Categories

1. `incident_feed`
   Systematically reports **external** security events: new ransomware victims,
   newly listed breaches, threat-actor activity, CVE listings. Each post is a
   different external incident. Typically terse, structured, repetitive in form
   but different in content. Threat-intelligence monitors belong here.

2. `news_media`
   A news organisation or a working journalist reporting stories. Posts link to
   articles, often the account's own outlet. Distinguished from `incident_feed`
   by being written as journalism rather than as a data feed.

3. `promotion`
   Promotes **its own** event, product, service, conference, or job openings.
   Sponsor thank-yous, speaker announcements, call-for-papers, ticket reminders,
   recruitment posts, affiliate links. The subject is the account's own offering.

4. `individual`
   A person speaking in their own voice — commenting, reacting, arguing, asking,
   sharing an opinion or a personal experience.

5. `org_other`
   An organisation that is none of the above: a government body, university,
   regulator, industry association, or a company posting about matters other
   than selling its own thing.

6. `unknown`
   Genuinely too little to tell.

## How to decide

- **`incident_feed` vs `news_media`**: does each post report a *different
  external incident* in a standardised way (feed), or is it journalism about a
  story (news)?
- **`promotion` vs `org_other`**: is the account's own event/product/vacancy the
  subject (promotion), or is it discussing the wider field (org_other)?
- **`incident_feed` and `promotion` describe a repeated pattern.** You are told
  how many posts the account has in this corpus. With fewer than three, no
  pattern can be established — an account that posted once about a breach is
  someone *sharing* that story, not a feed reporting it. In that case choose
  `individual`, `news_media` or `org_other` by what the account looks like, or
  `unknown` if you cannot tell. Reserve the two pattern categories for accounts
  that visibly do the same kind of thing repeatedly.
- Do not infer from the handle alone; the posts decide. The handle is context.

Answer with the required JSON only."""

_FIELD_RE = {
    "function": re.compile(r'"function"\s*:\s*"([a-z_]+)"'),
    "confidence": re.compile(r'"confidence"\s*:\s*"(high|medium|low)"'),
    "rationale": re.compile(r'"rationale"\s*:\s*"((?:[^"\\]|\\.)*)"'),
}


def _parse(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    out = {}
    for k, rx in _FIELD_RE.items():
        m = rx.search(content)
        if m:
            out[k] = m.group(1)
    if "function" not in out:
        raise ValueError(f"unparseable: {content[:160]!r}")
    out.setdefault("confidence", "low")
    out.setdefault("rationale", "")
    return out


def classify_author(client: httpx.Client, base_url: str, model: str, *,
                    handle: str, texts: list[str], n_total: int,
                    seed: int = 20260921, max_samples: int = 8,
                    timeout: float = 90.0) -> dict:
    sample = [re.sub(r"\s+", " ", t).strip()[:280] for t in texts[:max_samples]]
    body = "\n".join(f"{i}. {t}" for i, t in enumerate(sample, 1))
    user = (f"Account handle: {handle}\n"
            f"Relevant posts by this account in the corpus: {n_total}\n"
            f"Showing {len(sample)} of them:\n\n{body}")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user}],
        "temperature": 0.0, "seed": seed, "max_tokens": 500,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "actor", "strict": True,
                                            "schema": OUTPUT_SCHEMA}},
    }
    r = client.post(f"{base_url.rstrip('/')}/chat/completions", json=payload,
                    timeout=timeout)
    r.raise_for_status()
    d = _parse(r.json()["choices"][0]["message"]["content"])
    if d["function"] not in FUNCTION_KEYS:
        raise ValueError(f"function out of enum: {d['function']!r}")
    return d
