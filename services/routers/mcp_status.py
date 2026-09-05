"""MCP 状态/能力清单 API：供 GUI 设置页与诊断使用。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from core.settings import get_settings
from services.mcp.server import get_mcp_capabilities, mcp

router = APIRouter(prefix="/mcp", tags=["MCP"])


@router.get("/capabilities")
async def mcp_capabilities():
    return await get_mcp_capabilities()


@router.get("/status")
async def mcp_status():
    caps = await get_mcp_capabilities()
    settings = get_settings()
    base = (settings.public_base_url or f"http://localhost:{settings.port}").rstrip("/")
    return {
        "enabled": settings.mcp_enabled and caps["enabled"],
        "name": caps["name"],
        "version": caps["version"],
        "protocol_ref": caps["protocol_ref"],
        "transports": caps["transports"],
        "tools_count": caps["tools_count"],
        "resources_count": caps["resources_count"],
        "tools": caps["tools"],
        "resources": caps["resources"],
        "addresses": {
            "stdio": "python -m services.mcp.server --transport stdio",
            "streamable_http": f"{base}/mcp",
            "sse": f"{base}/sse",
        },
    }


@router.post("/test")
async def mcp_health_test():
    """最小 smoke：调用不依赖 LLM/DB 的 list_skills 工具验证工具执行路径。"""
    if mcp is None:
        raise HTTPException(status_code=503, detail="MCP 包未安装")
    from core.tracing import get_tracer

    tracer = get_tracer()
    span = tracer.start_span("mcp.health_test", "mcp", {"mcp.tool": "list_skills", "mcp.transport": "in-process"})
    try:
        result = await mcp.call_tool("list_skills", {})
        tracer.end_span(span)
        text = str(result)
        return {"success": True, "message": "MCP 工具调用成功", "tool": "list_skills", "result_preview": text[:300]}
    except Exception as e:  # noqa: BLE001
        tracer.end_span(span, status="error", error_type=e.__class__.__name__)
        return {"success": False, "error": str(e), "error_class": e.__class__.__name__}
