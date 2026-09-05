from __future__ import annotations

import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from core.llm_gateway.gateway import LLMGateway


@dataclass
class SkillContext:
    project_id: UUID | str
    db: AsyncSession | None
    llm: LLMGateway
    parameters: dict = field(default_factory=dict)
    knowledge_base: Any = None
    progress_callback: Callable | None = None


@dataclass
class SkillResult:
    success: bool
    data: dict = field(default_factory=dict)
    tokens_consumed: int = 0
    sources: list[dict] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


class Skill(ABC):
    name: str = ""
    description: str = ""
    category: str = ""
    version: str = "1.0.0"
    triggers: list[str] = []

    @abstractmethod
    async def execute(self, ctx: SkillContext) -> SkillResult: ...

    async def safe_execute(self, ctx: SkillContext) -> SkillResult:
        from core.tracing import get_tracer

        tracer = get_tracer()
        span = tracer.start_span(
            "skill.execute",
            "skill",
            {
                "skill.name": self.name,
                "project.id": str(ctx.project_id)[:40],
            },
        )
        try:
            if ctx.progress_callback:
                await ctx.progress_callback(self.name, "started", {})
            result = await self.execute(ctx)
            if ctx.progress_callback:
                await ctx.progress_callback(self.name, "completed", result.data)
            tracer.end_span(span, status="ok")
            return result
        except Exception as e:
            tracer.end_span(span, status="error", error_type=e.__class__.__name__)
            if ctx.progress_callback:
                await ctx.progress_callback(self.name, "failed", {"error": str(e)})
            return SkillResult(success=False, error=f"{str(e)}\n{traceback.format_exc()}")
