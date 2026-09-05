"""Load traceable enterprise evidence for the master qualification stage."""

from __future__ import annotations

import re
from typing import Iterable

from services.qualification.credential_adapter import confirm_candidate, extract_credentials
from services.qualification.models import Credential, Requirement


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", value)[:180].strip("-") or "source"


def _candidate_credentials(text: str, evidence_ref: str) -> tuple[list[Credential], list[str]]:
    extracted = extract_credentials(text, source_path=evidence_ref)
    credentials: list[Credential] = []
    for candidate in extracted.candidates:
        # Medium-confidence candidates remain useful when their source is a
        # project document. Warnings describe a missing field (for example a
        # certificate number), but must not erase an otherwise traceable
        # personnel/qualification candidate from matching.
        if candidate.confidence_level == "low":
            continue
        try:
            credentials.append(confirm_candidate(candidate, evidence_ref))
        except Exception as exc:  # noqa: BLE001 - 单条候选失败不影响其他证据
            extracted.warnings.append(f"{evidence_ref}: 候选证据绑定失败：{exc}")
    return credentials, list(extracted.warnings)


def _deduplicate(credentials: Iterable[Credential]) -> list[Credential]:
    seen: set[tuple] = set()
    result: list[Credential] = []
    for item in credentials:
        key = (
            item.credential_type,
            item.certificate_name,
            str(item.amount or item.contract_amount or ""),
            item.project_name,
            item.name if item.credential_type == "personnel" else None,
            item.personnel_title,
            item.region,
            tuple(item.evidence_refs),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


async def load_project_material_credentials(project_id: str) -> tuple[list[Credential], list[str]]:
    """Extract traceable credentials from parsed bid/reference documents in the project."""
    if not project_id:
        return [], []
    from sqlalchemy import select

    from services.database import async_session
    from services.models import Document

    credentials: list[Credential] = []
    warnings: list[str] = []
    async with async_session()() as db:
        documents = (
            await db.execute(
                select(Document).where(
                    Document.project_id == project_id,
                    Document.type.in_(["bid", "reference"]),
                )
            )
        ).scalars().all()
    for document in documents:
        if not (document.parsed_content or "").strip():
            continue
        rows, row_warnings = _candidate_credentials(
            document.parsed_content or "",
            f"document:{document.id}#parsed",
        )
        credentials.extend(rows)
        warnings.extend(row_warnings)
    return _deduplicate(credentials), warnings


async def load_enterprise_kb_credentials(
    requirements: list[Requirement],
) -> tuple[list[Credential], list[str]]:
    """Retrieve requirement-specific evidence from enterprise collections, read-only."""
    if not requirements:
        return [], []
    from core.rag_engine.kb_adapter import build_default_knowledge_base

    kb = await build_default_knowledge_base()
    if kb is None:
        return [], ["企业知识库当前不可用，资格核对仅使用项目内材料"]
    credentials: list[Credential] = []
    warnings: list[str] = []
    seen_chunks: set[str] = set()
    for requirement in requirements:
        query = (
            requirement.source_text
            or requirement.description
            or requirement.personnel_title
            or requirement.certificate_name
            or "资格证明"
        )
        try:
            documents = await kb.retrieve(str(query), top_k=12)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"企业知识库检索失败：{exc}")
            continue
        for document in documents:
            metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
            collection = str(metadata.get("collection") or "")
            if not collection.startswith("kb_ent_"):
                continue
            chunk_id = str(metadata.get("chunk_id") or metadata.get("source") or "chunk")
            unique_key = f"{collection}:{chunk_id}"
            if unique_key in seen_chunks:
                continue
            seen_chunks.add(unique_key)
            evidence_ref = f"manual:enterprise-kb-{_safe_label(unique_key)}"
            rows, row_warnings = _candidate_credentials(str(document.get("text") or ""), evidence_ref)
            credentials.extend(rows)
            warnings.extend(row_warnings)
    return _deduplicate(credentials), warnings


async def load_qualification_credentials(
    project_id: str,
    requirements: list[Requirement],
    explicit: list[dict] | None = None,
) -> tuple[list[dict], list[str]]:
    credentials = [Credential.model_validate(item) for item in (explicit or [])]
    project_rows, project_warnings = await load_project_material_credentials(project_id)
    kb_rows, kb_warnings = await load_enterprise_kb_credentials(requirements)
    credentials.extend(project_rows)
    credentials.extend(kb_rows)
    return [item.model_dump() for item in _deduplicate(credentials)], project_warnings + kb_warnings


__all__ = [
    "load_enterprise_kb_credentials",
    "load_project_material_credentials",
    "load_qualification_credentials",
]
