"""ReAct 解读节点（铁律1+2）。

- 仅 REACT_ALLOWED_NODES 中的开放节点可用 ReAct（本模块只服务 tender_interpretation，
  evidence_critique / competition_landscape 留同构占位接口）；
- 四件套：迭代预算(max_iterations) / 节点级工具白名单(grant_tools) /
  证据门接口占位(本期 passthrough，P-D2 接管) / tracing 成本记账(RunMetrics + CountingLLM)；
- 复用 core/agent_framework/agent.py:31-117 的 think_and_act 真循环；
- 检索工具显式传 collection_name（core/rag_engine/retriever.py:23-27 签名，只调用不修改）。
"""

from __future__ import annotations

import json
from typing import Any

from core.agent_engine.evidence_gate import build_citation_ledger_shared, ground_hard_facts
from core.agent_engine.iron_rules import (
    DEFAULT_REACT_MAX_ITERATIONS,
    NODE_INTERPRET,
    REACT_ALLOWED_NODES,
)
from core.agent_engine.metrics import CountingLLM, RunMetrics
from core.agent_framework.agent import Agent
from core.agent_framework.circuit_breaker import CircuitBreaker
from core.agent_framework.tool import ToolRegistry
from core.agent_framework.types import AgentContext, ToolDef
from core.tracing import get_tracer


def evidence_gate_placeholder(content: str, state: dict) -> str:
    """铁律2第三件套：证据门接口。P-D2 起由 Evidence Grounding Gate 接管：
    硬事实断言必须命中知识库原文，否则丢弃并标'待补充'。"""
    return content


def build_interpret_tools(state_getter, retrieval_collection: str, ledger: dict | None = None) -> ToolRegistry:
    """构建解读节点工具注册表（节点级白名单：仅以下三件）。

    P-D2：ledger 传入时，knowledge_search 命中的检索产物自动登记引用对照表。
    """
    registry = ToolRegistry()
    ledger, knowledge_search_fn = build_citation_ledger_shared(
        retrieval_collection, ledger if ledger is not None else {}
    )
    # 把 ledger 挂回注册表对象外层（调用方持同一 dict 引用即可读到登记结果）
    registry._citation_ledger = ledger  # noqa: SLF001

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
            description=(
                "检索知识库（混合检索）。collection_name 可省略：省略时自动检索全部业务知识库"
                "（招标库+企业库）；返回条目按序编号，供正文【N】引用标注。"
            ),
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

    async def calculator(expression: str) -> str:
        allowed = set("0123456789+-*/().,% ")
        if not set(expression) <= allowed:
            return json.dumps({"error": "表达式含非法字符"})
        try:
            return json.dumps({"result": eval(expression, {"__builtins__": {}})})  # noqa: S307
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": str(e)})

    registry.register_global(
        ToolDef(
            name="calculator",
            description="四则运算计算器",
            parameters={
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "算术表达式"}},
                "required": ["expression"],
            },
            handler=calculator,
        )
    )
    return registry


class InterpretAgent(Agent):
    name = "tender_interpret_agent"
    # G-0-2：明确要求先检索证据、对硬事实标注【N】引用——否则 Evidence Gate
    # 会把无引用的硬事实全部拒为"待补充"（解读节点 Grounding 0/6 的根因之一）。
    system_prompt = (
        "你是招标解读专家（开放性调查节点，允许 ReAct 多步探索）。"
        "请通读招标文件，产出结构化解读：项目概况、关键资格条件、评分权重要点、"
        "主要风险条款。最终以 JSON 收尾。"
        "\n引用铁律：凡涉及金额、日期时限、资质编号、技术参数值等硬事实的句子，"
        "必须先调用 knowledge_search 检索证据，并在该句末尾标注【N】"
        "（N 为检索返回条目的序号，同一编号可复用）；检索不到证据的硬事实不要编造，"
        "直接省略或注明'招标文件未提及'。"
    )

    async def run(self, task: str, **kwargs):
        # P-D1：解读节点直接使用 think_and_act 真循环（Agent 基类抽象方法的落地）
        return await self.think_and_act(task, max_iterations=kwargs.get("max_iterations", 10))


def assert_react_allowed(node_name: str) -> None:
    """铁律1守卫：非白名单节点禁用 ReAct。"""
    if node_name not in REACT_ALLOWED_NODES:
        raise RuntimeError(f"铁律1：节点 {node_name} 属结构化生产任务，禁用 ReAct")


async def run_interpret_node(
    state: dict,
    llm: Any,
    metrics: RunMetrics,
    max_iterations: int = DEFAULT_REACT_MAX_ITERATIONS,
    tool_whitelist: list[str] | None = None,
    retrieval_collection: str = "",
) -> dict:
    """解读节点执行体：think_and_act 真循环 + 四件套。"""
    assert_react_allowed(NODE_INTERPRET)
    tracer = get_tracer()
    span = tracer.start_span(
        f"react.{NODE_INTERPRET}", "agent", {"run.id": str(state.get("run_id", ""))[:40]}
    )
    metrics.start_node(NODE_INTERPRET)
    try:
        tender_text = (state.get("tender_text") or "").strip()
        if not tender_text:
            tracer.end_span(span, status="ok")
            return {
                "node_status": {NODE_INTERPRET: "skipped"},
                "interpretation": {"skipped": True, "reason": "缺招标文件文本"},
            }

        ledger: dict = {}
        registry = build_interpret_tools(lambda: state, retrieval_collection, ledger)
        # 铁律2第二件套：节点级工具白名单 grant_tools
        whitelist = tool_whitelist or ["read_tender_text", "knowledge_search", "calculator"]
        registry.grant_tools(InterpretAgent.name, whitelist)

        counting = CountingLLM(llm, metrics, NODE_INTERPRET)
        ctx = AgentContext(
            agent_id=f"{state.get('run_id', 'run')}:{NODE_INTERPRET}",
            agent_name=InterpretAgent.name,
            project_id=str(state.get("project_id", "")),
            llm=counting,
            tool_registry=registry,
            circuit_breaker=CircuitBreaker(),
            parameters={"memory_window": 10},
        )
        agent = InterpretAgent(ctx)
        # 铁律2第一件套：迭代预算
        # G-0-2：任务文本内联引用铁律（system_prompt 之外再强调一次，硬事实必须
        # 先检索后标注【N】，否则 Evidence Gate 全部拒为"待补充"）。
        task = (
            f"解读以下招标文件并给出结构化要点：\n{tender_text[:6000]}\n\n"
            "硬性要求：输出中凡含金额/日期时限/资质编号/技术参数值的句子，"
            "必须先调用 knowledge_search 用该事实的关键词检索，并在句末标注【N】"
            "（N=检索返回条目中的 n 字段）；最终 JSON 里含硬事实的字符串值也要带【N】后缀；"
            "检索不到证据的硬事实不要写入输出。"
        )
        result = await agent.think_and_act(
            task=task,
            max_iterations=max_iterations,
        )
        # 铁律2第三件套：证据门接口——P-D2 起 Evidence Grounding Gate 接管
        content = evidence_gate_placeholder(str(result.data or ""), state)
        grounding = ground_hard_facts(content, ledger)
        metrics.add_grounding(grounding["stats"])

        truncated = not result.success
        tracer.end_span(
            span,
            status="ok" if result.success else "error",
            error_type=None if result.success else "MaxIterations",
            attributes={"agent.name": InterpretAgent.name, "run.id": str(state.get("run_id", ""))[:40]},
        )
        return {
            "interpretation": {
                "content": content,
                "grounded_content": grounding["text"],
                "grounding_stats": grounding["stats"],
                "grounding_rejected": grounding["rejected"],
                "truncated_by_budget": truncated,
                "tool_calls": result.tool_calls_log,
                "whitelist": whitelist,
                "max_iterations": max_iterations,
            },
            "citation_ledger": {str(k): v for k, v in ledger.items()},
            "evidence_grounding": {
                "node": NODE_INTERPRET,
                "stats": grounding["stats"],
                "passed": grounding["passed"],
                "rejected": grounding["rejected"],
            },
            "node_status": {NODE_INTERPRET: "done" if result.success else "truncated"},
        }
    finally:
        metrics.end_node(NODE_INTERPRET)
