"""三专家节点：资格/技术/商务——窄职责 Pydantic 类型化节点（铁律1：禁 ReAct）。

LLM 调用前后挂确定性校验器：
- 前置：输入参数校验（tender_text 必须非空等，缺失则 skipped，不伪造）；
- 后置：schema 校验 + 必填字段 + 枚举/范围检查；校验失败有界重试（默认2次），
  仍失败则降级标注 "校验未过"（degraded=True），绝不静默放行。
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field, field_validator

from core.agent_engine.iron_rules import (
    NODE_COMMERCIAL,
    NODE_QUALIFICATION,
    NODE_TECHNICAL,
)
from core.agent_engine.metrics import CountingLLM
from core.tracing import get_tracer

# ---------------- 输出 Pydantic schema（窄职责类型化） ----------------


class Finding(BaseModel):
    item: str = Field(description="检查/评估条目")
    status: str = Field(description="pass/fail/warning")
    detail: str = ""

    @field_validator("status")
    @classmethod
    def _status_enum(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("pass", "fail", "warning"):
            raise ValueError(f"status 必须是 pass/fail/warning，得到: {v!r}")
        return v


class ExpertOutput(BaseModel):
    expert: str = Field(description="expert 名称: qualification/technical/commercial")
    findings: list[Finding] = Field(default_factory=list, min_length=1)
    overall_status: str
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("expert")
    @classmethod
    def _expert_enum(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("qualification", "technical", "commercial"):
            raise ValueError(f"expert 必须是 qualification/technical/commercial，得到: {v!r}")
        return v

    @field_validator("overall_status")
    @classmethod
    def _overall_enum(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("pass", "fail", "warning"):
            raise ValueError(f"overall_status 必须是 pass/fail/warning，得到: {v!r}")
        return v


EXPERT_SPECS: dict[str, dict] = {
    NODE_QUALIFICATION: {
        "expert": "qualification",
        "check_name": "资格符合性比对",
        "prompt": (
            "你是资格专家。对投标文件与招标文件的资格要求做逐项比对（结构化任务；status 只能取 pass/fail/warning）。"
            "返回JSON: {\"expert\":\"qualification\",\"findings\":[{\"item\",\"status\",\"detail\"}],"
            "\"overall_status\":\"pass/fail/warning\",\"confidence\":0-1}"
        ),
    },
    NODE_TECHNICAL: {
        "expert": "technical",
        "check_name": "技术参数应答比对",
        "prompt": (
            "你是技术专家。对招标技术参数要求与投标应答做逐项比对（结构化任务；status 只能取 pass/fail/warning）。"
            "返回JSON: {\"expert\":\"technical\",\"findings\":[{\"item\",\"status(pass/fail/warning)\",\"detail\"}],"
            "\"overall_status\":\"pass/fail/warning\",\"confidence\":0-1}"
        ),
    },
    NODE_COMMERCIAL: {
        "expert": "commercial",
        "check_name": "商务条款比对",
        "prompt": (
            "你是商务专家。对报价/工期/付款/保证金等商务条款做逐项比对（结构化任务；status 只能取 pass/fail/warning）。"
            "返回JSON: {\"expert\":\"commercial\",\"findings\":[{\"item\",\"status(pass/fail/warning)\",\"detail\"}],"
            "\"overall_status\":\"pass/fail/warning\",\"confidence\":0-1}"
        ),
    },
}


def pre_validate_expert_input(state: dict, node_name: str) -> str | None:
    """前置确定性校验：输入不足返回 skipped 原因（不伪造结果）。"""
    tender_text = (state.get("tender_text") or "").strip()
    bid_text = (state.get("bid_text") or "").strip()
    if not tender_text:
        return "缺招标文件文本，无法比对"
    if not bid_text:
        return "缺投标文件文本，无法比对"
    return None


def post_validate_expert_output(raw: Any, expected_expert: str) -> ExpertOutput:
    """后置确定性校验：Pydantic schema + expert 字段与节点匹配。"""
    if isinstance(raw, ExpertOutput):
        output = raw
    elif isinstance(raw, dict):
        output = ExpertOutput.model_validate(raw)
    else:
        raise ValueError(f"专家输出类型非法: {type(raw).__name__}")
    if output.expert != expected_expert:
        raise ValueError(f"expert 字段不匹配: 期望 {expected_expert}, 得到 {output.expert}")
    return output


async def run_expert_node(
    node_name: str,
    state: dict,
    llm: Any,
    max_retries: int = 2,
    metrics: Any = None,
) -> dict:
    """通用专家节点执行体（LLM 前后挂确定性校验器 + 有界重试 + 降级标注）。"""
    spec = EXPERT_SPECS[node_name]
    expected = spec["expert"]
    tracer = get_tracer()
    span = tracer.start_span(f"expert.{expected}", "agent", {"run.id": str(state.get("run_id", ""))[:40]})

    started = time.monotonic()
    llm_calls = 0
    try:
        skip_reason = pre_validate_expert_input(state, node_name)
        if skip_reason:
            tracer.end_span(span, status="ok", attributes={"expert.skipped": "input_missing"})
            return {
                "expert_results": {
                    node_name: {"skipped": True, "reason": skip_reason, "expert": expected}
                },
                "node_status": {node_name: "skipped"},
            }

        messages = [
            {"role": "system", "content": spec["prompt"]},
            {
                "role": "user",
                "content": (
                    f"招标文件（节选）：\n{(state.get('tender_text') or '')[:4000]}\n\n"
                    f"投标文件（节选）：\n{(state.get('bid_text') or '')[:4000]}"
                ),
            },
        ]

        last_error: Exception | None = None
        counting_llm = CountingLLM(llm, metrics, node_name) if metrics is not None else llm
        for _attempt in range(max_retries + 1):
            llm_calls += 1
            raw = await counting_llm.collect_json(messages=messages, temperature=0.1)
            try:
                output = post_validate_expert_output(raw, expected)
                tracer.end_span(span, status="ok")
                return {
                    "expert_results": {
                        node_name: {
                            "expert": output.expert,
                            "findings": [f.model_dump() for f in output.findings],
                            "overall_status": output.overall_status,
                            "confidence": output.confidence,
                            "degraded": False,
                        }
                    },
                    "node_status": {node_name: "done"},
                    "llm_calls": llm_calls,
                }
            except Exception as e:  # noqa: BLE001
                last_error = e

        # 有界重试后仍失败：降级标注"校验未过"，不得静默放行
        tracer.end_span(span, status="error", error_type="ValidationError")
        return {
            "expert_results": {
                node_name: {
                    "expert": expected,
                    "degraded": True,
                    "degrade_reason": f"校验未过（重试{max_retries}次）: {last_error}",
                    "raw_preview": str(raw)[:500],
                }
            },
            "node_status": {node_name: "degraded"},
            "llm_calls": llm_calls,
        }
    finally:
        if metrics is not None:
            metrics.end_node(node_name, started)
