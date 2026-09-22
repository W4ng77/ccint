"""LLM 主题分类器（v1 Phase 5，label_version = ``llm_v1``）。

为什么需要它
------------
对 ``other`` 桶做的 120 条开放式编码（``eval/other_sample.tsv``）测出：
**30% 的 ``other`` 本该落在已有的桶里**，其中 19% 是 ``data_breach``。
而且漏检不是零散的，是成批的同一事件：

* Thomson Reuters / 安省法院 C-Track 泄露 —— 13 条全漏
* 1.53 亿美加驾照泄露 —— 7 条全漏
* Golf Canada 56.9 万条 —— 2 条全漏

原因不在词表内容，在匹配机制：规则匹配的是**术语**（``data breach``、
``records exposed``），而新闻报道写的是**自然语言**——
"court records accessed"、"case management system hacked"、
"personal information may have been stolen"。再打多少词表补丁也追不上。

因此这里换的是机制，不是词表。

范围（[MUST] 只动一个变量）
---------------------------
本 labeler **只重判 topic_key**，``is_cyber`` / ``is_canada`` / ``is_relevant``
**原样继承自 rules_v2**，且只处理 rules_v2 判为 relevant 的那一批。
这样 ``rules_v2`` 与 ``llm_v1`` 的差异可以干净地归因到主题分类机制本身，
不会和相关性判定的变化混在一起。

相关性另有一条独立的验证路径（独立标注），不在本文件范围内。
但模型对"这条根本不是 Canada×cyber"的意见会记进 ``matched_terms.off_topic``
作为**旁证**，不改写 ``is_relevant``。

确定性
------
temperature=0 + 固定 seed + guided decoding（``response_format: json_schema``）。
同样输入应产出同样输出；小模型仍可能有非确定性，故把模型标识与 seed 一并
写进 ``matched_terms``，使每一行都可追溯到具体是谁在什么设置下判的。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from ..models import Label

log = logging.getLogger(__name__)

LABEL_VERSION = "llm_v1"

# [MUST] 顺序与 lexicons_v2/topics.yaml 完全一致。多主题帖按此顺序取第一个 ——
# 与规则版同一套裁决规则，否则两个 version 的差异里会混进排序口径的差异。
BUCKETS: list[tuple[str, str]] = [
    ("ransomware",
     "Ransomware specifically: ransom demanded, systems encrypted, double "
     "extortion, a named ransomware gang, a leak site, or an organisation "
     "paralysed/locked out by an attack of this kind."),
    ("phishing_scam",
     "Phishing, spear-phishing, smishing, vishing, business email compromise, "
     "fake login pages or fraudulent messages designed to steal credentials."),
    ("vulnerability_cve",
     "A specific software vulnerability, CVE identifier, zero-day, exploit, "
     "unpatched flaw, or patching guidance about one."),
    ("data_breach",
     "Data was exposed, accessed, leaked or stolen. THIS INCLUDES ordinary "
     "news wording that never uses the phrase 'data breach' — for example "
     "'court records were accessed', 'the case management system was hacked', "
     "'personal information may have been compromised', 'X million licences "
     "leaked', 'customer files exposed'. If the substance is that data got "
     "out, this is the bucket."),
    ("gov_advisory",
     "A government or national cyber agency issuing an advisory, alert, "
     "bulletin or guidance — Cyber Centre, CISA, Five Eyes joint guidance, "
     "RCMP warnings."),
    ("critical_infrastructure",
     "Attacks on or risks to power, water, healthcare, transport, pipelines, "
     "industrial control systems or other essential services."),
    ("fraud_financial",
     "Fraud, identity theft, scams causing financial loss, unauthorised "
     "transactions, crypto theft."),
    ("malware_infostealer",
     "Malware, infostealers, trojans, spyware, botnets — the software itself "
     "rather than a breach outcome."),
    ("ddos_outage",
     "Denial of service, sites taken offline, service outages and disruptions "
     "caused by attack."),
    ("policy_regulation",
     "A specific law, bill, regulation or regulator: PIPEDA, Bill C-xx, "
     "Law 25, a privacy commissioner, a statutory compliance deadline, an "
     "age-verification mandate, a fine issued under a statute. "
     "NOT this bucket: international alliances and trade relationships; "
     "defence procurement or industry certifications; government funding and "
     "investment announcements; 'digital sovereignty' as a political theme; "
     "an organisation's own internal policy such as a bank's password rules; "
     "or general commentary that something *ought* to be regulated. Those are "
     "`other`. Ask: is the post about a named legal instrument or regulator?"),
    ("other",
     "None of the above fits. Use this only after genuinely considering every "
     "bucket above — it is the residual, not a default."),
]
BUCKET_KEYS = [k for k, _ in BUCKETS]

# [MUST] 字段顺序：枚举/布尔在前，自由文本 rationale 殿后。
# 实测 vLLM 的 guided decoding 不约束 JSON 之间的空白，4B 模型写完一段长
# rationale 后会陷入 "\r\n" 死循环，直到耗尽 max_tokens —— 对象永远收不了尾。
# 把结构化字段排在前面，即使尾部烂掉，需要的信息也已经完整产出，可以修复解析。
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "topic_key": {"type": "string", "enum": BUCKET_KEYS},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "off_topic": {
            "type": "boolean",
            "description": "True if this post is not really about Canada-related "
                           "cybersecurity at all (e.g. 'hacked' used about "
                           "sport, or a personal anecdote).",
        },
        "rationale": {
            "type": "string",
            "maxLength": 240,
            "description": "ONE short sentence naming the concrete thing in the "
                           "post that decided it. Do not enumerate the buckets.",
        },
    },
    "required": ["topic_key", "confidence", "off_topic", "rationale"],
    "additionalProperties": False,
}

_FIELD_RE = {
    "topic_key": re.compile(r'"topic_key"\s*:\s*"([a-z_]+)"'),
    "confidence": re.compile(r'"confidence"\s*:\s*"(high|medium|low)"'),
    "off_topic": re.compile(r'"off_topic"\s*:\s*(true|false)'),
    "rationale": re.compile(r'"rationale"\s*:\s*"((?:[^"\\]|\\.)*)"'),
}


def parse_verdict_json(content: str) -> dict:
    """先正常解析；失败则从残缺输出里逐字段抢救。

    抢救是有条件的：``topic_key`` 必须在，否则宁可报错也不猜 ——
    一个猜出来的主题会静默污染整个 label_version。
    """
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    out: dict = {}
    for k, rx in _FIELD_RE.items():
        m = rx.search(content)
        if m:
            out[k] = m.group(1)
    if "topic_key" not in out:
        raise ValueError(f"unparseable model output: {content[:160]!r}")
    out["off_topic"] = out.get("off_topic") == "true"
    out.setdefault("confidence", "low")
    out.setdefault("rationale", "")
    log.debug("recovered truncated JSON for topic=%s", out["topic_key"])
    return out

_BUCKET_BLOCK = "\n".join(f"{i}. {k}\n   {d}" for i, (k, d) in enumerate(BUCKETS, 1))

SYSTEM_PROMPT = f"""You assign one topic label to a social-media post about \
cybersecurity, for a longitudinal study of Canada-related cybersecurity \
discussion.

## Buckets, in priority order

{_BUCKET_BLOCK}

## Rules

1. **Priority order decides ties.** If a post genuinely fits more than one
   bucket, choose the one that appears EARLIEST in the list above. A post about
   a ransomware attack that leaked data is `ransomware`, not `data_breach`.

2. **Judge the substance, not the vocabulary.** The post does not have to use a
   technical term to belong in a bucket. Newspapers write "the court system was
   hacked and records were accessed" — that is `data_breach`. This is the most
   common mistake; do not require a keyword to be present.

3. **`other` is a real answer, and a large one.** Conference announcements,
   job postings, vendor and product news, funding rounds, international
   relations, defence industry news, post-quantum research, AI-safety
   commentary and community activity genuinely belong in `other` under this
   taxonomy — roughly half of this corpus does. Do not force them into a bucket
   they do not fit. Check all ten buckets first, then use `other` without
   hesitation.

   A bucket must fit the post's **subject**, not merely be *adjacent* to it. If
   your reasoning needs the words "relates to", "is associated with" or "falls
   under", it does not fit — choose `other`.

4. **`off_topic` is separate from the bucket.** Set it true when the post is
   not really about Canada-related cybersecurity at all — "hacked down" in a
   football report, a personal story about guessing someone's password, a
   post whose only cyber content is a sponsor tag. Still give your best
   `topic_key` alongside it.

Answer with the required JSON only."""


@dataclass
class TopicVerdict:
    topic_key: str
    rationale: str
    confidence: str
    off_topic: bool
    raw: dict


def classify(client: httpx.Client, base_url: str, model: str, text: str,
             *, lang: str | None = None, seed: int = 20260921,
             timeout: float = 90.0) -> TopicVerdict:
    """判定单条。guided decoding 保证返回值一定在枚举内。"""
    user = f"Post language tag: {lang or 'unknown'}\n\nPost text:\n{text.strip()[:2000]}"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user}],
        "temperature": 0.0,
        "seed": seed,
        # rationale 偶尔写长会把 JSON 截断（实测 120 条里 2 条），
        # guided decoding 也救不了被截断的输出 —— 给足余量
        "max_tokens": 600,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "topic", "strict": True,
                                            "schema": OUTPUT_SCHEMA}},
    }
    r = client.post(f"{base_url.rstrip('/')}/chat/completions", json=payload,
                    timeout=timeout)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    d = parse_verdict_json(content)
    if d["topic_key"] not in BUCKET_KEYS:          # guided decoding 之外的保险
        raise ValueError(f"topic_key out of enum: {d['topic_key']!r}")
    return TopicVerdict(d["topic_key"], d.get("rationale", ""),
                        d.get("confidence", "low"), bool(d.get("off_topic")), d)


def to_label(base: dict, v: TopicVerdict, *, model: str, seed: int) -> Label:
    """把判定拼成 Label。相关性三元组原样继承 rules_v2。"""
    return Label(
        is_cyber=base["is_cyber"],
        is_canada=base["is_canada"],
        is_relevant=base["is_relevant"],
        # 'other' 在库里存 NULL，与规则版一致，否则 GROUP BY 会多出一个假桶
        topic_key=None if v.topic_key == "other" else v.topic_key,
        is_low_information=base["is_low_information"],
        matched_terms={
            "method": "llm", "model": model, "seed": seed,
            "rationale": v.rationale, "llm_confidence": v.confidence,
            "off_topic": v.off_topic,
            "inherited_relevance_from": "rules_v2",
        },
        confidence={"high": 0.9, "medium": 0.6, "low": 0.3}.get(v.confidence),
    )
