"""MCP 子模块：服务定义 + 独立进程入口 + ASGI 挂载。"""

from services.mcp.server import (
    create_http_app,
    get_mcp_capabilities,
    main,
    mcp,
    run_stdio,
)

__all__ = ["mcp", "get_mcp_capabilities", "create_http_app", "run_stdio", "main"]
