from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.storage import get_storage
from services.database import get_db
from services.llm_factory import get_llm_gateway
from services.models import Analysis, Document, Project, ProjectStatus

logger = logging.getLogger(__name__)


def _sanitize_text(text: str) -> str:
    """PostgreSQL UTF8 不接受 \x00，统一清理 C0 控制字符但保留 \n\r\t。"""
    return "".join(ch for ch in text if ch >= "\x20" or ch in "\n\r\t")


def _sanitize_metadata(meta):
    """递归清理 doc_metadata 中的字符串，防止控制字符导致 JSON 写入失败。"""
    if isinstance(meta, dict):
        return {k: _sanitize_metadata(v) for k, v in meta.items()}
    if isinstance(meta, list):
        return [_sanitize_metadata(v) for v in meta]
    if isinstance(meta, str):
        return _sanitize_text(meta)
    return meta


router = APIRouter()

# G7-4：后台落库任务强引用集合（防 asyncio.create_task 任务被 GC）
_INTERPRET_PERSIST_TASKS: set[asyncio.Task] = set()


async def _build_interpret_payload(project_id: str, db: AsyncSession, mode: str) -> dict:
    """解读子图运行入参构造（项目/文档/scoring 校验，垫片与异步图端点共用）。"""
    project_result = await db.execute(select(Project).where(Project.id == project_id))
    project = project_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    doc_result = await db.execute(select(Document).where(Document.id == project.tender_doc_id))
    doc = doc_result.scalar_one_or_none()
    if not doc or not doc.parsed_content:
        raise HTTPException(status_code=400, detail="请先解析招标文件")
    payload = {"project_id": project_id, "tender_text": doc.parsed_content, "mode": mode}
    if mode == "matrix":
        # 旧工作流语义：矩阵只消费库内已有解读维度的 scoring，不重跑全量解读
        analysis_result = await db.execute(select(Analysis).where(Analysis.project_id == project.id))
        analysis_row = analysis_result.scalar_one_or_none()
        scoring = (analysis_row.dimensions or {}).get("scoring") if analysis_row and analysis_row.dimensions else None
        if not scoring:
            raise HTTPException(status_code=400, detail="请先完成招标解读")
        payload["scoring_data"] = scoring
    return payload


async def _run_interpret_graph_compat(project_id: str, db: AsyncSession, mode: str):
    from services.interpret.graph_runtime import get_interpret_run_manager

    payload = await _build_interpret_payload(project_id, db, mode)
    manager = get_interpret_run_manager()
    record = await manager.create_run(payload)
    # 600s：解读 15 维在 qwen 延迟漂移下实测可达 ~360s+（G-2 实测漂移 46%），360s 卡线会超时丢结果
    record = await manager.wait_settled(record.run_id, timeout=600 if mode == "interpret" else 300)
    snap = record.snapshot or {}
    if mode == "interpret":
        result = snap.get("interpret_result") or {}
    elif mode == "matrix":
        result = snap.get("matrix_result") or {}
    else:
        result = snap.get("risk_result") or {}
    if result.get("success") and isinstance(result.get("data"), dict):
        project_result = await db.execute(select(Project).where(Project.id == project_id))
        project = project_result.scalar_one_or_none()
        analysis_result = await db.execute(select(Analysis).where(Analysis.project_id == project_id))
        analysis = analysis_result.scalar_one_or_none()
        data = result["data"]
        if mode == "interpret":
            if analysis:
                analysis.dimensions = data.get("dimensions", analysis.dimensions)
                analysis.scoring_matrix = data.get("scoring_matrix", analysis.scoring_matrix)
                analysis.risk_flags = data.get("risk_flags", analysis.risk_flags)
                analysis.sections = data.get("sections", analysis.sections)
            else:
                db.add(Analysis(project_id=project.id, dimensions=data.get("dimensions", {}),
                                 scoring_matrix=data.get("scoring_matrix", {}),
                                 risk_flags=data.get("risk_flags", {}), sections=data.get("sections", [])))
        elif mode == "matrix" and analysis:
            analysis.scoring_matrix = data
        await db.flush()
    return result


async def _persist_interpret_result(project_id: str, mode: str, result: dict) -> bool:
    """G7-4：把解读图 run 的最终结果持久化到 Analysis（与 _run_interpret_graph_compat 同口径：
    interpret→dimensions/scoring_matrix/risk_flags/sections；matrix→scoring_matrix；
    risk 与垫片一致不落库。自带独立会话，供异步端点的后台任务使用。返回是否落库。"""
    if not (isinstance(result, dict) and result.get("success") and isinstance(result.get("data"), dict)):
        return False
    if mode not in ("interpret", "matrix"):
        return False
    from services.database import async_session

    data = result["data"]
    async with async_session()() as db:
        project_result = await db.execute(select(Project).where(Project.id == project_id))
        project = project_result.scalar_one_or_none()
        if not project:
            return False
        analysis_result = await db.execute(select(Analysis).where(Analysis.project_id == project_id))
        analysis = analysis_result.scalar_one_or_none()
        if mode == "interpret":
            if analysis:
                analysis.dimensions = data.get("dimensions", analysis.dimensions)
                analysis.scoring_matrix = data.get("scoring_matrix", analysis.scoring_matrix)
                analysis.risk_flags = data.get("risk_flags", analysis.risk_flags)
                analysis.sections = data.get("sections", analysis.sections)
            else:
                db.add(Analysis(project_id=project.id, dimensions=data.get("dimensions", {}),
                                 scoring_matrix=data.get("scoring_matrix", {}),
                                 risk_flags=data.get("risk_flags", {}), sections=data.get("sections", [])))
        elif mode == "matrix" and analysis:
            analysis.scoring_matrix = data
        await db.commit()
    return True


async def _wait_and_persist_interpret_run(run_id: str, project_id: str, mode: str) -> None:
    """后台任务：等解读图 run 结束后落库（失败只记日志，不影响 run 本身）。"""
    from services.interpret.graph_runtime import get_interpret_run_manager

    try:
        manager = get_interpret_run_manager()
        record = await manager.wait_settled(run_id, timeout=600 if mode == "interpret" else 300)
        snap = record.snapshot or {}
        result_key = {"interpret": "interpret_result", "matrix": "matrix_result", "risk": "risk_result"}.get(
            mode, "interpret_result"
        )
        persisted = await _persist_interpret_result(project_id, mode, snap.get(result_key) or {})
        logger.info(
            "G7-4 interpret graph run persist: run_id=%s mode=%s persisted=%s status=%s",
            run_id, mode, persisted, record.status,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("G7-4 解读图结果落库失败 run_id=%s: %s", run_id, exc)

MAX_FILE_SIZE = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".wps", ".md"}


@router.post("/upload/{project_id}")
async def upload_tender_file(
    project_id: str,
    files: list[UploadFile] = File(...),
    document_type: str = "tender",
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    if document_type not in {"tender", "reference", "bid"}:
        raise HTTPException(status_code=400, detail="document_type 必须是 tender、reference 或 bid")

    upload_dir = Path(f"./projects/{project_id}")
    upload_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []
    for file in files:
        file_ext = Path(file.filename).suffix.lower() if file.filename else ""
        if file_ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"不支持的文件格式: {file_ext}")

        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"文件大小超过限制({MAX_FILE_SIZE // 1024 // 1024}MB)")

        # P1-4: 经存储抽象写入（local 后端目录结构/文件名与现状完全一致）
        storage = get_storage()
        object_key = f"projects/{project_id}/{file.filename}"
        storage.save(object_key, content)
        file_path = storage.local_path(object_key) or Path(object_key)

        doc = Document(
            project_id=project.id,
            type=document_type,
            file_path=str(file_path),
            original_name=file.filename,
            file_size=len(content),
        )
        db.add(doc)
        await db.flush()

        if document_type == "tender" and not project.tender_doc_id:
            project.tender_doc_id = doc.id
            await db.flush()

        uploaded.append(
            {
                "document_id": str(doc.id),
                "file_name": file.filename,
                "file_size": len(content),
                "type": document_type,
            }
        )

    return {"uploaded": uploaded, "total": len(uploaded)}


@router.get("/documents/{project_id}")
async def list_documents(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Document).where(Document.project_id == project_id).order_by(Document.created_at))
    docs = result.scalars().all()
    from core.rag_engine.project_evidence import collection_name, document_chunk_count

    evidence_collection = collection_name(project_id)
    documents = []
    for d in docs:
        indexed_chunks = 0
        if d.type in {"reference", "bid"} and d.parsed_content:
            indexed_chunks = await document_chunk_count(project_id, str(d.id))
        documents.append(
            {
                "id": str(d.id),
                "file_name": d.original_name,
                "file_size": d.file_size,
                "type": d.type,
                "parsed": d.parsed_content is not None,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "evidence_collection": evidence_collection if d.type in {"reference", "bid"} else None,
                "indexed_chunks": indexed_chunks,
            }
        )
    return {
        "evidence_collection": evidence_collection,
        "documents": documents,
    }


@router.get("/evidence/{project_id}/search")
async def search_project_evidence(project_id: str, query: str, top_k: int = 8):
    """Search the current project's isolated evidence collection.

    Project uploads are deliberately separate from global legal/enterprise
    knowledge bases; exposing this read path makes the exact evidence used by
    repair and recheck observable without merging collections.
    """
    from core.rag_engine.project_evidence import collection_name, retrieve

    return {
        "project_id": project_id,
        "evidence_collection": collection_name(project_id),
        "results": await retrieve(project_id, query, top_k=max(1, min(int(top_k), 20))),
    }


@router.get("/document/{document_id}")
async def get_document_content(document_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    content_preview = None
    if doc.parsed_content:
        content_preview = doc.parsed_content[:50000]

    return {
        "id": str(doc.id),
        "file_name": doc.original_name,
        "file_size": doc.file_size,
        "type": doc.type,
        "parsed_content": content_preview,
        "doc_metadata": doc.doc_metadata,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


@router.delete("/document/{document_id}")
async def delete_project_document(document_id: str, db: AsyncSession = Depends(get_db)):
    """Delete one project reference/bid document and its isolated RAG chunks."""
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.type not in {"reference", "bid"}:
        raise HTTPException(status_code=409, detail="招标文件不能从项目证据区删除")
    project_id = str(doc.project_id)
    from core.rag_engine.project_evidence import delete_document
    from services.models import StructuredArtifact, TenderEntity

    chunks_deleted = await delete_document(project_id, str(doc.id))
    try:
        Path(doc.file_path).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    # Structured extraction rows reference documents without database
    # cascades in older installations; remove those dependents explicitly so
    # a user-authorized evidence cleanup cannot fail with a FK violation.
    await db.execute(delete(TenderEntity).where(TenderEntity.document_id == doc.id))
    await db.execute(delete(StructuredArtifact).where(StructuredArtifact.document_id == doc.id))
    await db.delete(doc)
    await db.flush()
    return {"success": True, "document_id": document_id, "project_id": project_id, "chunks_deleted": chunks_deleted}


@router.post("/parse/{project_id}")
async def parse_tender_file(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    doc_result = await db.execute(select(Document).where(Document.id == project.tender_doc_id))
    doc = doc_result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="招标文件未上传")

    return await _parse_document_row(doc, project, db)


async def _parse_document_row(doc: Document, project: Project, db: AsyncSession) -> dict:
    from pathlib import Path

    from core.doc_engine import SectionDetector, get_parser

    file_ext = Path(doc.file_path).suffix
    try:
        parser = get_parser(file_ext)
        # P1-4: 兼容远端存储——本地无该文件时从存储后端取回到临时文件解析
        local_file = doc.file_path
        if not Path(local_file).exists():
            local_file = get_storage().ensure_local(doc.file_path)
        parsed = parser.parse(local_file)
        # 部分政府采购 PDF 使用双描边字体，pdfplumber 会提取成“广广 东东”。
        # 按行折叠高置信重复字符，避免解读、章节生成和检查都消费乱码；
        # 正常行由启发式原样保留。
        from core.doc_engine.layout import _dedup_double_draw

        parsed.text = "\n".join(_dedup_double_draw(line) for line in (parsed.text or "").splitlines())
        text = _sanitize_text(parsed.text)
        metadata = _sanitize_metadata(parsed.metadata)
        doc.parsed_content = text
        doc.doc_metadata = metadata
        project.status = ProjectStatus.INTERPRETING.value
        await db.flush()
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.warning("解析/写库失败 project_id=%s: %s", project.id, e)
        raise HTTPException(status_code=500, detail=f"文件解析或写库失败: {e}")

    # P-A：解析链完成后追加结构化产物（后台线程，失败降级，不改变现有产物与响应）
    try:
        from core.doc_engine.pipeline import run_structuring_background

        run_structuring_background(local_file, project_id=str(project.id), document_id=doc.id)
    except Exception as pa_exc:  # noqa: BLE001
        logger.warning("P-A 结构化任务启动失败（忽略）: %s", pa_exc)
    if doc.type in {"reference", "bid"}:
        try:
            # Await indexing so an immediate recheck sees the uploaded facts;
            # fire-and-forget indexing introduced a race in the upload flow.
            from core.rag_engine.project_evidence import index_document

            indexing = await index_document(str(project.id), str(doc.id), doc.original_name or "", text)
        except Exception as rag_exc:  # noqa: BLE001
            logger.warning("项目补充资料 RAG 任务启动失败（忽略）: %s", rag_exc)

    detector = SectionDetector(llm_gateway=get_llm_gateway())
    sections = await detector.detect_async(parsed.text)

    return {
        "project_id": str(project.id),
        "document_id": str(doc.id),
        "document_type": doc.type,
        "text_length": len(parsed.text),
        "tables_count": len(parsed.tables),
        "sections_count": len(sections),
        "sections": sections,
        "doc_metadata": parsed.metadata,
        "evidence_collection": indexing.get("collection") if doc.type in {"reference", "bid"} else None,
        "indexed_chunks": indexing.get("chunks_added", 0) if doc.type in {"reference", "bid"} else 0,
    }


@router.post("/parse-document/{document_id}")
async def parse_project_document(document_id: str, db: AsyncSession = Depends(get_db)):
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    project = (await db.execute(select(Project).where(Project.id == doc.project_id))).scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return await _parse_document_row(doc, project, db)


@router.get("/analysis/{project_id}")
async def get_analysis(project_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    analysis_result = await db.execute(select(Analysis).where(Analysis.project_id == project.id))
    analysis = analysis_result.scalar_one_or_none()

    doc_result = await db.execute(select(Document).where(Document.id == project.tender_doc_id))
    doc = doc_result.scalar_one_or_none()

    has_documents = False
    doc_list_result = await db.execute(
        select(Document).where(Document.project_id == project.id).order_by(Document.created_at)
    )
    doc_list = doc_list_result.scalars().all()
    has_documents = len(doc_list) > 0
    has_parsed = any(d.parsed_content is not None for d in doc_list)

    return {
        "has_documents": has_documents,
        "has_parsed": has_parsed,
        "has_analysis": analysis is not None and analysis.dimensions is not None,
        "analysis": {
            "dimensions": analysis.dimensions if analysis else None,
            "scoring_matrix": analysis.scoring_matrix if analysis else None,
            "risk_flags": analysis.risk_flags if analysis else None,
            "sections": analysis.sections if analysis else None,
        }
        if analysis
        else None,
        "parse_info": {
            "text_length": len(doc.parsed_content) if doc and doc.parsed_content else 0,
            "doc_metadata": doc.doc_metadata if doc else None,
        }
        if doc and doc.parsed_content
        else None,
    }


@router.post("/interpret/{project_id}")
async def interpret_tender(project_id: str, db: AsyncSession = Depends(get_db)):
    # G-4 垫片：走解读子图（mode=interpret）；旧直调 body 已在 G-5 删除
    return await _run_interpret_graph_compat(project_id, db, "interpret")


@router.post("/scoring-matrix/{project_id}")
async def build_scoring_matrix(project_id: str, db: AsyncSession = Depends(get_db)):
    # G-4 垫片：走解读子图（mode=matrix）；旧直调 body 已在 G-5 删除
    return await _run_interpret_graph_compat(project_id, db, "matrix")


@router.post("/risk-alert/{project_id}")
async def risk_alert(project_id: str, db: AsyncSession = Depends(get_db)):
    # G-4 垫片：走解读子图（mode=risk）；旧直调 body 已在 G-5 删除
    return await _run_interpret_graph_compat(project_id, db, "risk")


# ─────────────────────────────────────────────
# G-5 T1：解读子图异步查询端点（前端图运行视图数据源）
# ─────────────────────────────────────────────


@router.post("/{project_id}/graph/run")
async def create_interpret_graph_run(project_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    """创建解读子图运行（mode=interpret|matrix|risk），异步执行；旧垫片端点不动。"""
    from services.interpret.graph_runtime import get_interpret_run_manager

    mode = str(body.get("mode") or "interpret")
    if mode not in ("interpret", "matrix", "risk"):
        raise HTTPException(status_code=422, detail="mode 必须是 interpret / matrix / risk")
    payload = await _build_interpret_payload(project_id, db, mode)
    record = await get_interpret_run_manager().create_run(payload)
    # G7-4：异步 run 结束后把结果持久化到 Analysis（与垫片同口径），否则矩阵/风险
    # 前置校验（ analyses 记录）恒失败。后台任务持有引用防 GC。
    task = asyncio.create_task(_wait_and_persist_interpret_run(record.run_id, project_id, mode))
    _INTERPRET_PERSIST_TASKS.add(task)
    task.add_done_callback(_INTERPRET_PERSIST_TASKS.discard)
    return {"success": True, "run_id": record.run_id, "status": record.status}


@router.get("/{project_id}/graph/runs")
async def list_interpret_graph_runs(project_id: str):
    """解读子图运行列表（本项目过滤）。"""
    from services.interpret.graph_runtime import get_interpret_run_manager

    manager = get_interpret_run_manager()
    runs = [r for r in manager._runs.values() if r.project_id == project_id]
    runs.sort(key=lambda r: r.created_at, reverse=True)
    return {
        "success": True,
        "runs": [
            {
                "run_id": r.run_id,
                "project_id": r.project_id,
                "status": r.status,
                "created_at": r.created_at,
                "error": r.error,
            }
            for r in runs
        ],
    }


@router.get("/{project_id}/graph/runs/{run_id}")
async def get_interpret_graph_run(project_id: str, run_id: str):
    """解读子图运行快照（node_status + interpret/matrix/risk 结果摘要）。"""
    from services.interpret.graph_runtime import get_interpret_run_manager

    manager = get_interpret_run_manager()
    try:
        record = manager.get(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="解读图 run 不存在") from None
    if record.project_id != project_id:
        raise HTTPException(status_code=404, detail="解读图 run 不存在")
    snap = record.snapshot or {}
    node_status = snap.get("node_status", {})
    if not node_status and record.status == "running":
        node_status = {"interpret_dispatch": "running"}
    result_key = {"interpret": "interpret_result", "matrix": "matrix_result", "risk": "risk_result"}.get(
        str(snap.get("mode") or "interpret"), "interpret_result"
    )
    return {
        "success": True,
        "run_id": run_id,
        "project_id": record.project_id,
        "status": record.status,
        "error": record.error,
        "snapshot": {
            "node_status": node_status,
            "mode": snap.get("mode", ""),
            # mode 对应的最终结果（形状与旧垫片响应一致：{success,data,error,warnings}）
            "result": snap.get(result_key),
            "has_result": bool(
                snap.get("interpret_result") or snap.get("matrix_result") or snap.get("risk_result")
            ),
            "errors": [snap[k].get("error") for k in ("interpret_result", "matrix_result", "risk_result")
                       if snap.get(k) and snap[k].get("error")],
        },
    }


@router.post("/export/{project_id}")
async def export_interpret(
    project_id: str,
    format: str = "markdown",
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import PlainTextResponse

    from services.interpret.report_export import render_interpret_export

    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    analysis_result = await db.execute(select(Analysis).where(Analysis.project_id == project.id))
    analysis = analysis_result.scalar_one_or_none()
    if not analysis or not analysis.dimensions:
        raise HTTPException(status_code=400, detail="请先完成招标解读")

    # G-5：导出为确定性格式化（无 LLM），执行体在 services/interpret/report_export.py
    try:
        content = await render_interpret_export(analysis.dimensions, format, project.name)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    content_type = "text/markdown" if format == "markdown" else "text/html" if format == "html" else "application/json"
    return PlainTextResponse(content=content, media_type=content_type)
