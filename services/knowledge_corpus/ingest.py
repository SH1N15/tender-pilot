# -*- coding: utf-8 -*-
"""P-B 双库入库（B1/B2）：法规/企业语料 → P-A chunker → Embedder → Chroma(kb_legal_*/kb_ent_*) + knowledge_bases 行。

- 与生产 chroma_db 共存但使用全新 collection 命名（kb_legal_* / kb_ent_*），存量 kb_* 零污染；
- chunk 元数据：kb_type / source / source_url / issuer / effective_date / chunk_index / corpus_tag；
- 分批落盘 checkpoint（.pb_ingest_state.json），支持断点续跑；
- 用法：
  venv/Scripts/python.exe -m services.knowledge_corpus.ingest --kind legal --kb-name 法规合规库
  venv/Scripts/python.exe -m services.knowledge_corpus.ingest --kind enterprise --kb-name 企业私有库-科旭电子
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

STATE_FILE = Path("data/corpus_raw/.pb_ingest_state.json")
RAW_LAWS_DIR = Path("data/corpus_raw/laws")
CORPUS_TAG_LEGAL = "法规合规语料（真实采集）"
CORPUS_TAG_ENT = "AI 生成演示语料（调研增强，投产时由用户替换真实资料）"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def chunk_texts(text: str) -> list[dict]:
    """P-A 条款级语义分块（与 eval --chunker pa 同一入口）。"""
    from core.doc_engine.chunker import ChunkConfig, chunk_document

    result = chunk_document(text, project_id="pb", config=ChunkConfig())
    return [c for c in result["chunks"] if c["text"].strip()]


async def ingest(kind: str, kb_name: str, batch_size: int = 10) -> dict:
    from core.rag_engine.embedder import Embedder
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
    store = VectorStore(persist_dir=str(settings.chroma_dir))

    # 1. 收集文档
    docs: list[dict] = []
    if kind == "legal":
        for meta_path in sorted(RAW_LAWS_DIR.glob("*.meta.json")):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            txt_path = meta_path.with_suffix("").with_suffix(".txt")
            txt_path = RAW_LAWS_DIR / (meta_path.name[: -len(".meta.json")] + ".txt")
            if not txt_path.exists():
                continue
            docs.append(
                {
                    "source": meta["name"],
                    "source_url": meta["source_url"],
                    "issuer": meta["issuer"],
                    "effective_date": meta["effective_date"],
                    "text": txt_path.read_text(encoding="utf-8", errors="ignore"),
                }
            )
        # 废标条目 / 评分办法样本（结构化条目整体成文档）
        rej_path = Path("data/corpus_raw/bid_rejection/entries.json")
        if rej_path.exists():
            data = json.loads(rej_path.read_text(encoding="utf-8"))
            for i, e in enumerate(data["entries"]):
                docs.append(
                    {
                        "source": f"废标条目-{e['project'][:30]}-{i + 1}",
                        "source_url": f"local:{e['source_doc']}",
                        "issuer": "招标文件原文",
                        "effective_date": "",
                        "text": e["text"],
                    }
                )
        sm_path = Path("data/corpus_raw/scoring_methods/samples.json")
        if sm_path.exists():
            data = json.loads(sm_path.read_text(encoding="utf-8"))
            for s in data["samples"]:
                docs.append(
                    {
                        "source": f"评分办法样本-{s['project'][:40]}",
                        "source_url": f"local:{s['source_doc']}",
                        "issuer": "招标文件原文",
                        "effective_date": "",
                        "text": s["text"],
                    }
                )
    else:
        # 企业画像源档案（.test 6 txt，含成立时间/资质/业绩等事实口径），随新语料一并入库
        profile_dir = Path(r"D:\AgentProject\.test\企业资料-广州市科旭电子有限公司")
        for txt in sorted(profile_dir.glob("*.txt")):
            docs.append(
                {
                    "source": f"企业画像-{txt.stem}",
                    "source_url": f"local:{txt.name}",
                    "issuer": "广州市科旭电子有限公司（企业画像源档案）",
                    "effective_date": "",
                    "text": txt.read_text(encoding="utf-8", errors="ignore"),
                }
            )
        ent_dir = Path("data/corpus_raw/enterprise")
        for txt in sorted(ent_dir.glob("*.txt")):
            meta_file = txt.with_suffix(".meta.json")
            meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
            docs.append(
                {
                    "source": meta.get("title", txt.stem),
                    "source_url": "ai-generated",
                    "issuer": "广州市科旭电子有限公司（AI 生成演示）",
                    "effective_date": "",
                    "text": txt.read_text(encoding="utf-8", errors="ignore"),
                }
            )

    # 2. 分块
    # 统一 slug：collection 名用确定性短码（存 state 便于复跑同名）
    state = load_state()
    coll_key = f"{kind}:{kb_name}"
    if state.get(coll_key, {}).get("collection"):
        collection = state[coll_key]["collection"]
    else:
        # 确定性短码（md5，跨进程稳定；hash() 每进程随机不可用）
        code = int(hashlib.md5(coll_key.encode("utf-8")).hexdigest()[:8], 16) % 10**8
        collection = f"kb_legal_{code:08d}" if kind == "legal" else f"kb_ent_{code:08d}"

    all_chunks: list[dict] = []
    for d in docs:
        for i, ch in enumerate(chunk_texts(d["text"])):
            all_chunks.append(
                {
                    "text": ch["text"],
                    "metadata": {
                        "kb_type": kind,
                        "source": d["source"],
                        "source_url": d["source_url"],
                        "issuer": d["issuer"],
                        "effective_date": d["effective_date"],
                        "chunk_index": i,
                        "clause_no": ch.get("clause_no", ""),
                        "page": ch.get("page", 0),
                        "level_path": "/".join(ch.get("level_path", []))[:200],
                        "tags": ",".join(ch.get("tags", [])),
                        "corpus_tag": CORPUS_TAG_LEGAL if kind == "legal" else CORPUS_TAG_ENT,
                    },
                }
            )
    print(f"{len(docs)} docs -> {len(all_chunks)} chunks -> collection {collection}")

    # 2.5 幂等重置：清掉同名旧 KB 行与旧 collection（重建式入库）
    from sqlalchemy import select

    from services.database import async_session
    from services.models import KnowledgeBase as KBModel

    async with async_session()() as session:
        existing = await session.execute(select(KBModel).where(KBModel.name == kb_name))
        for row in existing.scalars().all():
            if row.collection_name and row.collection_name != collection:
                await store.delete_collection(row.collection_name)
            await session.delete(row)
        await session.commit()
    await store.delete_collection(collection)

    # 3. 分批向量化入库（断点续跑；重建模式下 checkpoint 从 0 起）
    st = {"collection": collection, "done": 0, "total": len(all_chunks)}
    state[coll_key] = st
    save_state(state)
    store.get_or_create_collection(collection)
    t0 = time.time()
    for start in range(st["done"], len(all_chunks), batch_size):
        part = all_chunks[start : start + batch_size]
        embeddings = await embedder.embed([c["text"] for c in part])
        await store.add_documents(
            collection_name=collection,
            ids=[f"{collection}_{start + i:06d}" for i in range(len(part))],
            texts=[c["text"] for c in part],
            embeddings=embeddings,
            metadatas=[c["metadata"] for c in part],
        )
        st["done"] = start + len(part)
        st["total"] = len(all_chunks)
        save_state(state)
        if (start // batch_size) % 5 == 0:
            print(f"ingested {st['done']}/{len(all_chunks)} ({time.time() - t0:.0f}s)")

    # 4. knowledge_bases 行
    async with async_session()() as session:
        kb = KBModel(
            name=kb_name,
            doc_count=len(all_chunks),
            embedding_model=settings.embedding_model,
            collection_name=collection,
            kb_type=kind,
            review_status="reviewed" if kind == "legal" else "draft",
            valid_until=None,
        )
        session.add(kb)
        await session.commit()
        kb_id = str(kb.id)

    return {"collection": collection, "kb_id": kb_id, "docs": len(docs), "chunks": len(all_chunks)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=["legal", "enterprise"])
    parser.add_argument("--kb-name", required=True)
    parser.add_argument("--batch", type=int, default=10)
    args = parser.parse_args()
    result = asyncio.run(ingest(args.kind, args.kb_name, args.batch))
    print(json.dumps(result, ensure_ascii=False))
