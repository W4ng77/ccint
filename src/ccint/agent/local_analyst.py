"""本地模型 analyst 后端（OpenAI 兼容端点，如 vLLM）。

[决策记录 / 偏离 HANDOFF §M6]
§M6 明确把本地模型列为 [OUT OF SCOPE]，理由是小模型在多步 tool use 和
「拒绝编造证据」这两点上与前沿模型差距明显，而这恰是本 agent 的全部价值。
**该判断依然成立，Anthropic API 仍是默认且推荐的后端。**

本后端的存在理由是一个现实约束：在没有 API 预算时，它让 v0 的 Definition of Done
（「agent 对第一名的解释」）可以被演示，而不是整块缺失。

三条保障，确保它不会悄悄降低交付标准：
  1. 必须显式 `--agent-backend local` 才会启用，默认路径一个字节不动；
  2. 产出的 `analysis_runs.agent_model` / 报告 provenance 如实记录本地模型 ID，
     读者一眼能看出这份分析不是前沿模型出的；
  3. post_id 幻觉校验与 alternative_explanation 必填与 Anthropic 后端**完全共用同一套代码**
     —— 小模型更容易编造证据，而这套校验正是为此设计的，它会把问题暴露在报告的
     warning 区，而不是掩盖。
"""
from __future__ import annotations

import json
import logging

import httpx

from ..models import TrendCandidate
from .analyst import (MAX_TOOL_TURNS, OUTPUT_SCHEMA, AnalysisResult, _dispatch,
                      render_prompt, schema_for, verify_evidence)
from .tools import TOOL_SCHEMAS, TopicTools

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"


def _openai_tools() -> list[dict]:
    """Anthropic 工具格式 → OpenAI 格式。工具定义本身共用，不重复维护。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOL_SCHEMAS
    ]


def resolve_model_identity(base_url: str, served_name: str) -> str:
    """把 served-model-name 解析成真实模型 ID。

    provenance 必须如实 —— `--served-model-name` 可以是任意别名（实测某环境用的是
    "user"），报告读者据此无法判断这份分析出自什么模型。vLLM 的 /v1/models
    在 `root` 字段里带真实模型路径。
    """
    try:
        r = httpx.get(f"{base_url}/models", timeout=10.0)
        for m in r.json().get("data", []):
            if m.get("id") == served_name and m.get("root"):
                return m["root"]
    except Exception as e:  # noqa: BLE001
        log.debug("无法解析真实模型 ID: %s", e)
    return served_name


def _post(base_url: str, payload: dict, timeout: float = 180.0) -> dict:
    r = httpx.post(f"{base_url}/chat/completions", json=payload, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"local model {r.status_code}: {r.text[:300]}")
    return r.json()


def run_analyst_local(
    conn,
    cand: TrendCandidate,
    *,
    as_of: str,
    window_days: int,
    label_version: str,
    model: str = "user",
    base_url: str = DEFAULT_BASE_URL,
    prompt_version: str = "analyst_v1",
    max_turns: int = MAX_TOOL_TURNS,
    null_result=None,
    temperature: float = 0.0,
) -> AnalysisResult:
    tools = TopicTools(conn, label_version)
    prompt = render_prompt(cand, as_of, window_days, prompt_version, null_result)
    out_schema = schema_for(prompt_version)

    messages = [{"role": "user", "content": prompt}]
    tool_calls_log: list[dict] = []

    # ---- 阶段 1：tool use 调查 ----
    for turn in range(max_turns):
        data = _post(base_url, {
            "model": model,
            "messages": messages,
            "tools": _openai_tools(),
            "tool_choice": "auto",
            "temperature": temperature,
            "max_tokens": 2048,
        })
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": calls,
        })
        if not calls:
            log.info("local agent 调查完成，共 %d 轮 tool use", turn)
            break

        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError as e:
                args, result = {}, {"error": f"tool arguments 不是合法 JSON: {e}"}
                tool_calls_log.append({"tool": name, "args": {}, "ok": False,
                                       "error": f"bad JSON arguments: {e}"})
            else:
                try:
                    result = _dispatch(tools, name, args)
                    tool_calls_log.append({"tool": name, "args": args, "ok": True})
                except Exception as e:  # noqa: BLE001
                    result = {"error": f"{type(e).__name__}: {e}"}
                    tool_calls_log.append({"tool": name, "args": args,
                                           "ok": False, "error": str(e)})
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
    else:
        log.warning("local agent 在 %d 轮内未停止调查，直接进入产出阶段", max_turns)

    # ---- 阶段 2：结构化产出 ----
    # 用 vLLM 的 guided decoding 保证 schema 合法 —— 4B 级模型自由生成 JSON 极不可靠，
    # 这一步把「格式是否合法」从模型能力问题变成解码约束问题。
    messages.append({
        "role": "user",
        "content": (
            "现在基于你上面查到的证据，输出最终分析。"
            "evidence 里的每个 post_id 必须是工具真实返回过的 id，不得编造。"
            "alternative_explanation 必填 —— 给出「这个上升也可能只是采集波动或"
            "单一新闻扩散」一类的替代解释。"
        ),
    })
    data = _post(base_url, {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 2048,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "analysis", "schema": out_schema, "strict": True},
        },
    })
    raw = data["choices"][0]["message"]["content"] or "{}"
    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"local model 未产出合法 JSON: {e}; raw={raw[:300]}") from e

    # ---- 阶段 3：与 Anthropic 后端共用的校验 ----
    warnings = verify_evidence(analysis, tools.served_post_ids)
    if warnings:
        log.warning("local agent 校验发现 %d 个问题", len(warnings))

    usage = data.get("usage") or {}
    return AnalysisResult(
        analysis=analysis,
        warnings=warnings,
        tool_calls=tool_calls_log,
        served_post_ids=set(tools.served_post_ids),
        post_urls=dict(tools.served_post_urls),
        model=f"local:{resolve_model_identity(base_url, model)}",
        prompt_version=prompt_version,
        usage={"prompt_tokens": usage.get("prompt_tokens"),
               "completion_tokens": usage.get("completion_tokens")},
    )
