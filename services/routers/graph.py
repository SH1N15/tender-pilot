"""P-D1 最小 REST 路由（/api/graph）。鉴权接现有 RBAC 依赖。

- POST /api/graph/runs                {project_id}              -> run_id（异步启动）
- GET  /api/graph/runs                -> 运行列表
- GET  /api/graph/runs/{run_id}       -> 状态快照（节点状态/挂起门/决策包/改判理由）
- POST /api/graph/runs/{run_id}/decision {action: approve|override, level?, reason} -> 恢复执行
- GET  /api/graph/runs/{run_id}/cost  -> 每节点 LLM 调用数/token/耗时
- POST /api/graph/timeouts/sweep      -> 应用门型超时策略（铁律4，运维触发或定时）

契约与错误码见 core/agent_engine/README.md（本模块 API 契约所在）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.database import get_db
from services.graph_runtime.runner import get_run_manager
from services.middleware.rbac_middleware import get_current_user, require_permission
from services.models import Document, Project, User

router = APIRouter()


class RunCreate(BaseModel):
    project_id: str
    outline_only: bool = False
    run_outline: bool | None = None  # None=自动：项目已有大纲则 False（防重建覆盖）
    check_ids: list[str] | None = None
    memory_query: str = ""
    chapter_ids: list[str] | None = None  # 限定正文生成章节（缺省=全部大纲章节）


class DecisionCreate(BaseModel):
    action: str  # approve | override | recheck
    level: str | None = None
    reason: str = ""
    namespace: str = "decision"
    decisions: list[dict] = []
    chapter_ids: list[str] = []
    check_ids: list[str] = []


async def _validate_run_preconditions(project_id: str, db: AsyncSession | None) -> None:
    """阻断空项目启动总图，避免无输入时直接落到一个无意义的决策门。

    ``db=None`` 是旧路由单测的显式无数据库模式，保留该模式以便测试注入的假运行器继续工作。
    真实服务始终提供会话，因此必须存在项目、招标文档和已解析正文。
    """
    if db is None or not isinstance(db, AsyncSession):
        return
    project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "GRAPH_PRECONDITION_FAILED",
                "reason": "项目不存在或当前登录用户无法访问该项目",
                "next_action": "请返回项目列表重新选择项目",
            },
        )
    if not project.tender_doc_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "GRAPH_PRECONDITION_FAILED",
                "reason": "尚未上传招标文件",
                "next_action": "请在当前全链路页面完成第 1 步“上传招标文件”",
            },
        )
    document = (
        await db.execute(select(Document).where(Document.id == project.tender_doc_id))
    ).scalar_one_or_none()
    if document is None or not (document.parsed_content or "").strip():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "GRAPH_PRECONDITION_FAILED",
                "reason": "招标文件尚未解析完成",
                "next_action": "请在当前全链路页面完成第 2 步“解析招标文件”",
            },
        )


async def _resolve_run_outline(db: AsyncSession | None, project_id: str, requested: bool | None) -> bool:
    """H 修复：run_outline 缺省自动判定——项目已有大纲则不重跑大纲物化，防覆盖已有章节。

    显式 true/false 尊重请求；DB 不可用/查询异常时回退 True（保守原行为）。
    """
    if requested is not None:
        return requested
    if db is None:
        return True
    try:
        from sqlalchemy import select

        from services.models import Outline

        result = await db.execute(select(Outline.id).where(Outline.project_id == project_id).limit(1))
        return result.scalar_one_or_none() is None
    except Exception:
        return True


@router.post("/runs")
async def create_run(
    payload: RunCreate,
    user: User = Depends(require_permission("project.create")),
    db: AsyncSession = Depends(get_db),
):
    await _validate_run_preconditions(payload.project_id, db)
    manager = get_run_manager()
    run_outline = await _resolve_run_outline(db, payload.project_id, payload.run_outline)
    record = await manager.create_run(
        payload.project_id,
        {
            "outline_only": payload.outline_only,
            "run_outline": run_outline,
            "check_ids": payload.check_ids,
            "memory_query": payload.memory_query,
            "chapter_ids": payload.chapter_ids,
        },
    )
    return {
        "success": True,
        "run_id": record.run_id,
        "status": record.status,
        "run_options": {"run_outline": run_outline},
    }


@router.get("/runs")
async def list_runs(user: User = Depends(get_current_user)):
    manager = get_run_manager()
    runs = await manager.list_runs_with_history()
    # 同一项目可能保留多次运行。明确标出最新运行，避免历史 NO_BID
    # 快照在前端被误认为当前结论。
    latest_by_project: dict[str, str] = {}
    for run in runs:
        project_id = str(run.project_id or "")
        if project_id and project_id not in latest_by_project:
            latest_by_project[project_id] = run.run_id
    return {
        "success": True,
        "runs": [
            {
                "run_id": r.run_id,
                "project_id": r.project_id,
                "status": r.status,
                "created_at": r.created_at,
                "final_level": (
                    (r.snapshot or {}).get("final_level")
                    or ((r.snapshot or {}).get("decision_package") or {}).get("level", "")
                ),
                "is_latest": latest_by_project.get(str(r.project_id or "")) == r.run_id,
            }
            for r in runs
        ],
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str, user: User = Depends(get_current_user)):
    try:
        record = await get_run_manager().get_or_reattach(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run 不存在")
    snapshot = record.snapshot or {}
    pending_namespace = snapshot.get("pending_gate_namespace")
    qualification = (snapshot.get("stage_results") or {}).get("qualification") or {}
    if not pending_namespace and record.status == "pending_decision":
        current_stage = str(snapshot.get("current_stage") or "")
        if current_stage.startswith("qualification") and (
            qualification.get("review_items")
            or qualification.get("hitl_error")
            or str(qualification.get("workflow_status") or "") == "waiting_human"
        ):
            pending_namespace = "qualification"
    # Older snapshots predate the separated action counters and may still
    # include non-current workflow notes in missing_material_findings.  Derive
    # the normalized view at read time so historical runs obey the same API
    # contract as newly generated reports.
    check_report = ((snapshot.get("stage_results") or {}).get("check") or {}).get("report")
    if isinstance(check_report, dict):
        from services.check.missing_materials import build_action_summary, is_non_current_material_matter

        findings = check_report.get("missing_material_findings")
        if isinstance(findings, list):
            normalized = []
            workflow = []
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                row = dict(finding)
                detail = str(row.get("detail") or row.get("reason") or "")
                # Recompute on every read. Older snapshots may have marked a
                # final signature/platform action as a material gap before the
                # workflow classifier learned its current-stage semantics.
                row["material_required"] = not is_non_current_material_matter(detail)
                (workflow if row["material_required"] is False else normalized).append(row)
            check_report["missing_material_findings"] = normalized[:200]
            summary = check_report.setdefault("summary", {})
            summary["missing_material_findings"] = len(normalized)
            check_report["workflow_findings"] = workflow[:200]
            check_report["action_summary"] = build_action_summary(
                check_report.get("check_results") or list((check_report.get("results") or {}).values()),
                normalized + workflow,
                check_report.get("feedback") or {},
            )
        # 历史运行的 decision_package 可能在材料分类规则更新前生成，
        # 不能继续把旧的缺料计数暴露给前端。读取详情时以当前报告摘要为唯一来源，
        # 仅覆盖派生计数，保留原有定级、理由、证据和风险明细。
        package = snapshot.get("decision_package")
        if isinstance(package, dict):
            current_summary = check_report.get("summary")
            action_summary = check_report.get("action_summary")
            if isinstance(current_summary, dict):
                refreshed_package = dict(package)
                risk_summary = refreshed_package.get("risk_summary")
                if isinstance(risk_summary, dict):
                    refreshed_risk_summary = dict(risk_summary)
                    refreshed_summary = dict(refreshed_risk_summary.get("summary") or {})
                    for key in ("total", "passed", "failed", "warning", "skipped", "missing_material_findings"):
                        if key in current_summary:
                            refreshed_summary[key] = current_summary[key]
                    refreshed_risk_summary["summary"] = refreshed_summary
                    if isinstance(action_summary, dict):
                        refreshed_risk_summary["action_summary"] = dict(action_summary)
                    refreshed_package["risk_summary"] = refreshed_risk_summary
                snapshot["decision_package"] = refreshed_package
    return {
        "success": True,
        "run_id": run_id,
        "project_id": record.project_id,
        "status": record.status,
        "error": record.error,
        # G7-2 修复：外层命名空间直取 snapshot 判定值（此前恒映射 "decision"，
        # 资格门挂起时外层误报 decision，与内层 snapshot 不一致）
        "pending_gate_namespace": pending_namespace,
        "snapshot": snapshot,
        "override_reason": snapshot.get("override_reason", ""),
        "decision_package": snapshot.get("decision_package"),
    }


@router.get("/runs/{run_id}/export-report")
async def export_run_report(
    run_id: str,
    format: str = "markdown",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """导出图运行自身的检查报告，直接读取 checkpoint 中的最新报告。"""
    try:
        record = await get_run_manager().get_or_reattach(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run 不存在")
    snapshot = record.snapshot or {}
    check_stage = (snapshot.get("stage_results") or {}).get("check") or {}
    report = check_stage.get("report") if isinstance(check_stage, dict) else None
    if not isinstance(report, dict):
        raise HTTPException(status_code=404, detail="当前运行还没有检查报告")
    results = report.get("results")
    if not isinstance(results, dict):
        results = {
            str(item.get("check_id") or f"check_{index + 1}"): item
            for index, item in enumerate(report.get("check_results") or [])
            if isinstance(item, dict)
        }
    project_result = await db.execute(select(Project).where(Project.id == record.project_id))
    project = project_result.scalar_one_or_none()
    from services.check.report_export import render_check_report_export
    try:
        content = await render_check_report_export(results, format, project.name if project else "未命名项目")
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    media_type = "text/markdown" if format == "markdown" else "text/html" if format == "html" else "application/json"
    return PlainTextResponse(content=content, media_type=media_type)


@router.post("/runs/{run_id}/decision")
async def decide(run_id: str, payload: DecisionCreate, user: User = Depends(require_permission("project.update"))):
    if payload.namespace not in ("qualification", "scope", "decision"):
        raise HTTPException(status_code=422, detail="namespace 必须是 qualification、scope 或 decision")
    if payload.action == "recheck" and payload.namespace != "decision":
        raise HTTPException(status_code=422, detail="recheck 只能用于最终检查决策门")
    if payload.action not in ("approve", "override", "recheck") and payload.namespace == "decision":
        raise HTTPException(status_code=422, detail="action 必须是 approve、override 或 recheck")
    try:
        manager = get_run_manager()
        user_id = user.name or user.email or user.id
        if payload.namespace == "decision" and not payload.decisions:
            kwargs = {"check_ids": payload.check_ids} if payload.action == "recheck" or payload.check_ids else {}
            snapshot = await manager.decide(
                run_id, payload.action, payload.reason, payload.level, user=user_id, **kwargs
            )
        else:
            kwargs = {"check_ids": payload.check_ids} if payload.action == "recheck" or payload.check_ids else {}
            snapshot = await manager.decide(
                run_id,
                payload.action,
                payload.reason,
                payload.level,
                user=user_id,
                namespace=payload.namespace,
                decisions=payload.chapter_ids if payload.namespace == "scope" else payload.decisions,
                **kwargs,
            )
    except KeyError:
        raise HTTPException(status_code=404, detail="run 不存在")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"success": True, "snapshot": snapshot}


@router.get("/runs/{run_id}/cost")
async def cost(run_id: str, user: User = Depends(get_current_user)):
    try:
        await get_run_manager().get_or_reattach(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run 不存在")
    return {"success": True, "cost": get_run_manager().cost(run_id)}


@router.get("/stats")
async def decision_stats(user: User = Depends(get_current_user)):
    """P-F 阶段 A 新增采集项：图 HITL 决策聚合（批准率/改判率/理由留痕/grounding 汇总）。

    只读聚合 graph_checkpoints 每线程终态快照；无 DB 时安全降级为 available=False。
    """
    from sqlalchemy import text

    from services.database import async_session, is_db_ready
    from services.graph_runtime.runner import summarize_checkpoint_decisions

    if not is_db_ready():
        return {"success": True, "available": False, "reason": "数据库不可用"}
    async with async_session()() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT g.thread_id, g.checkpoint FROM graph_checkpoints g "
                    "WHERE (g.thread_id, g.checkpoint_id) IN ("
                    "  SELECT thread_id, MAX(checkpoint_id) FROM graph_checkpoints GROUP BY thread_id)"
                )
            )
        ).fetchall()
    stats = summarize_checkpoint_decisions([(r[0], r[1]) for r in rows])
    # 内存 run 注册表补充当前在线 run 状态（重启后为空，属预期）
    manager = get_run_manager()
    live = {"running": 0, "pending_decision": 0, "finalized": 0, "failed": 0, "starting": 0}
    for rec in manager.list_runs():
        if rec.status in live:
            live[rec.status] += 1
    return {"success": True, "available": True, "persisted": stats, "live_runs": live}


@router.post("/timeouts/sweep")
async def sweep_timeouts(user: User = Depends(require_permission("project.update"))):
    outcomes = await get_run_manager().apply_timeout_policies()
    return {"success": True, "outcomes": outcomes}
