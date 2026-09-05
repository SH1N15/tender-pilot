"""AG-UI 官方协议服务（ag-ui-protocol 0.1.21，AG-UI spec 0.1.0）。

把现有 Agent Runtime（Supervisor/单 Agent）与资格预审 Workflow（HITL）
包装成标准 AG-UI 事件流（SSE）。事件名/结构与官方 SDK 一致：
- run 生命周期：RUN_STARTED / RUN_FINISHED / RUN_ERROR
- 文本消息：TEXT_MESSAGE_START / TEXT_MESSAGE_CONTENT(delta) / TEXT_MESSAGE_END
- 工具调用生命周期：TOOL_CALL_START / TOOL_CALL_RESULT
- 步骤：STEP_STARTED / STEP_FINISHED
- HITL：RUN_FINISHED(outcome=interrupt) + Interrupt；resume 恢复执行
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncGenerator

from ag_ui.core import (
    Interrupt,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
    UserMessage,
)

from core.tracing import get_tracer
from services.agent_runtime_helpers import run_pipeline, run_single_agent

logger = logging.getLogger(__name__)

AGUI_SDK_VERSION = "ag-ui-protocol 0.1.21"
AGUI_SPEC_REF = "AG-UI Protocol 0.1.0 (github.com/ag-ui-protocol/ag-ui)"

FINAL_MESSAGE_ID = "msg_final"


def _to_text(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(data)


class AGUIEventSink:
    """把 agent framework 的 {event:...} 回调转成 AG-UI 事件放入队列。"""

    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self._final_started = False

    async def __call__(self, event: dict) -> None:
        etype = event.get("event")
        try:
            if etype == "step_start":
                step = str(event.get("step", "step"))
                await self.queue.put(StepStartedEvent(step_name=step))
                await self.queue.put(
                    TextMessageStartEvent(
                        message_id=f"msg_{step}",
                        role="assistant",
                        name=str(event.get("agent") or ""),
                    )
                )
            elif etype == "step_finish":
                step = str(event.get("step", "step"))
                text = _to_text(event.get("data"))[:20000]
                await self.queue.put(TextMessageContentEvent(message_id=f"msg_{step}", delta=text))
                await self.queue.put(TextMessageEndEvent(message_id=f"msg_{step}"))
                await self.queue.put(StepFinishedEvent(step_name=step))
            elif etype == "tool_call_start":
                await self.queue.put(
                    ToolCallStartEvent(
                        tool_call_id=str(event.get("tool_call_id", "")),
                        tool_call_name=str(event.get("tool_call_name", "")),
                    )
                )
            elif etype == "tool_call_result":
                await self.queue.put(
                    ToolCallResultEvent(
                        message_id=f"tool_{event.get('tool_call_id', '')}",
                        tool_call_id=str(event.get("tool_call_id", "")),
                        content=str(event.get("content", ""))[:20000],
                        role="tool",
                    )
                )
            elif etype == "text_delta":
                delta = str(event.get("delta", ""))
                if not delta:
                    return  # 空增量不入队（SDK/前端都不需要空 TEXT_MESSAGE_CONTENT）
                if not self._final_started:
                    self._final_started = True
                    await self.queue.put(TextMessageStartEvent(message_id=FINAL_MESSAGE_ID, role="assistant"))
                await self.queue.put(
                    TextMessageContentEvent(
                        message_id=FINAL_MESSAGE_ID,
                        delta=delta,
                    )
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("AG-UI sink 事件转换失败（忽略）: %s", e)


def _final_text_from_result(result: Any) -> str:
    """从 AgentResult 提取兜底文本；无可读内容返回空串。"""
    if result is None:
        return ""
    data = getattr(result, "data", None)
    if isinstance(data, str) and data.strip():
        return data[:20000]
    if isinstance(data, dict) and data:
        # 常见结果键优先；否则退化为 JSON 文本
        for key in ("final_answer", "answer", "content", "text", "markdown"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value[:20000]
        return _to_text(data)[:20000]
    data_any = data if isinstance(data, (list, tuple)) and data else None
    if data_any:
        return _to_text(data_any)[:20000]
    error = getattr(result, "error", None)
    if error:
        return f"执行未产出文本：{error}"
    return ""


async def _run_inner(
    project_id: str,
    agent_name: str | None,
    task: str,
    pipeline_type: str,
    db: Any,
    sink: AGUIEventSink,
) -> Any:
    from services.database import db_session

    if db is None:
        async with db_session() as session:
            return await _run_inner(project_id, agent_name, task, pipeline_type, session, sink)
    if agent_name and agent_name != "supervisor":
        result = await run_single_agent(project_id, agent_name, task, db, event_sink=sink)
    else:
        result = await run_pipeline(project_id, pipeline_type, db, event_sink=sink)
    return result


async def run_agent_events(
    thread_id: str,
    run_id: str,
    project_id: str,
    task: str,
    db: Any,
    agent_name: str | None = None,
    pipeline_type: str = "bid_generation",
) -> AsyncGenerator[Any, None]:
    """Agent 运行事件流（逐步 + 工具 + 最终回答增量）。"""
    queue: asyncio.Queue = asyncio.Queue()
    sink = AGUIEventSink(queue)
    tracer = get_tracer()
    span = tracer.start_span(
        "agui.run_agent",
        "agui",
        {
            "agui.run_id": run_id,
            "agui.thread_id": thread_id,
            "agent.name": agent_name or "supervisor",
            "project.id": project_id[:40],
        },
    )

    yield RunStartedEvent(
        thread_id=thread_id,
        run_id=run_id,
        input=RunAgentInput(
            thread_id=thread_id,
            run_id=run_id,
            messages=[UserMessage(id=f"user_{uuid.uuid4().hex[:8]}", role="user", content=task)],
            tools=[],
            context=[],
            forwarded_props={},
        ),
    )

    run_task = asyncio.create_task(_run_inner(project_id, agent_name, task, pipeline_type, db, sink))
    final_started = False
    saw_content = False
    try:
        while True:
            done = run_task.done()
            try:
                if done:
                    if queue.empty():
                        break
                    item = queue.get_nowait()
                else:
                    item = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if isinstance(item, TextMessageContentEvent):
                final_started = True if item.message_id == FINAL_MESSAGE_ID else final_started
                saw_content = True
            elif isinstance(item, TextMessageStartEvent) and item.message_id == FINAL_MESSAGE_ID:
                final_started = True
            yield item

        run_result = run_task.result()
        if not saw_content:
            # P1-2 兜底：整个 run 没有任何文本增量（如 Agent 最终回答为空），
            # 用最终结果合成文本，绝不静默空跑。
            fallback_text = _final_text_from_result(run_result)
            if fallback_text:
                final_started = True
                yield TextMessageStartEvent(message_id=FINAL_MESSAGE_ID, role="assistant")
                yield TextMessageContentEvent(message_id=FINAL_MESSAGE_ID, delta=fallback_text)
        if final_started:
            yield TextMessageEndEvent(message_id=FINAL_MESSAGE_ID)
        yield RunFinishedEvent(
            thread_id=thread_id,
            run_id=run_id,
            outcome=RunFinishedSuccessOutcome(type="success"),
        )
        tracer.end_span(span)
    except Exception as e:  # noqa: BLE001
        logger.warning("AG-UI agent 运行失败: %s", e)
        if final_started:
            yield TextMessageEndEvent(message_id=FINAL_MESSAGE_ID)
        yield RunErrorEvent(message=f"任务执行失败: {e}", code="AGENT_RUN_ERROR")
        tracer.end_span(span, status="error", error_type=e.__class__.__name__)


async def run_qualification_events(
    thread_id: str,
    run_id: str,
    requirements: list[dict],
    credentials: list[dict],
    project_id: str | None = None,
) -> AsyncGenerator[Any, None]:
    """资格预审 Workflow 事件流：无 LLM；waiting_human 时发出 Interrupt。"""
    from services.qualification.workflow import run_qualification_workflow

    tracer = get_tracer()
    span = tracer.start_span(
        "agui.run_qualification",
        "agui",
        {
            "agui.run_id": run_id,
            "agui.thread_id": thread_id,
            "workflow.id": project_id or "",
            "project.id": (project_id or "")[:40],
        },
    )

    yield RunStartedEvent(
        thread_id=thread_id,
        run_id=run_id,
        input=RunAgentInput(
            thread_id=thread_id,
            run_id=run_id,
            messages=[
                UserMessage(
                    id=f"user_{uuid.uuid4().hex[:8]}",
                    role="user",
                    content="执行资格预审匹配并生成待审清单",
                )
            ],
            tools=[],
            context=[],
            forwarded_props={},
        ),
    )
    try:
        workflow = run_qualification_workflow(
            requirements=requirements,
            credentials=credentials,
            project_id=project_id,
            entrypoint="agui",
        )
        if workflow.status == "waiting_human":
            summary = _workflow_summary(workflow)
            yield StepStartedEvent(step_name="qualification_review")
            yield TextMessageStartEvent(message_id="msg_qualification", role="assistant", name="qualification_matcher")
            yield TextMessageContentEvent(message_id="msg_qualification", delta=summary)
            yield TextMessageEndEvent(message_id="msg_qualification")
            yield StepFinishedEvent(step_name="qualification_review")
            yield RunFinishedEvent(
                thread_id=thread_id,
                run_id=run_id,
                outcome=RunFinishedInterruptOutcome(
                    type="interrupt",
                    interrupts=[
                        Interrupt(
                            id=workflow.workflow_id,
                            reason="human_review_required",
                            message="资格预审存在 insufficient/警告项，需要人工逐条确认/驳回/标记不足",
                            response_schema={
                                "type": "object",
                                "properties": {
                                    "decisions": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "requirement_id": {"type": "string"},
                                                "decision": {
                                                    "type": "string",
                                                    "enum": ["confirm", "reject", "mark_insufficient"],
                                                },
                                            },
                                            "required": ["requirement_id", "decision"],
                                        },
                                    },
                                },
                                "required": ["decisions"],
                            },
                        )
                    ],
                ),
            )
            tracer.end_span(span)
        else:
            summary = _workflow_summary(workflow)
            yield TextMessageStartEvent(message_id="msg_qualification", role="assistant", name="qualification_matcher")
            yield TextMessageContentEvent(message_id="msg_qualification", delta=summary)
            yield TextMessageEndEvent(message_id="msg_qualification")
            yield RunFinishedEvent(
                thread_id=thread_id,
                run_id=run_id,
                outcome=RunFinishedSuccessOutcome(type="success"),
            )
            tracer.end_span(span)
    except Exception as e:  # noqa: BLE001
        logger.warning("AG-UI qualification 运行失败: %s", e)
        yield RunErrorEvent(message=f"资格预审执行失败: {e}", code="QUALIFICATION_RUN_ERROR")
        tracer.end_span(span, status="error", error_type=e.__class__.__name__)


async def resume_qualification_events(
    thread_id: str,
    run_id: str,
    workflow_id: str,
    decisions: list[dict],
) -> AsyncGenerator[Any, None]:
    """HITL resume：人工决策 → approve → 输出最终报告。"""
    from services.qualification.workflow import approve_qualification_workflow

    tracer = get_tracer()
    span = tracer.start_span(
        "agui.resume_qualification",
        "agui",
        {
            "agui.run_id": run_id,
            "agui.thread_id": thread_id,
            "workflow.id": workflow_id,
        },
    )
    yield RunStartedEvent(
        thread_id=thread_id,
        run_id=run_id,
        input=RunAgentInput(
            thread_id=thread_id,
            run_id=run_id,
            messages=[
                UserMessage(
                    id=f"user_{uuid.uuid4().hex[:8]}",
                    role="user",
                    content="提交人工审批决策并恢复执行",
                )
            ],
            tools=[],
            context=[],
            forwarded_props={},
        ),
    )
    try:
        workflow = approve_qualification_workflow(workflow_id, decisions)
        summary = _workflow_summary(workflow, resumed=True)
        yield TextMessageStartEvent(message_id="msg_qualification", role="assistant", name="qualification_matcher")
        yield TextMessageContentEvent(message_id="msg_qualification", delta=summary)
        yield TextMessageEndEvent(message_id="msg_qualification")
        yield RunFinishedEvent(
            thread_id=thread_id,
            run_id=run_id,
            outcome=RunFinishedSuccessOutcome(type="success"),
        )
        tracer.end_span(span)
    except Exception as e:  # noqa: BLE001
        logger.warning("AG-UI resume 失败: %s", e)
        yield RunErrorEvent(message=f"审批恢复失败: {e}", code="QUALIFICATION_RESUME_ERROR")
        tracer.end_span(span, status="error", error_type=e.__class__.__name__)


def _workflow_summary(workflow: Any, resumed: bool = False) -> str:
    report = workflow.report
    lines = [
        f"资格预审结果（{'人工审批后' if resumed else '规则匹配'}）",
        f"状态: {workflow.status}",
        f"总体判定: {report.overall_status}",
        (
            f"通过 {report.summary.met} / 不通过 {report.summary.unmet} / "
            f"信息不足 {report.summary.insufficient}（共 {report.summary.total}）"
        ),
    ]
    if getattr(workflow, "warnings", None):
        lines.append("警告: " + "；".join(str(w) for w in workflow.warnings[:5]))
    if workflow.review_items:
        lines.append("待审/已审项:")
        for item in workflow.review_items[:20]:
            lines.append(f"- {item.requirement_id}: {item.status} -> {item.decision or '待审'}")
    return "\n".join(lines)
