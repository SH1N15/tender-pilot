"""AG-UI 状态/能力 API：供 GUI 设置页与诊断使用。"""

from __future__ import annotations

from fastapi import APIRouter

from core.settings import get_settings
from services.agui import AGUI_SDK_VERSION, AGUI_SPEC_REF

router = APIRouter(prefix="/agui", tags=["AG-UI"])


@router.get("/status")
async def agui_status():
    s = get_settings()
    base = (s.public_base_url or f"http://localhost:{s.port}").rstrip("/")
    return {
        "enabled": s.agui_enabled,
        "spec_ref": AGUI_SPEC_REF,
        "sdk_version": AGUI_SDK_VERSION,
        "transport": "sse (text/event-stream)",
        "endpoints": {
            "run": f"{base}/api/agui/run",
            "resume": f"{base}/api/agui/resume",
        },
        "capabilities": {
            "streaming": True,
            "tool_call_lifecycle": True,
            "step_events": True,
            "hitl_interrupt": True,
            "resume": True,
        },
    }
