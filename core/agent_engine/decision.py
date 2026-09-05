"""决策包生成（铁律3：禁纯 LLM 定级）与 HITL 超时策略（铁律4）。

定级映射表（确定性、可单测、写死）：
- 资格类硬性检查存在 fail（或 disqualification_risk=True）        -> NO_BID
- 任一检查 fail（非资格硬性）或任一 warning 或任一 disqualification 风险 -> CAUTION
- 全部 pass（或全部 skipped 且无失败证据）                        -> BID
LLM 仅生成给决策包的解释文字（rationale），不参与定级。
"""

from __future__ import annotations

import json
import time
from typing import Any

from core.agent_engine.iron_rules import (
    DEFAULT_DECISION_TIMEOUT_SECONDS,
    MANUAL_ONLY_LEVELS,
)

LEVEL_BID = "BID"
LEVEL_CAUTION = "CAUTION"
LEVEL_NO_BID = "NO_BID"

# 资格硬性检查（这些检查 fail 直接 NO_BID）
HARD_CHECK_IDS: frozenset[str] = frozenset(
    {"qualification_check", "disqualification_check", "mandatory_req_check", "validity_check"}
)


def _status_of(item: dict) -> str:
    """从规则门单项结果提取状态：pass/fail/warning/skipped。

    兼容检查 skill 的多种返回形态（checks[].status、risk_level、disqualification_risk）。
    """
    status = item.get("status")
    if status:
        return str(status).lower()
    data = item.get("data") or {}
    checks = data.get("checks") or []
    statuses = [str(c.get("status", "")).lower() for c in checks if isinstance(c, dict)]
    if "fail" in statuses:
        return "fail"
    if "warning" in statuses:
        return "warning"
    if statuses and all(s == "pass" for s in statuses):
        return "pass"
    if data.get("disqualification_risk") is True:
        return "fail"
    risk = str(data.get("risk_level", "")).lower()
    if risk == "high":
        return "fail"
    if risk == "medium":
        return "warning"
    if risk == "low":
        return "pass"
    return "skipped"


def _explicit_hard_failure(item: dict) -> bool:
    """区分“能力/实质条款不满足”和“仅缺少文件动作或检查证据”。

    检查器在只有文本、没有扫描件/平台回执时可能把 risk_level 写成 high，
    但这不等于企业没有投标能力。只有存在明确的未满足、负偏离、失效或废标
    条款证据时，才允许硬性检查把总图定为 NO_BID。
    """
    check_id = str(item.get("check_id") or "")
    data = item.get("data") or {}
    if not isinstance(data, dict):
        return True
    # 兼容单测和旧检查器：没有结构化证据时保留原有保守语义。
    if not data:
        return True
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    checks = data.get("checks") if isinstance(data.get("checks"), list) else []
    check_statuses = [str(c.get("status") or "").lower() for c in checks if isinstance(c, dict)]

    if check_id == "qualification_check":
        if not isinstance(data.get("summary"), dict):
            # 旧检查器只有一段文字摘要时，视为明确证据，保持兼容。
            return bool(str(data.get("summary") or "").strip())
        return int(summary.get("failed", 0) or 0) > 0 or int(summary.get("unmet", 0) or 0) > 0
    if check_id == "mandatory_req_check":
        if int(data.get("negative_deviation", 0) or 0) > 0:
            return True
        # “模糊/未展开”是待补正文或人工复核，不等同于明确能力不满足。
        # 只有结构化结果给出 critical non_compliant，才触发 NO_BID。
        return any(
            isinstance(item, dict)
            and str(item.get("status") or "").lower() == "non_compliant"
            and str(item.get("severity") or "").lower() == "critical"
            for item in (data.get("items") or [])
        )
    if check_id == "validity_check":
        # 仅“无法核实/缺扫描件”是材料动作；明确过期、失效、日期冲突才是硬失败。
        if not check_statuses:
            return bool(data.get("invalid") or data.get("expired") or data.get("date_conflict"))
        for check in checks:
            if not isinstance(check, dict) or str(check.get("status") or "").lower() != "fail":
                continue
            detail = f"{check.get('detail', '')} {check.get('reason', '')}"
            if any(word in detail for word in ("过期", "失效", "不符合", "冲突", "早于投标截止")):
                return True
        return False
    if check_id == "disqualification_check":
        # 废标项必须有明确的实质性不响应；只有“未找到响应”仍交给人工/补正文。
        for clause in data.get("items") or []:
            if isinstance(clause, dict) and str(clause.get("status") or "").lower() == "fail":
                return True
        return bool(data.get("explicit_disqualification"))
    return True


def map_rules_to_level(rule_results: list[dict]) -> str:
    """铁律3核心：规则结果 -> 定级 的确定性映射函数（固定输入断言固定输出）。"""
    if not rule_results:
        return LEVEL_CAUTION  # 无任何规则证据时保守处理，不自动 BID
    has_fail = False
    has_warning = False
    hard_fail = False
    for item in rule_results:
        status = _status_of(item)
        check_id = str(item.get("check_id", ""))
        if status == "skipped":
            continue
        if status == "fail":
            has_fail = True
            if check_id in HARD_CHECK_IDS and _explicit_hard_failure(item):
                hard_fail = True
        elif status in ("warning", "error"):
            # error（执行异常）不伪造结果，也不放行：按警告处理（保守 CAUTION）
            has_warning = True
    if hard_fail:
        return LEVEL_NO_BID
    if has_fail or has_warning:
        return LEVEL_CAUTION
    if not any(_status_of(r) == "pass" for r in rule_results):
        # 没有任何"通过"证据（全 skipped/error 等）不放行 BID，保守 CAUTION
        return LEVEL_CAUTION
    return LEVEL_BID


def extract_evidence(rule_results: list[dict], limit: int = 12) -> list[dict]:
    """从规则结果提取证据引用（检查项/状态/摘要），供决策包第三要素。"""
    evidence: list[dict] = []
    for item in rule_results:
        status = _status_of(item)
        if status == "skipped":
            continue
        data = item.get("data") or {}
        evidence.append(
            {
                "check_id": item.get("check_id", ""),
                "check_name": item.get("check_name", ""),
                "status": status,
                "summary": str(data.get("summary", ""))[:300] or str(data)[:300],
            }
        )
        if len(evidence) >= limit:
            break
    return evidence


def extract_risks(rule_results: list[dict], limit: int = 10) -> list[dict]:
    """从规则结果提取风险清单，供决策包第四要素。"""
    risks: list[dict] = []
    for item in rule_results:
        status = _status_of(item)
        if status not in ("fail", "warning"):
            continue
        data = item.get("data") or {}
        risks.append(
            {
                "check_id": item.get("check_id", ""),
                "severity": "high" if status == "fail" else "medium",
                "detail": str(data.get("risk_detail", "") or data.get("summary", "") or data)[:300],
            }
        )
        if len(risks) >= limit:
            break
    return risks


def build_decision_package(
    rule_results: list[dict],
    expert_results: dict,
    risk_summary: dict,
    llm_explanation: str = "",
) -> dict:
    """构建决策包四要素：建议 + 理由 + 证据引用 + 风险清单。

    建议由 map_rules_to_level 确定性产生；rationale = 确定性摘要 + LLM 解释文字（可选）。
    """
    level = map_rules_to_level(rule_results)
    deterministic_rationale = (
        f"规则门共 {len(rule_results)} 项，"
        f"其中 fail={sum(1 for r in rule_results if _status_of(r) == 'fail')}，"
        f"warning={sum(1 for r in rule_results if _status_of(r) == 'warning')}，"
        f"skipped={sum(1 for r in rule_results if _status_of(r) == 'skipped')}；"
        f"按映射表定级为 {level}。"
    )
    return {
        "level": level,
        "rationale": deterministic_rationale + (f" LLM解读：{llm_explanation}" if llm_explanation else ""),
        "evidence": extract_evidence(rule_results),
        "risks": extract_risks(rule_results),
        "risk_summary": risk_summary,
        "generated_at": time.time(),
    }


async def generate_llm_explanation(llm: Any, decision_package: dict) -> str:
    """LLM 只写解释文字（铁律3），不改变定级。LLM 失败时降级为空串。"""
    if llm is None:
        return ""
    try:
        text = await llm.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是投标决策助理。基于给定的决策包JSON，用不超过150字向决策者"
                        "解释建议的依据。不得改变建议结论。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(decision_package, ensure_ascii=False, default=str)[:4000],
                },
            ],
            temperature=0.3,
        )
        return str(text)[:500]
    except Exception:  # noqa: BLE001
        return ""


# ---------------- 铁律4：HITL 超时策略 ----------------


def timeout_seconds_for(level: str, configured: float | None = None) -> float:
    """超时阈值可配置（测试用秒级）。默认 DEFAULT_DECISION_TIMEOUT_SECONDS。"""
    return float(configured if configured is not None else DEFAULT_DECISION_TIMEOUT_SECONDS)


def resolve_pending_gate(level: str, pending_seconds: float, configured: float | None = None) -> dict:
    """解析决策门等待状态；任何定级都不得因超时自动批准。"""
    threshold = timeout_seconds_for(level, configured)
    if level in MANUAL_ONLY_LEVELS:
        return {
            "action": "wait_human",
            "timeout": pending_seconds >= threshold,
            "level": level,
            "reason": (
                f"{level} 级决策已等待 {pending_seconds:.1f}s，仍须人工确认（铁律4）"
                if pending_seconds >= threshold
                else f"{level} 级决策必须人工（铁律4）"
            ),
        }
    return {"action": "wait", "reason": f"等待人工决策（已等待 {pending_seconds:.1f}s/{threshold:.0f}s）"}
