"""P-D2 散文论述证据批评节点（事后软审查，铁律1：开放性节点可用 ReAct）。

- 复用 agent_framework/agent.py think_and_act 真循环（同构解读节点）；
- 铁律2 四件套齐全：
  1) 迭代预算 max_iterations（默认 3，批评任务窄）；
  2) 节点级工具白名单 grant_tools——只读检索类工具（read_tender_text/knowledge_search）；
  3) 证据门接口：批评产出自带的【N】引用经 Evidence Grounding Gate 校验，
     无效引用标记剔除（其产出过 2.3 的校验）；
  4) tracing 记账：RunMetrics + CountingLLM；
- 输出：对论述段的风险标注（无据可依/与库内证据冲突/表述过强），
  写回图状态 critique_risks（append reducer），并进决策包风险清单；
- 任何失败降级标注 degraded，不阻塞主链路。
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.agent_engine.evidence_gate import CITATION_MARKER_RE, build_citation_ledger_shared
from core.agent_engine.iron_rules import (
    DEFAULT_CRITIQUE_MAX_ITERATIONS,
    NODE_EVIDENCE_CRITIQUE,
)
from core.agent_engine.metrics import CountingLLM, RunMetrics
from core.agent_framework.agent import Agent
from core.agent_framework.circuit_breaker import CircuitBreaker
from core.agent_framework.tool import ToolRegistry
from core.agent_framework.types import AgentContext, ToolDef
from core.tracing import get_tracer

RISK_TYPES: tuple[str, ...] = ("无据可依", "与库内证据冲突", "表述过强")

CRITIQUE_SYSTEM_PROMPT = (
    "你是证据批评审查员（只读检索）。对给定的标书论述文本做证据批评："
    "逐段检查是否存在以下三类风险："
    "1) 无据可依——论述关键主张在提供的参考材料中找不到支撑；"
    "2) 与库内证据冲突——论述与检索到的库内原文矛盾；"
    "3) 表述过强——承诺/断言超出证据支持范围。"
    "可调用 knowledge_search 核实（必须显式传 collection_name）。"
    f'最终以 JSON 收尾: {{"risks": [{{"segment": "原句摘录(<=100字)", '
    f'"risk_type": "{"|".join(RISK_TYPES)}", "note": "理由(<=80字)", "citation": 编号或null}}]}}'
    "。无风险的段落不要输出。"
)


class CritiqueAgent(Agent):
    name = "evidence_critique_agent"
    system_prompt = CRITIQUE_SYSTEM_PROMPT

    async def run(self, task: str, **kwargs):
        return await self.think_and_act(task, max_iterations=kwargs.get("max_iterations", 3))


def build_critique_tools(state_getter, retrieval_collection: str, ledger: dict) -> ToolRegistry:
    """批评节点工具注册表：白名单只读检索类（read_tender_text + 带记账的 knowledge_search）。"""
    registry = ToolRegistry()
    _, knowledge_search_fn = build_citation_ledger_shared(retrieval_collection, ledger)

    async def read_tender_text(offset: int = 0, length: int = 3000) -> str:
        text = state_getter().get("tender_text") or ""
        return json.dumps({"offset": offset, "text": text[offset : offset + length]}, ensure_ascii=False)

    registry.register_global(
        ToolDef(
            name="read_tender_text",
            description="读取招标文件解析文本的指定片段",
            parameters={
                "type": "object",
                "properties": {
                    "offset": {"type": "integer", "description": "起始偏移"},
                    "length": {"type": "integer", "description": "片段长度"},
                },
            },
            handler=read_tender_text,
        )
    )
    registry.register_global(
        ToolDef(
            name="knowledge_search",
            description="检索知识库（混合检索，必须显式指定 collection_name）",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "查询语句"},
                    "collection_name": {"type": "string", "description": "知识库集合名"},
                    "top_k": {"type": "integer", "description": "返回条数"},
                },
                "required": ["query"],
            },
            handler=knowledge_search_fn,
        )
    )
    return registry


def _extract_risks_json(raw: str) -> list[dict]:
    """从批评输出稳健提取 risks JSON 数组（确定性）。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("risks"), list):
            return [r for r in data["risks"] if isinstance(r, dict)]
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and isinstance(data.get("risks"), list):
                return [r for r in data["risks"] if isinstance(r, dict)]
        except Exception:  # noqa: BLE001
            return []
    return []


def _sanitize_risk(risk: dict, ledger: dict) -> dict | None:
    """证据门接口：风险标注自带的【N】引用经 Gate 同源校验，无效剔除。"""
    risk_type = str(risk.get("risk_type", "")).strip()
    if risk_type not in RISK_TYPES:
        return None
    note = str(risk.get("note", ""))[:200]
    citation: int | None = None
    m = CITATION_MARKER_RE.search(str(risk.get("note", ""))) or CITATION_MARKER_RE.search(
        str(risk.get("segment", ""))
    )
    if m:
        n = int(m.group(1))
        if n in ledger:  # 引用编号必须在本次检索对照表内
            citation = n
    return {
        "segment": str(risk.get("segment", ""))[:160],
        "risk_type": risk_type,
        "note": note,
        "citation": citation,
        "node": NODE_EVIDENCE_CRITIQUE,
    }


def collect_critique_prose(state: dict, limit: int = 6000) -> str:
    """汇总待批评的散文论述（解读内容 + 专家 findings 细节）。"""
    parts: list[str] = []
    interpretation = state.get("interpretation") or {}
    if isinstance(interpretation, dict):
        content = interpretation.get("content") or ""
        if content:
            parts.append(f"【解读论述】\n{content}")
    for node_name, result in (state.get("expert_results") or {}).items():
        if not isinstance(result, dict) or result.get("skipped"):
            continue
        for f in result.get("findings") or []:
            detail = str(f.get("detail") or "")
            if detail:
                parts.append(f"【{node_name}】{f.get('item', '')}: {detail}")
    return "\n".join(parts)[:limit]


async def run_evidence_critique_node(
    state: dict,
    llm: Any,
    metrics: RunMetrics,
    max_iterations: int = DEFAULT_CRITIQUE_MAX_ITERATIONS,
    tool_whitelist: list[str] | None = None,
    retrieval_collection: str = "",
) -> dict:
    """证据批评节点执行体：ReAct 真循环 + 四件套 + 风险标注写回图状态。"""
    tracer = get_tracer()
    span = tracer.start_span(
        f"react.{NODE_EVIDENCE_CRITIQUE}", "agent", {"run.id": str(state.get("run_id", ""))[:40]}
    )
    metrics.start_node(NODE_EVIDENCE_CRITIQUE)
    try:
        prose = collect_critique_prose(state)
        if not prose.strip() or llm is None:
            tracer.end_span(span, status="ok")
            return {
                "critique_risks": [],
                "node_status": {NODE_EVIDENCE_CRITIQUE: "skipped"},
            }

        ledger = {int(k): v for k, v in (state.get("citation_ledger") or {}).items()}
        registry = build_critique_tools(lambda: state, retrieval_collection, ledger)
        whitelist = tool_whitelist or ["read_tender_text", "knowledge_search"]
        registry.grant_tools(CritiqueAgent.name, whitelist)

        counting = CountingLLM(llm, metrics, NODE_EVIDENCE_CRITIQUE)
        ctx = AgentContext(
            agent_id=f"{state.get('run_id', 'run')}:{NODE_EVIDENCE_CRITIQUE}",
            agent_name=CritiqueAgent.name,
            project_id=str(state.get("project_id", "")),
            llm=counting,
            tool_registry=registry,
            circuit_breaker=CircuitBreaker(),
            parameters={"memory_window": 10},
        )
        agent = CritiqueAgent(ctx)
        result = await agent.think_and_act(
            task=f"对以下论述做证据批评并输出JSON风险清单：\n{prose}",
            max_iterations=max_iterations,
        )
        raw_risks = _extract_risks_json(str(result.data or ""))
        risks = [r for r in (_sanitize_risk(r, ledger) for r in raw_risks) if r is not None]
        truncated = not result.success
        if truncated and not risks:
            risks = [
                {
                    "segment": "",
                    "risk_type": "无据可依",
                    "note": "证据批评节点预算截断，未完成审查（降级标注，不阻塞主链路）",
                    "citation": None,
                    "node": NODE_EVIDENCE_CRITIQUE,
                    "degraded": True,
                }
            ]
        tracer.end_span(
            span,
            status="ok" if result.success else "error",
            error_type=None if result.success else "MaxIterations",
            attributes={
                "agent.name": CritiqueAgent.name,
                "run.id": str(state.get("run_id", ""))[:40],
                "critique.risks": len(risks),
            },
        )
        return {
            "critique_risks": risks,
            "citation_ledger": {str(k): v for k, v in ledger.items()},
            "node_status": {NODE_EVIDENCE_CRITIQUE: "done" if result.success else "truncated"},
        }
    except Exception as e:  # noqa: BLE001
        tracer.end_span(span, status="error", error_type=type(e).__name__)
        return {
            "critique_risks": [
                {
                    "segment": "",
                    "risk_type": "无据可依",
                    "note": f"证据批评节点异常降级: {e}",
                    "citation": None,
                    "node": NODE_EVIDENCE_CRITIQUE,
                    "degraded": True,
                }
            ],
            "node_status": {NODE_EVIDENCE_CRITIQUE: "degraded"},
        }
    finally:
        metrics.end_node(NODE_EVIDENCE_CRITIQUE)
