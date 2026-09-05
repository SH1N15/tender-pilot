"""项目补充资料的证据 RAG。

项目材料使用独立 collection，避免污染生产 kb_ent_*；最终投标正文仍保留在
Document/Chapter 中，检查阶段只按需检索这里的证据片段。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _is_internal_placeholder(text: str) -> bool:
    markers = (
        "开发测试资料", "非真实投标", "非真实企业文件", "测试占位",
        "测试值", "测试文本", "TEST-DEPOSIT-", "TEST-ID-", "TEST-RK-",
    )
    return any(marker in str(text or "") for marker in markers)


def _is_internal_source(source: str) -> bool:
    """整份资料级过滤，避免首块的测试标记被分块后失效。"""
    value = str(source or "")
    return any(marker in value for marker in ("测试资料", "测试草案", "开发测试", "占位"))


def collection_name(project_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]", "", str(project_id))[:32] or "unknown"
    return f"kb_proj_{slug}"


def _chunks(text: str, size: int = 1200, overlap: int = 160) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        chunk = text[start:end].strip()
        if chunk:
            out.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


async def index_document(project_id: str, document_id: str, source: str, text: str) -> dict:
    chunks = _chunks(text)
    if not chunks:
        return {"collection": collection_name(project_id), "chunks_added": 0}
    from core.rag_engine.kb_adapter import _build_retriever

    retriever, store = _build_retriever()
    collection = collection_name(project_id)
    ids = [
        "ev_" + hashlib.sha256(f"{document_id}:{i}:{chunk}".encode("utf-8")).hexdigest()[:32]
        for i, chunk in enumerate(chunks)
    ]
    metadata = [
        {
            "project_id": str(project_id),
            "document_id": str(document_id),
            "source": source,
            "chunk_index": i,
            "evidence_type": "project_reference",
        }
        for i in range(len(chunks))
    ]
    embeddings = await retriever.embedder.embed(chunks)
    replace = getattr(store, "replace_documents", None)
    if replace is not None:
        await replace(collection, ids, chunks, embeddings, metadata)
    else:
        # Compatibility for lightweight test doubles and older adapters.
        await store.add_documents(collection, ids, chunks, embeddings, metadata)
    return {"collection": collection, "chunks_added": len(chunks)}


async def retrieve(project_id: str, query: str, top_k: int = 5) -> list[dict]:
    if not project_id or not str(query).strip():
        return []
    from core.rag_engine.kb_adapter import _build_retriever

    retriever, store = _build_retriever()
    vectors = await retriever.embedder.embed([str(query)])
    # Ask for a wider candidate set, then apply document-version arbitration.
    # A project can contain several uploads covering the same fact (for
    # example an earlier deposit sheet and a later consolidated sheet).  Pure
    # vector score is insufficient because both documents are semantically
    # relevant; the newest overlapping source must win.
    requested_top_k = max(int(top_k), 1)
    result = await store.query(
        collection_name(project_id), vectors[0], top_k=max(requested_top_k * 5, requested_top_k)
    )
    ids = (result.get("ids") or [[]])[0]
    texts = (result.get("documents") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    collection = collection_name(project_id)
    candidates = [
        {
            "id": ids[i] if i < len(ids) else "",
            "text": texts[i] if i < len(texts) else "",
            "score": 1 - float(distances[i]) if i < len(distances) else 0.0,
            "metadata": {
                **(metas[i] if i < len(metas) and isinstance(metas[i], dict) else {}),
                "collection": collection,
            },
        }
        for i in range(len(texts))
        if str(texts[i]).strip()
        and not _is_internal_placeholder(texts[i])
        and not _is_internal_source(
            (metas[i] if i < len(metas) and isinstance(metas[i], dict) else {}).get("source", "")
        )
    ]

    # Resolve overlapping project uploads by recency.  This is deliberately
    # metadata-driven and does not special-case any customer or filename.
    # If database metadata is unavailable (unit tests/offline mode), the
    # original score ordering remains the fallback.
    try:
        from sqlalchemy import select

        from services.database import async_session
        from services.models import Document

        async with async_session()() as db:
            rows = (
                await db.execute(select(Document).where(Document.project_id == str(project_id)))
            ).scalars().all()
        versions = {
            str(row.id): {
                "created_at": row.created_at or datetime.min.replace(tzinfo=timezone.utc),
                "text": str(row.parsed_content or ""),
                "source": str(row.original_name or ""),
            }
            for row in rows
            if str(row.type or "") in {"reference", "bid"} and row.parsed_content
        }

        def _terms(value: str) -> set[str]:
            # Character n-grams keep this language agnostic and work for both
            # Chinese filenames/content and ASCII identifiers.
            raw = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9_.-]{2,}", value or "")
            stop = {"项目", "资料", "补充", "企业", "证明", "投标", "文件", "source", "txt", "md"}
            raw = [token for token in raw if token.casefold() not in stop]
            terms: set[str] = set(raw)
            for token in raw:
                if len(token) > 3 and re.fullmatch(r"[\u4e00-\u9fff]+", token):
                    terms.update(token[i : i + 2] for i in range(len(token) - 1))
            return terms

        # Compare against newer documents that share the query's subject.
        # Content-only overlap is deliberately conservative: broad Chinese
        # terms such as“项目/证明/企业”appear in many unrelated attachments.
        # Require either a source-name subject match or a much stronger
        # content/query overlap before treating a document as a replacement.
        query_terms = _terms(str(query))
        ordered_versions = sorted(versions.values(), key=lambda item: item["created_at"], reverse=True)
        for item in candidates:
            meta = item.get("metadata") or {}
            document_id = str(meta.get("document_id") or "")
            current = versions.get(document_id)
            if not current:
                continue
            current_terms = _terms(current["text"])
            for newer in ordered_versions:
                if newer is current or newer["created_at"] <= current["created_at"]:
                    continue
                newer_terms = _terms(newer["text"])
                shared = len(current_terms & newer_terms)
                subject_shared = len(query_terms & current_terms & newer_terms)
                source_shared = len(_terms(current["source"]) & _terms(newer["source"]))
                # Content overlap alone is not enough: every project upload
                # repeats the company/project header. Supersede only when
                # filenames share at least two meaningful subject tokens, or
                # when there is a very strong query-specific overlap.
                same_subject = source_shared >= 2 or (shared >= 24 and subject_shared >= 3)
                if shared >= 12 and same_subject:
                    item["_superseded"] = True
                    break

        candidates = [item for item in candidates if not item.get("_superseded")]
        # Keep the strongest result from each source before applying the
        # caller's requested top_k.  This prevents ten chunks of an old file
        # from crowding out a newer consolidated upload.
        deduped: list[dict] = []
        seen_sources: set[str] = set()
        for item in candidates:
            source = str((item.get("metadata") or {}).get("source") or "")
            if source and source in seen_sources:
                continue
            if source:
                seen_sources.add(source)
            deduped.append(item)
        candidates = deduped
        # For project-evidence queries, semantic top-k alone can hide a
        # required attachment behind similar chunks. Add one current chunk per
        # uploaded source so material checks see the whole evidence ledger.
        if "项目补充资料" in str(query):
            try:
                collection_obj = store.get_or_create_collection(collection)
                all_rows = collection_obj.get(include=["documents", "metadatas", "ids"])
                known = {str((item.get("metadata") or {}).get("source") or "") for item in candidates}
                for idx, doc_text in enumerate(all_rows.get("documents") or []):
                    meta = (all_rows.get("metadatas") or [])[idx] if idx < len(all_rows.get("metadatas") or []) else {}
                    source = str((meta or {}).get("source") or "")
                    if not source or source in known or not str(doc_text or "").strip():
                        continue
                    candidates.append({
                        "id": (all_rows.get("ids") or [""])[idx] if idx < len(all_rows.get("ids") or []) else "",
                        "text": doc_text,
                        "score": 0.0,
                        "metadata": {**(meta or {}), "collection": collection},
                    })
                    known.add(source)
            except Exception:  # noqa: BLE001
                pass
        # When several project uploads cover the same subject, the newest
        # consolidated source is the active working version.  Recency is only
        # a tie-breaker after semantic retrieval and never crosses project
        # boundaries; this prevents stale drafts from feeding repair prompts.
        created_by_source = {
            str(item["source"]): item["created_at"]
            for item in ordered_versions
            if str(item.get("source") or "")
        }
        candidates.sort(
            key=lambda item: (
                created_by_source.get(
                    str((item.get("metadata") or {}).get("source") or ""),
                    datetime.min.replace(tzinfo=timezone.utc),
                ),
                float(item.get("score") or 0.0),
            ),
            reverse=True,
        )
    except Exception:  # noqa: BLE001 - retrieval must work in offline/test mode
        pass

    return candidates[:requested_top_k]


async def document_chunk_count(project_id: str, document_id: str) -> int:
    """Return the number of indexed project-evidence chunks for one document."""
    if not project_id or not document_id:
        return 0
    from core.rag_engine.kb_adapter import _build_retriever

    _retriever, store = _build_retriever()
    collection = store.get_or_create_collection(collection_name(project_id))
    try:
        result = collection.get(where={"document_id": str(document_id)}, include=[])
        return len(result.get("ids") or [])
    except Exception:  # noqa: BLE001 - indexing status must not break document listing
        return 0


async def delete_document(project_id: str, document_id: str) -> int:
    """Remove one project's evidence document from the isolated collection."""
    if not project_id or not document_id:
        return 0
    from core.rag_engine.kb_adapter import _build_retriever

    _retriever, store = _build_retriever()
    collection = store.get_or_create_collection(collection_name(project_id))
    try:
        found = collection.get(where={"document_id": str(document_id)}, include=[])
        count = len(found.get("ids") or [])
        deleter = getattr(store, "delete_documents", None)
        if deleter is not None:
            await deleter(collection_name(project_id), {"document_id": str(document_id)})
        else:
            collection.delete(where={"document_id": str(document_id)})
        return count
    except Exception:  # noqa: BLE001
        return 0
def index_document_background(project_id: str, document_id: str, source: str, text: str) -> None:
    def _worker() -> None:
        try:
            result = asyncio.run(index_document(project_id, document_id, source, text))
            logger.info("项目证据 RAG 入库完成 project=%s document=%s: %s", project_id, document_id, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("项目证据 RAG 入库失败 project=%s document=%s: %s", project_id, document_id, exc)

    threading.Thread(target=_worker, name="project-evidence-rag", daemon=True).start()


__all__ = [
    "collection_name",
    "document_chunk_count",
    "delete_document",
    "index_document",
    "index_document_background",
    "retrieve",
]
