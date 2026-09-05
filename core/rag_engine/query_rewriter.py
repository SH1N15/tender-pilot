"""查询理解/改写（P-C C2）：轻量确定性规则优先，LLM 改写可选（temperature=0）。

确定性规则（无外部调用、可关）：
1. 去停用词（口语填充词，不携带检索信息）；
2. 同义词归一（把口语/变体映射到招标文本惯用词）；
3. 全半角/空白归一。
LLM 改写默认关闭；开启时经注入的 gateway（temperature=0）产出改写查询，
失败/超时回退到原查询——永不阻塞主链路。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 口语填充/无信息量词（不出现在招标原文关键词中，或会稀释 TF-IDF）
_STOPWORDS = {
    "的", "了", "吗", "呢", "啊", "请问", "什么", "哪些", "如何", "怎样", "是不是",
    "可以", "需要", "需要什么", "要求是什么", "怎么样", "关于", "以及", "还是",
    "一般", "多少", "多少万", "应该", "必须吗", "吗?", "？", "？?",
}

# 同义/变体归一（查询词 → 招标文件惯用词）；顺序敏感：长短语优先替换
_SYNONYMS: list[tuple[str, str]] = [
    ("投标供应商", "投标人"),
    ("供应商", "投标人"),
    ("采购人吗", "采购人"),
    ("废标项", "无效标"),
    ("废标条款", "无效投标条款"),
    ("串标", "串通投标"),
    ("围标", "串通投标"),
    ("质保期", "保修期"),
    ("交货期", "交付时间"),
    ("工期", "交付时间"),
    ("标书", "投标文件"),
    ("gz", "资格"),
]

_FULLWIDTH_TABLE = str.maketrans(
    {"（": "(", "）": ")", "　": " ", "：": ":", "；": ";", "，": ",", "。": ".", "？": "?"}
)


def rewrite_query_deterministic(query: str) -> str:
    """确定性规则改写：归一 → 同义词替换 → 去停用词。空串回退原查询。"""
    if not query or not query.strip():
        return query
    q = query.translate(_FULLWIDTH_TABLE)
    for src, dst in _SYNONYMS:
        if src in q:
            q = q.replace(src, dst)
    tokens = re.split(r"[\s,，。;；:：?？!！]+", q)
    kept = [t for t in tokens if t and t not in _STOPWORDS]
    rewritten = "".join(kept) if kept else q
    return rewritten.strip() or query


async def rewrite_query_llm(query: str, gateway, timeout_s: float = 8.0) -> str:
    """可选 LLM 改写（temperature=0）。任何失败回退原查询并记 warning。"""
    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是检索查询改写器。把口语化问题改写为适合在招标文件全文中"
                    "做关键词检索的短查询：保留关键实体与限定词，去口语，输出一行，"
                    "不要解释。"
                ),
            },
            {"role": "user", "content": query},
        ]
        text = await gateway.chat(messages, temperature=0.0, max_tokens=64)
        rewritten = (text or "").strip().splitlines()[0].strip() if text else ""
        return rewritten or query
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 查询改写失败，回退原查询: %s", e)
        return query
