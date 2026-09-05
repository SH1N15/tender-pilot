"""A2A 官方 SDK 服务装配：Agent Card + JSON-RPC/REST 路由 + FastAPI 挂载。

A2A 规范 1.0（a2a-sdk 1.1.2）。well-known Agent Card 路径：/.well-known/agent-card.json。
"""

from __future__ import annotations

import asyncio
import logging

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import InMemoryTaskStore
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    SendMessageRequest,
    Task,
    TaskState,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol
from a2a.utils.task import apply_history_length
from starlette.routing import Route

from services.a2a_server.executor import BidMasterAgentExecutor

logger = logging.getLogger(__name__)

A2A_SDK_VERSION = "a2a-sdk 1.1.2"
A2A_SPEC_REF = "A2A Protocol 1.0 (a2a-protocol.org/v1.0.0/specification)"

# 看门狗轮询间隔 / 事件通道宽限期（秒）
_TERMINAL_POLL_INTERVAL = 0.5
_EVENT_CHANNEL_GRACE = 2.0


class TaskStoreWatchdogHandler(DefaultRequestHandlerV2):
    """阻塞式 SendMessage 看门狗：事件通道与 TaskStore 终态双通道竞速。

    背景（P2-1 验收发现的真缺陷）：a2a-sdk 1.1.2 的阻塞式
    ``DefaultRequestHandlerV2.on_message_send`` 只依赖 ActiveTask 事件订阅流
    送达终态事件来结束等待。当消费端（EventConsumer）在「终态已写入
    TaskStore 之后、向订阅者分发终态事件之前」被楔死（例如曾有客户端中途断开
    的流式订阅残留、把订阅分发队列占满导致 ``_enqueue_to_subscribers`` 永久
    阻塞）时，终态虽已落库（GET /a2a/tasks 可见），但阻塞式调用方永远等不到
    事件——REST message:send 与 JSON-RPC SendMessage 同时挂死。

    workaround（最小侵入、仅用 SDK 公开接口）：并行轮询 TaskStore，终态落库
    即可返回最终 Task，不再单点依赖事件订阅流。事件通道正常时仍以事件通道
    结果优先（给 ``_EVENT_CHANNEL_GRACE`` 秒宽限），仅在其失效时走落库兜底，
    语义与 A2A「阻塞 send 返回终态 Task」一致，不发明协议。
    """

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Message | Task:
        send_task = asyncio.create_task(super().on_message_send(params, context))
        poll_task = asyncio.create_task(
            self._wait_terminal_in_store(params, context)
        )
        try:
            done, _ = await asyncio.wait(
                {send_task, poll_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if send_task in done:
                # 正常路径：事件通道先返回（含 Message-only / 异常）。
                poll_task.cancel()
                return send_task.result()

            # poll 先完成：给事件通道一个宽限期；仍未返回则认定事件通道楔死，
            # 取消事件等待并返回 TaskStore 中已落库的终态 Task。
            done2, _ = await asyncio.wait(
                {send_task}, timeout=_EVENT_CHANNEL_GRACE
            )
            if send_task in done2 and send_task.exception() is None:
                return send_task.result()
            if poll_task in done and poll_task.exception() is None:
                logger.warning(
                    "A2A 阻塞 send 事件通道未在宽限期内送达终态（task=%s），"
                    "以 TaskStore 落库终态兜底返回。",
                    params.message.task_id,
                )
                send_task.cancel()
                polled = poll_task.result()
                if isinstance(polled, Task):
                    return apply_history_length(polled, params.configuration)
                return polled
            # poll 失败或 send 抛错：还原原有异常语义
            if send_task in done:
                send_task.result()  # re-raise
            poll_task.result()  # re-raise poll 异常
            return await send_task  # 理论不可达；保底等待
        finally:
            for t in (send_task, poll_task):
                if not t.done():
                    t.cancel()

    async def _wait_terminal_in_store(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Task | None:
        """轮询 TaskStore，直到该任务落库为终态；永不抛 TaskNotFound。

        task_id 由 super().on_message_send 内部的 RequestContext 构造时回填进
        ``params.message.task_id``（SDK 原生行为），这里只读不改。
        """
        terminal = {
            TaskState.TASK_STATE_COMPLETED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_CANCELED,
            TaskState.TASK_STATE_REJECTED,
        }
        while not params.message.task_id:
            await asyncio.sleep(0.05)
        task_id = params.message.task_id
        while True:
            task = await self.task_store.get(task_id, context)
            if task is not None and task.status.state in terminal:
                return task
            await asyncio.sleep(_TERMINAL_POLL_INTERVAL)


def build_agent_card(card_url: str = "http://localhost:8000") -> AgentCard:
    """构造 Agent Card：supervisor + 6 个业务 Agent 作为 skills。"""
    card = AgentCard(
        name="投标智航 / TenderPilot Agent",
        description="智能招投标 Agent 平台：主管 Agent 编排招标解读/大纲/内容/合规/排版/导出 6 个业务 Agent。",
        version="0.2.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False, extended_agent_card=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    skills = [
        AgentSkill(
            id="supervisor",
            name="supervisor",
            description="主管 Agent：按模板或 LLM 规划编排完整投标生成流程",
            tags=["orchestration", "pipeline"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
        AgentSkill(
            id="tender_interpret_agent",
            name="tender_interpret_agent",
            description="招标解读 Agent：提取关键维度、评分矩阵、风险",
            tags=["interpret", "tender"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
        AgentSkill(
            id="outline_agent",
            name="outline_agent",
            description="大纲生成 Agent：基于解读结果生成投标大纲",
            tags=["outline"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
        AgentSkill(
            id="content_agent",
            name="content_agent",
            description="内容生成 Agent：按大纲逐章节生成投标正文",
            tags=["content"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
        AgentSkill(
            id="compliance_check_agent",
            name="compliance_check_agent",
            description="合规检查 Agent：全面检查投标文件合规性",
            tags=["compliance", "check"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
        AgentSkill(
            id="format_agent",
            name="format_agent",
            description="格式排版 Agent：排版美化投标文件",
            tags=["format"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
        AgentSkill(
            id="export_agent",
            name="export_agent",
            description="导出 Agent：导出最终投标文件",
            tags=["export"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
    ]
    card.skills.extend(skills)
    card.supported_interfaces.extend(
        [
            AgentInterface(
                url=card_url.rstrip("/") + "/",
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_1_0,
            ),
            AgentInterface(
                url=card_url.rstrip("/") + "/a2a",
                protocol_binding=TransportProtocol.HTTP_JSON.value,
                protocol_version=PROTOCOL_VERSION_1_0,
            ),
        ]
    )
    return card


def create_a2a_app(fastapi_app, card_url: str = "http://localhost:8000") -> dict:
    """在传入的 FastAPI app 上挂载 A2A 路由；返回内部组件供测试使用。"""
    agent_card = build_agent_card(card_url)
    task_store = InMemoryTaskStore()
    executor = BidMasterAgentExecutor()
    request_handler = TaskStoreWatchdogHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
    )

    # 挂载 Agent Card + JSON-RPC + REST（A2A 规范双传输）。
    # REST 挂载说明：SDK create_rest_routes 会在返回的路由表末尾追加一个
    # 根级 Mount("/{tenant}")（实测会让 /api/* 等后续注册的单段/多段路由 404，
    # 见 tests/test_a2a_rest.py::test_rest_mount_does_not_shadow_main_api）。
    # 最小侵入方案：用 path_prefix="/a2a" 把 REST 端点前缀到 /a2a/*，
    # 并过滤掉该 Mount，只保留显式 Route——零新增依赖、复用 SDK 原生路由。
    # 代价：REST 的 /{tenant}/... 多租户路径变体不可用（SendMessageRequest.tenant
    # 字段仍可从 body 传入），如需多租户再评估子应用挂载。
    rest_routes = create_rest_routes(request_handler, path_prefix="/a2a")
    rest_route_objects = [r for r in rest_routes if isinstance(r, Route)]
    add_a2a_routes_to_fastapi(
        fastapi_app,
        agent_card_routes=create_agent_card_routes(agent_card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler, rpc_url="/"),
        rest_routes=rest_route_objects,
    )
    return {
        "agent_card": agent_card,
        "task_store": task_store,
        "executor": executor,
        "request_handler": request_handler,
    }
