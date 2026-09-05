"""Business write paths for G-6 long-term memory."""

from __future__ import annotations

from collections import Counter

from core.agent_framework.memory import LongTermMemory


async def record_hitl_decision(project_id: str, *, action: str, reason: str = "", level: str = "") -> dict:
    return await LongTermMemory().record(
        project_id=project_id,
        source_type="hitl_decision",
        fact={"action": action, "reason": reason, "level": level},
    )


async def record_high_frequency_check_findings(project_id: str, results: list[dict]) -> dict | None:
    counts = Counter(
        str(item.get("check_id") or "")
        for item in results
        if item.get("status") in ("fail", "warning") and item.get("check_id")
    )
    if not counts:
        return None
    return await LongTermMemory().record(
        project_id=project_id,
        source_type="check_frequent_missing",
        fact={"counts": dict(counts)},
    )


async def record_credential_hit(project_id: str, *, credential_id: str, requirement_id: str) -> dict:
    return await LongTermMemory().record(
        project_id=project_id,
        source_type="credential_reuse_hit",
        fact={"credential_id": credential_id, "requirement_id": requirement_id},
    )


__all__ = ["record_hitl_decision", "record_high_frequency_check_findings", "record_credential_hit"]
