"""共享的 Agent Runtime 装配助手（vNext）。

A2A / AG-UI / 现有 /api/agent 路由都通过这里进入已有 AgentPool + Supervisor，
避免复制 Agent 装配逻辑。event_sink 用于 AG-UI 流式事件。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from core.agent_framework.checkpoint import CheckpointManager
from core.agent_framework.circuit_breaker import CircuitBreaker
from core.agent_framework.message_bus import MessageBus
from core.agent_framework.pool import AgentPool
from core.agent_framework.supervisor import SupervisorAgent
from core.agent_framework.tool import ToolRegistry
from core.agent_framework.types import AgentContext, AgentResult
from services.agents.agent_bootstrap import create_agent_registry, register_all_tools
from services.llm_factory import get_llm_gateway

AGENT_NAMES = [
    "tender_interpret_agent",
    "outline_agent",
    "content_agent",
    "compliance_check_agent",
    "format_agent",
    "export_agent",
]

AGENT_DISPLAY = {
    "supervisor": ("supervisor", "主管 Agent：流程编排"),
    "tender_interpret_agent": ("tender_interpret_agent", "招标解读 Agent"),
    "outline_agent": ("outline_agent", "大纲生成 Agent"),
    "content_agent": ("content_agent", "内容生成 Agent"),
    "compliance_check_agent": ("compliance_check_agent", "合规检查 Agent"),
    "format_agent": ("format_agent", "格式排版 Agent"),
    "export_agent": ("export_agent", "导出 Agent"),
}


@dataclass
class RuntimeBundle:
    ctx: AgentContext
    pool: AgentPool
    bus: MessageBus
    checkpoint: CheckpointManager
    supervisor: SupervisorAgent
    agent_names: list[str] = field(default_factory=lambda: list(AGENT_NAMES))


def _setup_tool_registry(ctx: AgentContext) -> None:
    registry = ToolRegistry()
    register_all_tools(registry)
    ctx.tool_registry = registry


async def create_runtime(
    project_id: str,
    db: Any,
    event_sink: Any = None,
    llm: Any = None,
) -> RuntimeBundle:
    llm = llm or get_llm_gateway()
    ctx = AgentContext(
        agent_id=str(uuid.uuid4()),
        agent_name="supervisor",
        project_id=project_id,
        db=db,
        llm=llm,
        parameters={},
        event_sink=event_sink,
    )
    ctx.circuit_breaker = CircuitBreaker()
    agent_registry = create_agent_registry()
    _setup_tool_registry(ctx)
    pool = AgentPool(agent_registry)
    bus = MessageBus()
    checkpoint = CheckpointManager()
    supervisor = SupervisorAgent(ctx)
    ctx.agent_pool = pool
    ctx.message_bus = bus
    ctx.checkpoint = checkpoint
    for name in AGENT_NAMES:
        agent = pool.get_or_create(name, project_id, ctx)
        bus.register_agent(name, agent)
    return RuntimeBundle(ctx=ctx, pool=pool, bus=bus, checkpoint=checkpoint, supervisor=supervisor)


async def run_single_agent(
    project_id: str,
    agent_name: str,
    task: str,
    db: Any,
    event_sink: Any = None,
    params: dict | None = None,
) -> AgentResult:
    bundle = await create_runtime(project_id, db, event_sink=event_sink)
    agent = bundle.pool.get_or_create(agent_name, project_id, bundle.ctx)
    return await agent.run(task, project_id=project_id, **(params or {}))


async def run_pipeline(
    project_id: str,
    pipeline_type: str,
    db: Any,
    event_sink: Any = None,
) -> AgentResult:
    bundle = await create_runtime(project_id, db, event_sink=event_sink)
    return await bundle.supervisor.run(
        task=f"执行{pipeline_type}流程，项目ID: {project_id}",
        project_id=project_id,
        pipeline_type=pipeline_type,
    )


def list_agents() -> list[dict[str, str]]:
    return [{"name": name, "description": desc} for name, desc in AGENT_DISPLAY.values()]
