"""P-D1 编排铁律（任务书第 3 节原文落成代码常量，单测断言）。

五条铁律：
1. ReAct 分流：仅"招标解读/证据批评/竞争态势"三类开放节点可用 ReAct；
   结构化生产任务（逐评分点响应/资格比对/参数应答）禁用 ReAct（本期即三专家节点）。
2. ReAct 节点四件套：迭代预算(max_iterations)、节点级工具白名单(grant_tools)、
   证据门接口占位(本期 passthrough，P-D2 接管)、tracing 成本记账。
3. 决策节点禁纯 LLM 定级：BID/CAUTION/NO_BID 由规则结果映射的确定性函数决定，
   LLM 只写解释文字。
4. 最终决策门必须人工确认：BID/CAUTION/NO_BID 均不自动放行；
   超时阈值仅用于等待监控和告警。
5. 审批接现有 RBAC（管理员可审批）；改判必须带理由，理由写回图状态并可经 API 查询；
   规则审核/飞轮深度对接本期只留记录接口。
"""

from __future__ import annotations

# 铁律1：允许 ReAct 的开放性节点（本期 tender_interpretation 必落地，另两类留占位）
REACT_ALLOWED_NODES: frozenset[str] = frozenset(
    {"tender_interpretation", "evidence_critique", "competition_landscape"}
)

# 铁律1：结构化生产任务节点，禁用 ReAct（窄职责类型化节点）
STRUCTURED_PRODUCTION_NODES: frozenset[str] = frozenset(
    {"qualification_expert", "technical_expert", "commercial_expert"}
)

# 铁律2：ReAct 节点默认迭代预算（可在运行配置覆盖）
DEFAULT_REACT_MAX_ITERATIONS = 6

# P-D2：证据批评节点迭代预算（批评任务窄，预算更小）
DEFAULT_CRITIQUE_MAX_ITERATIONS = 3

# 铁律4：决策超时默认阈值（秒）；任何最终建议都不自动放行
DEFAULT_DECISION_TIMEOUT_SECONDS = 3600.0

# 铁律4：所有最终定级均须人工确认
MANUAL_ONLY_LEVELS: frozenset[str] = frozenset({"BID", "CAUTION", "NO_BID"})

# 图拓扑节点名（固定，防漂移）
NODE_INTERPRET = "tender_interpretation"
NODE_QUALIFICATION = "qualification_expert"
NODE_TECHNICAL = "technical_expert"
NODE_COMMERCIAL = "commercial_expert"
NODE_RULE_GATE = "rule_gate"
NODE_RISK_SUMMARY = "risk_summary"
NODE_EVIDENCE_CRITIQUE = "evidence_critique"
NODE_DECISION_PACKAGE = "decision_package"
NODE_HITL_GATE = "hitl_decision_gate"
NODE_FINALIZE = "finalize"

GRAPH_NODE_ORDER: tuple[str, ...] = (
    NODE_INTERPRET,
    NODE_QUALIFICATION,
    NODE_TECHNICAL,
    NODE_COMMERCIAL,
    NODE_RULE_GATE,
    NODE_RISK_SUMMARY,
    NODE_EVIDENCE_CRITIQUE,
    NODE_DECISION_PACKAGE,
    NODE_HITL_GATE,
    NODE_FINALIZE,
)

# 铁律5：改判必须带理由
OVERRIDE_REQUIRES_REASON = True

IRON_RULES_TEXT: dict[int, str] = {
    1: "ReAct 分流：仅开放性调查节点可用 ReAct；结构化生产任务禁用 ReAct",
    2: "ReAct 节点四件套：迭代预算/工具白名单/证据门占位/tracing 记账",
    3: "决策节点禁纯 LLM 定级：定级=规则结果映射的确定性函数",
    4: "最终决策门必须人工确认：BID/CAUTION/NO_BID 均不自动放行",
    5: "审批接现有 RBAC；改判必须带理由并写回图状态可查",
}
