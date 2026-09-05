"""G-6 T1: check findings -> bounded chapter repair -> focused recheck helpers."""

from __future__ import annotations

import re
from typing import Awaitable, Callable

REPAIRABLE_STATUSES = {"fail", "warning"}

# 纯编号/章节号（如 "1"、"3.2.1"）不能作为标题匹配依据，防误映射
_SECTION_NO_RE = re.compile(r"^[\d.、（）()\-]+$")

_CHECK_CHAPTER_HINTS: dict[str, tuple[str, ...]] = {
    "pricing_check": ("报价", "价格", "商务"),
    "pricing_logic_check": ("报价", "价格", "商务", "费用"),
    "doc_integrity_check": ("目录", "附件", "文件", "资格"),
    "ebid_submit_check": ("投标函", "递交", "电子", "附件", "资格"),
    "signature_check": ("签字", "签章", "授权", "法定代表人"),
    "qualification_check": ("资格", "资质", "证明", "营业执照"),
    "compliance_check": ("资格", "合规", "证明", "承诺"),
    "mandatory_req_check": ("技术", "参数", "响应", "偏离"),
    "disqualification_check": ("废标", "偏离", "商务", "资格"),
}


def _match_chapter_title(text: str, chapter_titles: dict[str, str]) -> str | None:
    """确定性章节映射：finding 文本中最长命中的章节标题（≥2 字符且非纯编号）。"""
    best: str | None = None
    for chapter_id, title in (chapter_titles or {}).items():
        t = str(title or "").strip()
        if len(t) < 2 or _SECTION_NO_RE.match(t):
            continue
        if t in text and (best is None or len(t) > len(str(chapter_titles[best]))):
            best = chapter_id
    return best


def _fallback_chapter(check_id: str, text: str, chapter_titles: dict[str, str]) -> str | None:
    """Choose a semantic chapter when a checker omits chapter_id.

    This keeps structural findings repairable without depending on one tender's
    numeric chapter layout.  Existing exact-title matching still wins.
    """
    hints = _CHECK_CHAPTER_HINTS.get(check_id, ())
    if not hints:
        return None
    scored: list[tuple[int, int, str]] = []
    for index, (chapter_id, title) in enumerate((chapter_titles or {}).items()):
        value = str(title or "").strip()
        if len(value) < 2 or _SECTION_NO_RE.match(value):
            continue
        score = sum(value.count(hint) * 2 for hint in hints)
        score += sum(str(text or "").count(hint) for hint in hints)
        if score:
            scored.append((score, -index, str(chapter_id)))
    return max(scored)[2] if scored else None


def _finding_text(finding: dict) -> str:
    parts = [
        finding.get("detail"),
        finding.get("reason"),
        finding.get("tender_basis"),
        finding.get("suggestion"),
        finding.get("location_a"),
        finding.get("location_b"),
        finding.get("title"),
        finding.get("check_name"),
    ]
    return " ".join(str(p) for p in parts if p)


def extract_repair_queue(
    results: list[dict] | None,
    *,
    chapter_titles: dict[str, str] | None = None,
    max_tasks: int | None = None,
) -> list[dict]:
    """Extract only chapter-addressable fail/warning findings.

    Structural/error findings remain report-only because they do not identify a
    safe chapter rewrite target. 按章节映射（确定性，无 LLM）：finding 自带 chapter_id
    优先；否则用项目章节标题在 finding 文本中最长命中定位（location/detail 等字段）。
    """
    by_key: dict[tuple, dict] = {}
    for item in results or []:
        if not isinstance(item, dict) or item.get("status") not in REPAIRABLE_STATUSES:
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        checks = data.get("checks") if isinstance(data.get("checks"), list) else [item]
        for finding in checks:
            if not isinstance(finding, dict) or finding.get("status", item.get("status")) not in REPAIRABLE_STATUSES:
                continue
            chapter_id = finding.get("chapter_id") or item.get("chapter_id") or data.get("chapter_id")
            if not chapter_id and chapter_titles:
                chapter_id = _match_chapter_title(_finding_text(finding), chapter_titles)
            if not chapter_id and chapter_titles:
                chapter_id = _fallback_chapter(str(item.get("check_id") or ""), _finding_text(finding), chapter_titles)
            if not chapter_id:
                continue
            finding_text = str(
                finding.get("detail")
                or finding.get("reason")
                or item.get("reason")
                or _finding_text(finding)[:300]
            )
            row = {
                "task_id": f"{item.get('check_id', 'check')}:{chapter_id}",
                "check_id": item.get("check_id", ""),
                "chapter_id": str(chapter_id),
                "status_before": finding.get("status", item.get("status")),
                "finding": finding_text,
                # G7-R3：保留原始 finding 指纹（check_name+value_a/value_b），复检按
                # 指纹判定"该 finding 是否已被修复消除"，得到 finding 级解决率。
                "finding_refs": [{
                    "check_name": str(finding.get("check_name") or ""),
                    "value_a": str(finding.get("value_a") or "")[:200],
                    "value_b": str(finding.get("value_b") or "")[:200],
                }],
                "tender_basis": str(
                    finding.get("tender_basis")
                    or finding.get("tender_依据")
                    or item.get("tender_basis")
                    or item.get("依据")
                    or ""
                ),
                "suggestion": str(
                    finding.get("suggestion")
                    or finding.get("recommendation")
                    or item.get("suggestion")
                    or "按检查项修正本章节并保留可验证引用"
                ),
            }
            # G7-R3：同一章节多个 finding 合并为一个任务（文本拼接）——重写指令一次
            # 覆盖该章全部问题；否则每章只带第一条 finding，复检其余项必然仍失败。
            key = (row["check_id"], row["chapter_id"])
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = row
            else:
                if finding_text and finding_text not in existing["finding"]:
                    existing["finding"] = f"{existing['finding']}\n- {finding_text}"[:2000]
                if row["tender_basis"] and row["tender_basis"] not in existing["tender_basis"]:
                    existing["tender_basis"] = f"{existing['tender_basis']}\n{row['tender_basis']}"[:800]
                if row["suggestion"] and row["suggestion"] not in existing["suggestion"]:
                    existing["suggestion"] = f"{existing['suggestion']}\n- {row['suggestion']}"[:1200]
                existing.setdefault("finding_refs", []).extend(row["finding_refs"])
    rows = list(by_key.values())
    return rows if max_tasks is None else rows[: max(0, int(max_tasks))]


async def run_repair_queue(
    queue: list[dict],
    repair: Callable[[dict], Awaitable[dict]],
    recheck: Callable[[dict], Awaitable[dict]],
) -> dict:
    """Run at most one repair round and attach before/after numeric evidence."""
    repaired: list[dict] = []
    for task in queue:
        result = await repair(task)
        after = await recheck({**task, "repair": result})
        repaired.append(
            {
                **task,
                "repair": result,
                "recheck": after,
                "status_after": after.get("status", "error"),
                "fixed": after.get("status") == "pass",
            }
        )
    fixed = sum(1 for row in repaired if row["fixed"])
    return {
        "tasks": repaired,
        "total": len(repaired),
        "fixed": fixed,
        "recheck_pass_rate": (fixed / len(repaired)) if repaired else 1.0,
    }


__all__ = ["extract_repair_queue", "run_repair_queue", "REPAIRABLE_STATUSES"]
