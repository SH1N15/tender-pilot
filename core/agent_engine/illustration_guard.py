"""G-7 终验否决修复①：AI 配图脚手架剥离（确定性，无 LLM）。

正文中的 `[插图位置…]` / 光杆 `[插图位置]` 是 P3 配图功能的生成脚手架，
禁止进入最终投标正文：在 Grounding 硬门产出后、落库前确定性剥离，
剥离出的建议语作为侧栏元数据存 Chapter.citation_ledger["_illustrations"]。

Worker J（净化层缺陷③）：模式扩展——生成期模型不总按指令输出 [插图位置]，
EVAL_UAT_G7 实测漏网变体四类：
  a. `：此处可插入开标现场唱标记录截图或…模板图。`（裸冒号/引用块前缀：1.2.1/3.3）
  b. `[此处插入信用中国查询结果截图]`（方括号变体：2.3/2.3.2/11.1.2）
  c. `*：建议插入"进度偏差预警流程图"…* / *建议插入图片：…*`（建议插入句：8.3.2/9.1.2/11.1）
  d. `**建议配图描述**：` + 后续编号列表整块（7.1.2/10.3.2/1.3.2/2.2.1/6.4.3）
扩展入口 = strip_illustration_scaffold_extended（生成期门节点与导出净化层共用）；
原 strip_illustration_scaffold 行为逐字节不变（回归基线），新变体全部转存
建议语列表，与既有 `_illustrations` 侧栏元数据机制一致。
"""

from __future__ import annotations

import re

# 匹配 [插图位置] 与 [插图位置：…] / [插图位置:…]（含全角冒号与跨字符建议文本）
_ILLUSTRATION_RE = re.compile(r"\[插图位置[：:]?[^\]]*\]")

# G-7 终验二轮：建议配图语（*建议配图：xxx图…* / 【建议配图】… 等）同为脚手架
_ILLUSTRATION_RE2 = re.compile(r"\*{0,2}[【\[]?建议配图[】\]]?[：:][^\n\*]{0,120}\*{0,2}")

# 整行只剩脚手架与空白/分隔符时删除整行（含行尾换行），避免留下空行骨架
_LINE_ONLY_RE = re.compile(
    r"^[\s\-*|>#]*(?:\[插图位置[：:]?[^\]]*\]|(?:【\[]?建议配图[】\]]?[：:][^\n\*]{0,120}\*{0,2}))[\s\-*|:：]*$"
)

# ── Worker J 缺陷③：扩展变体 ───────────────────────────────────────────────

# b. 方括号「[此处插入…]」变体（先于 a 收集，防嵌套重复）
_BRACKET_INSERT_RE = re.compile(r"\[[^\[\]\n]{0,6}(?:此处)?插入[^\[\]\n]{0,160}\]")

# a. 「此处（可）插入」引导的建议句（允许前置裸冒号/引用块/斜体星号，句号、
#    分号收尾，尾随闭合格式星号一并吃掉）。必须带「此处」前缀——"可插入"
#    单独出现不剥（技术正文高频"数据插入/可插入式安装"会误伤）。
_INSERT_HERE_RE = re.compile(
    r"\*{0,2}[：:]?\s*[（(［[]?\s*此处\s*(?:可)?\s*插入\s*(?:图片)?\s*[：:]?[^\n*]{0,160}?"
    r"(?:[。；]\*{0,2}|[）)]\*{0,2}(?=\n|$)|(?=\n|$))",
    re.M,
)

# c. 「建议插入（图片）」引导的建议句
_SUGGEST_INSERT_RE = re.compile(
    r"\*{0,2}[：:]?\s*[（(［[]?\s*建议插入\s*(?:图片)?\s*[：:]?[^\n*]{0,160}?"
    r"(?:[。；]\*{0,2}|[）)]\*{0,2}(?=\n|$)|(?=\n|$))",
    re.M,
)

# d. 「建议配图描述」引导块：标题行 + 其后的编号/项目符号列表行
#    （兼容 `**建议配图描述**：` 与 `建议配图描述：**` 两种强调位）
_SUGGESTION_HEADER_LINE_RE = re.compile(
    r"^\s*[*#>\-\s]*[{［[]?\*{0,2}建议配图描述\*{0,2}[】\]]?[：:]\*{0,2}\s*$"
)
_SUGGESTION_INLINE_RE = re.compile(
    r"^\s*[*#>\-\s]*[{［[]?\*{0,2}建议配图描述[】\]]?[：:]"
)
# 建议块延续行：编号列表 / 项目符号 / 引用块内同段（不含【待补充】等硬事实降级标记）
_LIST_LIKE_RE = re.compile(r"^\s*(?:\*{0,2}\d+[\.、）\)]|\*{1,2}[\s*])|^\s*[*\-•]\s")

# 行中「建议配图描述」标题碎片（非行首形态，如上下文拼接后
# "…投标保证金。 **建议配图描述：**"——d 块/行首内联两条路都不命中，全局兜底）
_SUGGESTION_HEADER_FRAG_RE = re.compile(
    r"[\*\s]*[{［[]?\*{0,2}建议配图描述\*{0,2}[】\]]?[\*\s]*[：:][\*\s]*"
)


def _combined_re() -> re.Pattern:
    return re.compile(_ILLUSTRATION_RE.pattern + "|" + _ILLUSTRATION_RE2.pattern)


def strip_illustration_scaffold(text: str) -> tuple[str, list[str]]:
    """从正文中剥离配图脚手架（经典形态，行为与 G-7 修复逐字节一致）。

    返回 (清洗后正文, 建议语列表)。
    """
    source = str(text or "")
    combined = _combined_re()
    if not (source and combined.search(source)):
        return source, []
    suggestions = [m.group(0) for m in combined.finditer(source)]
    cleaned_lines: list[str] = []
    for line in source.split("\n"):
        if _LINE_ONLY_RE.match(line):
            continue  # 整行脚手架：整行删除
        cleaned_lines.append(combined.sub("", line))
    cleaned = "\n".join(cleaned_lines)
    # 收敛剥离留下的连续空行（>2 → 1）
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, suggestions


def strip_illustration_scaffold_extended(text: str) -> tuple[str, list[str]]:
    """Worker J：含全部漏网变体（a/b/c/d）的剥离。

    顺序：先整块剥离「建议配图描述」标题+列表块（d，防止被逐行正则切碎），
    再复用经典剥离（[插图位置]/建议配图），最后剥离 b/a/c 行内与整行形态。
    所有命中建议语并入返回列表（供侧栏元数据转存）。
    """
    source = str(text or "")
    if not source:
        return source, []
    suggestions: list[str] = []

    # ① d：「建议配图描述」块（标题行 + 后续列表行；空行后遇正文停止吞并）
    lines = source.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _SUGGESTION_HEADER_LINE_RE.match(line):
            block = [line.strip()]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip():
                    # 允许块内空行，但块后空行若下一行非列表则终止
                    if j + 1 < len(lines) and _LIST_LIKE_RE.match(lines[j + 1]):
                        j += 1
                        continue
                    break
                if _LIST_LIKE_RE.match(nxt) and not nxt.lstrip().startswith("【"):
                    block.append(nxt.strip())
                    j += 1
                    continue
                break
            suggestions.append(" ".join(block))
            i = j
            continue
        if _SUGGESTION_INLINE_RE.match(line):
            # 同行冒号后有内容（如「建议配图描述：插入…扫描件」）→ 整行收入并剥离
            suggestions.append(line.strip())
            i += 1
            continue
        out.append(line)
        i += 1
    text1 = "\n".join(out)

    # ② 经典形态（[插图位置…] / 建议配图：…）
    text2, legacy = strip_illustration_scaffold(text1)
    suggestions.extend(legacy)

    # ③ b → a → c
    def _sub_and_collect(pattern: re.Pattern, s: str) -> str:
        found = pattern.findall(s)
        if found:
            suggestions.extend(found)
            return pattern.sub("", s)
        return s

    text3 = _sub_and_collect(_BRACKET_INSERT_RE, text2)
    text3 = _sub_and_collect(_INSERT_HERE_RE, text3)
    text3 = _sub_and_collect(_SUGGEST_INSERT_RE, text3)
    # 行中「建议配图描述」碎片（上下文拼接产物，如清单 detail 里
    # "…投标保证金。 **建议配图描述：**（待补充）"）——非行首形态全局兜底剥离，
    # 行首形态已由 d 块/行内两条路处理，跳过行首避免与块路径重复计数。
    frag_lines: list[str] = []
    for line in text3.split("\n"):
        stripped = line.lstrip()
        if "建议配图描述" in line and not _SUGGESTION_INLINE_RE.match(line):
            indent = line[: len(line) - len(stripped)]
            new_indent = _SUGGESTION_HEADER_FRAG_RE.sub("", indent)
            new_body, n_frag = _SUGGESTION_HEADER_FRAG_RE.subn("", stripped)
            if n_frag:
                suggestions.extend(["建议配图描述"] * n_frag)
                frag_lines.append(new_indent + new_body)
                continue
        frag_lines.append(line)
    text3 = "\n".join(frag_lines)

    # 清理剥离后残留的整行空壳（"> ："、"*：*"、"："之类纯记号行；
    # 不含 | 与 -，避免误伤表格分隔行）
    text3 = re.sub(r"^\s*[>#＃＊*:：\s]+$", "", text3, flags=re.M)
    text3 = re.sub(r"\n{3,}", "\n\n", text3)
    return text3, suggestions


def scan_illustration_scaffold(text: str) -> int:
    """四类扫描用：统计正文中残留的插图脚手架数量（含 Worker J 扩展变体）。"""
    cleaned, removed = strip_illustration_scaffold_extended(text)
    del cleaned
    return len(removed)


__all__ = [
    "strip_illustration_scaffold",
    "strip_illustration_scaffold_extended",
    "scan_illustration_scaffold",
]
