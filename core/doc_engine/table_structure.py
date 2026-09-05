"""表格结构化（P-A 交付件 2.2）：平面 headers/rows → 表格对象。

- 表标题（caption）：由 pipeline 传入邻近版面块匹配，或显式指定；
- 两级表头：首行存在横向合并（同值重复/空槽）且次行为短标签行时识别为 category+sub；
- 合并单元格还原：python-docx 对横向/纵向合并会重复输出同值单元格 → 相邻同值折叠；
- 跨页表格合并（续表识别）：相邻表（页码连续）且满足「表头一致 / 无表头 / 显式续表标记」之一；
- 保留源位置（page_start/page_end + region_index）；输出 JSON + markdown 双格式。
借鉴：OpenBidAgent 表格证据保留思想 + 开源表格结构化（合并单元格还原）通用做法，未搬运代码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

CONTINUATION_MARKERS = ("续表", "接上表", "承上表")


@dataclass
class TableObject:
    table_id: str
    caption: str = ""
    header_rows: list[list[str]] = field(default_factory=list)  # 1 或 2 行
    rows: list[list[str]] = field(default_factory=list)
    page_start: int = 0
    page_end: int = 0
    region_index: int = 0
    merged_cells: list[dict] = field(default_factory=list)  # [{row,col,row_span,col_span,value}]
    merged_from_pages: list[int] = field(default_factory=list)  # 跨页合并时记录来源页
    header_level: int = 1

    def to_dict(self) -> dict:
        return {
            "table_id": self.table_id,
            "caption": self.caption,
            "header_rows": self.header_rows,
            "header_level": self.header_level,
            "rows": self.rows,
            "row_count": len(self.rows),
            "page_start": self.page_start,
            "page_end": self.page_end,
            "region_index": self.region_index,
            "merged_cells": self.merged_cells,
            "cross_page_merged": bool(self.merged_from_pages),
            "merged_from_pages": self.merged_from_pages,
        }

    def to_markdown(self) -> str:
        lines: list[str] = []
        if self.caption:
            lines.append(f"**{self.caption}**")
        if not self.header_rows and not self.rows:
            return "\n".join(lines)
        header = self.header_rows[-1] if self.header_rows else [""] * (len(self.rows[0]) if self.rows else 0)
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        for row in self.rows:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)


def _norm_cell(v: str) -> str:
    return re.sub(r"\s+", "", v or "")


def _is_header_like(row: list[str]) -> bool:
    cells = [_norm_cell(c) for c in row]
    cells = [c for c in cells if c]
    if not cells:
        return False
    short = sum(1 for c in cells if len(c) <= 12)
    return short / len(cells) >= 0.7


def _restore_merged(rows: list[list[str]]) -> tuple[list[list[str]], list[dict]]:
    """折叠 python-docx/pdfplumber 输出的重复单元格（横向与纵向）。"""
    restored: list[list[str]] = []
    merged: list[dict] = []
    for r_idx, row in enumerate(rows):
        new_row: list[str] = []
        for c_idx, val in enumerate(row):
            nv = _norm_cell(val)
            if new_row and nv and nv == _norm_cell(new_row[-1]):
                # 横向合并：同值重复
                for m in merged:
                    if m["row"] == r_idx and m["col"] == len(new_row) - 1:
                        m["col_span"] += 1
                        break
                else:
                    merged.append(
                        {"row": r_idx, "col": len(new_row) - 1, "row_span": 1, "col_span": 2, "value": new_row[-1]}
                    )
                continue
            new_row.append(val)
        restored.append(new_row)
    # 纵向合并：相邻行同列同值（且该值非空、非纯数字序号）
    cleaned: list[list[str]] = []
    for r_idx, row in enumerate(restored):
        out_row = list(row)
        if cleaned:
            for c_idx, val in enumerate(out_row):
                nv = _norm_cell(val)
                if not nv or nv.isdigit() or len(nv) < 2:
                    continue
                prev_row = cleaned[-1]
                if c_idx < len(prev_row) and _norm_cell(prev_row[c_idx]) == nv:
                    merged.append({"row": r_idx, "col": c_idx, "row_span": 2, "col_span": 1, "value": val})
                    out_row[c_idx] = ""
        cleaned.append(out_row)
    return restored, merged


def _detect_two_level_header(rows: list[list[str]]) -> tuple[int, list[list[str]]]:
    """返回 (表头行数, header_rows)。次行能归入首行类别（首行有重复/空槽）时视为两级。"""
    if len(rows) < 2:
        return min(len(rows), 1), rows[:1]
    r0, r1 = rows[0], rows[1]
    if not (_is_header_like(r0) and _is_header_like(r1)):
        return 1, rows[:1]
    n0 = [_norm_cell(c) for c in r0]
    vals = [v for v in n0 if v]
    has_dup = len(vals) != len(set(vals))  # 横向合并的类别行
    r1_cells = [_norm_cell(c) for c in r1]
    r1_short = all(len(c) <= 14 for c in r1_cells if c)
    if (has_dup or any(not v for v in n0)) and r1_short:
        return 2, [r0, r1]
    return 1, rows[:1]


def _expand_two_level(header_rows: list[list[str]]) -> list[list[str]]:
    """两级表头展开为等宽（供 markdown 输出时上级留空表示合并）。"""
    if len(header_rows) < 2:
        return header_rows
    width = max(len(r) for r in header_rows)
    out = []
    for r in header_rows:
        out.append(list(r) + [""] * (width - len(r)))
    return out


def build_table_object(
    flat_table: dict,
    table_id: str,
    caption: str = "",
    max_header_rows: int = 2,
) -> TableObject:
    """flat_table: {page, headers, rows} 或 docx 的 {index, headers, rows}。"""
    page = int(flat_table.get("page", flat_table.get("index", 0)) or 0)
    headers_raw = flat_table.get("headers", []) or []
    if headers_raw and isinstance(headers_raw[0], (list, tuple)):
        headers = [list(map(str, h)) for h in headers_raw]
    else:
        headers = [list(map(str, headers_raw))] if headers_raw else []
    rows = [list(map(str, r)) for r in flat_table.get("rows", [])]

    header_count = 1
    header_rows: list[list[str]] = []
    if max_header_rows >= 2 and len(rows) >= 2 and _is_header_like(rows[0]) and _is_header_like(rows[1]):
        # 原 headers 行 + 首个 header-like 数据行 → 尝试两级
        cand = headers + [rows[0]]
        header_count, header_rows = _detect_two_level_header(cand)
        if header_count == 2:
            rows = rows[1:]
        else:
            header_rows = headers
    else:
        header_rows = headers

    all_rows = header_rows + rows
    all_rows, merged = _restore_merged(all_rows)
    header_rows = all_rows[:header_count]
    rows = all_rows[header_count:]
    header_rows = _expand_two_level(header_rows)

    return TableObject(
        table_id=table_id,
        caption=caption,
        header_rows=header_rows,
        rows=rows,
        page_start=page,
        page_end=page,
        region_index=int(flat_table.get("region_index", flat_table.get("index", 0)) or 0),
        merged_cells=merged,
        header_level=header_count,
    )


def _headers_compatible(prev: TableObject, nxt: TableObject) -> bool:
    h1 = " ".join(_norm_cell(c) for r in prev.header_rows for c in r)
    h2 = " ".join(_norm_cell(c) for r in nxt.header_rows for c in r)
    if not h2:
        return True  # 续页表常无表头
    ratio = SequenceMatcher(None, h1, h2).ratio()
    return ratio >= 0.6


def merge_cross_page_tables(tables: list[TableObject]) -> list[TableObject]:
    """相邻表（页码连续且后表在前表之后）满足续表条件则合并。"""
    if not tables:
        return []
    merged: list[TableObject] = [tables[0]]
    for nxt in tables[1:]:
        prev = merged[-1]
        pages_contiguous = 0 < nxt.page_start - prev.page_end <= 1
        if not pages_contiguous:
            merged.append(nxt)
            continue
        marker = any(mk in nxt.caption for mk in CONTINUATION_MARKERS) if nxt.caption else False
        nxt_width = max((len(r) for r in nxt.header_rows + nxt.rows), default=0)
        prev_width = max((len(r) for r in prev.header_rows + prev.rows), default=-1)
        same_width = nxt_width == prev_width
        if (marker or _headers_compatible(prev, nxt)) and same_width:
            prev.rows.extend(nxt.rows)
            prev.page_end = max(prev.page_end, nxt.page_end)
            prev.merged_from_pages.append(nxt.page_start)
            if not prev.caption and nxt.caption:
                prev.caption = nxt.caption
        else:
            merged.append(nxt)
    return merged


def attach_captions(tables: list[TableObject], blocks: list[dict]) -> None:
    """用版面标题块（"表X…"/"附表…"/"…表"）为同页表格就近配标题。"""
    title_blocks = [
        b for b in blocks
        if b.get("type") == "title" and re.search(r"(表\s*\d+|附表|一览表|评分表|评审表|清单)", b.get("text", ""))
    ]
    used: set[str] = set()
    for tb in tables:
        candidates = [
            b for b in title_blocks
            if (b.get("page") or 0) in (tb.page_start, tb.page_start - 1, 0) and b["text"] not in used
        ]
        if candidates:
            tb.caption = candidates[-1]["text"][:80]
            used.add(tb.caption)


def tables_to_markdown_inplace(text: str, tables: list[TableObject]) -> str:
    """markdown 嵌回：按表 caption/标题行在原文中定位后插入 markdown 表格（保序追加）。"""
    parts = [text]
    for tb in tables:
        md = tb.to_markdown()
        head = f"\n\n[表格 {tb.table_id} p{tb.page_start}]"
        if tb.caption:
            head += f" {tb.caption}"
        parts.append(head + f"\n{md}")
    return "\n".join(parts) if len(parts) > 1 else text


def structure_tables(flat_tables: list[dict], blocks: list[dict] | None = None) -> list[TableObject]:
    """主入口：平面表 → 表格对象（含跨页合并与标题匹配）。"""
    objs = [build_table_object(ft, f"t{i:03d}") for i, ft in enumerate(flat_tables)]
    objs = merge_cross_page_tables(objs)
    if blocks:
        attach_captions(objs, blocks)
    return objs
