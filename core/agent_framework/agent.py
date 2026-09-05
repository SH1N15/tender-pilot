import asyncio
import json
import time
from abc import ABC, abstractmethod
from typing import Any

from core.agent_framework.memory import AgentMemory
from core.agent_framework.types import AgentContext, AgentMessage, AgentResult


class Agent(ABC):
    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    system_prompt: str = ""
    default_model: str | None = None
    default_temperature: float = 0.3

    available_tools: list[str] = []

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx
        self._memory = AgentMemory(
            window_size=ctx.parameters.get("memory_window", 10),
            db=ctx.db,
        )

    @abstractmethod
    async def run(self, task: str, **kwargs) -> AgentResult: ...

    async def think_and_act(self, task: str, max_iterations: int = 10) -> AgentResult:
        messages = [
            {"role": "system", "content": self.system_prompt},
            *self._memory.get_context(),
            {"role": "user", "content": task},
        ]
        tool_log: list[dict] = []
        tokens = 0
        sink = getattr(self.ctx, "event_sink", None)

        for i in range(max_iterations):
            response = await self.ctx.llm.chat_with_tools(
                messages=messages,
                tools=self.ctx.tool_registry.list_schemas(self.name),
                tool_choice="auto",
                model=self.default_model,
                temperature=self.default_temperature,
            )

            if response.has_tool_calls:
                for tool_call in response.tool_calls:
                    started = time.monotonic()
                    if sink is not None:
                        await sink(
                            {
                                "event": "tool_call_start",
                                "tool_call_id": tool_call.id,
                                "tool_call_name": tool_call.function.name,
                                "agent_name": self.name,
                            }
                        )
                    result = await self._execute_tool_with_retry(tool_call, max_retries=2)
                    elapsed = time.monotonic() - started
                    tool_log.append(
                        {
                            "id": tool_call.id,
                            "name": tool_call.function.name,
                            "arguments_len": len(tool_call.function.arguments),
                            "result_preview": result[:200],
                            "result_len": len(result),
                            "duration_ms": round(elapsed * 1000, 2),
                        }
                    )
                    if sink is not None:
                        await sink(
                            {
                                "event": "tool_call_result",
                                "tool_call_id": tool_call.id,
                                "tool_call_name": tool_call.function.name,
                                "content": result[:2000],
                                "agent_name": self.name,
                            }
                        )
                    messages.append({"role": "tool", "content": result, "tool_call_id": tool_call.id})

                for m in messages[-len(response.tool_calls) - 1 :]:
                    self._memory.add_message(m["role"], m["content"])
            else:
                # 最终回答：优先流式（有 event_sink 时逐 token 推送）
                content = response.content or ""
                if sink is not None and self.ctx.llm is not None:
                    deltas: list[str] = []
                    try:
                        async for chunk in self.ctx.llm.stream_chat(
                            messages=messages,
                            model=self.default_model,
                            temperature=self.default_temperature,
                        ):
                            deltas.append(chunk)
                            await sink({"event": "text_delta", "delta": chunk, "agent_name": self.name})
                    except Exception:
                        # 流式失败则回退到已有内容
                        pass
                    if deltas:
                        content = "".join(deltas)
                    elif content:
                        # 流式未产生增量时，仍推送一次完整文本，确保 AG-UI 有内容显示
                        await sink({"event": "text_delta", "delta": content, "agent_name": self.name})
                self._memory.add_exchange(task, content)
                return AgentResult(
                    success=True,
                    data=content,
                    tool_calls_log=tool_log,
                    tokens_consumed=tokens,
                )

        return AgentResult(success=False, error="达到最大迭代次数", data=messages, tool_calls_log=tool_log)

    async def _execute_tool_with_retry(self, tool_call, max_retries: int = 2) -> str:
        tool_name = tool_call.function.name

        if not self.ctx.circuit_breaker.can_execute(tool_name):
            return json.dumps({"error": f"工具 {tool_name} 已熔断，请稍后重试"})

        for attempt in range(max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    self.ctx.tool_registry.execute(tool_call, agent_name=self.name),
                    timeout=30.0,
                )
                self.ctx.circuit_breaker.record_success(tool_name)
                return json.dumps(result, ensure_ascii=False)[:4000]
            except asyncio.TimeoutError:
                self.ctx.circuit_breaker.record_failure(tool_name)
                if attempt < max_retries:
                    continue
                return json.dumps({"error": f"工具 {tool_name} 执行超时"})
            except Exception as e:
                self.ctx.circuit_breaker.record_failure(tool_name)
                if attempt < max_retries:
                    continue
                return json.dumps({"error": f"工具执行失败: {str(e)}"})

    async def send_message(self, receiver: str, content: Any, msg_type: str = "inform") -> AgentMessage:
        msg = AgentMessage(
            sender=self.name,
            receiver=receiver,
            message_type=msg_type,
            content=content,
        )
        await self.ctx.message_bus.send(msg)
        return msg

    async def ask_agent(self, agent_name: str, question: str, timeout: float = 60.0) -> Any:
        await self.send_message(agent_name, question, "question")
        response = await self.ctx.message_bus.receive(self.name, timeout=timeout)
        if response and response.message_type == "answer":
            return response.content
        return None
