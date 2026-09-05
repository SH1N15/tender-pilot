from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.settings import reload_settings
from services.database import get_db
from services.models import Document
from services.ocr import (
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    STATE_RUNNING,
    MinerUError,
    OCRTask,
    OCRTaskStore,
    build_ocr_config,
    get_ocr_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ocr", tags=["OCR"])


class OCRConfigUpdate(BaseModel):
    mode: str | None = None
    endpoint: str | None = None
    api_key: str | None = None
    timeout: int | None = None
    poll_interval: float | None = None
    max_polls: int | None = None
    clear_api_key: bool = False


class OCRTestRequest(BaseModel):
    mode: str | None = None
    endpoint: str | None = None
    api_key: str | None = None


def _task_public(task: OCRTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "project_id": task.project_id,
        "document_id": task.document_id,
        "file_path": task.file_path,
        "state": task.state,
        "error_class": task.error_class,
        "error_message": task.error_message,
        "polls": task.polls,
        "has_result": bool(task.markdown),
        "result_length": len(task.markdown) if task.markdown else 0,
        "updated_at": task.updated_at,
    }


@router.get("/config")
async def get_ocr_config():
    cfg = build_ocr_config()
    return cfg.to_public()


@router.post("/config")
async def update_ocr_config(body: OCRConfigUpdate):
    import services.env_store

    updates: dict[str, str] = {}
    key_kept = False
    if body.mode is not None:
        if body.mode not in ("off", "mock", "cloud", "selfhosted"):
            raise HTTPException(status_code=400, detail="mode 必须是 off/mock/cloud/selfhosted")
        updates["BMP_OCR_MODE"] = body.mode
    if body.endpoint is not None:
        updates["BMP_OCR_ENDPOINT"] = body.endpoint
    if body.clear_api_key:
        updates["BMP_OCR_API_KEY"] = ""
    elif body.api_key is not None:
        if body.api_key.strip() == "":
            # 硬保护：空串不允许静默清掉已有 key（防前端误传导致 key "丢失"）
            existing = services.env_store.read_env().get("BMP_OCR_API_KEY", "")
            if existing.strip():
                updates.pop("BMP_OCR_API_KEY", None)
                key_kept = True
            else:
                updates["BMP_OCR_API_KEY"] = ""
        else:
            updates["BMP_OCR_API_KEY"] = body.api_key
    if body.timeout is not None:
        updates["BMP_OCR_TIMEOUT"] = str(body.timeout)
    if body.poll_interval is not None:
        updates["BMP_OCR_POLL_INTERVAL"] = str(body.poll_interval)
    if body.max_polls is not None:
        updates["BMP_OCR_MAX_POLLS"] = str(body.max_polls)

    try:
        services.env_store.write_env_atomic(updates)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"写入 .env 失败: {e}")

    reload_settings()
    cfg = build_ocr_config()
    return {"success": True, "config": cfg.to_public(), "key_kept": key_kept}


@router.post("/test")
async def test_ocr(body: OCRTestRequest | None = None):
    cfg = build_ocr_config(
        mode=(body.mode if body and body.mode else None),
        endpoint=(body.endpoint if body and body.endpoint else None),
        api_key=(body.api_key if body and body.api_key is not None else None),
    )
    if cfg.mode == "off":
        return {"success": False, "error": "OCR 未启用（mode=off），请先在设置中配置", "error_class": "not_configured"}
    if cfg.mode == "cloud" and not cfg.api_key:
        return {"success": False, "error": "未配置 MinerU API Key", "error_class": "not_configured"}

    client = await get_ocr_client(cfg)
    if client is None:
        return {
            "success": False,
            "error": "OCR 客户端不可用（请检查 mode/endpoint/API Key）",
            "error_class": "not_configured",
        }
    try:
        from core.tracing import get_tracer

        tracer = get_tracer()
        span = tracer.start_span("ocr.test_connection", "ocr", {"ocr.mode": cfg.mode, "ocr.endpoint": cfg.endpoint})
        try:
            result = await client.test_connection()
            tracer.end_span(span)
            return {
                "success": True,
                "message": result.get("message", "连接成功"),
                "mode": cfg.mode,
                "endpoint": cfg.endpoint,
            }
        except MinerUError as e:
            tracer.end_span(span, status="error", error_type=e.error_class)
            return {"success": False, "error": str(e), "error_class": e.error_class}
        except Exception as e:  # noqa: BLE001
            tracer.end_span(span, status="error", error_type="unknown")
            return {"success": False, "error": str(e), "error_class": "unknown"}
    finally:
        await client.close()


def _needs_ocr(doc: Document) -> tuple[bool, str]:
    """判断文档是否需要 OCR：扫描件或无正文。"""
    meta = doc.doc_metadata or {}
    if meta.get("is_scanned"):
        return True, "is_scanned"
    text = doc.parsed_content or ""
    if len(text) < 200:
        return True, "low_text"
    return False, ""


@router.post("/scan/{project_id}")
async def scan_project(project_id: str, db: AsyncSession = Depends(get_db)):
    cfg = build_ocr_config()
    if cfg.mode == "off":
        raise HTTPException(status_code=400, detail="OCR 未启用（mode=off）")
    client = await get_ocr_client(cfg)
    if client is None:
        raise HTTPException(status_code=400, detail="OCR 客户端不可用，请先在设置中配置 API Key/endpoint")

    doc_result = await db.execute(select(Document).where(Document.project_id == project_id))
    docs = doc_result.scalars().all()
    if not docs:
        raise HTTPException(status_code=404, detail="项目没有文档")

    store = OCRTaskStore.instance()
    tasks: list[dict] = []
    try:
        for doc in docs:
            need, reason = _needs_ocr(doc)
            if not need:
                continue
            existing = [
                t
                for t in store.list_by_project(project_id)
                if t.document_id == str(doc.id) and t.state not in (STATE_DONE, STATE_FAILED)
            ]
            if existing:
                continue
            if not doc.file_path or not Path(doc.file_path).exists():
                continue
            task_id = await client.submit_file(doc.file_path, is_ocr=True)
            task = OCRTask(
                task_id=task_id,
                project_id=project_id,
                document_id=str(doc.id),
                file_path=doc.file_path,
                state=STATE_PENDING,
            )
            store.add(task)
            tasks.append(_task_public(task))
    finally:
        await client.close()

    return {"submitted": tasks, "count": len(tasks), "reason": "scanned_or_low_text"}


@router.get("/status/{project_id}")
async def ocr_status(project_id: str):
    store = OCRTaskStore.instance()
    tasks = store.list_by_project(project_id)
    return {
        "project_id": project_id,
        "tasks": [_task_public(t) for t in tasks],
        "summary": {
            "pending": sum(1 for t in tasks if t.state == STATE_PENDING),
            "running": sum(1 for t in tasks if t.state == STATE_RUNNING),
            "done": sum(1 for t in tasks if t.state == STATE_DONE),
            "failed": sum(1 for t in tasks if t.state == STATE_FAILED),
        },
    }


@router.post("/poll/{project_id}")
async def poll_tasks(project_id: str):
    cfg = build_ocr_config()
    client = await get_ocr_client(cfg)
    if client is None:
        raise HTTPException(status_code=400, detail="OCR 客户端不可用")
    store = OCRTaskStore.instance()
    updated: list[dict] = []
    try:
        for task in store.list_by_project(project_id):
            if task.state in (STATE_DONE, STATE_FAILED):
                continue
            if task.polls >= cfg.max_polls:
                task.state = STATE_FAILED
                task.error_class = "timeout"
                task.error_message = f"轮询超过最大次数 {cfg.max_polls}"
                updated.append(_task_public(task))
                continue
            task.polls += 1
            try:
                status = await client.query_task(task.task_id)
                task.state = status["state"]
                task.markdown = status.get("markdown")
                task.error_message = status.get("error")
            except MinerUError as e:
                task.state = STATE_FAILED
                task.error_class = e.error_class
                task.error_message = str(e)
            except Exception as e:  # noqa: BLE001
                task.state = STATE_FAILED
                task.error_class = "unknown"
                task.error_message = str(e)
            task.updated_at = time.time()
            updated.append(_task_public(task))
    finally:
        await client.close()
    return {"updated": updated, "count": len(updated)}


@router.post("/result/{project_id}")
async def apply_results(project_id: str, db: AsyncSession = Depends(get_db)):
    """把已完成 OCR 的 markdown 写回 Document.parsed_content（复用现有数据模型）。"""
    store = OCRTaskStore.instance()
    applied: list[dict] = []
    for task in store.list_by_project(project_id):
        if task.state != STATE_DONE or not task.markdown:
            continue
        doc_result = await db.execute(select(Document).where(Document.id == task.document_id))
        doc = doc_result.scalar_one_or_none()
        if not doc:
            continue
        meta = dict(doc.doc_metadata or {})
        if not doc.parsed_content or len(doc.parsed_content) < 200:
            doc.parsed_content = task.markdown
        else:
            doc.parsed_content = doc.parsed_content + "\n\n<!-- OCR 补充内容 -->\n\n" + task.markdown
        meta["ocr"] = {
            "task_id": task.task_id,
            "mode": build_ocr_config().mode,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }
        doc.doc_metadata = meta
        applied.append(
            {
                "document_id": task.document_id,
                "task_id": task.task_id,
                "text_length": len(doc.parsed_content),
            }
        )
    await db.flush()
    return {"applied": applied, "count": len(applied)}


@router.post("/run/{project_id}")
async def run_ocr_pipeline(project_id: str, db: AsyncSession = Depends(get_db)):
    """一键：扫描 → 提交 → 轮询 → 写回 parsed_content。"""
    await scan_project(project_id, db)
    cfg = build_ocr_config()
    client = await get_ocr_client(cfg)
    if client is None:
        raise HTTPException(status_code=400, detail="OCR 客户端不可用")
    store = OCRTaskStore.instance()
    try:
        for _ in range(cfg.max_polls):
            pending = [t for t in store.list_by_project(project_id) if t.state not in (STATE_DONE, STATE_FAILED)]
            if not pending:
                break
            for task in pending:
                try:
                    status = await client.query_task(task.task_id)
                    task.state = status["state"]
                    task.markdown = status.get("markdown")
                    task.error_message = status.get("error")
                    task.polls += 1
                except MinerUError as e:
                    task.state = STATE_FAILED
                    task.error_class = e.error_class
                    task.error_message = str(e)
                task.updated_at = time.time()
            import asyncio

            await asyncio.sleep(cfg.poll_interval)
    finally:
        await client.close()
    result = await apply_results(project_id, db)
    status = await ocr_status(project_id)
    return {"run": status, "applied": result}
