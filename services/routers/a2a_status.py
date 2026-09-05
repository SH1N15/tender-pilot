"""A2A 状态/Agent Card API：供 GUI 与诊断使用。"""

from __future__ import annotations

from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from fastapi import APIRouter

from core.settings import get_settings
from services.a2a_server import A2A_SDK_VERSION, A2A_SPEC_REF, build_agent_card

router = APIRouter(prefix="/a2a", tags=["A2A"])


def _card_url() -> str:
    s = get_settings()
    return (s.public_base_url or f"http://localhost:{s.port}").rstrip("/")


@router.get("/status")
async def a2a_status():
    s = get_settings()
    base = _card_url()
    return {
        "enabled": s.a2a_enabled,
        "spec_ref": A2A_SPEC_REF,
        "sdk_version": A2A_SDK_VERSION,
        "protocol_version": "1.0",
        "agent_card_url": f"{base}/.well-known/agent-card.json",
        "jsonrpc_url": f"{base}/",
        "skills_count": 7,
        "skills": [sk["id"] for sk in agent_card_to_dict(build_agent_card(base)).get("skills", [])],
        "transports": ["jsonrpc", "rest", "sse-streaming"],
    }


@router.get("/agent-card")
async def a2a_agent_card():
    base = _card_url()
    return agent_card_to_dict(build_agent_card(base))
