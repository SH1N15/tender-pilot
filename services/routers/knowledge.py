from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.rag_engine import Embedder, HybridRetriever, VectorStore
from core.settings import get_settings
from services.database import get_db
from services.models import KnowledgeBase as KnowledgeBaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md", ".html"}

_embedder_instance: Embedder | None = None
_vector_store_instance: VectorStore | None = None


def _resolve_embedding_api_key(settings) -> str:
    """请求时动态解析 Embedding Key：进程 env > keyring > .env；失败回退启动快照值。"""
    try:
        from core.secret_resolver import resolve_secret

        value, _source = resolve_secret("embedding_api_key")
        if value:
            return value
    except Exception:  # noqa: BLE001
        pass
    return settings.embedding_api_key


def _get_embedder() -> Embedder:
    global _embedder_instance
    settings = get_settings()
    api_key = _resolve_embedding_api_key(settings)
    if _embedder_instance is None:
        _embedder_instance = Embedder(
            {
                "mode": settings.embedding_mode,
                "model_name": settings.embedding_model,
                "api_key": api_key,
                "api_base": settings.embedding_api_base,
            }
        )
    else:
        # 设置页保存后无需重启即可生效（模型/Base/Key 均热更新）
        _embedder_instance.model_name = settings.embedding_model
        _embedder_instance.api_base = settings.embedding_api_base
        _embedder_instance.api_key = api_key
    return _embedder_instance


def _get_vector_store() -> VectorStore:
    global _vector_store_instance
    if _vector_store_instance is None:
        settings = get_settings()
        _vector_store_instance = VectorStore(persist_dir=settings.chroma_dir)
    return _vector_store_instance


def _get_retriever() -> HybridRetriever:
    return HybridRetriever(vector_store=_get_vector_store(), embedder=_get_embedder())


KB_TYPES = ("legal", "enterprise")
REVIEW_STATUSES = ("draft", "reviewed", "published")
COLLECTION_PREFIX = {"legal": "kb_legal_", "enterprise": "kb_ent_"}


class KnowledgeBaseCreate(BaseModel):
    name: str
    embedding_model: str = "text-embedding-v3"
    kb_type: str = "enterprise"  # legal | enterprise
    review_status: str = "draft"  # draft | reviewed | published
    valid_until: str | None = None  # ISO 日期，空=长期有效


def _parse_valid_until(value: str | None):
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"valid_until 需为 ISO 日期: {value}")


def _smart_chunk(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    if not text.strip():
        return []

    sentence_endings = re.compile(r"(?<=[。！？.!?])")

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks: list[str] = []
    current_chunk = ""
    overlap_text = ""

    for para in paragraphs:
        if len(para) > chunk_size:
            if current_chunk:
                chunks.append(current_chunk)
                overlap_text = current_chunk[-overlap:] if len(current_chunk) >= overlap else current_chunk
                current_chunk = ""

            sentences = sentence_endings.split(para)
            sentences = [s for s in sentences if s.strip()]

            sub_chunk = overlap_text
            for sent in sentences:
                if len(sub_chunk) + len(sent) > chunk_size and len(sub_chunk) > len(overlap_text):
                    chunks.append(sub_chunk)
                    overlap_text = sub_chunk[-overlap:] if len(sub_chunk) >= overlap else sub_chunk
                    sub_chunk = overlap_text
                sub_chunk += sent

            if sub_chunk.strip():
                current_chunk = sub_chunk
                overlap_text = ""
        else:
            candidate = (current_chunk + "\n\n" + para).strip() if current_chunk else para
            if len(candidate) > chunk_size and current_chunk:
                chunks.append(current_chunk)
                overlap_text = current_chunk[-overlap:] if len(current_chunk) >= overlap else current_chunk
                current_chunk = overlap_text + "\n\n" + para if overlap_text else para
            else:
                current_chunk = candidate

    if current_chunk.strip():
        chunks.append(current_chunk)

    return [c for c in chunks if c.strip()]


@router.get("/")
async def list_knowledge_bases(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(KnowledgeBaseModel).order_by(KnowledgeBaseModel.created_at.desc()))
    kbs = result.scalars().all()
    return {
        "knowledge_bases": [
            {
                "id": str(kb.id),
                "name": kb.name,
                "doc_count": kb.doc_count,
                "embedding_model": kb.embedding_model,
                "collection_name": kb.collection_name,
                "kb_type": getattr(kb, "kb_type", "enterprise"),
                "review_status": getattr(kb, "review_status", "draft"),
                "valid_until": kb.valid_until.isoformat() if getattr(kb, "valid_until", None) else None,
                "created_at": kb.created_at.isoformat() if kb.created_at else None,
            }
            for kb in kbs
        ]
    }


@router.post("/")
async def create_knowledge_base(kb: KnowledgeBaseCreate, db: AsyncSession = Depends(get_db)):
    if kb.kb_type not in KB_TYPES:
        raise HTTPException(status_code=400, detail=f"kb_type 必须是 {KB_TYPES} 之一")
    if kb.review_status not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail=f"review_status 必须是 {REVIEW_STATUSES} 之一")
    # P-B 双库命名：kb_legal_*（法规）/ kb_ent_*（企业），与存量 kb_* collection 隔离
    collection_name = f"{COLLECTION_PREFIX[kb.kb_type]}{uuid.uuid4().hex[:12]}"
    new_kb = KnowledgeBaseModel(
        name=kb.name,
        embedding_model=kb.embedding_model,
        collection_name=collection_name,
        kb_type=kb.kb_type,
        review_status=kb.review_status,
        valid_until=_parse_valid_until(kb.valid_until),
    )
    db.add(new_kb)
    await db.flush()

    vs = _get_vector_store()
    vs.get_or_create_collection(collection_name)

    return {
        "id": str(new_kb.id),
        "name": new_kb.name,
        "collection_name": collection_name,
    }


@router.delete("/{kb_id}")
async def delete_knowledge_base(kb_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(KnowledgeBaseModel).where(KnowledgeBaseModel.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    if kb.collection_name:
        vs = _get_vector_store()
        await vs.delete_collection(kb.collection_name)

    await db.delete(kb)
    await db.flush()
    return {"success": True}


@router.post("/{kb_id}/upload")
async def upload_documents(
    kb_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    import tempfile
    from pathlib import Path

    file_ext = Path(file.filename).suffix.lower() if file.filename else ""
    if file_ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {file_ext}，仅支持: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    result = await db.execute(select(KnowledgeBaseModel).where(KnowledgeBaseModel.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    content = await file.read()
    text = content.decode("utf-8", errors="ignore")

    parse_error = None
    with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        from core.doc_engine import get_parser

        parser = get_parser(file_ext)
        parsed = parser.parse(tmp_path)
        text = parsed.text
    except Exception as e:
        parse_error = str(e)
        logger.error(f"文档解析失败 [{file.filename}]: {e}", exc_info=True)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not text.strip():
        detail = "文档内容为空"
        if parse_error:
            detail += f"（解析错误: {parse_error}）"
        raise HTTPException(status_code=400, detail=detail)

    chunks = _smart_chunk(text, chunk_size=1000, overlap=200)
    if not chunks:
        raise HTTPException(status_code=400, detail="文档分块后无有效内容")

    kb_type = getattr(kb, "kb_type", "enterprise")
    chunk_metadatas = [
        {
            "source": file.filename,
            "kb_id": kb_id,
            "kb_type": kb_type,
            "chunk_index": i,
        }
        for i, _ in enumerate(chunks)
    ]

    try:
        from core.cost_guard import CircuitOpenError, QuotaExceededError, get_cost_guard

        guard = get_cost_guard()
        try:
            await guard.precheck("llm")  # embedding 属外部 LLM 家族调用，走配额/熔断
        except (CircuitOpenError, QuotaExceededError) as e:
            raise HTTPException(status_code=429, detail=f"Embedding 配额/熔断限制: {e}") from e

        retriever = _get_retriever()
        await retriever.add_documents(
            collection_name=kb.collection_name,
            texts=chunks,
            metadatas=chunk_metadatas,
        )
        await guard.record_result("llm", ok=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"向量入库失败 [{file.filename}]: {e}", exc_info=True)
        try:
            from core.cost_guard import get_cost_guard

            await get_cost_guard().record_result("llm", ok=False)
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status_code=500, detail=f"向量入库失败: {e}")

    kb.doc_count += len(chunks)
    await db.flush()

    response = {
        "success": True,
        "chunks_added": len(chunks),
        "total_docs": kb.doc_count,
    }
    if parse_error:
        response["warnings"] = [f"文档解析部分失败: {parse_error}，已使用原始文本"]
    return response


@router.post("/{kb_id}/search")
async def search_knowledge_base(
    kb_id: str,
    query: str,
    top_k: int = 5,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(KnowledgeBaseModel).where(KnowledgeBaseModel.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    retriever = _get_retriever()

    try:
        from core.cost_guard import CircuitOpenError, QuotaExceededError, get_cost_guard

        guard = get_cost_guard()
        try:
            await guard.precheck("llm")
        except (CircuitOpenError, QuotaExceededError) as e:
            raise HTTPException(status_code=429, detail=f"Embedding 配额/熔断限制: {e}") from e
    except HTTPException:
        raise

    results = await retriever.retrieve(
        query=query,
        collection_name=kb.collection_name,
        top_k=top_k,
    )

    # P-B 双库隔离：检索结果后过滤，仅返回与库 kb_type 一致的 chunk（元数据缺失视为不匹配）
    kb_type = getattr(kb, "kb_type", "enterprise")
    results = [r for r in results if (r.get("metadata") or {}).get("kb_type") == kb_type]

    return {"results": results}


class KnowledgeBaseStatusUpdate(BaseModel):
    review_status: str  # draft | reviewed | published
    valid_until: str | None = None


@router.patch("/{kb_id}/status")
async def update_knowledge_base_status(
    kb_id: str,
    payload: KnowledgeBaseStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    if payload.review_status not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail=f"review_status 必须是 {REVIEW_STATUSES} 之一")
    result = await db.execute(select(KnowledgeBaseModel).where(KnowledgeBaseModel.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    kb.review_status = payload.review_status
    kb.valid_until = _parse_valid_until(payload.valid_until)
    await db.flush()
    return {
        "id": str(kb.id),
        "kb_type": kb.kb_type,
        "review_status": kb.review_status,
        "valid_until": kb.valid_until.isoformat() if kb.valid_until else None,
    }
