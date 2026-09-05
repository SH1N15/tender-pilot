"""P-D2 Evidence Grounding Gate（前置硬门，确定性可单测）。

- 作用对象：硬事实断言——资质编号/业绩金额/技术参数值/日期时限类；
- 统一引用锚点格式：正文标记 【N】，N 映射引用对照表 ledger
  （n → chunk_id/source/excerpt/text，由 P-C 检索产物 metadata.chunk_id 构建）；
- 判定规则（确定性，固定输入固定输出）：
  ① 断言同句必须携带【N】标记；
  ② N 必须在本次检索 ledger 内（防编造引用编号）；
  ③ 断言数值（归一化后）必须在所引 chunk 原文中命中（与 eval/metrics/citation.py
     的"引用可锚定到库内原文"同源逻辑）；
- 任一不满足 → 丢弃该断言（原句替换为【待补充】(原因)），绝不编造；
- 放行/拒绝统计写 RunMetrics（grounding 字段），进 cost_report / 决策包证据。
"""

from __future__ import annotations

import re

# 统一引用标记：与 eval/metrics/citation.py DEFAULT_MARKER_PATTERNS 的【n】同源
CITATION_MARKER_RE = re.compile(r"【(\d{1,3})】")
PEND_SUPPLEMENT = "【待补充】"

# 硬事实断言模式（四类：金额 / 日期时限 / 资质编号 / 技术参数值）
HARD_FACT_PATTERNS: dict[str, re.Pattern[str]] = {
    "amount": re.compile(r"\d[\d,，]*(?:\.\d+)?\s*万?元"),
    "date_deadline": re.compile(
        r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
        r"|\d+\s*个?(?:工作日|日历天|日|天)"
        r"|\d{1,2}\s*月\s*\d{1,2}\s*日前"
    ),
    "credential_no": re.compile(
        r"[0-9A-Z]{18}"  # 统一社会信用代码
        r"|[A-Za-z0-9][A-Za-z0-9\-]{4,}\s*号"
    ),
    "param_value": re.compile(
        r"\d+(?:\.\d+)?\s*(?:kV|KV|kW|MW|MPa|mm|cm|km|㎡|m2|m³|吨|℃|%|GHz|MHz|L|Ah|Wh)"
    ),
}

_SENT_SPLIT_RE = re.compile(r"[^。；;\n]+")


def normalize_value(s: str) -> str:
    """数值归一化：全角→半角、去空格/逗号（确定性）。"""
    table = str.maketrans("，０１２３４５６７８９", ",0123456789")
    return (s or "").translate(table).replace(" ", "").replace(",", "").strip()


def build_ledger(retrieved: list[dict]) -> dict[int, dict]:
    """由检索产物构建引用对照表 ledger：n → {chunk_id, source, excerpt, text}。

    retrieved: P-C 检索产物 [{"text","score","metadata"}]，metadata 含 chunk_id/source。
    """
    ledger: dict[int, dict] = {}
    for i, doc in enumerate(retrieved or [], start=1):
        meta = doc.get("metadata") or {}
        text = str(doc.get("text") or "")
        ledger[i] = {
            "n": i,
            "chunk_id": str(meta.get("chunk_id") or meta.get("id") or f"chunk_{i}"),
            "source": str(meta.get("source", "")),
            "collection": str(meta.get("collection", "")),
            "excerpt": text[:200],
            "text": text,
        }
    return ledger


def ledger_texts(ledger: dict[int, dict]) -> list[str]:
    return [entry["text"] for _, entry in sorted(ledger.items())]


def ledger_for_output(ledger: dict[int, dict]) -> dict[str, dict]:
    """对外产物引用对照表（去掉全文，仅留 chunk_id/source/excerpt）。"""
    return {
        str(n): {"chunk_id": e["chunk_id"], "source": e["source"], "excerpt": e["excerpt"]}
        for n, e in sorted(ledger.items())
    }


def _split_sentences(text: str) -> list[str]:
    return [m.group(0) for m in _SENT_SPLIT_RE.finditer(text or "")]


def extract_hard_facts(text: str) -> list[dict]:
    """抽取硬事实断言：{kind, value, sentence, citation}（确定性）。

    citation=断言所在句中的【N】标记编号（无标记为 None）。
    """
    facts: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for sentence in _split_sentences(text or ""):
        citation_m = CITATION_MARKER_RE.search(sentence)
        citation = int(citation_m.group(1)) if citation_m else None
        for kind, pattern in HARD_FACT_PATTERNS.items():
            for m in pattern.finditer(sentence):
                value = m.group(0)
                key = (kind, normalize_value(value))
                if key in seen:
                    continue
                seen.add(key)
                facts.append(
                    {"kind": kind, "value": value, "sentence": sentence, "citation": citation}
                )
    return facts


def check_assertion(fact: dict, ledger: dict[int, dict]) -> tuple[bool, str]:
    """Gate 判定（确定性）：返回 (是否放行, 拒绝原因)。"""
    citation = fact.get("citation")
    if citation is None:
        # G-0-2：提示语按证据状态区分——不再一律"待补充"式含糊。
        # KPI 快照 §6.2：解读节点拒绝原因 100% 为"硬事实无【N】"，根因是
        # 节点未配默认检索 collection（ledger 恒空），模型无证据可标注。
        if ledger:
            return False, (
                f"硬事实未携带引用标记【N】（本次检索证据对照表已有 {len(ledger)} 条可引用，"
                "请为该断言补标对应【N】后重述）"
            )
        return False, (
            "硬事实未携带引用标记【N】（本次解读未检索到任何知识库证据——"
            "请先调用 knowledge_search 检索，再对硬事实句标注对应【N】）"
        )
    entry = ledger.get(int(citation))
    if entry is None:
        return False, f"引用【{citation}】不在本次检索证据对照表内（疑似编造引用）"
    value = normalize_value(str(fact.get("value", "")))
    if not value:
        return False, "断言值为空"
    chunk_norm = normalize_value(entry.get("text", ""))
    if value in chunk_norm:
        return True, ""
    return False, f"断言值『{fact.get('value')}』在所引库内原文（chunk_id={entry['chunk_id']}）未命中"


def ground_hard_facts(text: str, ledger: dict[int, dict]) -> dict:
    """对文本跑 Evidence Grounding Gate。

    返回 {text, stats, passed, rejected}：
    - text: 放行后的文本——被拒陃断言所在句替换为 `【待补充】(原因：...)`；
    - stats: {total, passed, rejected}（Grounding 统计，进 RunMetrics）。
    """
    facts = extract_hard_facts(text)
    out_text = text or ""
    passed: list[dict] = []
    rejected: list[dict] = []
    for fact in facts:
        ok, reason = check_assertion(fact, ledger)
        record = {
            "kind": fact["kind"],
            "value": fact["value"],
            "citation": fact["citation"],
            "sentence": fact["sentence"][:200],
        }
        if ok:
            passed.append(record)
        else:
            record["reason"] = reason
            rejected.append(record)
            replacement = f"{PEND_SUPPLEMENT}(原因：{reason}；待补充：{fact['value']})"
            if fact["sentence"] in out_text:
                out_text = out_text.replace(fact["sentence"], replacement, 1)
    return {
        "text": out_text,
        "stats": {"total": len(facts), "passed": len(passed), "rejected": len(rejected)},
        "passed": passed,
        "rejected": rejected,
    }


def make_ledger_anchor_func(ledger: dict[int, dict]):
    """构造 eval.metrics.citation.citation_valid_rate 的 anchor_func。

    语义：引用标记【N】有效 ⇔ N 在本次检索 ledger 内（引用编号映射到真实检索
    chunk，编造/超界编号无效）。与 Gate 的规则②同源。
    """

    def anchor(citation: str, chunks: list[str]) -> bool:
        m = re.search(r"\d{1,3}", citation or "")
        if not m:
            return False
        entry = ledger.get(int(m.group(0)))
        if entry is None:
            return False
        # 所引 chunk 必须在传入的 chunks（即真实检索文本集合）中
        return (entry.get("text") or entry.get("excerpt") or "") in set(chunks or [])

    return anchor


def summarize_grounding(stats: dict) -> str:
    return (
        f"硬事实断言 total={stats.get('total', 0)} "
        f"passed={stats.get('passed', 0)} rejected(待补充)={stats.get('rejected', 0)}"
    )


def build_citation_ledger_shared(retrieval_collection: str, ledger: dict | None = None):
    """P-D2 2.3：构建带引用记账的 knowledge_search 工具 handler（ReAct 节点共用）。

    knowledge_search 命中的检索产物（含 metadata.chunk_id/source）自动登记进
    ledger（n → chunk 条目），实现"检索产物 chunk_id 从工具层进图状态"。
    只读检索：不改知识库，只登记对照表。
    """
    import json as _json

    if ledger is None:
        ledger = {}

    async def knowledge_search(query: str, collection_name: str = "", top_k: int = 5) -> str:
        # G-0-2：显式 collection 优先；未提供时走 kb_adapter 双库入口（覆盖全部
        # 业务 collection），不再直接拒绝——此前解读节点未配 collection 时
        # knowledge_search 恒报错，grounding 0/N（KPI 快照 §6.2）。
        target = collection_name or retrieval_collection
        results: list[dict]
        if target:
            from core.rag_engine import Embedder, HybridRetriever, VectorStore

            retriever = HybridRetriever(vector_store=VectorStore(), embedder=Embedder())
            results = await retriever.retrieve(query=query, collection_name=target, top_k=top_k)
        else:
            from core.rag_engine.kb_adapter import build_default_knowledge_base

            adapter = await build_default_knowledge_base()
            if adapter is None:
                return _json.dumps({"error": "知识库为空：未配置任何业务 collection，无法检索证据"}, ensure_ascii=False)
            results = await adapter.retrieve(query, top_k=top_k)
        # 检索产物登记进引用对照表（chunk_id 锚点贯通）
        # G-0-2：工具返回中显式携带 n（对照表编号）——否则模型无从得知应标
        # 哪个【N】，输出永远不含引用标记，Grounding 恒 0（实测定位）。
        registered = []
        for doc in results:
            existing = len([k for k in ledger if isinstance(k, int)]) + 1
            ledger[existing] = {
                "n": existing,
                "chunk_id": str((doc.get("metadata") or {}).get("chunk_id") or existing),
                "source": str((doc.get("metadata") or {}).get("source", "")),
                "excerpt": str(doc.get("text") or "")[:200],
                "text": str(doc.get("text") or ""),
            }
            item = dict(doc)
            item["n"] = existing
            item["citation_hint"] = f"在引用该条证据的句子末尾标注【{existing}】"
            registered.append(item)
        return _json.dumps(registered, ensure_ascii=False, default=str)[:4000]

    return ledger, knowledge_search
