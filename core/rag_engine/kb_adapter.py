"""知识库检索适配器（P-C C4）：给 SkillContext.knowledge_base / 全局检索用的窄接口。

- 覆盖所有业务 collection（kb_legal_* / kb_ent_* / 存量 kb_*；排除 eval_ 前缀评测集合）；
- retrieve(query, top_k) 返回 [{"text","score","metadata"}]（与 ContentGenSkill 消费格式一致）；
- 生产 Reranker 按 settings 注入（BMP_RERANKER_*，默认关）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _is_internal_placeholder_doc(row: dict) -> bool:
    """Exclude known internal fixture documents from production retrieval.

    This is deliberately marker-based rather than filename-based.  Real
    customer material can mention testing, while the old fixture set carries
    explicit non-submission markers that must never override current facts.
    """
    text = str(row.get("text") or "")
    markers = (
        "开发测试资料", "非真实投标", "非真实企业文件", "测试占位",
        "测试值", "测试文本", "TEST-DEPOSIT-", "TEST-ID-", "TEST-RK-",
    )
    return any(marker in text for marker in markers)


class KnowledgeBaseAdapter:
    def __init__(self, retriever, collections: list[str]):
        self._retriever = retriever
        # 项目证据 collection 必须按 project_id 定向检索，禁止混入全局企业/法规检索。
        self._collections = [
            c for c in collections if not c.startswith("eval_") and not c.startswith("kb_proj_")
        ]

    async def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        merged: list[dict] = []
        for collection in self._collections:
            try:
                results = await self._retriever.retrieve(query, collection_name=collection, top_k=top_k)
                # G7-5：补注 collection 名（chroma metadata 不含库域名），供下游按企业域
                # （kb_ent_*）优先取料；不覆盖入库时已有的同名字段。
                for row in results:
                    if _is_internal_placeholder_doc(row):
                        continue
                    meta = row.get("metadata")
                    if isinstance(meta, dict):
                        meta.setdefault("collection", collection)
                merged.extend(results)
            except Exception as e:  # noqa: BLE001
                logger.warning("知识检索失败(collection=%s): %s", collection, e)
        merged.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return merged[:top_k]

    async def record(
        self,
        *,
        project_id: str,
        source_type: str,
        fact: dict,
        collection: str | None = None,
    ) -> dict:
        """写入专用长期记忆 collection；不允许写入业务 collection。"""
        if collection is None:
            collection = "kb_memory_uat" if (
                project_id.startswith("UAT-") or project_id.startswith("EVAL_UAT_")
            ) else "kb_memory"
        if collection in self._collections or not collection.startswith("kb_memory"):
            raise ValueError("长期记忆只能写入 kb_memory* 专用 collection")
        payload = json.dumps(fact, ensure_ascii=False, sort_keys=True)
        text = f"project_id={project_id} source_type={source_type} fact={payload}"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        metadata = {
            "project_id": project_id,
            "source_type": source_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "memory": True,
            "chunk_id": f"memory_{digest}",
            "source": "long_term_memory",
        }
        embedder = getattr(self._retriever, "embedder", None)
        store = getattr(self._retriever, "vector_store", None)
        if embedder is None or store is None:
            raise RuntimeError("长期记忆写入需要 retriever.embedder/vector_store")
        vectors = await embedder.embed([text])
        await store.add_documents(collection, [digest], [text], vectors, [metadata])
        return {"id": digest, "collection": collection, "metadata": metadata, "text": text}

    async def clear_project(self, project_id: str) -> int:
        """按 project_id 清理专用 collection 中的记忆。"""
        deleted = 0
        for collection_name in ("kb_memory", "kb_memory_uat"):
            try:
                collection = self._retriever.vector_store.get_or_create_collection(collection_name)
                result = collection.get(where={"project_id": project_id}, include=[])
                ids = result.get("ids") or []
                if ids:
                    collection.delete(ids=ids)
                    deleted += len(ids)
            except Exception as exc:  # noqa: BLE001
                logger.warning("长期记忆清理失败(collection=%s): %s", collection_name, exc)
        return deleted


def _build_retriever():
    from core.rag_engine.embedder import Embedder
    from core.rag_engine.reranker import Reranker
    from core.rag_engine.retriever import HybridRetriever
    from core.rag_engine.vector_store import VectorStore
    from core.secret_resolver import resolve_secret
    from core.settings import get_settings

    settings = get_settings()
    key, _src = resolve_secret("embedding_api_key")
    embedder = Embedder(
        {
            "mode": settings.embedding_mode,
            "model_name": settings.embedding_model,
            "api_key": key or settings.embedding_api_key,
            "api_base": settings.embedding_api_base,
        }
    )
    store = VectorStore(persist_dir=settings.chroma_dir)
    # ── G-0-3：Reranker 按域策略生产接入 ──────────────────────────────
    # 使用方法（治理开关，默认全关）：
    #   1. BMP_RERANKER_ENABLED=true          # 总开关（默认 false，显式开启）
    #   2. BMP_RERANKER_DOMAIN_POLICY=all     # all=全部域重排；
    #                                         # tender_only=仅招标域（kb_legal_*/kb_*）重排，
    #                                           企业域（kb_ent_*）跳过
    # 评测依据：pf-c-rerank-all/tenderonly-20260901 两报告显示 rerank 零增益/成本+96%，
    # 故默认不启用；语料增长后改以上两个环境变量一键切换，无需改代码。
    # eval 侧对应 CLI：eval.run --rerank --rerank-domain-policy tender_only（语义一致）。
    retriever = HybridRetriever(
        vector_store=store,
        embedder=embedder,
        reranker=Reranker.from_settings(),
        rerank_domain_policy=getattr(settings, "reranker_domain_policy", "all"),
    )
    # 总开关联动：reranker_enabled=true 时生产检索默认走重排级（受 domain_policy 约束）
    retriever.rerank_default = bool(getattr(settings, "reranker_enabled", False))
    return retriever, store


async def build_default_knowledge_base() -> KnowledgeBaseAdapter | None:
    """构造覆盖全部业务 collection 的知识库适配器；无业务库时返回 None。"""
    try:
        retriever, store = _build_retriever()
        collections = [
            c for c in store.list_collections() if not c.startswith("eval_") and not c.startswith("kb_proj_")
        ]
        if not collections:
            return None
        return KnowledgeBaseAdapter(retriever, collections)
    except Exception as e:  # noqa: BLE001
        logger.warning("知识库适配器构造失败（B 模式降级为无知识检索）: %s", e)
        return None
