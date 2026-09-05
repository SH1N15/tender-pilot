from __future__ import annotations

import asyncio
import logging
from typing import List

logger = logging.getLogger(__name__)

# BUG-7: text-embedding-v3 单条输入上限约 2048 token（中文 1 token≈1.5-1.8 字符），
# 超长分块直接调 API 会报 <400> InvalidParameter。入库前按字符截断到安全值。
DEFAULT_EMBED_MAX_CHARS = 4000
# BUG-7(实测补充): dashscope text-embedding-v3 单次批量请求最多 20 条文本，
# 超过报 <400> InvalidParameter: batch size is invalid。按每批 ≤10 条（安全余量）分组调用。
DEFAULT_EMBED_BATCH_SIZE = 10
# 批间遇到 429/限流时的退避重试等待（秒）
_RATE_LIMIT_RETRY_DELAY = 5.0


class Embedder:
    def __init__(self, config: dict):
        self.mode = config.get("mode", "api")
        self.model_name = config.get("model_name", "text-embedding-v3")
        self.api_key = config.get("api_key", "")
        self.api_base = config.get("api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.max_chars = int(config.get("max_chars", DEFAULT_EMBED_MAX_CHARS))
        self.batch_size = max(1, int(config.get("batch_size", DEFAULT_EMBED_BATCH_SIZE)))
        self._local_model = None

    async def embed(self, texts: List[str]) -> List[List[float]]:
        if self.mode == "local":
            return await self._local_embed(texts)
        return await self._api_embed(texts)

    async def _local_embed(self, texts: List[str]) -> List[List[float]]:
        if self._local_model is None:
            from sentence_transformers import SentenceTransformer

            self._local_model = SentenceTransformer("BAAI/bge-m3")
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(
            None, lambda: self._local_model.encode(texts, normalize_embeddings=True)
        )
        return embeddings.tolist()

    async def _api_embed(self, texts: List[str]) -> List[List[float]]:
        from openai import AsyncOpenAI

        # BUG-7: 超长块截断保护（记录 debug 日志，不中断整批）
        safe_texts: List[str] = []
        for i, t in enumerate(texts):
            if len(t) > self.max_chars:
                logger.debug(
                    "embedding 输入超长已截断: index=%d, 原长度=%d, 截断到=%d",
                    i,
                    len(t),
                    self.max_chars,
                )
                safe_texts.append(t[: self.max_chars])
            else:
                safe_texts.append(t)

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.api_base)

        # BUG-7(实测): 分批调用（每批 ≤10 条），逐批按原顺序拼接结果
        embeddings: List[List[float]] = []
        for start in range(0, len(safe_texts), self.batch_size):
            batch = safe_texts[start : start + self.batch_size]
            embeddings.extend(await self._embed_batch_with_retry(client, batch))
        return embeddings

    async def _embed_batch_with_retry(self, client, batch: List[str]) -> List[List[float]]:
        """单批调用；批间遇 429/限流做一次 5s 退避重试。"""
        try:
            return await self._call_embeddings(client, batch)
        except Exception as e:  # noqa: BLE001
            if not self._is_rate_limit_error(e):
                raise
            logger.warning(
                "embedding 批量调用遇限流(429)，%.0fs 后重试一次（本批 %d 条）",
                _RATE_LIMIT_RETRY_DELAY,
                len(batch),
            )
            await asyncio.sleep(_RATE_LIMIT_RETRY_DELAY)
            return await self._call_embeddings(client, batch)

    async def _call_embeddings(self, client, batch: List[str]) -> List[List[float]]:
        response = await client.embeddings.create(model=self.model_name, input=batch)
        return [item.embedding for item in response.data]

    @staticmethod
    def _is_rate_limit_error(e: Exception) -> bool:
        status = getattr(e, "status_code", None)
        if status is not None:
            return int(status) == 429
        text = str(e).lower()
        return "429" in text or "rate limit" in text or "too many requests" in text
