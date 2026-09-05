"""投标智航 / TenderPilot MCP Server（官方 MCP Python SDK，FastMCP v1 API）。

- 8 tools / 2 resources，工具复用现有业务服务（不复制逻辑）；
- transport：stdio（默认，供 MCP Inspector / 任意 MCP 客户端）、
  streamable-http / sse（HTTP 远程 transport）；
- 健康/能力清单：get_mcp_capabilities() 供 GUI 与诊断使用；
- 生命周期：单独进程运行 `python -m services.mcp.server --transport ...`，
  也可在同进程以 ASGI app 挂载（streamable_http_app / sse_app）。

隐私：工具只接收 project_id/kb_id 等标识，不接收也不记录 API Key。
"""

from __future__ import annotations

import argparse
import json
import logging
import types
from typing import Any

logger = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    FastMCP = None  # type: ignore

MCP_VERSION = "1.29.1"  # 官方 mcp Python SDK（FastMCP v1 兼容线）
MCP_PROTOCOL_REF = "Model Context Protocol (mcp >=1.9,<2, installed 1.29.1)"

mcp = FastMCP("投标智航 / TenderPilot MCP Server") if FastMCP else None


def _system_user() -> Any:
    """MCP 内部调用使用系统管理员身份（避免依赖 HTTP auth）。"""
    return types.SimpleNamespace(id="mcp-system", role="admin")


@mcp.tool()
async def interpret_tender(project_id: str) -> str:
    from services.database import db_session
    from services.routers.interpret import interpret_tender as _interpret

    async with db_session() as db:
        result = await _interpret(project_id, db)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def generate_outline(project_id: str, mode: str = "aligned") -> str:
    from services.database import db_session
    from services.routers.generate import generate_outline as _gen_outline

    async with db_session() as db:
        result = await _gen_outline(project_id, mode, db)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def run_compliance_check(project_id: str) -> str:
    from services.database import db_session
    from services.routers.check import check_compliance as _check

    async with db_session() as db:
        result = await _check(project_id, db)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def run_full_check(project_id: str) -> str:
    from services.database import db_session
    from services.routers.check import full_check as _full

    async with db_session() as db:
        result = await _full(project_id, db)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def run_selfcheck(project_id: str) -> str:
    from services.database import db_session
    from services.routers.check import run_selfcheck as _selfcheck

    async with db_session() as db:
        result = await _selfcheck(project_id, db)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def search_knowledge(kb_id: str, query: str, top_k: int = 5) -> str:
    from services.database import db_session
    from services.routers.knowledge import search_knowledge_base as _search

    async with db_session() as db:
        result = await _search(kb_id, query, top_k, db)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def list_skills() -> str:
    from core.skill_engine.registry import SkillRegistry

    registry = SkillRegistry.instance()
    return json.dumps({"skills": registry.list_all()}, ensure_ascii=False)


@mcp.tool()
async def get_project_status(project_id: str) -> str:
    from services.database import db_session
    from services.routers.projects import get_project as _get

    async with db_session() as db:
        result = await _get(project_id, db, _system_user())
    return json.dumps(result, ensure_ascii=False)


@mcp.resource("bidmaster://skills")
def get_skills_resource() -> str:
    from core.skill_engine.registry import SkillRegistry

    registry = SkillRegistry.instance()
    return json.dumps(registry.list_all(), ensure_ascii=False)


@mcp.resource("bidmaster://project/{project_id}")
def get_project_resource(project_id: str) -> str:
    import asyncio

    from services.database import db_session
    from services.routers.projects import get_project as _get

    async def _fetch():
        async with db_session() as db:
            return await _get(project_id, db, _system_user())

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_fetch())
        return json.dumps(result, ensure_ascii=False)
    finally:
        loop.close()


# ─────────────────────────────────────────────
# 健康 / 能力清单（供 GUI 与诊断）
# ─────────────────────────────────────────────


async def _capabilities_async() -> dict[str, Any]:
    tools = []
    resources = []
    if mcp is not None:
        try:
            for t in await mcp.list_tools():
                tools.append(
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "parameters": getattr(t, "inputSchema", None) or {},
                    }
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 MCP tools 失败: %s", e)
        try:
            for r in await mcp.list_resources():
                resources.append(
                    {
                        "uri": r.uri,
                        "name": r.name or "",
                        "description": r.description or "",
                        "mime_type": getattr(r, "mime_type", None) or "",
                    }
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 MCP resources 失败: %s", e)
        try:
            for tpl in await mcp.list_resource_templates():
                resources.append(
                    {
                        "uri_template": tpl.uriTemplate,
                        "name": tpl.name or "",
                        "description": tpl.description or "",
                    }
                )
        except Exception:  # noqa: BLE001
            pass

    return {
        "enabled": mcp is not None,
        "name": "投标智航 / TenderPilot MCP Server",
        "version": MCP_VERSION,
        "protocol_ref": MCP_PROTOCOL_REF,
        "transports": ["stdio", "streamable-http", "sse"],
        "tools_count": len(tools),
        "resources_count": len(resources),
        "tools": tools,
        "resources": resources,
    }


async def get_mcp_capabilities() -> dict[str, Any]:
    """返回 MCP 服务能力清单（不调用任何外部服务）。"""
    return await _capabilities_async()


# ─────────────────────────────────────────────
# 启动入口
# ─────────────────────────────────────────────


def create_http_app(transport: str = "streamable-http"):
    """返回可挂载的 ASGI app（streamable-http 或 sse）。"""
    if mcp is None:
        raise RuntimeError("mcp 包未安装")
    if transport == "streamable-http":
        return mcp.streamable_http_app()
    if transport == "sse":
        return mcp.sse_app()
    raise ValueError(f"不支持的 transport: {transport}")


def run_stdio() -> None:
    if mcp is None:
        raise RuntimeError("mcp 包未安装")
    mcp.run(transport="stdio")


def main() -> None:
    parser = argparse.ArgumentParser(description="投标智航 / TenderPilot MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.transport == "stdio":
        run_stdio()
        return

    import uvicorn

    app = create_http_app(args.transport)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
