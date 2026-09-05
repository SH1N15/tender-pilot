"""BGE-Reranker 重排客户端（P-C C1）。

- 调用硅基流动风格 POST {api_base}/rerank（参数 model/query/documents/top_n/return_documents）；
- 批量分批、超时、限流退避重试，风格对齐 embedder.py 的生产级处理；
- 降级契约：所有失败以 RerankerError 抛出，由调用方（HybridRetriever）捕获后
  跳过重排、保留融合序并记 warning——主检索链路永不受影响；
- Key 经 core.secret_resolver / settings 解析，明文不落日志。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_BATCH_SIZE = 16          # 单次请求文档数上限（安全余量）
_RATE_LIMIT_RETRY_DELAY = 5.0


class RerankerError(RuntimeError):
    """重排调用失败（网络/鉴权/响应异常）。调用方应降级为不重排。"""


class Reranker:
    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.api_base = (config.get("api_base", "https://api.siliconflow.cn/v1")).rstrip("/")
        self.model = config.get("model", "BAAI/bge-reranker-v2-m3")
        self.timeout = float(config.get("timeout", 15))
        self.max_retries = int(config.get("max_retries", 1))

    @classmethod
    def from_settings(cls) -> "Reranker | None":
        """按 settings 构造；未启用或缺 Key 返回 None（调用方跳过重排）。"""
        from core.secret_resolver import resolve_secret
        from core.settings import get_settings

        settings = get_settings()
        if not settings.reranker_enabled:
            return None
        key, _src = resolve_secret("reranker_api_key")
        key = key or settings.reranker_api_key
        if not key:
            logger.warning("reranker_enabled=true 但未配置 Key，跳过重排（降级）")
            return None
        return cls(
            {
                "api_key": key,
                "api_base": settings.reranker_api_base,
                "model": settings.reranker_model,
                "timeout": settings.reranker_timeout,
            }
        )

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[tuple[int, float]]:
        """返回 [(原文档下标, 相关性分数)]，按分数降序。失败抛 RerankerError。

        注意：top_n 不下推到分批请求（硅基流动对 top_n=0/跨批分数截断敏感），
        每批全量打分后在结果层统一截断。
        """
        if not documents:
            return []
        pairs: list[tuple[int, float]] = []
        for start in range(0, len(documents), _BATCH_SIZE):
            batch = documents[start : start + _BATCH_SIZE]
            pairs.extend(await self._rerank_batch(query, batch, offset=start))
        pairs.sort(key=lambda x: x[1], reverse=True)
        return pairs if top_n is None else pairs[:top_n]

    async def _rerank_batch(self, query: str, docs: list[str], offset: int) -> list[tuple[int, float]]:
        import httpx

        payload: dict = {
            "model": self.model,
            "query": query,
            "documents": docs,
            "return_documents": False,
        }
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        f"{self.api_base}/rerank",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                results = data.get("results")
                if results is None:
                    raise RerankerError(f"rerank 响应缺少 results 字段: {str(data)[:120]}")
                return [(offset + int(r["index"]), float(r.get("relevance_score", 0.0))) for r in results]
            except RerankerError:
                raise
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if not self._is_rate_limit_error(e) or attempt >= self.max_retries:
                    break
                logger.warning("rerank 遇限流(429)，%.0fs 后重试（本批 %d 条）", _RATE_LIMIT_RETRY_DELAY, len(docs))
                await asyncio.sleep(_RATE_LIMIT_RETRY_DELAY)
        raise RerankerError(f"rerank 调用失败: {last_exc}")

    @staticmethod
    def _is_rate_limit_error(e: Exception) -> bool:
        status = getattr(e, "response", None)
        code = getattr(status, "status_code", None) if status is not None else None
        if code is not None:
            return int(code) == 429
        text = str(e).lower()
        return "429" in text or "rate limit" in text or "too many requests" in text
