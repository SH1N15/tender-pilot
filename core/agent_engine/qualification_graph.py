"""G-1 资格预审入图：资格预审工作流成为 LangGraph 图（P-G 任务书 G-1 条目 1/2/3）。

拓扑（全部确定性节点，铁律 2：禁 ReAct，无 LLM 调用）：
    资格要求提取(qualification_extract) → 企业凭证比对(qualification_match)
    → HITL 审批门(qualification_hitl_gate, interrupt; confirm/reject/mark_insufficient)
    → 完成终态(qualification_finalize)

语义约束：
- 三种审批动作语义与旧自研状态机（services/qualification/workflow.py）完全一致：
  内部直接复用 approve_qualification_workflow / run_qualification_workflow（不复制逻辑）；
- 旧状态机保留只读回归：图运行产生的 workflow 同时写入 WorkflowStore（可经
  GET /api/qualification/workflow/{id} 查询），旧 API 一律不删不改；
- 飞轮数据源不断：比对节点写 run Trace、审批门写 approval Trace，均经
  services/qualification/flywheel.py 白名单脱敏（TraceStore.append 内置清洗）；
- kill 后可从最近 checkpoint resume：workflow 对象缺失时由图状态重建入 store；
- 资格审批门是人工专属门：超时策略永不自动决策（不伪造 confirm/reject）。
"""

from __future__ import annotations

import operator
import time
import uuid
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from services.qualification.analysis_adapter import adapt_analysis
from services.qualification.flywheel import record_approval_trace, record_run_trace
from services.qualification.matcher import match_qualifications
from services.qualification.models import Credential, MatchReport, Requirement
from services.qualification.workflow import (
    DuplicateDecisionError,
    InvalidDecisionError,
    QualificationWorkflow,
    ReviewItem,
    UnknownRequirementError,
    WorkflowNotFoundError,
    WorkflowStore,
    _coerce_decision,
    approve_qualification_workflow,
    run_qualification_workflow,
)

# 图拓扑节点名（固定，防漂移；不复用铁律常量集合，避免影响 P-D1 断言）
NODE_Q_EXTRACT = "qualification_extract"
NODE_Q_MATCH = "qualification_match"
NODE_Q_HITL = "qualification_hitl_gate"
NODE_Q_FINALIZE = "qualification_finalize"
QUALIFICATION_GRAPH_NODES: tuple[str, ...] = (NODE_Q_EXTRACT, NODE_Q_MATCH, NODE_Q_HITL, NODE_Q_FINALIZE)

# 图模式入口 entrypoint（飞轮 entrypoint_counts 会出现新键，口径计算不变）
ENTRYPOINT_GRAPH = "graph"

# 资格审批门超时策略：人工专属门，永不自动决策（语义按 confirm/reject/mark_insufficient）
QUALIFICATION_GATE_AUTO_DECISION: str | None = None


def merge_dict(left: dict | None, right: dict | None) -> dict:
    base = dict(left or {})
    base.update(right or {})
    return base


class QualificationGraphState(TypedDict, total=False):
    """资格预审图状态（全部为 JSON 可序列化值，便于 PG checkpoint）。"""

    # 输入
    run_id: str
    project_id: str
    entrypoint: str
    dimensions: dict  # 招标解读结果（from_analysis 口径）；与 requirements 二选一
    requirements: list[dict]  # 直接给定的资格要求（MatchRequest 口径）
    credentials: list[dict]

    # 节点产出
    extracted_requirements: list[dict]  # extract 节点产出（已校验的 Requirement dict）
    adapter_warnings: list[str]
    unresolved_count: int
    force_review: bool  # G-5：外部强制 waiting_human（旧 run-from-* force_review 语义）
    workflow_id: str  # = run_id；同时是 WorkflowStore 主键（旧 API 只读可查）
    trace_id: str
    report: dict  # 比对节点 MatchReport.model_dump()
    review_items: list[dict]
    workflow_status: str  # waiting_human / resumed / completed
    warnings: Annotated[list[str], operator.add]
    decisions: Annotated[list[dict], operator.add]  # 人工审批记录（追加式）

    # HITL
    hitl_error: str  # 审批门校验失败（未知项/重复改判/数据无效），不静默吞掉

    # 终态
    final_report: dict
    errors: Annotated[list[str], operator.add]
    node_status: Annotated[dict[str, str], merge_dict]
    current_stage: str


# --------------------------------------------------------------------------- #
# 类型化节点（窄职责：Pydantic 输入校验，确定性逻辑，禁 ReAct/禁 LLM）
# --------------------------------------------------------------------------- #


def run_extract_node(state: dict) -> dict:
    """资格要求提取：dimensions（解读适配）或 requirements（直给）→ 已校验 Requirement 列表。"""
    extracted: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []
    unresolved = 0

    dimensions = state.get("dimensions") or {}
    raw_requirements = state.get("requirements") or []
    if dimensions:
        adapter = adapt_analysis(dict(dimensions))
        extracted = [r.model_dump() for r in adapter.requirements]
        warnings = list(adapter.warnings)
        unresolved = len(adapter.unresolved_items)
        node_status = "done" if extracted else "skipped"
        if not extracted:
            errors.append("未从解读结果中解析出任何资格要求")
    elif raw_requirements:
        for raw in raw_requirements:
            try:
                extracted.append(Requirement.model_validate(raw).model_dump())
            except ValidationError as e:
                errors.append(f"资格要求校验失败: {e.errors()[0].get('msg', '数据无效')}")
        node_status = "done" if extracted else "skipped"
        if not extracted:
            errors.append("无任何合法资格要求")
    else:
        node_status = "skipped"
        errors.append("未提供 dimensions 或 requirements，无法提取资格要求")

    out = {
        "extracted_requirements": extracted,
        "unresolved_count": unresolved,
        "warnings": warnings,
        "errors": errors,
        "node_status": {NODE_Q_EXTRACT: node_status},
    }
    if dimensions:
        # requirements 直给路径不回写 adapter_warnings：保留外部注入的 extra_warnings
        # （run-from-* 兼容语义，G-5），供 match 节点并入 workflow.warnings
        out["adapter_warnings"] = warnings
    return out


def run_match_node(state: dict) -> dict:
    """企业凭证比对：确定性 matcher（无 LLM），并写脱敏 run Trace（飞轮数据源不断）。"""
    requirements = [Requirement.model_validate(r) for r in (state.get("extracted_requirements") or [])]
    credentials: list[Credential] = []
    errors: list[str] = []
    for raw in state.get("credentials") or []:
        try:
            credentials.append(Credential.model_validate(raw))
        except ValidationError as e:
            errors.append(f"企业凭证校验失败: {e.errors()[0].get('msg', '数据无效')}")

    run_id = str(state.get("run_id") or f"qrun_{uuid.uuid4().hex[:12]}")
    started = time.perf_counter()
    match_qualifications(requirements, credentials)  # 与 run_qualification_workflow 内部同口径预热
    latency_ms = (time.perf_counter() - started) * 1000

    extra_warnings = list(state.get("adapter_warnings") or [])
    force_review = int(state.get("unresolved_count") or 0) > 0 or bool(state.get("force_review"))
    entrypoint = str(state.get("entrypoint") or ENTRYPOINT_GRAPH)
    wf = run_qualification_workflow(
        [r.model_dump() for r in requirements],
        [c.model_dump() for c in credentials],
        workflow_id=run_id,
        extra_warnings=extra_warnings,
        force_review=force_review,
        project_id=state.get("project_id") or None,
        entrypoint=entrypoint,
    )
    trace_id, trace_warnings = record_run_trace(
        entrypoint=entrypoint,
        project_id=state.get("project_id") or None,
        workflow_id=wf.workflow_id,
        workflow_status=wf.status,
        report=wf.report,
        review_items=wf.review_items,
        warnings=wf.warnings,
        unresolved_count=int(state.get("unresolved_count") or 0),
        latency_ms=latency_ms,
    )
    if trace_warnings:
        wf.warnings.extend(trace_warnings)
        WorkflowStore.instance().save(wf)

    node_status = "done"
    if errors:
        node_status = "error"
    return {
        "workflow_id": wf.workflow_id,
        "trace_id": trace_id,
        "report": wf.report.model_dump(),
        "review_items": [item.model_dump() for item in wf.review_items],
        "workflow_status": wf.status,
        "warnings": list(trace_warnings),
        "errors": errors,
        "node_status": {NODE_Q_MATCH: node_status},
    }


def _workflow_from_state(values: dict) -> QualificationWorkflow:
    """由图状态重建 workflow 对象（kill 重启后 store 缺失时的恢复路径，不伪造数据）。"""
    report = MatchReport.model_validate(values.get("report") or {})
    review_items = [ReviewItem.model_validate(item) for item in (values.get("review_items") or [])]
    wf = QualificationWorkflow(
        workflow_id=str(values.get("workflow_id") or ""),
        status=str(values.get("workflow_status") or "waiting_human"),
        project_id=values.get("project_id") or None,
        entrypoint=str(values.get("entrypoint") or ENTRYPOINT_GRAPH),
        report=report,
        review_items=review_items,
        decisions=list(values.get("decisions") or []),
        warnings=list(values.get("warnings") or []),
    )
    return wf


def _ensure_workflow_in_store(values: dict) -> QualificationWorkflow:
    """审批前确保 store 中存在 workflow（进程重启后由图状态重建）。"""
    workflow_id = str(values.get("workflow_id") or "")
    wf = WorkflowStore.instance().get(workflow_id)
    if wf is None:
        wf = _workflow_from_state(values)
        WorkflowStore.instance().save(wf)
    return wf


def run_hitl_gate_node(state: dict) -> dict:
    """HITL 审批门：有 review_items 时 interrupt 挂起；恢复值={"decisions": [...]}。

    三种动作语义复用 approve_qualification_workflow（confirm/reject/mark_insufficient
    与旧状态机逐字一致：幂等/重复改判拒绝/未知项报错/全部决策完成→completed）。
    未全部决策完成时再次 interrupt（多轮审批），直至 completed 才放行到 finalize。
    """
    review_items = state.get("review_items") or []
    if not review_items:
        return {"node_status": {NODE_Q_HITL: "done"}}  # 无需人工：直通终态

    workflow_id = str(state.get("workflow_id") or "")
    resume_value = interrupt(
        {
            "workflow_id": workflow_id,
            "review_items": review_items,
            "report": state.get("report"),
        }
    )
    started = time.perf_counter()

    values = dict(state)
    values.setdefault("entrypoint", ENTRYPOINT_GRAPH)
    new_decisions_all: list[dict] = []
    trace_warnings_all: list[str] = []
    while True:
        decisions_raw = (resume_value or {}).get("decisions") or []
        try:
            parsed = [_coerce_decision(d) for d in decisions_raw]
        except InvalidDecisionError as e:
            return {"hitl_error": str(e), "node_status": {NODE_Q_HITL: "error"}}

        wf = _ensure_workflow_in_store(values)
        prev_count = len(wf.decisions)
        try:
            wf = approve_qualification_workflow(workflow_id, parsed)
        except (WorkflowNotFoundError, UnknownRequirementError, DuplicateDecisionError, InvalidDecisionError) as e:
            return {"hitl_error": str(e), "node_status": {NODE_Q_HITL: "error"}}

        new_decisions = wf.decisions[prev_count:]
        if new_decisions:
            latency_ms = (time.perf_counter() - started) * 1000
            _trace_id, trace_warnings = record_approval_trace(
                workflow_id=workflow_id,
                project_id=wf.project_id,
                workflow=wf,
                new_decisions=new_decisions,
                latency_ms=latency_ms,
            )
            if trace_warnings:
                wf.warnings.extend(trace_warnings)
                WorkflowStore.instance().save(wf)
            new_decisions_all.extend(dict(d) for d in new_decisions)
            trace_warnings_all.extend(trace_warnings)
            values["review_items"] = [item.model_dump() for item in wf.review_items]
            values["decisions"] = list(values.get("decisions") or []) + [dict(d) for d in new_decisions]

        if wf.status == "completed":
            break
        # 未全部决策：再次挂起等待下一轮（多轮审批，语义同旧状态机 resumed）
        pending_items = [item.model_dump() for item in wf.review_items if not item.decision]
        resume_value = interrupt({"workflow_id": workflow_id, "review_items": pending_items})

    return {
        "workflow_status": wf.status,
        "decisions": new_decisions_all,
        "review_items": [item.model_dump() for item in wf.review_items],
        "report": wf.report.model_dump(),
        "warnings": trace_warnings_all,
        "node_status": {NODE_Q_HITL: "done"},
    }


def run_finalize_node(state: dict) -> dict:
    """完成终态：全量决策完成后用人工决策重建最终报告（语义同旧状态机 _apply_decisions）。"""
    workflow_id = str(state.get("workflow_id") or "")
    wf = WorkflowStore.instance().get(workflow_id)
    if wf is None:
        wf = _workflow_from_state(state)
    return {
        "final_report": wf.report.model_dump(),
        "workflow_status": wf.status,
        "current_stage": "completed",
        "node_status": {NODE_Q_FINALIZE: "done"},
    }


def _route_after_gate(state: dict) -> str:
    return END if state.get("hitl_error") else NODE_Q_FINALIZE


# --------------------------------------------------------------------------- #
# 吸收点：matcher 结果 → P-D1 expert 节点输出 schema（findings[].status）
# --------------------------------------------------------------------------- #

_STATUS_TO_EXPERT_STATUS = {"met": "pass", "unmet": "fail", "insufficient": "warning"}
_OVERALL_TO_EXPERT_STATUS = {"met": "pass", "unmet": "fail", "insufficient": "warning"}


def match_report_to_expert_findings(report: MatchReport) -> dict:
    """把资格预审 MatchReport 映射为 P-D1 qualification_expert 输出 schema。

    met→pass / unmet→fail / insufficient→warning；confidence=met 占比（确定性，无 LLM）。
    产出可直接通过 experts.post_validate_expert_output(raw, "qualification") 校验。
    """
    findings = [
        {
            "item": f"{r.requirement_type}:{r.requirement_id}",
            "status": _STATUS_TO_EXPERT_STATUS.get(r.status, "warning"),
            "detail": r.reason,
        }
        for r in report.results
    ]
    if not findings:
        findings = [{"item": "qualification_scan", "status": "warning", "detail": "无资格要求条目"}]
    total = len(report.results)
    met = sum(1 for r in report.results if r.status == "met")
    return {
        "expert": "qualification",
        "findings": findings,
        "overall_status": _OVERALL_TO_EXPERT_STATUS.get(report.overall_status, "warning"),
        "confidence": round(met / total, 4) if total else 0.0,
    }


# --------------------------------------------------------------------------- #
# 超时策略（铁律4 机制复用；资格审批门语义=人工专属，永不自动决策）
# --------------------------------------------------------------------------- #


def resolve_qualification_gate(pending_seconds: float, threshold: float | None = None) -> dict:
    """资格审批门超时策略：永不自动 confirm/reject（不伪造人工决策），只等待人工。"""
    del pending_seconds, threshold  # 人工专属门：阈值不参与自动决策
    return {
        "action": QUALIFICATION_GATE_AUTO_DECISION or "wait_human",
        "reason": "资格审批门为人工专属门（confirm/reject/mark_insufficient 语义），永不自动决策",
    }


# --------------------------------------------------------------------------- #
# 编排器
# --------------------------------------------------------------------------- #


class QualificationGraphOrchestrator:
    """资格预审图编排器：build / run_until_interrupt / resume / snapshot / timeout。"""

    def __init__(self, checkpointer: Any = None):
        self.checkpointer = checkpointer
        self._gate_started: dict[str, float] = {}
        self._graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(QualificationGraphState)
        graph.add_node(NODE_Q_EXTRACT, lambda state: run_extract_node(dict(state)))
        graph.add_node(NODE_Q_MATCH, lambda state: run_match_node(dict(state)))
        graph.add_node(NODE_Q_HITL, lambda state: run_hitl_gate_node(dict(state)))
        graph.add_node(NODE_Q_FINALIZE, lambda state: run_finalize_node(dict(state)))
        graph.add_edge(START, NODE_Q_EXTRACT)
        graph.add_edge(NODE_Q_EXTRACT, NODE_Q_MATCH)
        graph.add_edge(NODE_Q_MATCH, NODE_Q_HITL)
        graph.add_conditional_edges(NODE_Q_HITL, _route_after_gate, {NODE_Q_FINALIZE: NODE_Q_FINALIZE, END: END})
        graph.add_edge(NODE_Q_FINALIZE, END)
        return graph.compile(checkpointer=self.checkpointer)

    def _config(self, run_id: str) -> dict:
        from core.settings import graph_runtime_config

        return graph_runtime_config(run_id)

    async def run_until_interrupt(self, run_id: str, run_input: dict) -> dict:
        run_input = {"run_id": run_id, "entrypoint": ENTRYPOINT_GRAPH, **run_input}
        await self._graph.ainvoke(run_input, self._config(run_id))
        snap = await self.snapshot(run_id)
        if snap.get("pending_gate"):
            self._gate_started[run_id] = time.monotonic()
        return snap

    async def resume(self, run_id: str, decisions: list[dict]) -> dict:
        """从审批门恢复（decisions 为 WorkflowDecision 口径 dict 列表）。"""
        await self._graph.ainvoke(Command(resume={"decisions": list(decisions)}), self._config(run_id))
        self._gate_started.pop(run_id, None)
        return await self.snapshot(run_id)

    async def snapshot(self, run_id: str) -> dict:
        state = await self._graph.aget_state(self._config(run_id))
        values = dict(state.values or {})
        # 挂起判定：next 有待执行节点，或 gate 任务带着未消费的 interrupt（多轮审批场景）
        gate_pending = bool(state.next) or any(
            getattr(t, "interrupts", None) for t in (state.tasks or [])
        )
        return {
            "run_id": run_id,
            "current_stage": values.get("current_stage", ""),
            "node_status": values.get("node_status", {}),
            "pending_gate": "qualification_hitl_gate" if gate_pending else None,
            "next_nodes": list(state.next or []),
            "workflow_id": str(values.get("workflow_id") or ""),
            "workflow_status": values.get("workflow_status", ""),
            "report": values.get("report"),
            "review_items": values.get("review_items", []),
            "decisions": values.get("decisions", []),
            "warnings": values.get("warnings", []),
            "trace_id": values.get("trace_id", ""),
            "unresolved_count": values.get("unresolved_count", 0),
            "final_report": values.get("final_report"),
            "hitl_error": values.get("hitl_error", ""),
            "errors": values.get("errors", []),
        }

    def workflow_payload(self, run_id: str, state_values: dict) -> dict:
        """workflow 只读视图（口径同旧 API _wf_response）。"""
        wf = WorkflowStore.instance().get(run_id) or _workflow_from_state(state_values)
        return {
            "workflow_id": wf.workflow_id,
            "status": wf.status,
            "report": wf.report.model_dump(),
            "review_items": [item.model_dump() for item in wf.review_items],
            "decisions": wf.decisions,
            "warnings": wf.warnings,
        }

    async def apply_gate_timeout(self, run_id: str) -> dict:
        """超时巡检：资格审批门人工专属，永不自动决策，只返回 wait_human。"""
        snap = await self.snapshot(run_id)
        if not snap.get("pending_gate"):
            return {"run_id": run_id, "action": "not_pending"}
        result = resolve_qualification_gate(self._pending_seconds(run_id))
        return {"run_id": run_id, **result}

    def _pending_seconds(self, run_id: str) -> float:
        started = self._gate_started.get(run_id)
        return max(0.0, time.monotonic() - started) if started else 0.0


async def run_full_qualification_graph(
    run_input: dict, decisions: list[dict] | None = None, checkpointer: Any = None
) -> dict:
    """便捷入口：全图跑 + 一次审批（demo/脚本用）。"""
    orchestrator = QualificationGraphOrchestrator(checkpointer=checkpointer)
    run_id = run_input.get("run_id") or f"qrun_{uuid.uuid4().hex[:12]}"
    snap = await orchestrator.run_until_interrupt(run_id, run_input)
    if decisions is not None and snap.get("pending_gate"):
        snap = await orchestrator.resume(run_id, decisions)
    return snap


__all__ = [
    "NODE_Q_EXTRACT",
    "NODE_Q_MATCH",
    "NODE_Q_HITL",
    "NODE_Q_FINALIZE",
    "QUALIFICATION_GRAPH_NODES",
    "ENTRYPOINT_GRAPH",
    "QUALIFICATION_GATE_AUTO_DECISION",
    "QualificationGraphState",
    "QualificationGraphOrchestrator",
    "run_extract_node",
    "run_match_node",
    "run_hitl_gate_node",
    "run_finalize_node",
    "match_report_to_expert_findings",
    "resolve_qualification_gate",
    "run_full_qualification_graph",
]
