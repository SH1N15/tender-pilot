"""P-D1 主编排图状态 schema。

GraphState 为 LangGraph StateGraph 的状态容器：
- 并行专家节点写 expert_results（带 merge reducer）；
- rule_gate 写 rule_results（列表，append reducer）；
- 决策包四要素：建议(level) + 理由(rationale) + 证据引用(evidence) + 风险清单(risks)；
- 改判理由 override_reason 写回图状态，可经 GET /api/graph/runs/{id} 查询（铁律5）。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def merge_dict(left: dict | None, right: dict | None) -> dict:
    base = dict(left or {})
    base.update(right or {})
    return base


class GraphState(TypedDict, total=False):
    # 输入
    run_id: str
    project_id: str
    tender_text: str
    bid_text: str

    # 节点产出
    interpretation: dict  # 解读节点（ReAct）产出
    expert_results: Annotated[dict[str, Any], merge_dict]  # 三专家并行分支合并
    rule_results: Annotated[list[dict], operator.add]  # 规则门逐项结果（skipped 带原因）
    risk_summary: dict
    # P-D2 引用锚点贯通：检索产物 chunk_id 从工具层进图状态
    citation_ledger: dict  # {n(int 或 str): {chunk_id, source, excerpt, text}}（引用对照表）
    evidence_grounding: dict  # Gate 统计+放行/拒绝明细 {stats, passed, rejected}
    critique_risks: Annotated[list[dict], operator.add]  # 证据批评节点风险标注（并进决策包）
    decision_package: dict  # 四要素：level/rationale/evidence/risks
    human_decision: dict  # {action, level?, reason, decided_by, decided_at}
    override_reason: str  # 铁律5：改判理由写回图状态

    # 记账/观测
    node_status: Annotated[dict[str, str], merge_dict]  # node -> done/error/skipped
    node_costs: Annotated[dict[str, dict], merge_dict]  # node -> {llm_calls, tokens, duration_ms}
    llm_calls: Annotated[int, operator.add]
    tokens_consumed: Annotated[int, operator.add]
    errors: Annotated[list[str], operator.add]

    current_stage: str
    final_level: str  # 终态定级（人工改判优先）
