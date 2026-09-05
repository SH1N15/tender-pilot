"""Worker J（G-7 终验否决修复·导出净化层）：交付物统一净化。

背景：数据库 chapters.content 保留【n】引用锚点与硬门拒收原因（溯源能力），
但导出物（DOCX/HTML/MD）是给评标委员会看的投标文件，不得携带生成期内部痕迹。
实测缺陷（EVAL_UAT_G7，176 章）：
  ① 锚点泄漏：【1】×612、【2】×298…共 1732 处纯数字锚点 + 【引用n】×217 +
     点分【4.4.3】×15 + 指令回显【N】；真实投标文件不存在；
  ② 硬门拒收原因整段入正文：`【待补充】(原因：…；待补充：90 天)` 29 处（含
     嵌套双写），内部拒收逻辑不该给用户看，且格内其实已有提取值；
     `（知识库无据，待补充）` 43 处——"知识库"是系统内部概念；
  ③ 插图建议变体漏网：`:此处可插入…`/`[此处插入…]`/`建议插入…`/
     `**建议配图描述**：+列表块`（illustration_guard 扩展，本模块复用）；
  ④ 占位符未填：`[采购人名称]`/`[招标人名称]` 在解读结果有据时按解读口径回填。

口径（确定性、幂等、无 LLM）：
- 【数字】/【引用数字】/【数字.数字…】/【N】 → 删除（锚点族全部剥离；
  文档自有的「【重要】」「[2011]300号」等非纯数字/方括号形态不动）；
- 【待补充】(原因：R；待补充：V) → 有提取值 V → `V（待确认）`；无 V → `（待补充）`；
  嵌套重复块合并为一次输出；
- （知识库无据，待补充[:H]） → `（待补充[:H]）`；H 含"知识库/检索/生成"等内部
  口径时丢提示只留（待补充）；句中 `,知识库无据` 尾巴单独清除；
- 插图建议（含扩展变体）→ 剥离（导出层丢弃；生成期门节点已负责转存 _illustrations 元数据）；
- 裸 JSON 元数据泄漏块（`sources:`/`illustration_suggestions:` + 数组行）→ 删除；
- fill_map（采购人/招标人等）→ 回填 [采购人名称] 类占位符。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.agent_engine.illustration_guard import strip_illustration_scaffold_extended

logger = logging.getLogger(__name__)

# ── ① 引用锚点族 ───────────────────────────────────────────────────────────
# 纯数字【1】【12】【123】；引用变体【引用1】；点分【4.4.3】（模型把章节号写进
# 锚点形态）；指令回显【N】。【2011】这类 4 位年份锚点不剥（正文里多为真实年度
# 强调，实测仅 1 处且无锚点语境；年份政策号本来就用方括号 [2011] 不在此列）。
_ANCHOR_RE = re.compile(
    r"【(?:(?:引用)?\d+(?:\.\d+)+|引用\s*\d{1,3}|\d{1,3}|N)】"
)

# ── ② 硬门拒收块 / 无据口径 ────────────────────────────────────────────────
# 块首（与 evidence_gate/grounding_hard_gate 的替换语义同源）：
_PENDING_BLOCK_START_RE = re.compile(r"【待补充】\s*[（(]\s*原因[：:]")
# 块内提取值：…；待补充：V（V 到块尾）
_PENDING_VALUE_RE = re.compile(r"待补充[：:]\s*([^；;)）]{1,60})\s*$")
# 裸【待补充】（无原因块）
_PENDING_BARE_RE = re.compile(r"【待补充】")
# （知识库无据，待补充[:提示]）——提示含内部口径词则整体丢提示
_KB_PENDING_RE = re.compile(r"[（(]\s*知识库无据[，,]\s*待补充(?:[：:]([^（）()\n]{0,80}))?\s*[）)]")
_KB_JARGON_HINT_RE = re.compile(r"知识库|检索|生成|参考材料|资料后")
# 句中"知识库无据，待补充"（无独立括号包裹，如"…进一步确认，知识库无据，待补充）。"）
_KB_INLINE_RE = re.compile(r"知识库无据[，,]\s*(?=待补充)")
# 残余"（…，知识库无据）"等裸口径（含被引号包裹的转述形态"‘知识库无据’"）
_KB_TAIL_RE = re.compile(r"[，,、]?\s*[“\"'‘]?知识库无据[”\"'’]?\s*[，,]?")
_INTERNAL_SOURCE_RE = re.compile(r"(?:证据库|知识库|来源注释|内部来源)\s*[：:]\s*[^\n。；;]+")
_TEXT_ATTACHMENT_RE = re.compile(r"(?i)(?<![A-Za-z0-9])[^\s，。；;（）()]+\.txt\b")

# ── ④ 占位符回填 ──────────────────────────────────────────────────────────
_PLACEHOLDER_KEYS = ("采购人名称", "招标人名称", "甲方名称", "采购单位", "建设单位", "需求单位")

# ── 附加：生成期裸 JSON 元数据泄漏块 ───────────────────────────────────────
_META_LEAK_HEAD_RE = re.compile(
    r"^\s*[\"“”]?(?:sources|illustration_suggestions|citation_ledger|word_count)[\"“”]?\s*[:：]\s*\[?\s*$"
)
_META_LEAK_ITEM_RE = re.compile(r"^\s*(?:[\[\]，,、\s*]*|[\"“”][^\"“”]*[\"“”]?\s*,?\s*)$")
_META_LEAK_CLOSE_RE = re.compile(r"^\s*\]\s*[,，]?\s*$")


def build_export_fill_map(analysis_dimensions: dict | None) -> dict[str, str]:
    """从解读结果 dimensions 构建导出占位符回填表（当前：采购人/招标人系）。

    优先级 buyer_info.unit_name > project_info.procurement_unit > project_info.buyer_name。
    无据返回 {}（占位符原样保留——投标人名称等属企业侧数据源问题，不在本层）。
    """
    dims = analysis_dimensions if isinstance(analysis_dimensions, dict) else {}
    buyer = dims.get("buyer_info") if isinstance(dims.get("buyer_info"), dict) else {}
    pinfo = dims.get("project_info") if isinstance(dims.get("project_info"), dict) else {}
    name = ""
    for src, key in ((buyer, "unit_name"), (buyer, "name"), (pinfo, "procurement_unit"), (pinfo, "buyer_name")):
        val = str(src.get(key) or "").strip()
        if val:
            name = val
            break
    if not name:
        return {}
    return {k: name for k in _PLACEHOLDER_KEYS}


def _strip_pending_blocks(text: str) -> tuple[str, int]:
    """剥离 【待补充】(原因：…) 块（含嵌套双写），保提取值。返回 (文本, 次数)。"""
    n = 0
    while True:
        m = _PENDING_BLOCK_START_RE.search(text)
        if not m:
            break
        start = m.start()
        # 块体扫到第一个 ASCII ')'（生成器内部原因只用全角括号）；无 ')' 则到行尾
        close = text.find(")", m.end())
        eol = text.find("\n", m.end())
        if close == -1:
            end = eol if eol != -1 else len(text)
        elif eol != -1 and close > eol:
            # ')' 在下一行——本块未闭合，截到行尾（防吞后续正文）
            end = eol
        else:
            end = close + 1
        block = text[start:end]
        vm = _PENDING_VALUE_RE.search(block.rstrip("。.；;）) \n"))
        value = (vm.group(1).strip() if vm else "")
        if value and not _KB_JARGON_HINT_RE.search(value) and "重述" not in value:
            replacement = f"{value}（待确认）"
        else:
            replacement = "（待补充）"
        text = text[:start] + replacement + text[end:]
        n += 1
    # 裸【待补充】（无拒收原因块）统一为（待补充）——与压缩后的原因块同一视觉口径
    text, n_bare = _PENDING_BARE_RE.subn("（待补充）", text)
    return text, n + n_bare


def _collapse_kb_pending(text: str) -> tuple[str, int]:
    """（知识库无据，待补充[:提示]）→（待补充[:提示]）；内部口径提示丢弃。"""
    n = 0

    def _repl(m: re.Match) -> str:
        nonlocal n
        n += 1
        hint = (m.group(1) or "").strip()
        if hint and not _KB_JARGON_HINT_RE.search(hint):
            return f"（待补充：{hint}）"
        return "（待补充）"

    text = _KB_PENDING_RE.sub(_repl, text)
    # 句中"，知识库无据，待补充"（独立括号外形态）：删内部口径前缀，保留"待补充"
    text, n_inline = _KB_INLINE_RE.subn("", text)
    # 残余裸"知识库无据"口径（如"…（注：…，知识库无据）"）
    text, n2 = _KB_TAIL_RE.subn("", text)
    return text, n + n_inline + n2


def _strip_meta_leaks(text: str) -> tuple[str, int]:
    """删生成期裸 JSON 元数据泄漏块（'sources:' / 'illustration_suggestions:' + 数组行）。

    6.4.3 实证形态：
        sources:
        [
        "引用1",
        ...
        ]
    仅当头部行命中键名且其后连续为 JSON 碎片行（空/括号/带引号串）时才吞并，
    正常正文中的 "sources:" 罕见，双条件（键名行+数组体）防误删。
    """
    lines = text.split("\n")
    out: list[str] = []
    n = 0
    i = 0
    while i < len(lines):
        if _META_LEAK_HEAD_RE.match(lines[i]):
            j = i + 1
            closed = False
            while j < len(lines) and (_META_LEAK_ITEM_RE.match(lines[j]) or not lines[j].strip()):
                if _META_LEAK_CLOSE_RE.match(lines[j]):
                    closed = True
                    j += 1
                    break
                j += 1
            if closed:
                n += 1
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out), n


def sanitize_export_text(
    text: str,
    fill_map: dict[str, str] | None = None,
) -> tuple[str, dict[str, int]]:
    """导出净化主入口（确定性、幂等）。数据库原文不动，只作用于导出物文本。

    返回 (净化后文本, 计数报告)：
      anchors      剥离的引用锚点【n】族数
      pending      压缩的【待补充】(原因…) 块数（含嵌套合并）
      kb_pending   统一的（知识库无据，待补充…）数
      illustrations 剥离的插图建议脚手架数
      placeholders 回填的占位符数
      meta_leaks   删除的裸 JSON 元数据块数
    """
    source = str(text or "")
    if not source:
        return source, {}
    report: dict[str, int] = {}

    cleaned, n_pending = _strip_pending_blocks(source)
    cleaned, n_kb = _collapse_kb_pending(cleaned)
    cleaned, illu_suggestions = strip_illustration_scaffold_extended(cleaned)
    n_illu = len(illu_suggestions)
    cleaned, n_meta = _strip_meta_leaks(cleaned)
    cleaned, n_internal = _INTERNAL_SOURCE_RE.subn("", cleaned)
    cleaned, n_txt = _TEXT_ATTACHMENT_RE.subn("相关附件", cleaned)

    n_anchor = len(_ANCHOR_RE.findall(cleaned))
    cleaned = _ANCHOR_RE.sub("", cleaned)

    n_ph = 0
    if fill_map:
        keys = "|".join(re.escape(k) for k in sorted(fill_map, key=len, reverse=True))
        if keys:
            pat = re.compile(rf"\[({keys})\]")
            cleaned, n_ph = pat.subn(lambda m: fill_map.get(m.group(1), m.group(0)), cleaned)

    # 锚点剥离后的收尾清理：句读前悬空空格不强制处理（保守）；收敛空行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    report = {
        "anchors": n_anchor,
        "pending": n_pending,
        "kb_pending": n_kb,
        "illustrations": n_illu,
        "placeholders": n_ph,
        "meta_leaks": n_meta,
        "internal_sources": n_internal,
        "txt_attachment_names": n_txt,
    }
    return cleaned, report


def scan_residual(text: str) -> dict[str, int]:
    """负责人口径复扫：净化后文本中各类内部痕迹残留数（应全为 0）。

    pending_reason 只认硬门替换签名（【待补充】+ 括号"原因："），不误伤用户
    正文里合法的"（原因：…）"表述。
    """
    source = str(text or "")
    return {
        "anchors": len(_ANCHOR_RE.findall(source)),
        "pending_reason": len(_PENDING_BLOCK_START_RE.findall(source)),
        "kb_no_basis": len(re.findall(r"知识库无据", source)),
        "illustrations": len(re.findall(r"此处可插入|此处插入|插图位置|建议插入|建议配图", source)),
        "bracket_placeholder": len(re.findall(r"\[(?:采购人名称|招标人名称)\]", source)),
    }


def sanitize_docx_paragraphs(doc: Any, fill_map: dict[str, str] | None = None) -> dict[str, int]:
    """排版管线 DOCX（用户上传/中转文件）净化：段落+表格单元格 run 级替换。

    run 文本各自净化后，若整段文本仍命中净化规则（标记跨 run 断裂），
    以首 run 承载整段净化文本、其余 run 清空兜底。
    """
    total: dict[str, int] = {}

    def _merge(counts: dict[str, int]) -> None:
        for k, v in (counts or {}).items():
            total[k] = total.get(k, 0) + v

    def _para(p: Any) -> None:
        runs = list(getattr(p, "runs", []) or [])
        if not runs:
            return
        # run 级净化（保留格式）；随后段级复扫兜底标记跨 run 断裂
        for r in runs:
            t = r.text or ""
            if not any(v for v in scan_residual(t).values()):
                continue
            new, cnt = sanitize_export_text(t, fill_map)
            if new != t:
                r.text = new
                _merge(cnt)
        full = "".join(rr.text or "" for rr in runs)
        if any(v for v in scan_residual(full).values()):
            new, cnt = sanitize_export_text(full, fill_map)
            if new != full:
                runs[0].text = new
                for rr in runs[1:]:
                    rr.text = ""
                _merge(cnt)

    for p in doc.paragraphs:
        _para(p)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    _para(p)
    return total


__all__ = [
    "build_export_fill_map",
    "sanitize_export_text",
    "scan_residual",
    "sanitize_docx_paragraphs",
]
