"""版面结构化（几何启发式，P-A 交付件 2.1）。

输入 PDF/DOCX，输出带阅读顺序与层级标签的块序列：
    block = {type: title/paragraph/list_item/table_ref, level, page, order, text, bbox, meta}

设计要点：
- 纯几何/字号/编号启发式，不引入重型版面模型；
- 页眉页脚：跨页重复的顶部/底部行按出现占比剔除（默认 ≥60% 页出现）；
- 多栏检测：按行 x 覆盖区间聚类，存在稳定纵向空隙则两栏按左列→右列重建阅读顺序；
- 标题层级：优先编号规律（复用 section_detector 的 SECTION_PATTERNS，叠加不替换），
  辅以字号/加粗（PDF 用 pdfplumber 字符尺寸，DOCX 用样式与 run 属性）；
- 扫描/图片型 PDF：走 MinerU（services/ocr），失败优雅降级为现有平面提取。
借鉴：OpenBidAgent (github.com/Queen-M0/OpenBidAgent) 文档处理层"多解析器+结构化块"思路，
仅借鉴结构与启发式原则，未搬运代码。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from core.doc_engine.section_detector import SECTION_PATTERNS

logger = logging.getLogger(__name__)

HEADER_FOOTER_MIN_RATIO = 0.6  # 出现在 ≥60% 页面顶部/底部即视为页眉/页脚
COLUMN_GAP_RATIO = 0.18  # 纵向空隙宽度超过页宽 18% 视为分栏
MIN_TITLE_LEN = 2
MAX_TITLE_LEN = 60

_BLOCK_TYPE_TITLE = "title"
_BLOCK_TYPE_PARAGRAPH = "paragraph"
_BLOCK_TYPE_LIST = "list_item"


@dataclass
class LayoutBlock:
    type: str
    level: int  # 标题层级 1-4；非标题为 0
    page: int  # 1-based；DOCX 无页码概念时为 0
    order: int  # 阅读顺序（全局递增）
    text: str
    bbox: list[float] | None = None  # [x0, y0, x1, y1]（PDF 有；DOCX 为 None）
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "level": self.level,
            "page": self.page,
            "order": self.order,
            "text": self.text,
            "bbox": self.bbox,
            "meta": self.meta,
        }


@dataclass
class LayoutResult:
    blocks: list[LayoutBlock] = field(default_factory=list)
    parser_used: str = "heuristic"
    degraded: bool = False  # 扫描件 MinerU 失败降级标记
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "parser_used": self.parser_used,
            "degraded": self.degraded,
            "notes": self.notes,
            "block_count": len(self.blocks),
            "blocks": [b.to_dict() for b in self.blocks],
        }


def _is_title_text(line: str) -> int | None:
    """编号规律判定标题层级（复用 section_detector 规则，叠加不替换）。"""
    stripped = line.strip()
    if not (MIN_TITLE_LEN <= len(stripped) <= MAX_TITLE_LEN):
        return None
    if any(ch in stripped for ch in "。；；,，"):
        return None  # 含句读的长行更可能是正文
    for pattern in SECTION_PATTERNS:
        if pattern.match(stripped):
            return _level_from_id(stripped)
    return None


def _level_from_id(section_id: str) -> int:
    if re.match(r"^第[一二三四五六七八九十百千]+[章部分编篇]", section_id):
        return 1
    if re.match(r"^第[一二三四五六七八九十百千]+节", section_id):
        return 2
    m = re.match(r"^(\d+(?:[\.．]\d+)*)", section_id)
    if m:
        return min(m.group(1).count(".") + m.group(1).count("．") + 1, 4)
    if re.match(r"^[一二三四五六七八九十]+[、．.]", section_id):
        return 2
    return 3


# ---------------------------------------------------------------------------
# PDF：几何启发式
# ---------------------------------------------------------------------------


def _dedup_double_draw(text: str) -> str:
    """部分政府采购 PDF 用双描边伪造加粗，字符被重复绘制（"广广 东东"）。

    仅当相邻重复对占比很高（>40%）时按对折叠，正常文本零改动。
    """
    chars = text.replace(" ", "")
    if len(chars) < 4:
        return text
    pairs = sum(1 for a, b in zip(chars, chars[1:]) if a == b)
    if pairs / max(len(chars) - 1, 1) <= 0.4:
        return text
    out = []
    i = 0
    while i < len(chars):
        out.append(chars[i])
        if i + 1 < len(chars) and chars[i + 1] == chars[i]:
            i += 2
        else:
            i += 1
    return "".join(out)


def _lines_from_page(page) -> list[dict]:
    """pdfplumber 页 → 行（按 y 聚簇词，保留 x 范围与最大字号/加粗）。"""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    if not words:
        return []
    lines: dict[int, dict] = {}
    for w in words:
        key = round(w["top"] / 3)  # 3pt 容差聚簇
        ln = lines.setdefault(
            key,
            {"text_parts": [], "x0": w["x0"], "x1": w["x1"], "top": w["top"], "size": 0.0, "bold": False},
        )
        ln["text_parts"].append((w["x0"], w["text"]))
        ln["x0"] = min(ln["x0"], w["x0"])
        ln["x1"] = max(ln["x1"], w["x1"])
        ln["size"] = max(ln["size"], float(w.get("bottom", 0)) - float(w.get("top", 0)) or 0.0)
        if "bold" in (w.get("fontname") or "").lower():
            ln["bold"] = True
    out = []
    for key in sorted(lines):
        ln = lines[key]
        parts = sorted(ln["text_parts"], key=lambda p: p[0])
        raw = " ".join(p[1] for p in parts).strip()
        if not raw:
            continue
        # 内容完整性硬约束：块正文保留源字符（含双描边重复字符）；
        # 去重折叠结果只存 meta.text_dd 供消费方按需选择，不回写正文。
        dd = _dedup_double_draw(raw)
        if dd == raw:
            dd = ""
        out.append(
            {
                "text": raw,
                "text_dd": dd,
                "x0": ln["x0"],
                "x1": ln["x1"],
                "top": ln["top"],
                "size": ln["size"],
                "bold": ln["bold"],
            }
        )
    return out


def _detect_column_split(lines: list[dict], page_width: float) -> float | None:
    """检测稳定分栏空隙，返回空隙中心 x（无则 None）。"""
    if page_width <= 0 or len(lines) < 6:
        return None
    mid = page_width / 2
    left_cnt = sum(1 for ln in lines if ln["x1"] < mid)
    right_cnt = sum(1 for ln in lines if ln["x0"] > mid)
    span_cnt = sum(1 for ln in lines if ln["x0"] < mid and ln["x1"] > mid)
    if span_cnt > len(lines) * 0.3:
        return None  # 跨栏行多，非典型两栏
    if left_cnt >= len(lines) * 0.3 and right_cnt >= len(lines) * 0.3:
        return mid
    return None


def _split_by_column(lines: list[dict], split_x: float) -> list[dict]:
    """两栏重排：左列自上而下，再右列。

    内容完整性硬约束：跨栏行（标题/表头等横贯两栏的行）绝不丢弃——
    按其 top 位置并入左列阅读流（左列先读，跨栏行通常是其所在纵向位置的节标题）。
    """
    left = [ln for ln in lines if ln["x1"] <= split_x]
    right = [ln for ln in lines if ln["x0"] > split_x]
    spanning = [ln for ln in lines if ln["x0"] <= split_x < ln["x1"]]
    if spanning and left:
        # 跨栏行按 top 归位插入左列（右列仍在左列之后整体阅读）
        merged_left = sorted(left + spanning, key=lambda ln: ln["top"])
        return merged_left + sorted(right, key=lambda ln: ln["top"])
    if spanning:  # 无左列时跨栏行与右列按 top 归并
        return sorted(right + spanning, key=lambda ln: ln["top"])
    return sorted(left, key=lambda ln: ln["top"]) + sorted(right, key=lambda ln: ln["top"])


def _merge_lines_to_paragraphs(lines: list[dict], split_x: float | None) -> list[dict]:
    """把连续行合并为段落/标题候选：标题行独立，正文行拼接。"""
    if split_x is not None:
        lines = _split_by_column(lines, split_x)
    merged: list[dict] = []
    for ln in lines:
        # 标题判定用去重变体（双描边标题"第第一一章章"折叠后才能命中编号规律），正文仍保留原始字符
        lvl = _is_title_text(ln.get("text_dd") or ln["text"])
        big = ln["size"] >= 14 or (ln["bold"] and ln["size"] >= 12)
        if lvl is not None or (big and len(ln.get("text_dd") or ln["text"]) <= MAX_TITLE_LEN):
            merged.append({**ln, "title_level": lvl or 1, "is_title": True})
            continue
        prev = merged[-1] if merged else None
        if prev and not prev.get("is_title") and prev.get("size") == ln["size"] and len(ln["text"]) < 40:
            # 短行续段（中文 PDF 常见折行）
            prev["text"] += ln["text"]
            prev["x1"] = max(prev["x1"], ln["x1"])
        else:
            merged.append({**ln, "title_level": lvl or 0, "is_title": False})
    return merged


def _page_recurring_lines(pages_lines: list[list[dict]]) -> set[str]:
    """跨页重复的顶/底行文本（页眉/页脚候选）。"""
    if len(pages_lines) < 3:
        return set()
    top_texts, bottom_texts = [], []
    for lines in pages_lines:
        if not lines:
            continue
        sorted_lines = sorted(lines, key=lambda ln: ln["top"])
        page_h = max(ln["top"] for ln in sorted_lines) or 1
        for ln in sorted_lines[:2]:
            top_texts.append(ln["text"])
        for ln in sorted_lines[-2:]:
            if ln["top"] > page_h * 0.85:
                bottom_texts.append(ln["text"])
    recurring = set()
    for group in (top_texts, bottom_texts):
        for text, cnt in Counter(group).items():
            if cnt >= max(2, int(len(pages_lines) * HEADER_FOOTER_MIN_RATIO)):
                recurring.add(text)
    return recurring


def parse_pdf_layout(path: str) -> LayoutResult:
    import pdfplumber

    result = LayoutResult(parser_used="pdfplumber+heuristic")
    order = 0
    with pdfplumber.open(path) as pdf:
        pages_lines: list[list[dict]] = []
        page_widths = []
        for page in pdf.pages:
            lines = _lines_from_page(page)
            pages_lines.append(lines)
            page_widths.append(float(page.width))
        recurring = _page_recurring_lines(pages_lines)
        avg_width = sum(page_widths) / len(page_widths) if page_widths else 0

        for page_num, lines in enumerate(pages_lines, start=1):
            split_x = _detect_column_split(lines, avg_width)
            merged = _merge_lines_to_paragraphs(lines, split_x)
            for item in merged:
                text = item["text"]
                if text in recurring:
                    continue  # 页眉/页脚剔除
                if item.get("is_title"):
                    btype, level = _BLOCK_TYPE_TITLE, item["title_level"]
                else:
                    btype, level = _BLOCK_TYPE_PARAGRAPH, 0
                result.blocks.append(
                    LayoutBlock(
                        type=btype,
                        level=level,
                        page=page_num,
                        order=order,
                        text=text,
                        bbox=[
                            round(item["x0"], 1),
                            round(item["top"], 1),
                            round(item["x1"], 1),
                            round(item["top"] + item["size"], 1),
                        ],
                        meta={
                            "size": round(item["size"], 1),
                            "bold": item["bold"],
                            **({"text_dd": item["text_dd"]} if item.get("text_dd") else {}),
                        },
                    )
                )
                order += 1
    return result


# ---------------------------------------------------------------------------
# DOCX：样式启发式
# ---------------------------------------------------------------------------


def parse_docx_layout(path: str) -> LayoutResult:
    from docx import Document

    result = LayoutResult(parser_used="python-docx+styles")
    doc = Document(path)
    order = 0
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower() if para.style is not None else ""
        level = 0
        if "heading" in style or "标题" in style:
            digits = re.findall(r"\d+", style)
            level = int(digits[0]) if digits else 1
        if level == 0:
            level = _is_title_text(text) or 0
        sizes = [r.font.size.pt for r in para.runs if r.font.size is not None]
        bold = any(r.bold for r in para.runs if r.bold is not None)
        is_title = level > 0 or ("标题" in style)
        result.blocks.append(
            LayoutBlock(
                type=_BLOCK_TYPE_TITLE if is_title else _BLOCK_TYPE_PARAGRAPH,
                level=level if is_title else 0,
                page=0,
                order=order,
                text=text,
                bbox=None,
                meta={
                    "style": para.style.name if para.style is not None else "",
                    "bold": bold,
                    "size": max(sizes) if sizes else None,
                },
            )
        )
        order += 1
    return result


# ---------------------------------------------------------------------------
# 扫描件：MinerU + 优雅降级
# ---------------------------------------------------------------------------


def is_scanned_pdf(path: str) -> bool:
    """与现有 PdfParser 同口径：全文字符量 < 页数*50 视为扫描/图片型。"""
    import pdfplumber

    try:
        with pdfplumber.open(path) as pdf:
            total_text = ""
            for page in pdf.pages:
                total_text += page.extract_text() or ""
            page_count = max(len(pdf.pages), 1)
        return len(total_text.strip()) < page_count * 50
    except Exception:
        return False


def parse_scanned_layout(path: str) -> LayoutResult:
    """MinerU 路径：失败时降级为现有 pdfplumber 平面提取（不抛异常）。"""
    try:
        import asyncio

        from services.ocr.mineru_adapter import MinerUError, get_ocr_client

        async def _run() -> str:
            client = await get_ocr_client()
            task_id = await client.submit_file(path, is_ocr=True)
            for _ in range(120):
                data = await client.query_task(task_id)
                state = data.get("state")
                if state == "done":
                    return data.get("markdown") or ""
                if state == "failed":
                    raise MinerUError("upstream", f"MinerU 任务失败: {data.get('error_message')}")
                await asyncio.sleep(5)
            raise MinerUError("timeout", "MinerU 轮询超时")

        markdown = asyncio.run(_run())
        return _markdown_to_blocks(markdown)
    except Exception as exc:  # noqa: BLE001 优雅降级
        logger.warning("MinerU 解析失败，降级为平面提取: %s", exc)
        result = parse_pdf_layout(path)
        result.degraded = True
        result.notes.append(f"mineru_failed_degraded: {type(exc).__name__}")
        return result


def _markdown_to_blocks(markdown: str) -> LayoutResult:
    result = LayoutResult(parser_used="mineru")
    order = 0
    page = 0
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            result.blocks.append(LayoutBlock(_BLOCK_TYPE_TITLE, len(m.group(1)), page, order, m.group(2)))
        elif re.match(r"^[-*]\s+", line):
            result.blocks.append(LayoutBlock(_BLOCK_TYPE_LIST, 0, page, order, re.sub(r"^[-*]\s+", "", line)))
        else:
            result.blocks.append(LayoutBlock(_BLOCK_TYPE_PARAGRAPH, 0, page, order, line))
        order += 1
    return result


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


def build_layout(file_path: str) -> LayoutResult:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".pdf":
        if is_scanned_pdf(file_path):
            return parse_scanned_layout(file_path)
        return parse_pdf_layout(file_path)
    if suffix in (".docx", ".doc", ".wps"):
        return parse_docx_layout(file_path)
    # txt/html 等：按空行分段
    result = LayoutResult(parser_used="plain")
    text = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    order = 0
    for para in text.split("\n"):
        line = para.strip()
        if not line:
            continue
        lvl = _is_title_text(line)
        result.blocks.append(LayoutBlock(_BLOCK_TYPE_TITLE if lvl else _BLOCK_TYPE_PARAGRAPH, lvl or 0, 0, order, line))
        order += 1
    return result


def layout_to_text(blocks: list[LayoutBlock]) -> str:
    """块序列 → 平面文本（供既有消费方兼容/嵌入使用，保持阅读顺序）。"""
    return "\n".join(b.text for b in blocks)
