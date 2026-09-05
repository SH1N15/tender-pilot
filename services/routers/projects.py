from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import bindparam, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.storage import get_storage
from services.database import get_db
from services.middleware.rbac_middleware import get_current_user, require_permission
from services.models import (
    Analysis,
    Chapter,
    CheckReport,
    Document,
    DocumentType,
    Outline,
    Project,
    ProjectStatus,
    StructuredArtifact,
    TenderEntity,
    User,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# 非终态 run 状态：存在任一即拒绝删除项目（防呆②）
_ACTIVE_RUN_STATUSES = {"starting", "running", "pending_decision", "waiting_human"}

MAX_FILE_SIZE = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".wps", ".md"}


@router.get("/")
async def list_projects(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Project).where(Project.user_id == current_user.id).order_by(Project.created_at.desc())
    )
    projects = result.scalars().all()
    return {
        "projects": [
            {
                "id": str(p.id),
                "name": p.name,
                "status": p.status.value if isinstance(p.status, ProjectStatus) else p.status,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in projects
        ]
    }


@router.post("/")
async def create_project(
    name: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("project.create")),
    tender_file: UploadFile | None = File(None),
):
    project = Project(name=name, status=ProjectStatus.CREATED.value, user_id=current_user.id)
    db.add(project)
    await db.flush()

    if tender_file:
        file_ext = Path(tender_file.filename).suffix.lower() if tender_file.filename else ""
        if file_ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"不支持的文件格式: {file_ext}")
        # P1-4: 经存储抽象写入（local 后端目录结构/文件名与现状完全一致）
        content = await tender_file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"文件大小超过限制({MAX_FILE_SIZE // 1024 // 1024}MB)")
        storage = get_storage()
        object_key = f"projects/{project.id}/{tender_file.filename}"
        storage.save(object_key, content)
        file_path = storage.local_path(object_key) or Path(object_key)

        doc = Document(
            project_id=project.id,
            type=DocumentType.TENDER.value,
            file_path=str(file_path),
            original_name=tender_file.filename,
            file_size=len(content),
        )
        db.add(doc)
        await db.flush()
        project.tender_doc_id = doc.id

    await db.flush()
    return {
        "project_id": str(project.id),
        "name": project.name,
        "status": project.status if isinstance(project.status, str) else project.status.value,
    }


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    if project.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无权访问该项目")
    return {
        "id": str(project.id),
        "name": project.name,
        "status": project.status.value if isinstance(project.status, ProjectStatus) else project.status,
        "config": project.config,
        "created_at": project.created_at.isoformat() if project.created_at else None,
    }


@router.patch("/{project_id}/status")
async def update_project_status(
    project_id: str,
    status: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("project.update")),
):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    if project.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无权修改该项目")
    try:
        project.status = ProjectStatus(status).value
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效状态: {status}")
    await db.flush()
    return {"project_id": str(project.id), "status": project.status}


@router.post("/{project_id}/gate/{stage}")
async def confirm_gate(
    project_id: str,
    stage: str,
    reviewer: str = "user",
    current_user: User = Depends(get_current_user),
):
    from core.agent_engine.gate_keeper import GateKeeper

    gk = GateKeeper()
    gk.mark_passed(project_id, stage, reviewer)
    return {"project_id": project_id, "stage": stage, "gate_passed": True}


# --------------------------------------------------------------------------- #
# Worker K：项目级联删除（DELETE /api/projects/{project_id}）
#
# 级联范围（全部按 project_id 精确匹配，绝不误删他项目）：
#   DB：chapters / documents / analyses / outlines / check_reports /
#       tender_entities / structured_artifacts / projects 本体，
#       graph_checkpoints + graph_checkpoint_writes（thread_id=run_id，
#       按"每线程终态 checkpoint 的 channel_values.project_id"归属反查，
#       重启后内存注册表为空也能清）、grun/gen/check/qrun 内存 run 注册表、
#       资格预审 WorkflowStore 内存条目；
#   文件：存储对象 projects/{pid}/*（local 目录整体清理 + minio 按 key 删除，
#       两后端通用）、data/structured/{pid[:8]} 解析产物、
#       GateKeeper projects/{pid}/.stages 闸门文件；
#   长期记忆：kb_memory / kb_memory_uat（现成 LongTermMemory.clear_project）。
#   说明：knowledge_bases 为全局/企业域资产，不与项目关联（models.py 无
#   project_id 外键），不删；eval trace（flywheel.jsonl）只存 project_id 的
#   SHA-256 摘要，按脱敏设计不回查删除。
# 防呆：①?confirm=true 必填否则 400；②存在 starting/running/
#   pending_decision/waiting_human 的图 run 时 409 拒绝。
# --------------------------------------------------------------------------- #


def _project_active_runs(project_id: str) -> list[dict]:
    """四个图 run 管理器中该项目未到终态的 run（删除防呆②的依据）。"""
    blockers: list[dict] = []
    try:
        from services.check.graph_runtime import get_check_run_manager
        from services.generate.graph_runtime import get_generation_run_manager
        from services.graph_runtime.runner import get_run_manager
        from services.qualification.graph_runtime import get_qualification_run_manager

        for kind, manager in (
            ("master", get_run_manager()),
            ("generate", get_generation_run_manager()),
            ("check", get_check_run_manager()),
            ("qualification", get_qualification_run_manager()),
        ):
            for run_id, rec in getattr(manager, "_runs", {}).items():
                if rec.project_id == project_id and rec.status in _ACTIVE_RUN_STATUSES:
                    blockers.append(
                        {
                            "kind": kind,
                            "run_id": getattr(rec, "run_id", None) or run_id,
                            "status": rec.status,
                        }
                    )
    except Exception:  # noqa: BLE001 - 管理器未初始化视为无活动 run
        logger.warning("活动 run 探测失败（按无活动 run 处理）", exc_info=True)
    return blockers


async def _project_checkpoint_threads(db: AsyncSession, project_id: str) -> list[str]:
    """graph_checkpoints 中归属该项目的 thread_id（每线程终态 checkpoint 解码查 project_id）。"""
    from core.agent_engine.checkpoint import PGCheckpointSaver

    try:
        rows = (
            await db.execute(
                text(
                    "SELECT g.thread_id, g.checkpoint FROM graph_checkpoints g "
                    "WHERE (g.thread_id, g.checkpoint_id) IN ("
                    "  SELECT thread_id, MAX(checkpoint_id) FROM graph_checkpoints GROUP BY thread_id)"
                )
            )
        ).fetchall()
    except Exception:  # noqa: BLE001 - 表不存在（迁移未跑）按无线程处理
        logger.warning("checkpoint 线程扫描失败（按 0 处理）", exc_info=True)
        return []
    saver = PGCheckpointSaver(None)
    threads: list[str] = []
    for thread_id, raw in rows:
        try:
            cp = saver._load(raw) or {}
            v = cp.get("channel_values") or {}
        except Exception:  # noqa: BLE001
            continue
        if v.get("project_id") == project_id:
            threads.append(str(thread_id))
    return threads


async def _clear_project_memory(project_id: str) -> int:
    """长期记忆（kb_memory/kb_memory_uat）按项目清理，复用现成 clear_project。"""
    try:
        from core.agent_framework.memory import LongTermMemory

        return await LongTermMemory().clear_project(project_id)
    except Exception:  # noqa: BLE001 - 向量库不可用不阻断项目删除
        logger.warning("长期记忆清理失败（忽略）: %s", project_id, exc_info=True)
        return 0


def _purge_project_files(project_id: str, docs: list[tuple[str | None, str | None]]) -> int:
    """删除项目文件：文档对象（存储 key，local/minio 通用）+ local projects/{pid} 目录。

    docs: (file_path, original_name) 列表。只删 `projects/{pid}/` 前缀下的对象，
    绝不触碰 uploads/formatted 等共享目录。返回删除的文件数。
    """
    removed = 0
    try:
        storage = get_storage()
    except Exception:  # noqa: BLE001
        logger.warning("存储后端不可用，跳过文件清理", exc_info=True)
        return 0
    for file_path, original_name in docs:
        base = original_name or (Path(file_path).name if file_path else "")
        if not base:
            continue
        key = f"projects/{project_id}/{base}"
        try:
            if storage.exists(key):
                storage.delete(key)
                removed += 1
        except Exception:  # noqa: BLE001 - 对象不存在/后端异常不阻断删除
            logger.warning("项目文档对象删除失败: %s", key, exc_info=True)
    # local 后端：整个 projects/{pid} 目录（含 GateKeeper .stages）一并清理
    root = getattr(storage, "root", None)
    if root is not None:
        proj_dir = Path(root) / "projects" / project_id
        if proj_dir.is_dir():
            shutil.rmtree(proj_dir, ignore_errors=True)
            removed += 1
    return removed


def _purge_structured_artifacts(paths: list[str]) -> int:
    """删除 data/structured/{pid[:8]} 解析产物文件；目录删空后移除。"""
    removed = 0
    parents = set()
    for raw in paths:
        if not raw:
            continue
        p = Path(raw)
        try:
            if p.is_file():
                p.unlink()
                removed += 1
                parents.add(p.parent)
        except OSError:
            logger.warning("结构化产物删除失败: %s", raw, exc_info=True)
    for d in parents:
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return removed


def _purge_in_memory_records(project_id: str) -> int:
    """清掉四个 run 管理器与资格预审 WorkflowStore 中该项目的内存残留记录。"""
    purged = 0
    try:
        from services.check.graph_runtime import get_check_run_manager
        from services.generate.graph_runtime import get_generation_run_manager
        from services.graph_runtime.runner import get_run_manager
        from services.qualification.graph_runtime import get_qualification_run_manager
        from services.qualification.workflow import WorkflowStore

        for manager in (
            get_run_manager(),
            get_generation_run_manager(),
            get_check_run_manager(),
            get_qualification_run_manager(),
        ):
            runs = getattr(manager, "_runs", {})
            for run_id in [k for k, r in runs.items() if getattr(r, "project_id", None) == project_id]:
                runs.pop(run_id, None)
                purged += 1
        store = WorkflowStore.instance()
        wf = getattr(store, "_workflows", {})
        for wid in [k for k, w in wf.items() if getattr(w, "project_id", None) == project_id]:
            wf.pop(wid, None)
            purged += 1
    except Exception:  # noqa: BLE001
        logger.warning("内存 run/workflow 清理失败（忽略）", exc_info=True)
    return purged


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    confirm: bool = Query(False, description="防呆：必须显式 ?confirm=true 才执行删除"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("project.delete")),
):
    if not confirm:
        raise HTTPException(status_code=400, detail="删除项目需显式确认：请携带查询参数 ?confirm=true")

    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    if project.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无权删除该项目")

    blockers = _project_active_runs(project_id)
    if blockers:
        raise HTTPException(
            status_code=409,
            detail=f"项目仍有进行中的图运行（{blockers[0]['kind']} run {blockers[0]['run_id']}"
            f"，状态 {blockers[0]['status']}），请等待结束或停止后再删除",
        )

    # 删除前采集：文档对象清单、结构化产物路径、checkpoint 线程清单
    docs = (
        await db.execute(
            select(Document.file_path, Document.original_name).where(Document.project_id == project_id)
        )
    ).all()
    artifact_paths = (
        await db.execute(
            select(StructuredArtifact.path).where(StructuredArtifact.project_id == project_id)
        )
    ).scalars().all()
    threads = set(await _project_checkpoint_threads(db, project_id))

    # ---- 事务内级联删除（get_db 统一 commit/rollback）----
    deleted: dict[str, int] = {}
    project_name = project.name

    # 解除 projects.tender_doc_id 循环外键（含跨项目误引用兜底）
    await db.execute(
        update(Project)
        .where(
            Project.tender_doc_id.in_(select(Document.id).where(Document.project_id == project_id))
        )
        .values(tender_doc_id=None)
    )
    await db.execute(update(Project).where(Project.id == project_id).values(tender_doc_id=None))

    doc_id_sub = select(Document.id).where(Document.project_id == project_id)
    for label, stmt in (
        (
            "structured_artifacts",
            delete(StructuredArtifact).where(
                (StructuredArtifact.project_id == project_id) | StructuredArtifact.document_id.in_(doc_id_sub)
            ),
        ),
        (
            "tender_entities",
            delete(TenderEntity).where(
                (TenderEntity.project_id == project_id) | TenderEntity.document_id.in_(doc_id_sub)
            ),
        ),
        ("chapters", delete(Chapter).where(Chapter.project_id == project_id)),
        ("check_reports", delete(CheckReport).where(CheckReport.project_id == project_id)),
        ("documents", delete(Document).where(Document.project_id == project_id)),
        ("analyses", delete(Analysis).where(Analysis.project_id == project_id)),
        ("outlines", delete(Outline).where(Outline.project_id == project_id)),
        ("project", delete(Project).where(Project.id == project_id)),
    ):
        res = await db.execute(stmt)
        deleted[label] = int(res.rowcount or 0)

    # graph checkpoints（thread_id=run_id；先精确线程删除，防呆：仅限该项目归属线程）
    if threads:
        cp = await db.execute(
            text("DELETE FROM graph_checkpoints WHERE thread_id IN :ts").bindparams(
                bindparam("ts", expanding=True)
            ),
            {"ts": sorted(threads)},
        )
        wr = await db.execute(
            text("DELETE FROM graph_checkpoint_writes WHERE thread_id IN :ts").bindparams(
                bindparam("ts", expanding=True)
            ),
            {"ts": sorted(threads)},
        )
        deleted["graph_checkpoints"] = int(cp.rowcount or 0)
        deleted["graph_checkpoint_writes"] = int(wr.rowcount or 0)
    else:
        deleted["graph_checkpoints"] = 0
        deleted["graph_checkpoint_writes"] = 0
    deleted["graph_threads"] = len(threads)

    # 显式提交：DB 级联删除先落库，之后的文件/记忆清理失败不回滚数据删除
    await db.commit()

    # ---- 清文件/内存/长期记忆（DB 已删成，逐项容错）----
    deleted["files"] = _purge_project_files(project_id, [(r[0], r[1]) for r in docs])
    deleted["structured_artifact_files"] = _purge_structured_artifacts(list(artifact_paths))
    deleted["memory"] = await _clear_project_memory(project_id)
    deleted["in_memory_runs"] = _purge_in_memory_records(project_id)

    return {"success": True, "project_id": project_id, "name": project_name, "deleted": deleted}


@router.get("/{project_id}/gate")
async def list_gates(
    project_id: str,
    current_user: User = Depends(get_current_user),
):
    from core.agent_engine.gate_keeper import GateKeeper

    gk = GateKeeper()
    passed = gk.list_passed_stages(project_id)
    return {"project_id": project_id, "passed_stages": passed}
