"""A2A AgentExecutor：把 A2A 请求路由进已有 AgentPool / Supervisor。"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from a2a.server.agent_execution import AgentExecutor
from a2a.types.a2a_pb2 import Role, TaskState, TaskStatus, TaskStatusUpdateEvent

from core.tracing import get_tracer
from services.agent_runtime_helpers import run_pipeline, run_single_agent

logger = logging.getLogger(__name__)


def _build_status(state, message_text: str | None = None) -> TaskStatus:
    """构造 A2A TaskStatus（timestamp 为 protobuf Timestamp，message 为 Message）。"""
    from google.protobuf.timestamp_pb2 import Timestamp

    ts = Timestamp()
    ts.FromDatetime(datetime.now(timezone.utc))
    status = TaskStatus(state=state, timestamp=ts)
    if message_text:
        status.message.message_id = f"agent_{uuid.uuid4().hex[:8]}"
        status.message.role = Role.ROLE_AGENT
        status.message.parts.add(text=message_text)
    return status


class BidMasterAgentExecutor(AgentExecutor):
    """A2A 执行器：metadata 支持 project_id / agent / pipeline_type。

    - agent 指定时：执行单个业务 Agent（run_single_agent）；
    - 否则：执行 Supervisor 流程（run_pipeline，默认 bid_generation）。
    """

    async def execute(self, context, event_queue) -> None:
        task_id = context.task_id
        context_id = context.context_id
        user_input = context.get_user_input()
        metadata = context.metadata or {}
        project_id = str(metadata.get("project_id") or metadata.get("projectId") or "")
        agent_name = str(metadata.get("agent") or "")
        pipeline_type = str(metadata.get("pipeline_type") or "bid_generation")

        tracer = get_tracer()
        span = tracer.start_span(
            "a2a.execute",
            "a2a",
            {"a2a.method": "send_message", "a2a.protocol_version": "1.0", "project.id": project_id[:40]},
        )

        from a2a.types.a2a_pb2 import Task as A2ATask

        # 先入队初始 Task，再入队状态更新（A2A SDK 强制顺序）
        await event_queue.enqueue_event(
            A2ATask(
                id=task_id,
                context_id=context_id,
                status=_build_status(TaskState.TASK_STATE_SUBMITTED),
            )
        )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=_build_status(TaskState.TASK_STATE_WORKING),
            )
        )

        try:
            from services.database import db_session

            async with db_session() as db:
                if agent_name and agent_name != "supervisor":
                    result = await run_single_agent(project_id, agent_name, user_input, db)
                    text = _agent_result_text(result)
                else:
                    result = await run_pipeline(project_id, pipeline_type, db)
                    text = _agent_result_text(result)
            status = _build_status(TaskState.TASK_STATE_COMPLETED, text[:20000])
            tracer.end_span(span)
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=status,
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("A2A 任务失败: %s", e)
            tracer.end_span(span, status="error", error_type=e.__class__.__name__)
            status = _build_status(TaskState.TASK_STATE_FAILED, f"任务执行失败: {e}"[:2000])
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=status,
                )
            )

    async def cancel(self, context, event_queue) -> None:
        task_id = context.task_id
        context_id = context.context_id
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=_build_status(TaskState.TASK_STATE_CANCELED),
            )
        )


def _agent_result_text(result) -> str:
    if result is None:
        return "（无结果）"
    if getattr(result, "success", False):
        data = result.data
        if isinstance(data, str):
            return data
        import json

        try:
            return json.dumps(data, ensure_ascii=False, default=str)[:20000]
        except Exception:  # noqa: BLE001
            return str(data)
    return getattr(result, "error", "未知错误") or "任务失败"
