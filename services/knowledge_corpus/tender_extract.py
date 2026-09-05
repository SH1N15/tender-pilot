# -*- coding: utf-8 -*-
"""P-B 废标/否决投标条目与评分办法样本提取（B1）。

来源：本地真实招标文件（D:/AgentProject/.test/ 下 4 个项目，docx 解析）。
- 废标条目：P-A chunker 标记「废标」的 chunk，再按编号拆成逐条结构化条目（逐字引用原文，不改写）；
- 评分办法样本：按「评分/评标办法」章节切出的独立章节文本，每章为一个样本；
- 输出：data/corpus_raw/bid_rejection/entries.json 与 scoring_methods/samples.json，逐条带 source 引用。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from core.doc_engine.chunker import ChunkConfig, chunk_document
from core.doc_engine.parsers.docx_parser import DocxParser

TENDER_DIRS = [
    r"D:\AgentProject\.test\南方医科大学珠江医院能谱CT采购项目公开招标公告",
    r"D:\AgentProject\.test\南方医科大学第五附属医院HIS新系统升级改造项目招标文件（2026082801）",
    r"D:\AgentProject\.test\惠东县安墩镇城镇老旧街区改造项目（重要材料）招标文件（2026082809）",
    r"D:\AgentProject\.test\清华附中湾区学校功能室设施设备-智慧城校区2026年物理信息通用设备采购项目招标文件（2026083002）",
]

REJECTION_DIR = Path("data/corpus_raw/bid_rejection")
SCORING_DIR = Path("data/corpus_raw/scoring_methods")

ITEM_SPLIT = re.compile(r"(?=（[一二三四五六七八九十]+）|(?<!\d)\d+[\.．、]\d+|^\d+[\.．、])")
REJECT_HINT = r"废标|无效投标|否决投标|拒绝投标|视为无效|不予受理|投标无效|中标无效"
CLAUSE_START = re.compile(r"^(第[一二三四五六七八九十百千]+条|\d+[\.．]\d+|（\d+）|\(\d+\)|\d+[\.．、](?!\d))")


def parse_tender_text(path: Path) -> str:
    parsed = DocxParser().parse(str(path))
    return parsed.text


def extract_entries(text: str, source_doc: str, project: str) -> list[dict]:
    result = chunk_document(text, project_id=project, config=ChunkConfig())
    entries: list[dict] = []
    for ch in result["chunks"]:
        if "废标" not in ch["tags"]:
            continue
        # 逐条拆分：先按编号/（一）等模式，再把长段按句拆，保证条目粒度可溯源
        pieces = [p.strip() for p in ITEM_SPLIT.split(ch["text"]) if p and p.strip()]
        merged: list[str] = []
        buf = ""
        for piece in pieces:
            if len(piece) < 60 and buf and not re.search(REJECT_HINT, buf):
                buf += piece
            else:
                if buf:
                    # 长段含多种情形时按句拆为独立条目
                    if len(buf) > 260:
                        sentences = [s.strip() for s in re.split(r"(?<=[。；;])", buf) if s.strip()]
                        if len(sentences) > 1:
                            merged.extend(sentences)
                            buf = ""
                            continue
                    merged.append(buf)
                buf = piece
        if buf:
            merged.append(buf)
        for piece in merged:
            if re.search(REJECT_HINT, piece):
                entries.append(
                    {
                        "text": piece,
                        "source_doc": source_doc,
                        "project": project,
                        "level_path": ch["level_path"],
                        "clause_no": ch["clause_no"],
                        "page": ch["page"],
                        "quote_source": (f"{source_doc} {ch['level_path'][-1] if ch['level_path'] else ''} "
                                         f"第{ch['clause_no']}条") if ch["clause_no"] else source_doc,
                        "extracted_at": time.strftime("%Y-%m-%d"),
                    }
                )
    return entries


def extract_scoring_sections(text: str, source_doc: str, project: str) -> list[dict]:
    """评分办法样本：以「评分办法/评标办法」标题起的连续章节文本为一个样本。"""
    samples: list[dict] = []
    result = chunk_document(text, project_id=project, config=ChunkConfig(chunk_size=1600))
    scoring_chunks = [
        ch
        for ch in result["chunks"]
        if ("评分" in ch["tags"] or re.search(r"评标办法|评分细则|评分标准", ch["text"][:200]))
    ]
    if scoring_chunks:
        joined = "\n".join(ch["text"] for ch in scoring_chunks)
        samples.append(
            {
                "sample_id": "",
                "project": project,
                "source_doc": source_doc,
                "section": next((ch["level_path"][0] for ch in scoring_chunks if ch["level_path"]), "评标办法"),
                "text": joined[:30000],
                "char_count": len(joined),
                "extracted_at": time.strftime("%Y-%m-%d"),
            }
        )
    return samples


def extract_raw_entries(text: str, source_doc: str, project: str) -> list[dict]:
    """全文条款级扫描兜底：不经 chunker，直接按「第X条 / （一） / 数字编号」切全文，
    保留所有含否决情形的独立条目（含表格行文本），逐字引用。"""
    out: list[dict] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    buf = ""
    items: list[str] = []

    def flush():
        nonlocal buf
        if buf.strip():
            items.append(buf.strip())
        buf = ""

    for ln in lines:
        if CLAUSE_START.match(ln):
            flush()
            buf = ln
        elif re.match(r"^（[一二三四五六七八九十]+）", ln) and len(ln) < 200:
            flush()
            buf = ln
        else:
            # 表格行（tab/多空格分隔的短行）视为独立条目候选
            if len(ln) < 60 and re.search(REJECT_HINT, ln) and buf and len(buf) > 300:
                flush()
                buf = ln
            else:
                buf = (buf + "\n" + ln) if buf else ln
            if len(buf) > 800:
                flush()
    flush()

    for it in items:
        if not re.search(REJECT_HINT, it):
            continue
        if len(it) > 700:
            # 超长块再按句拆，仅保留含否决情形的句/段（每句独立成条）
            sentences = [s.strip() for s in re.split(r"(?<=[。；;])", it) if s.strip()]
            pieces = [s for s in sentences if re.search(REJECT_HINT, s) and len(s) >= 20]
            if not pieces:
                continue
        else:
            pieces = [it]
        for piece in pieces:
            m = CLAUSE_START.match(piece)
            out.append(
                {
                    "text": piece[:1500],
                    "source_doc": source_doc,
                    "project": project,
                    "level_path": [],
                    "clause_no": m.group(0) if m else "",
                    "page": 0,
                    "quote_source": f"{source_doc}" + (f" {m.group(0)}" if m else ""),
                    "extracted_at": time.strftime("%Y-%m-%d"),
                    "extractor": "raw_scan",
                }
            )
    return out


def extract_law_rejection_entries() -> list[dict]:
    """从已采集法规全文中提取含否决/无效/废标情形的「第X条」原文条目（真实法规条款）。"""
    laws_dir = Path("data/corpus_raw/laws")
    out: list[dict] = []
    for meta_path in sorted(laws_dir.glob("*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        txt_path = laws_dir / (meta_path.name[: -len(".meta.json")] + ".txt")
        if not txt_path.exists():
            continue
        text = txt_path.read_text(encoding="utf-8", errors="ignore")
        # 抽取「第X条」原文（到下一个「第X条」为止），保留含否决情形的条款
        articles = re.split(r"(?=第[一二三四五六七八九十百千]+条)", text)
        for art in articles:
            if not art.strip() or len(art) > 1200 or len(art) < 20:
                continue
            if re.search(REJECT_HINT, art):
                sub_items = [p.strip() for p in re.split(r"(?=（[一二三四五六七八九十]+）)", art) if p.strip()]
                pieces = sub_items if len(sub_items) > 1 else [art.strip().replace("\n", " ")]
                for piece in pieces:
                    if not re.search(REJECT_HINT, piece):
                        continue
                    m = re.match(r"第[一二三四五六七八九十百千]+条", piece)
                    out.append(
                        {
                            "text": piece.replace("\n", " ")[:1500],
                            "source_doc": meta["name"],
                            "project": meta["name"],
                            "level_path": [],
                            "clause_no": m.group(0) if m else "",
                            "page": 0,
                            "quote_source": f"{meta['name']}（{meta['issuer']}，{meta['effective_date']}施行）"
                                f"来源：{meta['source_url']}",
                            "extracted_at": time.strftime("%Y-%m-%d"),
                        }
                    )
    return out


def main() -> None:
    REJECTION_DIR.mkdir(parents=True, exist_ok=True)
    SCORING_DIR.mkdir(parents=True, exist_ok=True)
    all_entries: list[dict] = []
    all_samples: list[dict] = []
    for d in TENDER_DIRS:
        dp = Path(d)
        project = dp.name
        docx_files = sorted(dp.glob("*.docx"))
        if not docx_files:
            print(f"[skip] no docx: {project}")
            continue
        docx = docx_files[0]
        print(f"parsing {docx.name} ...")
        text = parse_tender_text(docx)
        entries = extract_entries(text, docx.name, project)
        samples = extract_scoring_sections(text, docx.name, project)
        # 全文条款级兜底扫描，去重后并入
        seen_prefix = {e["text"][:60] for e in entries}
        for re_ in extract_raw_entries(text, docx.name, project):
            if re_["text"][:60] not in seen_prefix:
                entries.append(re_)
                seen_prefix.add(re_["text"][:60])
        for s in samples:
            s["sample_id"] = f"SM-{len(all_samples) + 1:02d}"
        all_entries.extend(entries)
        all_samples.extend(samples)
        print(f"  -> {len(entries)} rejection entries, {len(samples)} scoring samples")
    (REJECTION_DIR / "entries.json").write_text(
        json.dumps({"total": len(all_entries), "entries": all_entries}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    (SCORING_DIR / "samples.json").write_text(
        json.dumps({"total": len(all_samples), "samples": all_samples}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    # 法规条款级废标条目
    law_entries = extract_law_rejection_entries()
    (REJECTION_DIR / "law_entries.json").write_text(
        json.dumps({"total": len(law_entries), "entries": law_entries}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(
        f"TOTAL rejection entries: {len(all_entries)} (tender) + {len(law_entries)} (law), "
        f"scoring samples: {len(all_samples)}"
    )


if __name__ == "__main__":
    main()
