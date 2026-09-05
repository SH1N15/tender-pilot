"""P-D1 主编排图（真实可运行）。

拓扑：
    招标解读(ReAct, 铁律1/2) → [资格专家 ‖ 技术专家 ‖ 商务专家](并行窄职责, 禁ReAct)
    → 规则门(确定性, 经 services/check 只读包装) → 风险汇总(确定性)
    → 决策包生成(定级=规则映射, 铁律3; LLM只写解释) → HITL决策门(interrupt) → 终态

本文件原为 LangGraph 0 调用的 DSL 演示代码，P-D1 重构为真实主编排；
GateKeeper（旧文件闸门）保留不动，资格预审自研状态机不被触碰（并存保回归）。
"""

from __future__ import annotations

import time
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from core.agent_engine.decision import (
    build_decision_package,
    generate_llm_explanation,
    resolve_pending_gate,
)
from core.agent_engine.evidence_critique import run_evidence_critique_node
from core.agent_engine.experts import run_expert_node
from core.agent_engine.iron_rules import (
    NODE_COMMERCIAL,
    NODE_DECISION_PACKAGE,
    NODE_EVIDENCE_CRITIQUE,
    NODE_FINALIZE,
    NODE_HITL_GATE,
    NODE_INTERPRET,
    NODE_QUALIFICATION,
    NODE_RISK_SUMMARY,
    NODE_RULE_GATE,
    NODE_TECHNICAL,
)
from core.agent_engine.metrics import CountingLLM, RunMetrics
from core.agent_engine.react_node import run_interpret_node
from core.agent_engine.rule_gate import run_risk_summary_node, run_rule_gate_node
from core.agent_engine.state import GraphState

EXPERT_NODES = (NODE_QUALIFICATION, NODE_TECHNICAL, NODE_COMMERCIAL)


class BidGraphOrchestrator:
    """主编排器：构建并运行 P-D1 主图。

    Args:
        llm: LLM 网关（duck-typed: chat/collect_json/chat_with_tools）；
        checkpointer: langgraph BaseCheckpointSaver（PG 自研 JSONB saver 或内存 saver）；
        decision_timeout_seconds: HITL 超时阈值（秒级可配，铁律4）。
    """

    def __init__(
        self,
        llm: Any,
        checkpointer: Any = None,
        decision_timeout_seconds: float | None = None,
        react_max_iterations: int = 6,
        critique_max_iterations: int = 3,
        retrieval_collection: str = "",
    ):
        self.llm = llm
        self.checkpointer = checkpointer
        self.decision_timeout_seconds = decision_timeout_seconds
        self.react_max_iterations = react_max_iterations
        self.critique_max_iterations = critique_max_iterations
        self.retrieval_collection = retrieval_collection
        self._metrics: dict[str, RunMetrics] = {}
        self._graph = self._build_graph()

    # ---------------- 图构建 ----------------

    def _build_graph(self):
        graph = StateGraph(GraphState)

        async def interpret_node(state: GraphState) -> dict:
            metrics = self._metrics.get(state.get("run_id", ""), RunMetrics(state.get("run_id", "")))
            metrics.start_node(NODE_INTERPRET)
            try:
                return await run_interpret_node(
                    dict(state),
                    self.llm,
                    metrics,
                    max_iterations=self.react_max_iterations,
                    retrieval_collection=self.retrieval_collection,
                )
            finally:
                metrics.end_node(NODE_INTERPRET)

        def make_expert(node_name: str):
            async def expert_node(state: GraphState) -> dict:
                metrics = self._metrics.get(state.get("run_id", ""), RunMetrics(state.get("run_id", "")))
                metrics.start_node(node_name)
                try:
                    return await run_expert_node(node_name, dict(state), self.llm, metrics=metrics)
                finally:
                    metrics.end_node(node_name)

            return expert_node

        async def rule_gate_node(state: GraphState) -> dict:
            metrics = self._metrics.get(state.get("run_id", ""), RunMetrics(state.get("run_id", "")))
            metrics.start_node(NODE_RULE_GATE)
            try:
                return await run_rule_gate_node(dict(state), self.llm, metrics)
            finally:
                metrics.end_node(NODE_RULE_GATE)

        async def risk_summary_node(state: GraphState) -> dict:
            metrics = self._metrics.get(state.get("run_id", ""), RunMetrics(state.get("run_id", "")))
            metrics.start_node(NODE_RISK_SUMMARY)
            try:
                return run_risk_summary_node(dict(state), metrics)
            finally:
                metrics.end_node(NODE_RISK_SUMMARY)

        async def evidence_critique_node(state: GraphState) -> dict:
            """P-D2：散文论述证据批评（事后软审查，风险标注进决策包）。"""
            metrics = self._metrics.get(state.get("run_id", ""), RunMetrics(state.get("run_id", "")))
            try:
                return await run_evidence_critique_node(
                    dict(state),
                    self.llm,
                    metrics,
                    max_iterations=self.critique_max_iterations,
                    retrieval_collection=self.retrieval_collection,
                )
            finally:
                metrics.end_node(NODE_EVIDENCE_CRITIQUE)

        async def decision_package_node(state: GraphState) -> dict:
            metrics = self._metrics.get(state.get("run_id", ""), RunMetrics(state.get("run_id", "")))
            metrics.start_node(NODE_DECISION_PACKAGE)
            started = time.monotonic()
            try:
                package = build_decision_package(
                    state.get("rule_results") or [],
                    state.get("expert_results") or {},
                    state.get("risk_summary") or {},
                )
                # P-D2：证据批评风险标注并入决策包风险清单
                critique_risks = state.get("critique_risks") or []
                if critique_risks:
                    package["risks"] = list(package.get("risks") or []) + list(critique_risks)
                # 铁律3：LLM 只写解释文字，定级已在 build_decision_package 内确定性产生
                counting_llm = (
                    CountingLLM(self.llm, metrics, NODE_DECISION_PACKAGE)
                    if (metrics is not None and self.llm is not None)
                    else self.llm
                )
                explanation = await generate_llm_explanation(counting_llm, package)
                if explanation:
                    package["rationale"] = package["rationale"] + f" LLM解读：{explanation}"
                # 记录决策门挂起起始时间（铁律4 超时策略用）
                metrics.nodes.setdefault(NODE_DECISION_PACKAGE, {})["_gate_started_at"] = time.monotonic()
                return {
                    "decision_package": package,
                    "node_status": {NODE_DECISION_PACKAGE: "done"},
                }
            finally:
                metrics.end_node(NODE_DECISION_PACKAGE, started)

        async def hitl_gate_node(state: GraphState) -> dict:
            """HITL 决策门：interrupt 挂起，恢复值={action: approve|override, level?, reason}。"""
            package = state.get("decision_package") or {}
            decision = interrupt({"decision_package": package})
            updates: dict[str, Any] = {"human_decision": dict(decision)}
            action = str(decision.get("action", "approve"))
            if action == "override":
                reason = str(decision.get("reason", "")).strip()
                if not reason:  # 铁律5：改判必须带理由
                    return {**updates, "errors": ["override 必须携带理由（铁律5）"]}
                updates["override_reason"] = reason
                updates["final_level"] = str(decision.get("level", package.get("level", "")))
            else:
                updates["final_level"] = str(package.get("level", ""))
            return updates

        def finalize_node(state: GraphState) -> dict:
            package = state.get("decision_package") or {}
            human = state.get("human_decision") or {}
            return {
                "current_stage": "finalized",
                "node_status": {NODE_FINALIZE: "done"},
                "final_level": state.get("final_level") or package.get("level") or human.get("level") or "",
                "decision_package": {**package, "final_decision": human},
            }

        graph.add_node(NODE_INTERPRET, interpret_node)
        for name in EXPERT_NODES:
            graph.add_node(name, make_expert(name))
        graph.add_node(NODE_RULE_GATE, rule_gate_node)
        graph.add_node(NODE_RISK_SUMMARY, risk_summary_node)
        graph.add_node(NODE_EVIDENCE_CRITIQUE, evidence_critique_node)
        graph.add_node(NODE_DECISION_PACKAGE, decision_package_node)
        graph.add_node(NODE_HITL_GATE, hitl_gate_node)
        graph.add_node(NODE_FINALIZE, finalize_node)

        graph.add_edge(START, NODE_INTERPRET)
        graph.add_edge(NODE_INTERPRET, NODE_QUALIFICATION)
        graph.add_edge(NODE_INTERPRET, NODE_TECHNICAL)
        graph.add_edge(NODE_INTERPRET, NODE_COMMERCIAL)
        for name in EXPERT_NODES:
            graph.add_edge(name, NODE_RULE_GATE)
        graph.add_edge(NODE_RULE_GATE, NODE_RISK_SUMMARY)
        graph.add_edge(NODE_RISK_SUMMARY, NODE_EVIDENCE_CRITIQUE)
        graph.add_edge(NODE_EVIDENCE_CRITIQUE, NODE_DECISION_PACKAGE)
        graph.add_edge(NODE_DECISION_PACKAGE, NODE_HITL_GATE)
        graph.add_edge(NODE_HITL_GATE, NODE_FINALIZE)
        graph.add_edge(NODE_FINALIZE, END)

        return graph.compile(checkpointer=self.checkpointer)

    # ---------------- 运行入口 ----------------

    def _config(self, thread_id: str) -> dict:
        from core.settings import graph_runtime_config

        return graph_runtime_config(thread_id)

    async def run_until_interrupt(self, run_id: str, run_input: dict) -> dict[str, Any]:
        """启动一次图运行，跑到 HITL 决策门挂起（或已过门则到终态）。"""
        self._metrics[run_id] = RunMetrics(run_id)
        run_input = {"run_id": run_id, **run_input}
        await self._graph.ainvoke(run_input, self._config(run_id))
        return await self.snapshot(run_id)

    async def resume(self, run_id: str, decision: dict) -> dict[str, Any]:
        """从 HITL 决策门恢复执行（approve / override+reason）。"""
        if not self._metrics.get(run_id):
            self._metrics[run_id] = RunMetrics(run_id)
        await self._graph.ainvoke(Command(resume=decision), self._config(run_id))
        return await self.snapshot(run_id)

    async def snapshot(self, run_id: str) -> dict[str, Any]:
        """状态快照：各节点状态/当前挂起门/决策包。"""
        state = await self._graph.aget_state(self._config(run_id))
        values = dict(state.values or {})
        return {
            "run_id": run_id,
            "current_stage": values.get("current_stage", ""),
            "node_status": values.get("node_status", {}),
            "pending_gate": "hitl_decision_gate" if state.next else None,
            "next_nodes": list(state.next or []),
            "decision_package": values.get("decision_package"),
            "evidence_grounding": values.get("evidence_grounding"),
            "critique_risks": values.get("critique_risks", []),
            "human_decision": values.get("human_decision"),
            "override_reason": values.get("override_reason", ""),
            "final_level": values.get("final_level", ""),
            "errors": values.get("errors", []),
        }

    def cost_report(self, run_id: str) -> dict:
        metrics = self._metrics.get(run_id)
        return metrics.snapshot() if metrics else {
            "run_id": run_id, "nodes": {}, "total_llm_calls": 0, "total_tokens": 0
        }

    # ---------------- 铁律4：超时策略 ----------------
    async def apply_gate_timeout(self, run_id: str, configured: float | None = None) -> dict:
        """若运行挂在决策门且达到阈值，仅返回人工等待/告警，不自动放行。"""
        snap = await self.snapshot(run_id)
        package = snap.get("decision_package") or {}
        level = str(package.get("level", ""))
        if not snap.get("pending_gate"):
            return {"action": "not_pending"}
        pending_seconds = self._pending_seconds(run_id)
        threshold = configured if configured is not None else self.decision_timeout_seconds
        result = resolve_pending_gate(level, pending_seconds, threshold)
        if result.get("action") == "approve":
            await self.resume(run_id, {"action": "approve", "auto": True, "reason": result["reason"]})
            result["applied"] = True
        return result

    def _pending_seconds(self, run_id: str) -> float:
        metrics = self._metrics.get(run_id)
        if metrics is None:
            return 0.0
        bucket = metrics.nodes.get(NODE_DECISION_PACKAGE, {})
        started = bucket.get("_gate_started_at")
        return max(0.0, time.monotonic() - started) if started else 0.0



# 兼容别名：旧 __init__ 延迟导入 AgentOrchestrator
AgentOrchestrator = BidGraphOrchestrator


async def run_full_graph_demo(llm: Any, checkpointer: Any, run_input: dict, decision: dict | None = None) -> dict:
    """便捷入口：全图跑 + 一次决策（demo/脚本用）。"""
    orchestrator = BidGraphOrchestrator(llm=llm, checkpointer=checkpointer)
    run_id = run_input.get("run_id", "demo")
    await orchestrator.run_until_interrupt(run_id, run_input)
    if decision is not None:
        await orchestrator.resume(run_id, decision)
    return await orchestrator.snapshot(run_id)
