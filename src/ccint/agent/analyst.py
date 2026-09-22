"""Agent analyst（HANDOFF §M6）。

[MUST] Agent 不判断什么算 trend —— trend 由 M5 统计层检出，agent 只做 analyst。
       工具全部只读，agent 拿不到任何能改排名或自选分析对象的能力。
[MUST] evidence 中每个 post_id 必须真实存在于本次工具返回结果中。发现幻觉
       记入报告 warning 区，不静默丢弃。
[MUST] alternative_explanation 必填。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..models import TrendCandidate
from .tools import TOOL_SCHEMAS, TopicTools

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
MAX_TOOL_TURNS = 12

# [MUST] 输出必须结构化，不是自由文本。用 output_config.format 由 API 保证 schema 合法。
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "topic_key": {"type": "string"},
        "what_is_happening": {
            "type": "string",
            "description": "具体说明发生了什么，点名实际的实体与事件。",
        },
        "is_single_event_or_multiple": {
            "type": "string",
            "enum": ["single", "multiple", "unclear"],
        },
        "key_entities": {"type": "array", "items": {"type": "string"}},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["post_id", "why"],
                "additionalProperties": False,
            },
        },
        "alternative_explanation": {
            "type": "string",
            "description": "必填。这个上升也可能只是采集波动/单一新闻扩散/少数账号刷量。",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "confidence_reason": {"type": "string"},
    },
    "required": [
        "topic_key", "what_is_happening", "is_single_event_or_multiple",
        "key_entities", "evidence", "alternative_explanation",
        "confidence", "confidence_reason",
    ],
    "additionalProperties": False,
}


# analyst_v2：零模型判定为 noise 时使用。强制 agent 就「能否宣称趋势」表态，
# 不允许把一个统计上不可区分于噪声的变化写成「正在上升」。
OUTPUT_SCHEMA_V2 = json.loads(json.dumps(OUTPUT_SCHEMA))
OUTPUT_SCHEMA_V2["properties"]["trend_claim_supported"] = {
    "type": "string",
    "enum": ["supported", "not_supported", "undetermined"],
    "description": "本窗口的证据是否支持『该主题正在上升』。"
                   "统计层判定为 noise 时，除非你有压倒性的内容层证据，否则应为 not_supported。",
}
OUTPUT_SCHEMA_V2["properties"]["coherence"] = {
    "type": "string",
    "enum": ["one_story", "few_stories", "unrelated"],
    "description": "这些帖子是同一件事、几件事，还是只是共享标签的无关内容。",
}
OUTPUT_SCHEMA_V2["required"] = OUTPUT_SCHEMA["required"] + [
    "trend_claim_supported", "coherence"]

OUTPUT_SCHEMAS = {"analyst_v1": OUTPUT_SCHEMA, "analyst_v2": OUTPUT_SCHEMA_V2}


def schema_for(prompt_version: str) -> dict:
    return OUTPUT_SCHEMAS.get(prompt_version, OUTPUT_SCHEMA)


@dataclass
class AnalysisResult:
    analysis: dict
    warnings: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    served_post_ids: set[int] = field(default_factory=set)
    post_urls: dict = field(default_factory=dict)
    model: str | None = None
    prompt_version: str = "analyst_v1"
    usage: dict = field(default_factory=dict)


def render_prompt(cand: TrendCandidate, as_of: str, window_days: int,
                  version: str = "analyst_v1", null_result=None) -> str:
    tpl = (PROMPT_DIR / f"{version}.md").read_text(encoding="utf-8")
    growth = "n/a (previous window had no posts)" if cand.rel_growth is None \
        else f"{cand.rel_growth:+.0%}"
    fields = dict(
        topic_key=cand.topic_key, as_of=as_of, window_days=window_days,
        cur_posts=cand.cur_posts, cur_authors=cand.cur_authors,
        prev_posts=cand.prev_posts, prev_authors=cand.prev_authors,
        abs_delta=f"{cand.abs_delta:+d}", rel_growth=growth,
        cur_share=f"{cand.cur_share:.1%}", prev_share=f"{cand.prev_share:.1%}",
    )
    if null_result is not None:
        t = null_result.by_topic().get(cand.topic_key)
        if t is not None:
            fields.update(
                null_method=null_result.method, null_unit=null_result.unit,
                null_iter=f"{null_result.n_iter:,}",
                null_sd=f"{t.null_sd:.2f}", null_p95=f"{t.null_p95_abs:.0f}",
                p_raw=f"{t.p_raw:.3f}", p_fwer=f"{t.p_fwer:.3f}",
                verdict=t.verdict, max_null_p95=f"{null_result.max_null_p95:.0f}",
                n_topics_tested=len(null_result.topics),
            )
    return tpl.format(**fields)


def verify_evidence(analysis: dict, served: set[int]) -> list[str]:
    """[MUST] 逐一核对 evidence 的 post_id 是否真实来自工具返回。

    幻觉不静默丢弃 —— 记入 warning，让报告读者知道本次 agent 的可靠程度。
    """
    warnings: list[str] = []
    ev = analysis.get("evidence") or []
    if not ev:
        warnings.append("agent 未提供任何 evidence —— 结论无法追溯到具体 post。")
    for item in ev:
        pid = item.get("post_id")
        if pid is None or int(pid) not in served:
            warnings.append(
                f"HALLUCINATED post_id={pid}：该 id 不在本次工具返回的 "
                f"{len(served)} 条结果中。相关结论不可采信。"
            )
    if not (analysis.get("alternative_explanation") or "").strip():
        warnings.append("alternative_explanation 为空 —— 该字段是强制要求。")
    return warnings


def _dispatch(tools: TopicTools, name: str, args: dict):
    fn = {
        "get_topic_stats": tools.get_topic_stats,
        "get_representative_posts": tools.get_representative_posts,
        "compare_periods": tools.compare_periods,
    }.get(name)
    if fn is None:
        raise ValueError(f"unknown tool: {name}")
    return fn(**args)


def run_analyst(
    conn,
    cand: TrendCandidate,
    *,
    as_of: str,
    window_days: int,
    label_version: str,
    model: str,
    api_key: str,
    prompt_version: str = "analyst_v1",
    client=None,
    max_turns: int = MAX_TOOL_TURNS,
    null_result=None,
) -> AnalysisResult:
    """跑 agent loop：tool use → 结构化输出 → post_id 校验。"""
    import anthropic

    client = client or anthropic.Anthropic(api_key=api_key)
    tools = TopicTools(conn, label_version)
    prompt = render_prompt(cand, as_of, window_days, prompt_version, null_result)
    out_schema = schema_for(prompt_version)

    messages = [{"role": "user", "content": prompt}]
    tool_calls: list[dict] = []
    final_text = None

    for turn in range(max_turns):
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            tools=TOOL_SCHEMAS,
            output_config={"format": {"type": "json_schema", "schema": out_schema}},
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "refusal":
            raise RuntimeError(f"model refused: {getattr(resp, 'stop_details', None)}")

        if resp.stop_reason != "tool_use":
            final_text = "".join(b.text for b in resp.content if b.type == "text")
            break

        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            args = block.input if isinstance(block.input, dict) else json.loads(block.input)
            log.info("agent tool: %s(%s)", block.name, args)
            try:
                out = _dispatch(tools, block.name, args)
                tool_calls.append({"tool": block.name, "args": args, "ok": True})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(out, ensure_ascii=False)})
            except Exception as e:  # noqa: BLE001
                tool_calls.append({"tool": block.name, "args": args,
                                   "ok": False, "error": str(e)})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "is_error": True, "content": str(e)})
        # [MUST] 所有 tool_result 必须放在同一条 user message 里
        messages.append({"role": "user", "content": results})
    else:
        raise RuntimeError(f"agent 在 {max_turns} 轮内未收敛")

    if not final_text:
        raise RuntimeError("agent 未返回结构化输出")
    analysis = json.loads(final_text)

    warnings = verify_evidence(analysis, tools.served_post_ids)
    if warnings:
        log.warning("agent output warnings: %s", warnings)

    return AnalysisResult(
        analysis=analysis, warnings=warnings, tool_calls=tool_calls,
        served_post_ids=set(tools.served_post_ids),
        post_urls=dict(tools.served_post_urls), model=model,
        prompt_version=prompt_version,
        usage={"turns": len(tool_calls)},
    )
