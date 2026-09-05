"""资格预审 API：POST /api/qualification/match 与 Workflow/HITL 端点。

不依赖数据库、不调用 LLM，可直接本地启动验证。
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.database import get_db
from services.models import Analysis, Document, Project
from services.qualification.analysis_adapter import AdapterResult, adapt_analysis
from services.qualification.credential_adapter import (
    CredentialCandidate,
    InvalidEvidenceRefError,
    confirm_candidate,
    extract_credentials,
)
from services.qualification.evaluator import (
    DatasetLoadError,
    DatasetNotFoundError,
    EvalReport,
    list_datasets,
    run_evaluation,
)
from services.qualification.flywheel import (
    compute_metrics,
    get_trace_store,
    record_approval_trace,
    record_run_trace,
)
from services.qualification.graph_runtime import get_qualification_run_manager
from services.qualification.matcher import match_qualifications
from services.qualification.models import Credential, MatchRequest, Requirement
from services.qualification.workflow import (
    DuplicateDecisionError,
    InvalidDecisionError,
    QualificationWorkflow,
    UnknownRequirementError,
    WorkflowApproveRequest,
    WorkflowIdConflictError,
    WorkflowNotFoundError,
    WorkflowStore,
    _coerce_decision,
    get_qualification_workflow,
)
from services.routers.route_skill_shims import approve_qualification_workflow as _approve_via_shim


class FromAnalysisRequest(BaseModel):
    """POST /api/qualification/from-analysis 请求体。"""

    dimensions: dict[str, Any]


class RunFromAnalysisRequest(BaseModel):
    """POST /api/qualification/workflow/run-from-analysis 请求体。"""

    dimensions: dict[str, Any]
    credentials: list[Credential] = Field(default_factory=list)


class RunProjectWorkflowRequest(BaseModel):
    """POST /api/qualification/workflow/run-from-project/{project_id} 请求体。"""

    credentials: list[Credential] = Field(default_factory=list)


_OCR_SCANNED_WARNING = "招标文件为扫描件（is_scanned=true），尚未 OCR：解读结果可能不完整，请人工核对或后续阶段接入 OCR"

# 进程内最近一次评测报告（不写入业务 run trace，不污染飞轮生产指标）
_latest_eval_report: EvalReport | None = None


class EvalRunRequest(BaseModel):
    """POST /api/qualification/flywheel/eval/run 请求体（可选）。"""

    dataset_name: str = "synthetic_baseline"


class CredentialsFromTextRequest(BaseModel):
    """POST /api/qualification/credentials/from-text 请求体。"""

    text: str
    source_label: str = "manual"


class CredentialConfirmRequest(BaseModel):
    """POST /api/qualification/credentials/confirm 请求体。"""

    candidate: CredentialCandidate
    evidence_ref: str


class QualificationGraphRunRequest(BaseModel):
    """POST /api/qualification/graph/run 请求体（G-1 图模式入口；与旧端点并存）。"""

    requirements: list[Requirement] = Field(default_factory=list)
    credentials: list[Credential] = Field(default_factory=list)
    dimensions: dict[str, Any] | None = None
    project_id: str | None = None


MAX_CREDENTIAL_TEXT_SIZE = 200 * 1024  # 200KB


router = APIRouter()


def _wf_response(wf: QualificationWorkflow) -> dict:
    return {
        "workflow_id": wf.workflow_id,
        "status": wf.status,
        "report": wf.report.model_dump(),
        "review_items": [item.model_dump() for item in wf.review_items],
        "decisions": wf.decisions,
        "warnings": wf.warnings,
    }


# --------------------------------------------------------------------------- #
# G-5：旧 workflow 端点的图执行垫片（响应形状/错误码与旧直调逐字段一致；
# 判定口径：图 workflow_id == run_id，store 快照经 orchestrator.workflow_payload 复原）
# --------------------------------------------------------------------------- #


async def _run_qualification_graph_compat(
    payload: dict,
    *,
    entrypoint: str,
    project_id: str | None = None,
    extra_warnings: list[str] | None = None,
    force_review: bool = False,
    adapter: AdapterResult | None = None,
) -> dict:
    """旧 workflow/run* 端点 → 资格预审图运行管理器（图模式入口），旧响应形状不变。

    - run Trace 记 entrypoint 口径（manual/from_analysis/from_project），飞轮指标不变；
    - extra_warnings 并入 workflow.warnings（复刻旧 run_qualification_workflow 语义）；
    - force_review=True 时强制 waiting_human（未解析项/扫描件场景）。
    """
    from services.qualification.graph_runtime import get_qualification_run_manager

    manager = get_qualification_run_manager()
    graph_payload = dict(payload)
    graph_payload["entrypoint"] = entrypoint
    if project_id:
        graph_payload["project_id"] = project_id
    graph_payload["adapter_warnings"] = list(extra_warnings or [])
    graph_payload["force_review"] = force_review
    try:
        record = await manager.create_run(graph_payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await manager.wait_settled(record.run_id)
    if record.status == "failed":
        raise HTTPException(status_code=500, detail=record.error or "资格预审图执行失败")
    snap = record.snapshot or {}
    # store 优先（含决策明细与重建报告，口径同旧 _wf_response）；快照兜底
    store_wf = WorkflowStore.instance().get(record.run_id)
    if store_wf is not None:
        wf = _wf_response(store_wf)
    else:
        wf = {
            "workflow_id": record.run_id,
            "status": str(snap.get("workflow_status") or record.status),
            "report": snap.get("report") or {},
            "review_items": snap.get("review_items") or [],
            "decisions": snap.get("decisions") or [],
            "warnings": list(snap.get("warnings") or []) + list(extra_warnings or []),
        }
    if adapter is not None:
        wf["adapter"] = adapter.model_dump()
    if project_id:
        wf["project_id"] = project_id
    wf["trace_id"] = str(snap.get("trace_id") or "")
    return wf


@router.post("/match", summary="招标要求—企业能力资格预审匹配")
async def qualification_match(body: MatchRequest):
    if not body.requirements:
        raise HTTPException(status_code=400, detail="requirements 不能为空")
    # credentials 允许为空：此时所有要求都会判定为 insufficient（无证据不得 met）
    start = time.perf_counter()
    report = match_qualifications(body.requirements, body.credentials)
    latency_ms = (time.perf_counter() - start) * 1000
    trace_id, trace_warnings = record_run_trace(
        entrypoint="manual",
        project_id=None,
        workflow_id=None,
        workflow_status=None,
        report=report,
        review_items=[],
        warnings=report.warnings,
        unresolved_count=0,
        latency_ms=latency_ms,
    )
    if trace_warnings:
        report.warnings.extend(trace_warnings)
    payload = report.model_dump()
    payload["trace_id"] = trace_id
    return payload


@router.post("/workflow/run", summary="启动资格预审 Workflow（可暂停/人工确认）")
async def workflow_run(body: MatchRequest):
    # G-5：改走资格预审图运行管理器（图内 run/approval Trace 语义不变，entrypoint=manual）
    if not body.requirements:
        raise HTTPException(status_code=400, detail="requirements 不能为空")
    return await _run_qualification_graph_compat(
        body.model_dump(), entrypoint="manual"
    )


@router.post("/from-analysis", summary="招标解读结果适配为资格预审要求")
async def from_analysis(body: FromAnalysisRequest):
    return adapt_analysis(body.dimensions).model_dump()


@router.post("/workflow/run-from-analysis", summary="招标解读结果适配后启动审批流程")
async def workflow_run_from_analysis(body: RunFromAnalysisRequest):
    # G-5：改走资格预审图运行管理器（entrypoint=from_analysis，unresolved 强制 waiting_human）
    adapter: AdapterResult = adapt_analysis(body.dimensions)
    if not adapter.requirements:
        raise HTTPException(status_code=400, detail="未从分析结果中解析出任何资格要求")
    extra = _adapter_extra_warnings(adapter)
    return await _run_qualification_graph_compat(
        {
            "requirements": [r.model_dump() for r in adapter.requirements],
            "credentials": [c.model_dump() for c in body.credentials],
        },
        entrypoint="from_analysis",
        extra_warnings=extra,
        force_review=bool(adapter.unresolved_items),
        adapter=adapter,
    )


async def _load_project(project_id: str, db: AsyncSession) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


async def _load_analysis(project: Project, db: AsyncSession) -> Analysis:
    result = await db.execute(select(Analysis).where(Analysis.project_id == project.id))
    analysis = result.scalar_one_or_none()
    if not analysis or not analysis.dimensions:
        raise HTTPException(status_code=400, detail="尚未完成招标解读（Analysis.dimensions 为空）")
    return analysis


async def _load_tender_scanned(project: Project, db: AsyncSession) -> bool:
    """扫描件 PDF 由现有 parser 标记 doc_metadata.is_scanned，尚未 OCR。"""
    if not project.tender_doc_id:
        return False
    result = await db.execute(select(Document).where(Document.id == project.tender_doc_id))
    doc = result.scalar_one_or_none()
    meta = doc.doc_metadata if doc and isinstance(doc.doc_metadata, dict) else {}
    return bool(meta.get("is_scanned"))


def _adapter_extra_warnings(adapter: AdapterResult) -> list[str]:
    extra: list[str] = list(adapter.warnings)
    for item in adapter.unresolved_items:
        extra.append(f"未解析条目 {item.source_path or item.source_field}: {item.reason}")
    return extra


@router.post("/from-project/{project_id}", summary="从项目招标解读结果导入资格要求")
async def from_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _load_project(project_id, db)
    analysis = await _load_analysis(project, db)
    adapter = adapt_analysis(dict(analysis.dimensions))
    if not adapter.requirements:
        raise HTTPException(status_code=400, detail="无法从分析结果中解析出任何资格要求")
    if await _load_tender_scanned(project, db):
        adapter.warnings.append(_OCR_SCANNED_WARNING)
    payload = adapter.model_dump()
    payload["project_id"] = project_id
    return payload


@router.post("/workflow/run-from-project/{project_id}", summary="从项目招标解读结果启动审批流程")
async def workflow_run_from_project(
    project_id: str,
    body: RunProjectWorkflowRequest,
    db: AsyncSession = Depends(get_db),
):
    # G-5：改走资格预审图运行管理器（entrypoint=from_project，unresolved/扫描件强制 waiting_human）
    project = await _load_project(project_id, db)
    analysis = await _load_analysis(project, db)
    adapter = adapt_analysis(dict(analysis.dimensions))
    if not adapter.requirements:
        raise HTTPException(status_code=400, detail="无法从分析结果中解析出任何资格要求")
    scanned = await _load_tender_scanned(project, db)
    if scanned:
        adapter.warnings.append(_OCR_SCANNED_WARNING)
    extra = _adapter_extra_warnings(adapter)
    return await _run_qualification_graph_compat(
        {
            "requirements": [r.model_dump() for r in adapter.requirements],
            "credentials": [c.model_dump() for c in body.credentials],
        },
        entrypoint="from_project",
        project_id=project_id,
        extra_warnings=extra,
        force_review=bool(adapter.unresolved_items) or scanned,
        adapter=adapter,
    )


@router.post("/credentials/from-text", summary="从文本抽取企业材料候选（候选+人工确认）")
async def credentials_from_text(body: CredentialsFromTextRequest):
    if not body.text or not body.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    if len(body.text) > MAX_CREDENTIAL_TEXT_SIZE:
        raise HTTPException(status_code=400, detail=f"text 超过大小限制（{MAX_CREDENTIAL_TEXT_SIZE // 1024}KB）")
    result = extract_credentials(body.text, source_path=body.source_label)
    return result.model_dump()


@router.post("/credentials/from-project/{project_id}", summary="从项目企业材料（bid/reference）抽取候选")
async def credentials_from_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _load_project(project_id, db)
    docs_result = await db.execute(
        select(Document).where(
            Document.project_id == project.id,
            Document.type.in_(["bid", "reference"]),
        )
    )
    docs = docs_result.scalars().all()
    material_docs = [d for d in docs if d.parsed_content]
    scanned_docs = [d for d in docs if isinstance(d.doc_metadata, dict) and d.doc_metadata.get("is_scanned")]
    if not material_docs:
        if scanned_docs:
            return {
                "candidates": [],
                "unresolved_items": [],
                "warnings": ["企业材料为扫描件（is_scanned=true），尚未 OCR：无法抽取候选，请先人工 OCR 或核对"],
            }
        raise HTTPException(status_code=400, detail="项目暂无企业材料文档（bid/reference）或解析内容为空")
    joiner = chr(10) + chr(10)
    result = extract_credentials(
        joiner.join(d.parsed_content or "" for d in material_docs),
        source_path=f"project:{project_id}",
    )
    if scanned_docs:
        result.warnings.append("部分企业材料为扫描件（is_scanned=true），尚未 OCR，其内容未纳入抽取")
    return result.model_dump()


@router.post("/credentials/confirm", summary="确认候选为正式 Credential（绑定证据引用）")
async def credentials_confirm(body: CredentialConfirmRequest):
    try:
        credential = confirm_candidate(body.candidate, body.evidence_ref)
    except InvalidEvidenceRefError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return credential.model_dump()


@router.get("/flywheel/metrics", summary="资格预审数据飞轮指标")
async def flywheel_metrics():
    events = get_trace_store().read_events()
    return compute_metrics(events)


@router.get("/flywheel/traces", summary="脱敏后的最近 Trace 事件")
async def flywheel_traces(limit: int = 50):
    bounded = max(1, min(limit, 200))
    events = get_trace_store().read_events(limit=bounded)
    return {"traces": events, "count": len(events)}


@router.get("/flywheel/export", summary="导出匿名化评测数据（NDJSON）")
async def flywheel_export():
    events = get_trace_store().read_events(limit=None)
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    separator = chr(10)
    return PlainTextResponse(separator.join(lines) + (separator if lines else ""), media_type="application/x-ndjson")


@router.get("/flywheel/eval/datasets", summary="列出内置评测数据集")
async def eval_datasets():
    return {"datasets": list_datasets()}


@router.post("/flywheel/eval/run", summary="运行离线回归评测（只允许白名单数据集）")
async def eval_run(body: EvalRunRequest | None = None):
    global _latest_eval_report
    dataset_name = (body.dataset_name if body else None) or "synthetic_baseline"
    try:
        report = run_evaluation(dataset_name)
    except DatasetNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except DatasetLoadError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    _latest_eval_report = report
    return report.model_dump()


@router.get("/flywheel/eval/latest", summary="最近一次评测报告")
async def eval_latest():
    if _latest_eval_report is None:
        raise HTTPException(status_code=404, detail="尚未运行过评测")
    return _latest_eval_report.model_dump()


@router.get("/workflow/{workflow_id}", summary="查询资格预审 Workflow 状态")
async def workflow_get(workflow_id: str):
    try:
        wf = get_qualification_workflow(workflow_id)
    except WorkflowNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _wf_response(wf)


# --------------------------------------------------------------------------- #
# G-1 图模式端点（P-G 任务书 G-1 条目 2：图入口，与旧状态机端点并存可回退）
# --------------------------------------------------------------------------- #


@router.post("/graph/run", summary="图模式资格预审：启动资格预审图运行（提取→比对→HITL 审批门）")
async def qualification_graph_run(body: QualificationGraphRunRequest):
    manager = get_qualification_run_manager()
    try:
        record = await manager.create_run(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await manager.wait_settled(record.run_id)
    return {"success": True, "run_id": record.run_id, "status": record.status, "error": record.error}


@router.get("/graph/runs", summary="图模式资格预审运行列表")
async def qualification_graph_runs():
    manager = get_qualification_run_manager()
    return {
        "success": True,
        "runs": [
            {
                "run_id": r.run_id,
                "project_id": r.project_id,
                "status": r.status,
                "created_at": r.created_at,
                "workflow_status": (r.snapshot or {}).get("workflow_status", ""),
            }
            for r in manager.list_runs()
        ],
    }


@router.get("/graph/runs/{run_id}", summary="图模式资格预审运行快照（节点状态/审批门/报告）")
async def qualification_graph_snapshot(run_id: str):
    manager = get_qualification_run_manager()
    try:
        record = manager.get(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run 不存在") from None
    return {
        "success": True,
        "run_id": run_id,
        "status": record.status,
        "error": record.error,
        "pending_gate_namespace": "qualification" if record.snapshot.get("pending_gate") else None,
        "snapshot": record.snapshot,
    }


@router.post("/graph/runs/{run_id}/decision", summary="图模式资格预审人工审批（confirm/reject/mark_insufficient）")
async def qualification_graph_decision(run_id: str, body: WorkflowApproveRequest):
    manager = get_qualification_run_manager()
    try:
        snapshot = await manager.decide(run_id, [d.model_dump() for d in body.decisions])
    except KeyError:
        raise HTTPException(status_code=404, detail="run 不存在") from None
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except WorkflowNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except UnknownRequirementError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except DuplicateDecisionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except InvalidDecisionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if snapshot.get("hitl_error"):
        raise HTTPException(status_code=400, detail=snapshot["hitl_error"])
    return {"success": True, "snapshot": snapshot}


@router.post("/graph/timeouts/sweep", summary="图模式资格审批门超时巡检（人工专属门，永不自动决策）")
async def qualification_graph_sweep():
    outcomes = await get_qualification_run_manager().apply_timeout_policies()
    return {"success": True, "outcomes": outcomes}


@router.post("/workflow/{workflow_id}/approve", summary="人工审批 Workflow 评审项")
async def workflow_approve(workflow_id: str, body: WorkflowApproveRequest):
    """G-5：审批语义统一走图运行管理器 decide（镜像 /graph/runs/{id}/decision 契约）。

    兼容映射（任务书 T3）：workflow_id == 图 run_id（图创建 run 时即写入 WorkflowStore）。
    - 旧状态机-only 的 workflow（无图 run）：404 同旧（decide 前置校验同口径）；
    - 错误码复刻旧 approve：未走图挂起的 run → 409；未知项 422；重复改判 409；非法决策 400。
    """
    manager = get_qualification_run_manager()
    try:
        wf = get_qualification_workflow(workflow_id)
    except WorkflowNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    try:
        record = manager.get(workflow_id)
    except KeyError:
        # 非 run 管理器创建的旧 workflow（历史数据/外部创建）：按旧语义继续审批（只读回归兼容）
        return _approve_workflow_legacy(workflow_id, body)
    if record.status not in ("waiting_human", "resumed"):
        # 幂等预检（旧契约：已 completed 的 workflow 重复提交相同决策 → 200 返回现状；冲突 → 409）
        if wf.status == "completed" and _decisions_compatible(wf, body.decisions):
            payload = _wf_response(wf)
            payload["trace_id"] = None
            return payload
        raise HTTPException(status_code=409, detail=f"run {workflow_id} 当前状态 {record.status} 不在资格审批门")
    try:
        snapshot = await manager.decide(workflow_id, [d.model_dump() for d in body.decisions])
    except UnknownRequirementError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except DuplicateDecisionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except InvalidDecisionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if snapshot.get("hitl_error"):
        raise HTTPException(status_code=400, detail=snapshot["hitl_error"])
    wf = get_qualification_workflow(workflow_id)
    payload = _wf_response(wf)
    payload["trace_id"] = str(snapshot.get("trace_id") or "")
    return payload


def _decisions_compatible(wf: QualificationWorkflow, decisions: list) -> bool:
    """幂等判定：提交决策与已审批记录一致（重复提交同决策），且无未知项。"""
    try:
        parsed = [_coerce_decision(d.model_dump() if hasattr(d, "model_dump") else d) for d in decisions]
    except InvalidDecisionError:
        return False
    item_ids = {item.requirement_id for item in wf.review_items}
    existing = {rec["requirement_id"]: rec["decision"] for rec in wf.decisions}
    for d in parsed:
        if d.requirement_id not in item_ids or existing.get(d.requirement_id) != d.decision:
            return False
    return True


def _approve_workflow_legacy(workflow_id: str, body: WorkflowApproveRequest) -> dict:
    """旧直调 approve 的兼容执行体（仅历史 workflow 无图 run 时触达；G-5 保留只读回归路径）。"""
    try:
        wf = get_qualification_workflow(workflow_id)
    except WorkflowNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    prev_decision_count = len(wf.decisions)
    start = time.perf_counter()
    try:
        wf = _approve_via_shim(workflow_id, body.decisions)
    except UnknownRequirementError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except DuplicateDecisionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except (WorkflowIdConflictError, InvalidDecisionError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    latency_ms = (time.perf_counter() - start) * 1000
    new_decisions = wf.decisions[prev_decision_count:]
    trace_id = None
    if new_decisions:
        trace_id, trace_warnings = record_approval_trace(
            workflow_id=workflow_id,
            project_id=wf.project_id,
            workflow=wf,
            new_decisions=new_decisions,
            latency_ms=latency_ms,
        )
        if trace_warnings:
            wf.warnings.extend(trace_warnings)
    payload = _wf_response(wf)
    payload["trace_id"] = trace_id
    return payload


__all__ = ["router"]
