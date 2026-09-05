from __future__ import annotations

import logging
import os
import pickle
import re
from collections import Counter
from typing import List

import numpy as np

from core.rag_engine.embedder import Embedder
from core.rag_engine.vector_store import VectorStore

logger = logging.getLogger(__name__)

# 索引持久化格式版本：tokenize 实现变更时 +1，加载时自动失效重建（C3）
_INDEX_FORMAT_VERSION = 3
_DEFAULT_PERSIST_DIR = os.path.join("data", "rag_index")  # 独立目录，不混入 chroma_db
# 解析层字符重复工件折叠（"投投标标"→"投标"；仅作用于分词/索引，不改原文）
_CJK_DOUBLE_RE = re.compile(r"([一-鿿])+")


def _load_jieba():
    try:
        import jieba

        jieba.setLogLevel(logging.WARNING)
        return jieba
    except Exception as e:  # noqa: BLE001
        logger.warning("jieba 不可用（%s），中文退回单字切分", e)
        return None


class HybridRetriever:
    """混合检索：向量 + TF-IDF 关键词 → RRF 融合 →（可选）Reranker 重排。

    P-C 增强：
    - C1: 可选重排级（Reranker 实例注入 + retrieve(rerank=True) 显式开启；
      失败自动降级为融合序并记 warning）；
    - C2: 中文走 jieba 分词（不可用时退回单字切分），查询改写钩子；
    - C3: TF-IDF/关键词索引持久化（pickle，独立目录 data/rag_index），
      启动加载、写入失效重建，重复入库幂等（同文本跳过）；
    - 检索产物保留 metadata（含 chunk_id/source/chunk_index，P-D2 引用锚点依赖）。
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedder: Embedder,
        reranker=None,
        persist_dir: str | None = _DEFAULT_PERSIST_DIR,
        candidate_k: int = 20,
        rerank_domain_policy: str = "all",
    ):
        self.vector_store = vector_store
        self.embedder = embedder
        self.reranker = reranker
        self.rerank_default = False  # 配置开关：默认关，显式 retrieve(rerank=True) 开启
        self.rerank_alpha = 1.0  # 1.0=纯重排序；<1.0=重排分与 RRF 融合分加权（防跨编码器整体覆盖融合序）
        # G-0-3：按域重排策略（all | tender_only），对齐 eval 侧 --rerank-domain-policy；
        # tender_only 时企业域（kb_ent_*）collection 跳过重排。
        self.rerank_domain_policy = rerank_domain_policy
        self.candidate_k = max(int(candidate_k), 1)
        self._documents: list[str] = []
        self._doc_metadatas: list[dict] = []
        self._tfidf_matrix: np.ndarray | None = None
        self._idf: np.ndarray | None = None
        self._vocab: dict[str, int] = {}
        self._jieba = _load_jieba()
        self._persist_dir = persist_dir
        if persist_dir:
            self._load_index()

    # ── 检索入口 ──────────────────────────────────────────────

    async def retrieve(
        self,
        query: str,
        collection_name: str,
        top_k: int = 5,
        rerank: bool | None = None,
        rewrite_fn=None,
    ) -> list[dict]:
        """rerank=None 时按是否注入 Reranker 实例决定；rewrite_fn(query)->str 可选改写。"""
        search_query = query
        if rewrite_fn is not None:
            try:
                search_query = rewrite_fn(query) or query
            except Exception as e:  # noqa: BLE001
                logger.warning("查询改写失败，使用原查询: %s", e)

        vector_results = await self._vector_search(search_query, collection_name, top_k)
        keyword_results = self._bm25_search(search_query, top_k)
        fused = self._rrf_fusion(vector_results, keyword_results, k=60)

        do_rerank = rerank if rerank is not None else self.rerank_default
        # G-0-3：tender_only 策略下企业域（kb_ent_*）跳过重排（企业小语料重排负收益）
        if do_rerank and self.rerank_domain_policy == "tender_only" and collection_name.startswith("kb_ent"):
            do_rerank = False
        if do_rerank and self.reranker is not None and fused:
            fused = await self._rerank_stage(query, fused, top_k)

        return fused[:top_k]

    async def _rerank_stage(self, original_query: str, fused: list[dict], top_k: int) -> list[dict]:
        """重排级：候选池取 RRF 前 candidate_k，调 Reranker；任何失败降级为融合序。"""
        candidates = fused[: max(self.candidate_k, top_k)]
        try:
            ranked = await self.reranker.rerank(original_query, [c["text"] for c in candidates], top_n=top_k)
        except Exception as e:  # noqa: BLE001
            logger.warning("重排失败，降级为 RRF 融合序: %s", e)
            return fused
        out = []
        if self.rerank_alpha >= 1.0:
            for idx, score in ranked:
                item = dict(candidates[idx])
                item["score"] = score
                out.append(item)
            return out
        # 加权混合：重排相关分归一 + RRF 分归一
        rrf_max = max(c["score"] for c in candidates) or 1.0
        for idx, score in ranked:
            item = dict(candidates[idx])
            item["score"] = self.rerank_alpha * score + (1 - self.rerank_alpha) * candidates[idx]["score"] / rrf_max
            out.append(item)
        out.sort(key=lambda x: x["score"], reverse=True)
        return out

    async def _vector_search(
        self,
        query: str,
        collection_name: str,
        top_k: int,
    ) -> list[dict]:
        query_embeddings = await self.embedder.embed([query])
        if not query_embeddings:
            return []

        results = await self.vector_store.query(
            collection_name=collection_name,
            query_embedding=query_embeddings[0],
            top_k=top_k * 3,
        )

        if not results.get("documents") or not results["documents"][0]:
            return []

        scored = []
        for i, doc in enumerate(results["documents"][0]):
            dist = results["distances"][0][i] if results.get("distances") else 0
            metadata = results["metadatas"][0][i] if results.get("metadatas") else {}
            similarity = max(0.0, 1 - dist) if dist is not None else 0.0
            scored.append(
                {
                    "text": doc,
                    "score": similarity,
                    "metadata": metadata,
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    # ── 分词（C2: jieba 优先，单字退回）──────────────────────

    def _tokenize(self, text: str) -> list[str]:
        if self._jieba is not None:
            return self._tokenize_jieba(text)
        return self._tokenize_char(text)

    def _tokenize_jieba(self, text: str) -> list[str]:
        tokens: list[str] = []
        for m in re.finditer(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", text):
            seg = m.group(0)
            if "\u4e00" <= seg[0] <= "\u9fff":
                for word in self._jieba.lcut(seg):
                    word = word.strip()
                    if word:
                        tokens.append(word.lower())
            else:
                tokens.append(seg.lower())
        return tokens

    def _tokenize_char(self, text: str) -> list[str]:
        tokens: list[str] = []
        for char in text:
            if "\u4e00" <= char <= "\u9fff":
                tokens.append(char)
            else:
                for word in re.findall(r"[a-zA-Z0-9]+", char):
                    tokens.append(word.lower())
        return tokens

    # ── TF-IDF 索引（C3: 持久化）─────────────────────────────

    def _build_tfidf(self, docs: list[str]) -> None:
        if not docs:
            self._tfidf_matrix = None
            self._idf = None
            self._vocab = {}
            return

        tokenized_docs = [self._tokenize(doc) for doc in docs]

        vocab: dict[str, int] = {}
        for tokens in tokenized_docs:
            for t in tokens:
                if t not in vocab:
                    vocab[t] = len(vocab)
        self._vocab = vocab

        n_docs = len(docs)
        n_terms = len(vocab)

        if n_terms == 0:
            self._tfidf_matrix = np.zeros((n_docs, 1), dtype=np.float32)
            self._idf = np.zeros(1, dtype=np.float32)
            return

        tf_matrix = np.zeros((n_docs, n_terms), dtype=np.float32)
        for i, tokens in enumerate(tokenized_docs):
            counts = Counter(tokens)
            total = len(tokens) if tokens else 1
            for token, count in counts.items():
                if token in vocab:
                    tf_matrix[i, vocab[token]] = count / total

        df = np.sum(tf_matrix > 0, axis=0)
        idf = np.log((n_docs + 1) / (df + 1)) + 1
        self._idf = idf

        self._tfidf_matrix = tf_matrix * idf

        norms = np.linalg.norm(self._tfidf_matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._tfidf_matrix = self._tfidf_matrix / norms

    def _index_payload(self) -> dict:
        return {
            "format_version": _INDEX_FORMAT_VERSION,
            "tokenizer": "jieba" if self._jieba is not None else "char",
            "documents": self._documents,
            "doc_metadatas": self._doc_metadatas,
            "tfidf_matrix": self._tfidf_matrix,
            "idf": self._idf,
            "vocab": self._vocab,
        }

    def _load_index(self) -> bool:
        """加载持久化索引；版本/分词器不匹配或损坏时丢弃重建（返回 False）。"""
        path = os.path.join(self._persist_dir, "tfidf_index.pkl")
        if not os.path.exists(path):
            return False
        try:
            with open(path, "rb") as f:
                payload = pickle.load(f)
            if (
                payload.get("format_version") != _INDEX_FORMAT_VERSION
                or payload.get("tokenizer") != ("jieba" if self._jieba is not None else "char")
            ):
                logger.info("TF-IDF 索引版本/分词器不匹配，丢弃重建: %s", path)
                return False
            self._documents = payload["documents"]
            self._doc_metadatas = payload["doc_metadatas"]
            self._tfidf_matrix = payload["tfidf_matrix"]
            self._idf = payload["idf"]
            self._vocab = payload["vocab"]
            logger.info("TF-IDF 索引已从 %s 加载（%d docs）", path, len(self._documents))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("TF-IDF 索引加载失败，将重建: %s (%s)", path, e)
            return False

    def save_index(self) -> None:
        if not self._persist_dir:
            return
        try:
            os.makedirs(self._persist_dir, exist_ok=True)
            path = os.path.join(self._persist_dir, "tfidf_index.pkl")
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump(self._index_payload(), f)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            logger.warning("TF-IDF 索引持久化失败（不影响检索）: %s", e)

    # ── 关键词检索 / 融合 ────────────────────────────────────

    def _bm25_search(self, query: str, top_k: int) -> list[tuple[str, float]]:
        if self._tfidf_matrix is None or not self._documents:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens or not self._vocab:
            return []

        n_terms = len(self._vocab)
        query_vec = np.zeros(n_terms, dtype=np.float32)
        counts = Counter(query_tokens)
        total = len(query_tokens)
        for token, count in counts.items():
            if token in self._vocab:
                query_vec[self._vocab[token]] = count / total

        if self._idf is not None:
            query_vec = query_vec * self._idf

        norm = np.linalg.norm(query_vec)
        if norm > 0:
            query_vec = query_vec / norm

        scores = self._tfidf_matrix @ query_vec

        top_indices = np.argsort(scores)[::-1][:top_k]

        results: list[tuple[str, float]] = []
        for idx in top_indices:
            if scores[idx] > 0:
                results.append((self._documents[idx], float(scores[idx])))
        return results

    def _rrf_fusion(
        self,
        vector_results: list[dict],
        keyword_results: list[tuple[str, float]],
        k: int = 60,
    ) -> list[dict]:
        rrf_scores: dict[str, float] = {}
        text_meta: dict[str, dict] = {}

        for rank, item in enumerate(vector_results):
            text = item["text"]
            rrf_scores[text] = rrf_scores.get(text, 0.0) + 1.0 / (k + rank + 1)
            if text not in text_meta:
                text_meta[text] = item.get("metadata", {})

        for rank, (text, _score) in enumerate(keyword_results):
            rrf_scores[text] = rrf_scores.get(text, 0.0) + 1.0 / (k + rank + 1)
            if text not in text_meta:
                # 关键词路命中：优先回查已索引元数据（保留 chunk_id 等锚点字段）
                if text in self._documents:
                    idx = self._documents.index(text)
                    text_meta[text] = self._doc_metadatas[idx] if idx < len(self._doc_metadatas) else {}
                else:
                    text_meta[text] = {}

        fused = [
            {"text": text, "score": score, "metadata": text_meta.get(text, {})} for text, score in rrf_scores.items()
        ]
        fused.sort(key=lambda x: x["score"], reverse=True)
        return fused

    # ── 父上下文扩展（small-to-big，P-C C6 设计见报告）────────

    def expand_neighbors(self, results: list[dict], window: int = 1) -> list[dict]:
        """命中细块时用同 source 相邻 chunk_index 的已索引文本拼接扩展。

        依赖入库 metadata 含 source/chunk_index（eval/入库管线均带）；缺元数据时原样返回。
        """
        if window <= 0 or not self._documents:
            return results
        by_key: dict[tuple, str] = {}
        for doc, meta in zip(self._documents, self._doc_metadatas):
            if meta.get("source") is not None and meta.get("chunk_index") is not None:
                by_key[(meta["source"], int(meta["chunk_index"]))] = doc
        if not by_key:
            return results
        expanded = []
        for item in results:
            meta = item.get("metadata") or {}
            source = meta.get("source")
            idx = meta.get("chunk_index")
            if source is None or idx is None:
                expanded.append(item)
                continue
            parts = []
            for j in range(int(idx) - window, int(idx) + window + 1):
                text = by_key.get((source, j))
                if text:
                    parts.append(text)
            out = dict(item)
            joined = "\n".join(parts)
            if joined and joined != item["text"]:
                out["expanded_text"] = joined
            expanded.append(out)
        return expanded

    # ── 入库（C3: 幂等 + 持久化）─────────────────────────────

    async def add_documents(
        self,
        collection_name: str,
        texts: List[str],
        metadatas: List[dict] | None = None,
    ):
        if not texts:
            return

        # 幂等：同文本已入库的跳过（重复上传不重建索引/不重复嵌入）
        existing = set(self._documents)
        new_texts: List[str] = []
        new_metadatas: List[dict] = []
        for i, t in enumerate(texts):
            if t in existing:
                continue
            existing.add(t)
            new_texts.append(t)
            new_metadatas.append(metadatas[i] if metadatas and i < len(metadatas) else {})

        if not new_texts:
            logger.info("add_documents 幂等跳过：%d 条文本均已入库", len(texts))
            return

        self._documents.extend(new_texts)
        self._doc_metadatas.extend(new_metadatas)

        self._build_tfidf(self._documents)
        self.save_index()

        embeddings = await self.embedder.embed(new_texts)
        # 全局唯一 id：避免跨次上传 id 冲突导致 Chroma add 失败
        import uuid

        ids = [f"doc_{uuid.uuid4().hex}" for _ in new_texts]
        await self.vector_store.add_documents(
            collection_name=collection_name,
            ids=ids,
            texts=new_texts,
            embeddings=embeddings,
            metadatas=new_metadatas if any(new_metadatas) else None,
        )
