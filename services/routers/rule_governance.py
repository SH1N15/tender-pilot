"""规则治理 API：候选 -> 审批 -> 规则包 -> 发布(门禁) -> 回滚。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.qualification.rule_governance import (
    PublicationBlockedError,
    RuleGovernanceStore,
    RuleValidationError,
    approve_proposal,
    create_rule_pack,
    generate_rule_proposals,
    publish_rule_pack,
    reject_proposal,
    rollback_rule_pack,
)

router = APIRouter(prefix="/rules", tags=["规则治理"])


class ReviewRequest(BaseModel):
    reviewer: str = ""
    note: str = ""


class CreatePackRequest(BaseModel):
    name: str
    proposal_ids: list[str]
    created_by: str = "admin"


class PublishRequest(BaseModel):
    published_by: str = "admin"


class RollbackRequest(BaseModel):
    rolled_back_by: str = "admin"


@router.get("/proposals")
async def list_proposals(status: str | None = None):
    store = RuleGovernanceStore.instance()
    proposals = store.list_proposals(status=status)
    return {"proposals": [p.model_dump() for p in proposals], "count": len(proposals)}


@router.post("/proposals/generate")
async def generate(limit: int = 5):
    try:
        created = generate_rule_proposals(limit=limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"候选生成失败: {e}")
    return {"created": [p.model_dump() for p in created], "count": len(created)}


@router.post("/proposals/{proposal_id}/approve")
async def approve(proposal_id: str, body: ReviewRequest):
    try:
        proposal = approve_proposal(proposal_id, reviewer=body.reviewer, note=body.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return proposal.model_dump()


@router.post("/proposals/{proposal_id}/reject")
async def reject(proposal_id: str, body: ReviewRequest):
    try:
        proposal = reject_proposal(proposal_id, reviewer=body.reviewer, note=body.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return proposal.model_dump()


@router.get("/packs")
async def list_packs():
    store = RuleGovernanceStore.instance()
    return {"packs": [p.model_dump() for p in store.list_packs()], "count": len(store.list_packs())}


@router.post("/packs")
async def create_pack(body: CreatePackRequest):
    try:
        pack = create_rule_pack(body.name, body.proposal_ids, created_by=body.created_by)
    except (ValueError, RuleValidationError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return pack.model_dump()


@router.post("/packs/{pack_id}/publish")
async def publish_pack(pack_id: str, body: PublishRequest):
    try:
        pack = publish_rule_pack(pack_id, published_by=body.published_by)
    except PublicationBlockedError as e:
        raise HTTPException(status_code=409, detail={"message": str(e), "details": e.details})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return pack.model_dump()


@router.post("/packs/{pack_id}/rollback")
async def rollback_pack(pack_id: str, body: RollbackRequest):
    try:
        pack = rollback_rule_pack(pack_id, rolled_back_by=body.rolled_back_by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return pack.model_dump()


@router.get("/audit")
async def list_audit(limit: int = 100):
    store = RuleGovernanceStore.instance()
    return {"events": store.list_audit(limit=limit), "count": min(len(store.list_audit(limit=limit)), limit)}
