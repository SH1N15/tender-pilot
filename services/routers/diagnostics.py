"""环境诊断 API：GUI「环境诊断」卡片。不打印 secret。"""

from __future__ import annotations

from fastapi import APIRouter

from services.env_check import run_checks

router = APIRouter(prefix="/diagnostics", tags=["诊断"])


@router.get("")
async def diagnostics():
    checks = await run_checks()
    allowed = ("ok", "configured", "enabled", "off", "disabled", "not_configured")
    return {"checks": checks, "overall_ok": all(c.get("status") in allowed for c in checks)}
