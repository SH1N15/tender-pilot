"""规则门（确定性节点）与风险汇总（确定性节点）。

规则门：22 项检查引擎以图内确定性节点形态接入——经 services/check 新增只读包装
入口（services/check/graph_adapter.py，本任务新增）调用现有规则引擎，
严禁改动现有检查行为；输入不足的检查项显式返回 skipped(带原因)，禁伪造。
节点本身无 LLM 路由：固定清单、固定顺序、逐项执行。
风险汇总：对规则结果做确定性聚合（fail/warning/skipped 计数 + 高危项列表）。
"""

from __future__ import annotations

import time
from typing import Any

from core.agent_engine.metrics import CountingLLM


async def run_rule_gate_node(state: dict, llm: Any = None, metrics: Any = None) -> dict:
    """确定性规则门节点：逐项调用现有检查 skill（只读包装），输入不足显式 skipped。"""
    from services.check.graph_adapter import run_all_checks

    node = "rule_gate"
    if metrics is not None:
        metrics.start_node(node)
    started = time.monotonic()
    try:
        counting_llm = CountingLLM(llm, metrics, node) if (metrics is not None and llm is not None) else llm
        results = await run_all_checks(
            tender_text=state.get("tender_text") or "",
            bid_text=state.get("bid_text") or "",
            llm=counting_llm,
            project_id=str(state.get("project_id", "")),
        )
        llm_calls = sum(1 for r in results if not r.get("skipped"))
        return {
            "rule_results": results,
            "node_status": {"rule_gate": "done"},
            "llm_calls": llm_calls,
        }
    finally:
        if metrics is not None:
            metrics.end_node(node, started)


def run_risk_summary_node(state: dict, metrics: Any = None) -> dict:
    """确定性风险汇总：聚合规则门结果。"""
    node = "risk_summary"
    if metrics is not None:
        metrics.start_node(node)
    try:
        results = state.get("rule_results") or []
        counts = {"pass": 0, "fail": 0, "warning": 0, "skipped": 0, "error": 0}
        high_risks: list[str] = []
        skipped_reasons: list[str] = []
        for item in results:
            status = str(item.get("status", "skipped")).lower()
            counts[status] = counts.get(status, 0) + 1
            if status == "fail":
                high_risks.append(str(item.get("check_name") or item.get("check_id")))
            if status == "skipped":
                skipped_reasons.append(f"{item.get('check_id')}: {item.get('reason', '')}")
        overall_risk = "high" if counts["fail"] else ("medium" if (counts["warning"] or counts.get("error")) else "low")
        return {
            "risk_summary": {
                "counts": counts,
                "overall_risk": overall_risk,
                "high_risk_items": high_risks,
                "skipped": skipped_reasons,
            },
            "node_status": {"risk_summary": "done"},
        }
    finally:
        if metrics is not None:
            metrics.end_node(node)


def _status_of(item: dict) -> str:
    return str(item.get("status", "skipped")).lower()
