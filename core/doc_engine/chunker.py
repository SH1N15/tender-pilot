"""条款级语义分块（P-A 交付件 2.3）：章节 → 条款 → 评分点三层。

- chunk 粒度 = 条款或语义完整的段落组；评分细则表逐评分项成 chunk；
- 元数据：{project_id, 层级路径, 条款号, 页码, 类型标签, 表格引用, 字符数}；
- 类型标签规则优先（词表），LLM 兜底仅补「其他」且较长的 chunk（可关）；
- 尺寸/重叠可配置；默认 chunk_size=800 / overlap=120 / min_chunk=60（记录于 config 输出）；
- 相邻小段合并策略：同一条款/章节内的短段落顺序拼接直到达到 chunk_size，
  不跨条款/章节拼接，避免语义边界污染；孤段（<min_chunk）并入同节前块；
- 与 section_detector/onnx_classifier 叠加不替换：复用其编号规律做章节切分信号，
  对外行为零改动。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 800
DEFAULT_OVERLAP = 120
DEFAULT_MIN_CHUNK = 60

TAG_LEXICON: list[tuple[str, str]] = [
    # (标签, 词表) 优先级从上到下递增匹配顺序无关，输出为多标签
    ("资格", r"资格|资质|营业执照|投标人应具备|供应商应具备|业绩要求|财务要求"),
    ("评分", r"评分|评审|评标|分值|打分|得分|技术分|商务分|价格分"),
    ("废标", r"废标|无效投标|否决投标|拒绝投标|视为无效|不予受理"),
    ("商务", r"商务|付款|支付|合同|工期|交付|交货|售后服务|质保|违约|报价"),
    ("技术", r"技术|参数|需求|配置|功能|性能|规格|设备|系统"),
]

CLAUSE_PATTERN = re.compile(r"^(第[一二三四五六七八九十百千\d]+条|[（(]\d+[)）]|\d+[\.．]\d+)")
TAG_ORDER = ["废标", "评分", "资格", "商务", "技术", "其他"]

SCORING_TABLE_HINT = r"评分|评审|分值|权重|打分|评标"
TABLE_TITLE_HINT = r"表\s*\d+|附表|一览表|评分表|评审表|清单"


@dataclass
class ChunkConfig:
    chunk_size: int = DEFAULT_CHUNK_SIZE
    overlap: int = DEFAULT_OVERLAP
    min_chunk: int = DEFAULT_MIN_CHUNK
    scoring_item_chunk: bool = True  # 评分细则表逐评分项成 chunk
    table_as_chunk: bool = True  # 非评分表整体成 chunk（markdown 保序）
    llm_tag_fallback: bool = False  # LLM 兜底类型标签（默认关，规则优先）

    def to_dict(self) -> dict:
        return {
            "chunk_size": self.chunk_size,
            "overlap": self.overlap,
            "min_chunk": self.min_chunk,
            "scoring_item_chunk": self.scoring_item_chunk,
            "table_as_chunk": self.table_as_chunk,
            "llm_tag_fallback": self.llm_tag_fallback,
        }


@dataclass
class SemanticChunk:
    text: str
    level_path: list[str] = field(default_factory=list)  # e.g. ["第三章 评标办法", "3.2 评分细则"]
    clause_no: str = ""
    page: int = 0
    tags: list[str] = field(default_factory=list)
    table_refs: list[str] = field(default_factory=list)
    chunk_kind: str = "clause"  # clause|scoring_item|table|paragraph_group
    char_count: int = 0

    def to_dict(self, project_id: str = "", chunk_id: str = "") -> dict:
        return {
            "chunk_id": chunk_id,
            "project_id": project_id,
            "text": self.text,
            "level_path": self.level_path,
            "clause_no": self.clause_no,
            "page": self.page,
            "tags": self.tags,
            "table_refs": self.table_refs,
            "chunk_kind": self.chunk_kind,
            "char_count": self.char_count or len(self.text),
        }


def rule_tags(text: str) -> list[str]:
    tags = [tag for tag, pattern in TAG_LEXICON if re.search(pattern, text)]
    return tags or ["其他"]


def top_tag(tags: list[str]) -> str:
    for t in TAG_ORDER:
        if t in tags:
            return t
    return "其他"


# ---------------------------------------------------------------------------
# 行级结构解析
# ---------------------------------------------------------------------------


CLAUSE_HEADING_PATTERN = re.compile(r"^第[一二三四五六七八九十百千\d]+条")


def _heading_level(line: str) -> int | None:
    from core.doc_engine.layout import _is_title_text

    if CLAUSE_HEADING_PATTERN.match(line):
        return None  # 第X条 是条款不是章节标题
    return _is_title_text(line)


class _Section:
    __slots__ = ("path", "lines", "page")

    def __init__(self, path: list[str], page: int):
        self.path = path
        self.lines: list[tuple[str, int]] = []  # (text, page)
        self.page = page


def _parse_sections(source, use_blocks: bool) -> list[_Section]:
    """source: LayoutBlock 列表（use_blocks=True）或纯文本。返回带层级路径的节。"""
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    current: _Section | None = None

    def new_section(path: list[str], page: int) -> _Section:
        s = _Section(path, page)
        sections.append(s)
        return s

    if use_blocks:
        items = [
            (b.text, b.page, b.type == "title", b.level, (getattr(b, "meta", {}) or {}).get("text_dd", ""))
            for b in source
        ]
    else:
        items = [(ln, 0, False, 0, "") for ln in source.split("\n")]

    for text, page, is_title_hint, hint_level, text_dd in items:
        line = text.strip()
        if not line:
            continue
        lvl = _heading_level(line)
        if lvl is None and text_dd and text_dd.strip() != line:
            # 双描边标题（"第第一一章章"）折叠变体兜底判定，正文仍保留原始字符
            lvl = _heading_level(text_dd.strip())
        if lvl is None and is_title_hint and hint_level:
            lvl = hint_level
        if lvl is not None:
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            stack.append((lvl, line[:60]))
            current = new_section([t for _, t in stack], page)
            current.lines.append((line, page))
            continue
        if current is None:
            current = new_section([], page)
        current.lines.append((line, page))
    return sections


def _split_clauses(lines: list[tuple[str, int]]) -> list[tuple[str, str, int]]:
    """节内按条款号切分：返回 [(clause_no, joined_text, page)]。无条款结构时整节一块。"""
    clauses: list[tuple[str, str, int]] = []
    buf: list[str] = []
    cur_no = ""
    cur_page = lines[0][1] if lines else 0
    for text, page in lines:
        m = CLAUSE_PATTERN.match(text)
        if m and len(text) <= 120:
            if buf:
                clauses.append((cur_no, "\n".join(buf), cur_page))
            cur_no = m.group(1).rstrip(".． ")
            buf = [text]
            cur_page = page
        else:
            buf.append(text)
    if buf:
        clauses.append((cur_no, "\n".join(buf), cur_page))
    return clauses


def _pack(text: str, cfg: ChunkConfig, overlap: int | None = None) -> list[str]:
    """长文本按句边界打包；重叠取尾部 overlap 字符。"""
    ov = cfg.overlap if overlap is None else overlap
    if len(text) <= cfg.chunk_size:
        return [text]
    sentences = re.split(r"(?<=[。！？!?；;\n])", text)
    out: list[str] = []
    cur = ""
    for sent in sentences:
        if not sent:
            continue
        if len(cur) + len(sent) > cfg.chunk_size and cur:
            out.append(cur)
            cur = cur[-ov:] if ov and len(cur) >= ov else ""
        cur += sent
    if cur.strip():
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# 表格 chunk（评分项逐条）
# ---------------------------------------------------------------------------


def _is_scoring_table(t) -> bool:
    joined = (t.caption or "") + " " + " ".join(c for r in t.header_rows for c in r)
    return bool(re.search(SCORING_TABLE_HINT, joined))


def table_chunks(tables: list, cfg: ChunkConfig) -> list[SemanticChunk]:
    """tables: TableObject 列表。评分表逐行成 chunk；其余表整体 markdown 一块。"""
    out: list[SemanticChunk] = []
    for t in tables:
        skip_table = (
            t.caption
            and re.search(TABLE_TITLE_HINT, t.caption) is None
            and not _is_scoring_table(t)
            and not cfg.table_as_chunk
        )
        if skip_table:
            continue
        if cfg.scoring_item_chunk and _is_scoring_table(t):
            header = t.header_rows[-1] if t.header_rows else []
            for i, row in enumerate(t.rows):
                cells = [f"{h}:{v}" for h, v in zip(header, row) if (v or "").strip()]
                text = (f"{t.caption}\n" if t.caption else "") + "；".join(cells)
                if not text.strip():
                    continue
                out.append(
                    SemanticChunk(
                        text=text,
                        level_path=[t.caption] if t.caption else [],
                        page=t.page_start,
                        tags=rule_tags(text) + ["评分"] if "评分" not in rule_tags(text) else rule_tags(text),
                        table_refs=[t.table_id],
                        chunk_kind="scoring_item",
                        char_count=len(text),
                    )
                )
        elif cfg.table_as_chunk:
            text = t.to_markdown()
            out.append(
                SemanticChunk(
                    text=text,
                    level_path=[t.caption] if t.caption else [],
                    page=t.page_start,
                    tags=rule_tags(text),
                    table_refs=[t.table_id],
                    chunk_kind="table",
                    char_count=len(text),
                )
            )
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def chunk_document(
    text: str,
    blocks=None,
    tables: list | None = None,
    project_id: str = "",
    config: ChunkConfig | None = None,
    llm_gateway=None,
) -> dict:
    """返回 {chunks: [...], config: {...}, stats: {...}}。

    blocks: layout.LayoutBlock 列表（可选，提供页码/标题层级）；tables: TableObject 列表（可选）。
    """
    cfg = config or ChunkConfig()
    sections = _parse_sections(blocks if blocks is not None else text, use_blocks=blocks is not None)
    chunks: list[SemanticChunk] = []
    prev_body = ""  # 上一 chunk 未加前缀的裸文本（防止 overlap 前缀链式滚雪球）

    def _emit(buf: list[str], path: list[str], page: int, clause_no: str) -> None:
        nonlocal prev_body
        """拼装一个 chunk；上一 chunk 尾部 overlap 作为前缀保证跨边界连续性。"""
        body = "\n".join(buf)
        boundary_head = ""
        if cfg.overlap and chunks and prev_body:
            tail = prev_body.rstrip()[-cfg.overlap :]
            if tail:
                boundary_head = tail + "\n"
        ch = SemanticChunk(
            text=boundary_head + body,
            level_path=list(path),
            clause_no=clause_no,
            page=page,
            tags=[],
            chunk_kind="clause" if clause_no else "paragraph_group",
        )
        ch.tags = rule_tags(ch.text)
        ch.char_count = len(ch.text)
        chunks.append(ch)
        prev_body = body

    for sec in sections:
        body = [(t, p) for t, p in sec.lines]
        if not body:
            continue
        if len(body) == 1 and sec.path and body[0][0] == sec.path[-1]:
            pieces = [(body[0][0], body[0][1], "")]  # 纯标题节：标题行保留为 piece（内容完整性，不丢标题文本）
        else:
            pieces = []
            for clause_no, ctext, page in _split_clauses(body):
                for piece in _pack(ctext, cfg):
                    pieces.append((piece, page, clause_no))
        # 节内相邻小段顺序拼接直到 chunk_size（设计文档既定行为；条款号/页码取节内首段）
        buf: list[str] = []
        buf_len = 0
        buf_page = pieces[0][1]
        buf_clause = pieces[0][2]
        for text, page, clause_no in pieces:
            if buf and buf_len + len(text) + 1 > cfg.chunk_size:
                _emit(buf, sec.path, buf_page, buf_clause)
                buf, buf_len = [], 0
                buf_page, buf_clause = page, clause_no
            if not buf:
                buf_page, buf_clause = page, clause_no
            elif not buf_clause and clause_no:
                buf_clause = clause_no  # 打包块条款号取首个非空（起条款号）
            buf.append(text)
            buf_len += len(text) + 1
        if buf:
            _emit(buf, sec.path, buf_page, buf_clause)

    tc = table_chunks(tables or [], cfg)
    chunks.extend(tc)

    if cfg.llm_tag_fallback and llm_gateway is not None:
        _llm_tag_fallback(chunks, llm_gateway)

    out = []
    for i, ch in enumerate(chunks):
        out.append(ch.to_dict(project_id=project_id, chunk_id=f"{project_id or 'doc'}_c{i:04d}"))
    stats = {
        "total_chunks": len(out),
        "clause_chunks": sum(1 for c in out if c["chunk_kind"] == "clause"),
        "paragraph_group_chunks": sum(1 for c in out if c["chunk_kind"] == "paragraph_group"),
        "scoring_item_chunks": sum(1 for c in out if c["chunk_kind"] == "scoring_item"),
        "table_chunks": sum(1 for c in out if c["chunk_kind"] == "table"),
        "tag_counts": {t: sum(1 for c in out if t in c["tags"]) for t in TAG_ORDER},
    }
    return {"chunks": out, "config": cfg.to_dict(), "stats": stats}


def _llm_tag_fallback(chunks: list[SemanticChunk], llm_gateway) -> None:
    """对规则标为「其他」的 chunk 用 LLM 补标签（temperature=0，批量，失败静默保留规则结果）。"""
    import asyncio

    targets = [c for c in chunks if c.tags == ["其他"] and len(c.text) > 50]
    if not targets:
        return

    async def _run() -> None:
        for start in range(0, len(targets), 10):
            batch = targets[start : start + 10]
            numbered = "\n".join(f"{i}. {c.text[:200]}" for i, c in enumerate(batch))
            messages = [
                {
                    "role": "system",
                    "content": (
                        "对以下招标文件片段分类。标签只能从：资格、评分、商务、技术、废标、其他 中选，"
                        '可多选。返回JSON: {"items":[{"i":0,"tags":["资格"]}]}\n\n' + numbered
                    ),
                }
            ]
            try:
                result = await llm_gateway.collect_json(messages, temperature=0.0)
                for item in result.get("items", []):
                    i = int(item.get("i", -1))
                    tags = [t for t in item.get("tags", []) if t in TAG_ORDER]
                    if 0 <= i < len(batch) and tags:
                        batch[i].tags = tags
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM 类型标签兜底失败（保留规则结果）: %s", exc)
                return

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM 类型标签兜底异常: %s", exc)
