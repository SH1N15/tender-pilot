"""LLM Gateway — 直接使用 OpenAI SDK 调用，绕过 litellm 的参数转发问题。

参考 ai_bidding (requests.post) 和 OpenBidKit (fetch) 的直接调用方式，
改用 openai.AsyncOpenAI 客户端直接调用 OpenAI 兼容 API，
确保 response_format、temperature、max_tokens 等参数被正确传递。
"""

import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Callable

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI

from core.agent_framework.types import ToolCallItem, ToolCallResponse
from core.cost_guard import CircuitOpenError
from core.exceptions import LLMGatewayError
from core.llm_gateway.json_repair import JsonRepairEngine

logger = logging.getLogger(__name__)


class LLMGateway:
    def __init__(self, config: dict):
        self.providers = config.get("providers", [])
        self.default_model = config.get("default_model", "deepseek/deepseek-chat")
        self.fallback_models = config.get("fallback_models", [])
        self.max_retries = config.get("max_retries", 3)
        # BUG-6: 全局并发闸门 —— 所有 chat 调用过闸，防止并发打挂上游代理后触发熔断雪崩
        self._max_concurrency = max(1, int(config.get("max_concurrency", 2)))
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._in_flight = 0  # 当前占闸请求数（日志/排障用）
        # BUG-6: 可重试错误（429/5xx/超时/连接）的退避序列；重试期间不占闸
        self._retry_backoff = [float(x) for x in config.get("retry_backoff", [5.0, 15.0])]
        self._last_error_summary = ""  # 最近一次失败原因摘要（熔断排障用）
        self.json_repair = JsonRepairEngine()
        self._token_usage: list[dict] = []
        # G-0 P8-4：端点是否支持 response_format=json_object（None=未知）。
        # 不支持时每次 collect_json 都会先吃一个 400 再降级重试（BadRequestError
        # span 计数翻倍），故探测到一次后记住，后续直接不带该参数。
        self._response_format_supported: bool | None = True
        # 显式请求超时（P0-5）：默认 60s，可经 config["timeout"] 覆盖
        self._timeout = float(config.get("timeout", 60.0))
        # 思考模式开关（2026-09-02）：qwen3.7-flash 属混合思考模型且默认开启思考，
        # 官方实测思考使总耗时约为非思考 3 倍（token 占输出 60%+）。
        # 传 extra_body={"enable_thinking": ...} 显式控制；None=不传（交由 API 默认）。
        self._enable_thinking: bool | None = config.get("enable_thinking", None)

        # 从 default_model 中提取实际模型名（去掉 provider 前缀）
        self._resolved_model = self._strip_provider_prefix(self.default_model)

        # 构建 OpenAI 客户端
        self._client = self._build_client()

    @staticmethod
    def _strip_provider_prefix(model: str) -> str:
        """去掉 litellm 风格的 provider 前缀，如 'deepseek/deepseek-chat' -> 'deepseek-chat'"""
        known_prefixes = {
            "deepseek",
            "openai",
            "ollama",
            "zhipu",
            "dashscope",
            "azure",
            "anthropic",
            "cohere",
            "huggingface",
            "vertex_ai",
            "gemini",
            "mistral",
            "groq",
            "together_ai",
            "replicate",
        }
        if "/" in model:
            prefix, rest = model.split("/", 1)
            if prefix in known_prefixes:
                return rest
        return model

    def _build_client(self) -> AsyncOpenAI:
        """根据配置构建 OpenAI AsyncClient"""
        api_key = ""
        api_base = "https://api.openai.com/v1"
        if self.providers:
            provider = self.providers[0]
            api_key = provider.get("api_key") or ""
            api_base = provider.get("api_base") or api_base

        # 确保 api_base 以 /v1 结尾（兼容不同格式）
        if api_base and not api_base.rstrip("/").endswith("/v1"):
            api_base = api_base.rstrip("/") + "/v1"

        # 显式 timeout（P0-5）
        return AsyncOpenAI(
            api_key=api_key or "sk-placeholder",
            base_url=api_base,
            timeout=self._timeout,
            max_retries=0,  # 我们自己控制重试
        )

    async def _client_create(self, kwargs: dict):
        """带 tracing 的 OpenAI create 调用（记录耗时/状态/token，不记录敏感内容）。"""
        from core.tracing import get_tracer

        tracer = get_tracer()
        model = str(kwargs.get("model", ""))
        provider = model.split("/")[0] if "/" in model else ""
        span = tracer.start_span("llm.chat", "llm", {"llm.model": model, "llm.provider": provider})
        try:
            response = await self._client.chat.completions.create(**kwargs)
            usage = getattr(response, "usage", None)
            token_usage = {}
            if usage is not None:
                token_usage = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(usage, "completion_tokens", 0),
                    "total_tokens": getattr(usage, "total_tokens", 0),
                }
            tracer.end_span(span, status="ok", token_usage=token_usage)
            return response
        except Exception as e:  # noqa: BLE001
            # P8-4（G-0）：错误文本截断入 span（error.message 在 tracing 白名单内，
            # 只含上游错误消息，不含 api_key/敏感头），排障不再只看 error_type。
            tracer.end_span(
                span,
                status="error",
                error_type=e.__class__.__name__,
                attributes={"error.message": str(e)[:300]},
            )
            raise

    @staticmethod
    def _is_retryable_error(e: Exception) -> bool:
        """BUG-6: 429/5xx/超时/连接类错误可重试；其余 4xx 不重试直接抛。"""
        if isinstance(e, (APITimeoutError, APIConnectionError)):
            return True
        if isinstance(e, APIError):
            status = getattr(e, "status_code", None)
            if status is None:
                return True  # 无状态码的 APIError 按可重试处理（保守）
            return status == 429 or int(status) >= 500
        return True  # 未知异常按可重试处理（与既有重试语义一致）

    async def _gated_client_create(self, kwargs: dict):
        """过全局并发闸门后再发真实请求；退避等待在闸外进行，不占坑。"""
        async with self._semaphore:
            self._in_flight += 1
            try:
                return await self._client_create(kwargs)
            finally:
                self._in_flight -= 1

    async def _precheck_with_logging(self, guard, key: str | None = None):
        """成本守卫预检；熔断打开时输出并发/失败原因摘要便于排障（BUG-6 第4点）。"""
        try:
            await guard.precheck("llm", key=key)
        except CircuitOpenError:
            logger.error(
                f"[LLM] 熔断打开，请求被拦截：闸门={self._max_concurrency}, "
                f"在途请求={self._in_flight}, 最近失败摘要={self._last_error_summary[:200]}"
            )
            raise

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.7,
        stream: bool = False,
        response_format: dict | None = None,
        max_tokens: int | None = None,
    ) -> str | AsyncGenerator[str, None]:
        """调用 LLM 获取文本响应。

        关键改进：
        1. 使用 OpenAI SDK 直接调用，确保参数正确传递
        2. response_format 不支持时自动降级重试
        3. 详细的日志记录
        """
        model_name = model or self._resolved_model
        effective_max_tokens = max_tokens or 8192

        # P0-5 成本守卫：对外请求前必须过守卫（熔断/日配额拒绝时不发起真实请求）
        from core.cost_guard import get_cost_guard

        guard = get_cost_guard()
        # P8-3（G-0）：预检按请求模型分键——A 模型熔断不再拦截 B 模型
        await self._precheck_with_logging(guard, key=model_name)

        last_error = None
        attempted_model = model_name  # P8-3：熔断计费按实际尝试的模型分键
        for attempt in range(self.max_retries):
            # 每次尝试可能使用不同的模型（降级）
            current_model = self._get_model_for_attempt(attempt, model_name)
            attempted_model = current_model

            try:
                t0 = time.monotonic()
                logger.info(
                    f"[LLM] 请求开始 model={current_model}, "
                    f"attempt={attempt + 1}/{self.max_retries}, "
                    f"messages={len(messages)}条, temperature={temperature}, "
                    f"max_tokens={effective_max_tokens}, "
                    f"response_format={response_format}, stream={stream}, "
                    f"在途={self._in_flight}/{self._max_concurrency}"
                )

                # 构建 kwargs
                kwargs: dict = {
                    "model": current_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": effective_max_tokens,
                }
                if self._enable_thinking is not None:
                    # 思考模式显式控制（见 __init__ 注释）：官方关闭思考可省 60%~75% 耗时
                    kwargs["extra_body"] = {"enable_thinking": self._enable_thinking}
                if response_format and self._response_format_supported is not False:
                    kwargs["response_format"] = response_format

                if stream:
                    response = await self._gated_client_create({**kwargs, "stream": True})
                    logger.info(f"[LLM] 流式响应开始 model={current_model}")
                    await guard.record_result("llm", ok=True, key=current_model)
                    return self._stream_response(response)

                response = await self._gated_client_create(kwargs)
                content = response.choices[0].message.content or ""

                # 记录 token 使用量
                if response.usage:
                    self._record_usage(current_model, response.usage)
                    usage_info = (
                        f"prompt_tokens={response.usage.prompt_tokens}, "
                        f"completion_tokens={response.usage.completion_tokens}"
                    )
                else:
                    usage_info = "usage=N/A"

                elapsed = time.monotonic() - t0
                logger.info(
                    f"[LLM] 请求完成 model={current_model}, "
                    f"耗时={elapsed:.2f}s, "
                    f"输出长度={len(content)}字符, {usage_info}"
                )
                total_tokens = getattr(response.usage, "total_tokens", 0) if response.usage else 0
                await guard.record_result("llm", ok=True, tokens=int(total_tokens or 0), key=current_model)
                return content

            except (APIError, APITimeoutError, APIConnectionError) as e:
                elapsed = time.monotonic() - t0
                error_msg = str(e)

                # 检查是否是 response_format 不支持的错误
                if response_format and self._is_response_format_error(e):
                    self._response_format_supported = False  # G-0：记住端点不支持，后续调用不再带
                    logger.warning(f"[LLM] response_format 不支持，去掉后重试 model={current_model}")
                    try:
                        kwargs_no_fmt = {k: v for k, v in kwargs.items() if k != "response_format"}
                        response = await self._gated_client_create(kwargs_no_fmt)
                        content = response.choices[0].message.content or ""
                        if response.usage:
                            self._record_usage(current_model, response.usage)
                        elapsed = time.monotonic() - t0
                        logger.info(
                            f"[LLM] 降级请求完成(无response_format) model={current_model}, "
                            f"耗时={elapsed:.2f}s, 输出长度={len(content)}字符"
                        )
                        return content
                    except Exception as fallback_err:
                        logger.error(f"[LLM] 降级请求也失败: {fallback_err}")
                        last_error = fallback_err
                        continue

                self._last_error_summary = f"{e.__class__.__name__}: {error_msg[:200]}"
                last_error = e
                retryable = self._is_retryable_error(e)
                logger.error(
                    f"[LLM] 请求失败 model={current_model}, "
                    f"attempt={attempt + 1}/{self.max_retries}, "
                    f"耗时={elapsed:.2f}s, retryable={retryable}, 错误={error_msg[:200]}"
                )
                if not retryable:
                    # BUG-6: 4xx（除429）不重试，直接计失败并抛出
                    break
                if attempt < self.max_retries - 1:
                    delay = self._retry_backoff[min(attempt, len(self._retry_backoff) - 1)]
                    logger.warning(
                        f"[LLM] 可重试错误，{delay:.0f}s 后重试（退避期间不占并发闸）: {error_msg[:120]}"
                    )
                    await asyncio.sleep(delay)
                continue

            except Exception as e:
                elapsed = time.monotonic() - t0
                self._last_error_summary = f"{e.__class__.__name__}: {str(e)[:200]}"
                logger.error(
                    f"[LLM] 未知异常 model={current_model}, "
                    f"attempt={attempt + 1}/{self.max_retries}, "
                    f"耗时={elapsed:.2f}s, 错误={str(e)[:200]}"
                )
                if attempt < self.max_retries - 1:
                    delay = self._retry_backoff[min(attempt, len(self._retry_backoff) - 1)]
                    await asyncio.sleep(delay)
                continue

        # BUG-6: 重试耗尽才计为 cost_guard 失败（重试期间不逐次计入，避免瞬间打满熔断）
        # P8-3：失败计入实际尝试的模型分键，不污染其他模型的熔断计数
        await guard.record_result("llm", ok=False, key=attempted_model)
        raise LLMGatewayError(f"所有重试失败: {last_error}") from last_error

    async def collect_json(
        self,
        messages: list[dict],
        schema: type | None = None,
        validator: Callable | None = None,
        model: str | None = None,
        temperature: float = 0.3,
        max_repair_attempts: int = 2,
        max_tokens: int | None = None,
    ) -> dict:
        """调用 LLM 获取 JSON 响应，带修复和校验。

        改进：
        1. 增加重试机制（最多3次尝试，与 OpenBidKit 一致）
        2. 每次 LLM 调用都强制 response_format=json_object
        3. 详细的日志记录
        4. 支持 max_tokens 参数控制输出长度
        """
        t0 = time.monotonic()
        logger.info(
            f"[LLM.collect_json] 开始 model={model or self._resolved_model}, "
            f"temperature={temperature}, max_repair={max_repair_attempts}, "
            f"max_tokens={max_tokens or 'default'}"
        )

        max_attempts = 3  # 与 OpenBidKit 一致：1次正常 + 2次重试
        last_error = None

        for attempt in range(max_attempts):
            try:
                response_text = await self.chat(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    max_tokens=max_tokens,
                )

                # 处理流式响应
                if isinstance(response_text, AsyncGenerator):
                    chunks = []
                    try:
                        async for chunk in response_text:
                            chunks.append(chunk)
                    finally:
                        if hasattr(response_text, "aclose"):
                            await response_text.aclose()
                    response_text = "".join(chunks)

                logger.info(
                    f"[LLM.collect_json] chat完成, "
                    f"attempt={attempt + 1}/{max_attempts}, "
                    f"耗时={time.monotonic() - t0:.2f}s, "
                    f"原始文本长度={len(response_text)}字符, "
                    f"前200字符={response_text[:200]}"
                )

                # 尝试解析和修复 JSON
                result = await self.json_repair.repair_and_validate(
                    raw_text=response_text,
                    schema=schema,
                    validator=validator,
                    repair_chat_fn=self.chat,
                    max_attempts=max_repair_attempts,
                )

                logger.info(
                    f"[LLM.collect_json] 全部完成, "
                    f"总耗时={time.monotonic() - t0:.2f}s, "
                    f"结果类型={type(result).__name__}"
                )
                return result

            except Exception as e:
                last_error = e
                logger.warning(f"[LLM.collect_json] 第{attempt + 1}次尝试失败: {str(e)[:200]}")
                if attempt < max_attempts - 1:
                    logger.info(f"[LLM.collect_json] 准备第{attempt + 2}次重试...")
                    continue

        logger.error(f"[LLM.collect_json] 所有尝试失败: {last_error}")
        raise LLMGatewayError(f"JSON收集失败(重试{max_attempts}次): {last_error}") from last_error

    async def stream_chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        result = await self.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            stream=True,
        )
        if isinstance(result, AsyncGenerator):
            async for chunk in result:
                yield chunk

    def _get_model_for_attempt(self, attempt: int, original_model: str) -> str:
        """根据重试次数选择模型（支持降级）"""
        if attempt == 0:
            return original_model
        if not self.fallback_models:
            return original_model
        idx = min(attempt - 1, len(self.fallback_models) - 1)
        fallback = self.fallback_models[idx]
        return self._strip_provider_prefix(fallback)

    @staticmethod
    def _is_response_format_error(error: Exception) -> bool:
        """检查是否是 response_format 不支持的错误。

        G-0 P8-4 复现结论：此前关键词含 "invalid_request_error"——这是 DashScope
        所有 4xx 参数错的通用 type，导致任何 400（如 Temperature 越界）都被误判为
        "response_format 不支持" 而多发一次必失败的重试（BadRequestError 计数翻倍）。
        现只匹配真正与 response_format 相关的错误文本。
        """
        error_str = str(error).lower()
        return any(
            keyword in error_str
            for keyword in [
                "response_format",
                "json_object",
                "json mode",
                "not supported",
                "unsupported parameter",
            ]
        )

    async def _stream_response(self, response) -> AsyncGenerator[str, None]:
        async for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    def _record_usage(self, model: str, usage: Any):
        if usage:
            self._token_usage.append(
                {
                    "model": model,
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(usage, "completion_tokens", 0),
                    "total_tokens": getattr(usage, "total_tokens", 0),
                }
            )

    def get_token_summary(self) -> dict:
        total_prompt = sum(u["prompt_tokens"] for u in self._token_usage)
        total_completion = sum(u["completion_tokens"] for u in self._token_usage)
        return {
            "total_requests": len(self._token_usage),
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
        }

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        model: str | None = None,
        temperature: float = 0.3,
    ) -> ToolCallResponse:
        """支持 function calling 的聊天接口"""
        model_name = model or self._resolved_model

        kwargs: dict = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        last_error = None
        for attempt in range(self.max_retries):
            try:
                current_model = self._get_model_for_attempt(attempt, model_name)
                kwargs["model"] = current_model

                response = await self._gated_client_create(kwargs)
                msg = response.choices[0].message
                self._record_usage(current_model, response.usage)

                if msg.tool_calls:
                    calls = []
                    for tc in msg.tool_calls:
                        calls.append(
                            ToolCallItem(
                                id=tc.id,
                                function_name=tc.function.name,
                                arguments=tc.function.arguments,
                            )
                        )
                    return ToolCallResponse(
                        has_tool_calls=True,
                        tool_calls=calls,
                        content=msg.content or "",
                    )
                return ToolCallResponse(
                    has_tool_calls=False,
                    content=msg.content or "",
                )
            except Exception as e:
                last_error = e
                logger.error(f"[LLM.chat_with_tools] 失败 attempt={attempt + 1}: {e}")
                continue

        raise LLMGatewayError(f"chat_with_tools 所有重试失败: {last_error}") from last_error
