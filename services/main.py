from __future__ import annotations

import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.cost_guard import CircuitOpenError, QuotaExceededError
from core.exceptions import (
    GateNotPassedError,
    JsonRepairError,
    LLMGatewayError,
    ProjectNotFoundError,
    SkillNotFoundError,
    UnsupportedFormatError,
)
from core.settings import get_settings
from core.tracing.middleware import TracingMiddleware
from services.agui.routes import router as agui_routes_router
from services.database import close_db, init_db, is_db_ready
from services.routers import (
    a2a_status,
    agui_status,
    ai_image,
    auth,
    check,
    diagnostics,
    format_doc,
    generate,
    graph,
    interpret,
    knowledge,
    llm_config,
    mcp_status,
    monitor,
    news,
    ocr,
    projects,
    qualification,
    rbac,
    rule_governance,
    secrets,
    skills,
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# 降低 SQLAlchemy 引擎日志级别（太多 INFO 日志）
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from services.skill_bootstrap import register_builtin_skills

    register_builtin_skills()
    await init_db()
    # vNext: 初始化轻量 tracing（内存 + JSONL + 可选 OTLP）
    from core.settings import get_settings
    from core.tracing import get_tracer

    _s = get_settings()
    if _s.trace_enabled:
        get_tracer().configure(trace_dir=_s.trace_dir, otlp_endpoint=_s.otlp_endpoint)
    yield
    get_tracer().flush_otlp()
    await close_db()


app = FastAPI(
    title="投标智航 / TenderPilot API",
    description="全流程智能招投标平台",
    version="0.2.0",
    lifespan=lifespan,
)

# vNext: 挂载 A2A 官方协议路由（/.well-known/agent-card.json + JSON-RPC + REST）
_s = get_settings()
if _s.a2a_enabled:
    from services.a2a_server import create_a2a_app as _create_a2a_app

    _A2A_CARD_URL = (_s.public_base_url or f"http://localhost:{_s.port}").rstrip("/")
    _create_a2a_app(app, card_url=_A2A_CARD_URL)

# vNext: 同进程挂载 MCP streamable-http（/mcp），便于 GUI 直连测试
if _s.mcp_enabled:
    try:
        from services.mcp.server import create_http_app as _create_mcp_app

        app.mount("/mcp", _create_mcp_app("streamable-http"), name="mcp-streamable-http")
    except Exception as _mcp_e:  # noqa: BLE001
        import logging as _logging

        _logging.getLogger(__name__).warning("MCP 同进程挂载失败（可继续使用独立进程）: %s", _mcp_e)

app.add_middleware(
    TracingMiddleware,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(LLMGatewayError)
async def llm_gateway_error_handler(request: Request, exc: LLMGatewayError):
    return JSONResponse(status_code=502, content={"detail": f"LLM网关错误: {str(exc)}"})


@app.exception_handler(JsonRepairError)
async def json_repair_error_handler(request: Request, exc: JsonRepairError):
    return JSONResponse(status_code=422, content={"detail": f"JSON修复失败: {str(exc)}"})


@app.exception_handler(SkillNotFoundError)
async def skill_not_found_handler(request: Request, exc: SkillNotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(UnsupportedFormatError)
async def unsupported_format_handler(request: Request, exc: UnsupportedFormatError):
    return JSONResponse(status_code=415, content={"detail": str(exc)})


@app.exception_handler(GateNotPassedError)
async def gate_not_passed_handler(request: Request, exc: GateNotPassedError):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(ProjectNotFoundError)
async def project_not_found_handler(request: Request, exc: ProjectNotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


# P0-5 成本守卫：超限 → 429，熔断 → 503（detail 已为中文可读）
@app.exception_handler(QuotaExceededError)
async def quota_exceeded_handler(request: Request, exc: QuotaExceededError):
    return JSONResponse(status_code=429, content={"detail": str(exc)})


@app.exception_handler(CircuitOpenError)
async def circuit_open_handler(request: Request, exc: CircuitOpenError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    return JSONResponse(
        status_code=500,
        content={
            "detail": "服务器内部错误",
            "error": str(exc),
            "traceback": tb if get_settings().debug else None,
        },
    )


app.include_router(auth.router, prefix="/api/auth", tags=["认证"])
app.include_router(projects.router, prefix="/api/projects", tags=["项目管理"])
app.include_router(interpret.router, prefix="/api/interpret", tags=["招标解读"])
app.include_router(generate.router, prefix="/api/generate", tags=["投标生成"])
app.include_router(check.router, prefix="/api/check", tags=["投标检查"])
app.include_router(format_doc.router, prefix="/api/format", tags=["文档输出"])
app.include_router(skills.router, prefix="/api/skills", tags=["Skill管理"])
app.include_router(llm_config.router, prefix="/api/llm", tags=["LLM配置"])
app.include_router(news.router, prefix="/api/news", tags=["资讯中心"])
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["知识库"])
app.include_router(rbac.router, prefix="/api/rbac", tags=["权限管理"])
app.include_router(ai_image.router, prefix="/api/ai-image", tags=["AI配图"])
# G-5（2026-09-01）：/api/agent 独立旁路退役——前端/MCP/A2A/AG-UI 全部无引用（grep 复核 0 命中），
# 多 Agent 编排统一走 /api/graph 主编排图。agent_runtime.py 文件保留不删（库组件仍可引用）。
app.include_router(qualification.router, prefix="/api/qualification", tags=["资格预审"])
app.include_router(ocr.router, prefix="/api", tags=["OCR"])
app.include_router(mcp_status.router, prefix="/api", tags=["MCP"])
app.include_router(a2a_status.router, prefix="/api", tags=["A2A"])
app.include_router(agui_status.router, prefix="/api", tags=["AG-UI"])
app.include_router(secrets.router, prefix="/api/secrets", tags=["密钥管理"])
app.include_router(rule_governance.router, prefix="/api", tags=["规则治理"])
app.include_router(monitor.router, prefix="/api", tags=["监控"])
app.include_router(diagnostics.router, prefix="/api", tags=["诊断"])
app.include_router(agui_routes_router, prefix="/api", tags=["AG-UI"])
# P-D1：LangGraph 主编排图运行路由（增量挂载）
app.include_router(graph.router, prefix="/api/graph", tags=["图编排"])


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "app": "投标智航 / TenderPilot", "version": "0.2.0", "db_ready": is_db_ready()}


@app.get("/api/stats")
async def get_stats():
    from core.skill_engine.registry import SkillRegistry
    from services.llm_factory import get_llm_gateway

    registry = SkillRegistry.instance()
    try:
        gateway = get_llm_gateway()
        token_usage = gateway.get_token_summary()
    except Exception:
        token_usage = {}
    return {
        "skills_count": len(registry._skills),
        "skills": registry.list_all(),
        "token_usage": token_usage,
        "db_ready": is_db_ready(),
    }
