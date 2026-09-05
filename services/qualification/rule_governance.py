"""受控学习：数据飞轮 -> 规则候选 -> 人工审核 -> 版本发布/回滚。

安全边界（本模块默认"永不自动发布"）：
1. 候选生成只做确定性统计（不调用 LLM 直接写规则）；
2. 规则 schema 白名单化：只允许固定的规则模板与参数，拒绝任意代码/正则；
   即使模板字段是字符串，也做灾难性正则/边界校验（防御性）；
3. 发布前必须通过：规则校验 + 现有基准评测 + 回归门禁；未通过 -> 拒绝发布；
4. 发布/回滚全部写入追加式审计日志（data/rules/audit.jsonl）。

当前闭环不把新规则自动接入 matcher 执行（避免破坏现有确定性匹配），
只做「候选 -> 审批 -> 版本 -> 发布(带门禁) -> 可回滚」的最小安全闭环，
后续可把已发布规则包接入 matcher 的叠加层（见 docs/rule-governance.md）。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ProposalStatus = Literal["pending", "approved", "rejected"]
PackStatus = Literal["draft", "published", "rolled_back"]

# --------------------------------------------------------------------------- #
# 规则模板白名单（只允许这些模板；每个模板声明允许的 params 字段与类型）
# --------------------------------------------------------------------------- #
ALLOWED_RULE_TEMPLATES: dict[str, dict[str, str]] = {
    # 要求某类型资格必须提供证据引用（evidence_refs）才能判定 met
    "require_evidence": {},
    # insufficient 次数阈值 -> 建议人工复核（参数 threshold: int >= 1）
    "insufficient_review_threshold": {"threshold": "int"},
    # 证书名称最小长度（参数 min_length: int >= 1）
    "certificate_name_min_length": {"min_length": "int"},
    # 金额要求下界倍数（参数 multiplier: float in (0, 100]）
    "capital_min_amount_multiplier": {"multiplier": "float"},
    # G-0-4：图改判频次候选——同一改判后 level 达到 threshold 次建议人工复核放宽/收紧
    # 对应铁律参数（参数 threshold: int >= 1；数据源 .dev/graph_override_log.jsonl）
    "override_review_threshold": {"threshold": "int"},
}

ALLOWED_PARAM_TYPES = {"int": int, "float": float}


class RuleValidationError(ValueError):
    pass


class PublicationBlockedError(ValueError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


class RuleProposal(BaseModel):
    proposal_id: str
    source: str = "flywheel"  # 数据飞轮
    template: str
    requirement_type: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    statistics: dict[str, Any] = Field(default_factory=dict)  # 确定性统计摘要（白名单）
    rationale: str = ""
    status: ProposalStatus = "pending"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    reviewed_at: str | None = None
    reviewer: str = ""
    review_note: str = ""


class RulePack(BaseModel):
    pack_id: str
    version: str  # semver，如 1.0.0
    name: str
    status: PackStatus = "draft"
    rules: list[dict[str, Any]] = Field(default_factory=list)
    proposal_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    published_at: str | None = None
    published_by: str = ""
    rolled_back_to: str | None = None  # 回滚到的上一个已发布版本
    eval_summary: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "system"


class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    actor: str
    action: str  # propose / approve / reject / create_pack / publish / rollback / block
    target_id: str
    detail: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# --------------------------------------------------------------------------- #
# 校验：schema 白名单 + 灾难性正则/边界
# --------------------------------------------------------------------------- #

_CATASTROPHIC_RE = re.compile(
    r"\(\?:[^)]*\*\)\+\+|\(\*\+|\([^)]*\)\{[0-9]{3,}\}|"
    r"\[\^?[^\]]*\]\{[0-9]{3,}\}|\*\{2,}|\+\{2,}|\?\{2,}|\*\+\+|\+\+\+"
)


def validate_regex_safety(pattern: str, field_name: str = "pattern") -> None:
    """拒绝灾难性正则/超长重复（ReDoS 防御）。我们不允许模板外任意正则。"""
    if not isinstance(pattern, str):
        raise RuleValidationError(f"{field_name} 必须是字符串")
    if len(pattern) > 200:
        raise RuleValidationError(f"{field_name} 长度超过 200")
    if _CATASTROPHIC_RE.search(pattern):
        raise RuleValidationError(f"{field_name} 包含疑似灾难性正则模式")
    # 边界：避免嵌套量词
    if re.search(r"\([^)]*[+*{][^)]*\)[+*{]", pattern):
        raise RuleValidationError(f"{field_name} 存在嵌套量词（可能 ReDoS）")


def validate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """校验单条规则：模板白名单 + params 白名单 + 边界。返回规范化规则。"""
    if not isinstance(rule, dict):
        raise RuleValidationError("规则必须是对象")
    unknown = set(rule.keys()) - {"template", "requirement_type", "params", "rationale"}
    if unknown:
        raise RuleValidationError(f"规则包含未知字段: {sorted(unknown)}")
    template = rule.get("template")
    if template not in ALLOWED_RULE_TEMPLATES:
        raise RuleValidationError(f"不允许的规则模板: {template!r}")
    requirement_type = rule.get("requirement_type", "")
    allowed_types = ("certificate", "capital", "project_experience", "personnel", "region")
    if requirement_type and requirement_type not in allowed_types:
        raise RuleValidationError(f"不允许的资格类型: {requirement_type!r}")

    allowed_params = ALLOWED_RULE_TEMPLATES[template]
    params = rule.get("params") or {}
    if not isinstance(params, dict):
        raise RuleValidationError("params 必须是对象")
    unknown_params = set(params.keys()) - set(allowed_params.keys())
    if unknown_params:
        raise RuleValidationError(f"模板 {template} 不允许参数: {sorted(unknown_params)}")
    normalized: dict[str, Any] = {}
    for key, expected in allowed_params.items():
        if key not in params:
            continue
        value = params[key]
        ptype = ALLOWED_PARAM_TYPES[expected]
        try:
            value = ptype(value)
        except (TypeError, ValueError):
            raise RuleValidationError(f"参数 {key} 必须是 {expected}")
        if expected == "int" and value < 1:
            raise RuleValidationError(f"参数 {key} 必须 >= 1")
        if expected == "float" and not (0 < value <= 100):
            raise RuleValidationError(f"参数 {key} 必须在 (0, 100] 区间")
        normalized[key] = value
    # 防御：任何字符串字段都不允许灾难性正则特征（模板字段当前都是标量）
    for k, v in params.items():
        if isinstance(v, str):
            validate_regex_safety(v, f"params.{k}")

    return {
        "template": template,
        "requirement_type": requirement_type,
        "params": normalized,
        "rationale": str(rule.get("rationale", ""))[:500],
    }


# --------------------------------------------------------------------------- #
# 存储：内存 + JSONL 持久化
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuleGovernanceStore:
    _instance: "RuleGovernanceStore | None" = None

    def __init__(self, base_dir: str | Path | None = None):
        base = Path(base_dir or "data/rules")
        self.base_dir = base
        self.proposals_path = base / "proposals.jsonl"
        self.packs_path = base / "packs.jsonl"
        self.audit_path = base / "audit.jsonl"
        self._proposals: dict[str, RuleProposal] = {}
        self._packs: dict[str, RulePack] = {}
        self._audit: list[AuditEvent] = []
        self._load()

    @classmethod
    def instance(cls) -> "RuleGovernanceStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def _load(self) -> None:
        for path, container in (
            (self.proposals_path, self._proposals),
            (self.packs_path, self._packs),
        ):
            if not path.exists():
                continue
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if container is self._proposals:
                        obj = RuleProposal.model_validate(data)
                    else:
                        obj = RulePack.model_validate(data)
                    container[obj.pack_id if isinstance(obj, RulePack) else obj.proposal_id] = obj
            except Exception as e:  # noqa: BLE001
                logger.warning("规则治理存储加载失败（忽略损坏行）: %s", e)
        if self.audit_path.exists():
            try:
                for line in self.audit_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        self._audit.append(AuditEvent.model_validate(json.loads(line)))
            except Exception as e:  # noqa: BLE001
                logger.warning("审计日志加载失败（忽略损坏行）: %s", e)

    def _append(self, path: Path, obj: BaseModel) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(obj.model_dump_json() + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning("规则治理持久化失败（忽略）: %s", e)

    def audit(self, actor: str, action: str, target_id: str, detail: dict | None = None) -> AuditEvent:
        event = AuditEvent(actor=actor, action=action, target_id=target_id, detail=detail or {})
        self._audit.append(event)
        self._append(self.audit_path, event)
        return event

    def list_audit(self, limit: int = 100) -> list[dict]:
        return [e.model_dump() for e in self._audit[-limit:]]

    def save_proposal(self, proposal: RuleProposal) -> None:
        self._proposals[proposal.proposal_id] = proposal
        self._append(self.proposals_path, proposal)

    def get_proposal(self, proposal_id: str) -> RuleProposal | None:
        return self._proposals.get(proposal_id)

    def list_proposals(self, status: str | None = None) -> list[RuleProposal]:
        items = list(self._proposals.values())
        if status:
            items = [p for p in items if p.status == status]
        return sorted(items, key=lambda p: p.created_at, reverse=True)

    def save_pack(self, pack: RulePack) -> None:
        self._packs[pack.pack_id] = pack
        self._append(self.packs_path, pack)

    def get_pack(self, pack_id: str) -> RulePack | None:
        return self._packs.get(pack_id)

    def list_packs(self) -> list[RulePack]:
        return sorted(self._packs.values(), key=lambda p: p.created_at, reverse=True)

    def published_packs(self) -> list[RulePack]:
        return [p for p in self._packs.values() if p.status == "published"]


# --------------------------------------------------------------------------- #
# 候选生成（确定性统计，无 LLM）
# --------------------------------------------------------------------------- #


def _proposal_stats(proposal: RuleProposal) -> dict[str, Any]:
    return {
        "template": proposal.template,
        "requirement_type": proposal.requirement_type,
        "params": proposal.params,
        "statistics": proposal.statistics,
    }


def _read_override_log(path: str | Path) -> list[dict]:
    """G-0-4：读取图改判日志（JSONL，字段 run_id/project_id/level/reason/user/at）。

    level=改判后 level。损坏行跳过；文件不存在返回空。
    """
    p = Path(path)
    if not p.exists():
        return []
    entries: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("level"):
                entries.append(data)
    except OSError as e:
        logger.warning("改判日志读取失败（忽略）: %s", e)
    return entries


def generate_rule_proposals(
    limit: int = 5,
    store: RuleGovernanceStore | None = None,
    override_log_path: str | Path | None = None,
) -> list[RuleProposal]:
    """从两个数据源生成确定性候选：资格预审 Trace + 图改判日志（G-0-4 第二数据源）。

    只输出统计模式；不生成可执行代码/正则。
    """
    store = store or RuleGovernanceStore.instance()
    from services.qualification.flywheel import get_trace_store

    trace_store = get_trace_store()
    events = trace_store.read_events()

    # 统计：按 requirement_type 无法直接从 trace 得到（trace 只存聚合），
    # 因此用 approval 决策分布 + insufficient 次数推断模式
    run_count = sum(1 for e in events if e.get("event_type") == "run")
    approvals = [e for e in events if e.get("event_type") == "approval"]
    reject_count = sum(1 for a in approvals if (a.get("decision_counts") or {}).get("reject", 0) > 0)
    total_insufficient = sum((a.get("decision_counts") or {}).get("mark_insufficient", 0) for a in approvals)
    total_reject = sum((a.get("decision_counts") or {}).get("reject", 0) for a in approvals)

    candidates: list[RuleProposal] = []

    if total_insufficient >= 3:
        candidates.append(
            RuleProposal(
                proposal_id=uuid.uuid4().hex[:12],
                template="insufficient_review_threshold",
                requirement_type="",
                params={"threshold": min(3 + (total_insufficient // 3), 20)},
                statistics={
                    "run_count": run_count,
                    "approval_count": len(approvals),
                    "total_insufficient": total_insufficient,
                },
                rationale=(
                    f"历史 {len(approvals)} 次审批中共出现 {total_insufficient} 次信息不足标记，"
                    f"建议将人工复核阈值设为 {min(3 + (total_insufficient // 3), 20)}"
                    "（仍需人工审批，不会自动发布）"
                ),
            )
        )
    if total_reject >= 3:
        candidates.append(
            RuleProposal(
                proposal_id=uuid.uuid4().hex[:12],
                template="require_evidence",
                requirement_type="",
                params={},
                statistics={
                    "run_count": run_count,
                    "approval_count": len(approvals),
                    "total_reject": total_reject,
                },
                rationale=(
                    f"历史审批中 {total_reject} 次被人工否决，建议强化证据引用要求（require_evidence），需人工审批"
                ),
            )
        )
    if reject_count > 0 and run_count >= 2:
        candidates.append(
            RuleProposal(
                proposal_id=uuid.uuid4().hex[:12],
                template="certificate_name_min_length",
                requirement_type="certificate",
                params={"min_length": 2},
                statistics={"run_count": run_count, "reject_runs": reject_count},
                rationale="证书类要求多次人工否决，建议收紧证书名称最小长度，需人工审批",
            )
        )

    # ── G-0-4：第二数据源——图改判日志（.dev/graph_override_log.jsonl）──────
    # 按（改判后）level 频次生成候选：同一 level 被人工反复改判 >=2 次，
    # 说明对应铁律/规则参数可能过严或过松，建议人工复核（不自动发布）。
    # 注：日志只记录改判后 level，不记录改判前 level（runner._log_override 结构）。
    import os

    log_path = override_log_path or os.getenv("GRAPH_OVERRIDE_LOG", ".dev/graph_override_log.jsonl")
    overrides = _read_override_log(log_path)
    if overrides:
        by_level: dict[str, int] = {}
        for e in overrides:
            by_level[str(e.get("level"))] = by_level.get(str(e.get("level")), 0) + 1
        for level, count in sorted(by_level.items(), key=lambda kv: -kv[1]):
            if count < 2:
                continue
            candidates.append(
                RuleProposal(
                    proposal_id=uuid.uuid4().hex[:12],
                    source="graph_override_log",
                    template="override_review_threshold",
                    requirement_type="",
                    params={"threshold": min(count, 20)},
                    statistics={
                        "override_count": count,
                        "overrides_by_level": dict(by_level),
                        "sample_run_ids": [
                            str(e.get("run_id", "")) for e in overrides if str(e.get("level")) == level
                        ][:5],
                    },
                    rationale=(
                        f"图 HITL 改判日志中改判为 {level} 已达 {count} 次，"
                        "对应规则/铁律参数疑似偏离人工判断，建议人工复核调整（仍需审批，不会自动发布）"
                    ),
                )
            )

    created: list[RuleProposal] = []
    for cand in candidates[:limit]:
        existing = [
            p for p in store.list_proposals(status="pending") if p.template == cand.template and p.params == cand.params
        ]
        if existing:
            continue
        store.save_proposal(cand)
        store.audit("flywheel", "propose", cand.proposal_id, {"template": cand.template})
        created.append(cand)
    return created


# --------------------------------------------------------------------------- #
# 审批 / 规则包 / 发布门禁 / 回滚
# --------------------------------------------------------------------------- #


def approve_proposal(
    proposal_id: str,
    reviewer: str = "",
    note: str = "",
    store: RuleGovernanceStore | None = None,
) -> RuleProposal:
    store = store or RuleGovernanceStore.instance()
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        raise ValueError(f"候选不存在: {proposal_id}")
    if proposal.status != "pending":
        raise ValueError(f"候选已处理（status={proposal.status}）")
    proposal.status = "approved"
    proposal.reviewer = reviewer
    proposal.review_note = note
    proposal.reviewed_at = _now_iso()
    store.save_proposal(proposal)
    store.audit(reviewer or "system", "approve", proposal_id, {"template": proposal.template})
    return proposal


def reject_proposal(
    proposal_id: str,
    reviewer: str = "",
    note: str = "",
    store: RuleGovernanceStore | None = None,
) -> RuleProposal:
    store = store or RuleGovernanceStore.instance()
    proposal = store.get_proposal(proposal_id)
    if proposal is None:
        raise ValueError(f"候选不存在: {proposal_id}")
    if proposal.status != "pending":
        raise ValueError(f"候选已处理（status={proposal.status}）")
    proposal.status = "rejected"
    proposal.reviewer = reviewer
    proposal.review_note = note
    proposal.reviewed_at = _now_iso()
    store.save_proposal(proposal)
    store.audit(reviewer or "system", "reject", proposal_id, {"template": proposal.template})
    return proposal


def next_version(existing: list[RulePack]) -> str:
    versions = [p.version for p in existing if p.status == "published"]
    if not versions:
        return "1.0.0"
    nums = []
    for v in versions:
        parts = v.split(".")
        try:
            nums.append(tuple(int(x) for x in parts[:3]))
        except ValueError:
            continue
    if not nums:
        return "1.0.0"
    major, minor, patch = max(nums)
    return f"{major}.{minor}.{patch + 1}"


def create_rule_pack(
    name: str,
    proposal_ids: list[str],
    created_by: str = "admin",
    store: RuleGovernanceStore | None = None,
) -> RulePack:
    store = store or RuleGovernanceStore.instance()
    if not proposal_ids:
        raise ValueError("规则包至少需要一个已审批候选")
    rules: list[dict[str, Any]] = []
    for pid in proposal_ids:
        proposal = store.get_proposal(pid)
        if proposal is None:
            raise ValueError(f"候选不存在: {pid}")
        if proposal.status != "approved":
            raise ValueError(f"候选未审批: {pid}")
        rules.append(
            {
                "template": proposal.template,
                "requirement_type": proposal.requirement_type,
                "params": proposal.params,
                "rationale": proposal.rationale,
            }
        )
    for rule in rules:
        validate_rule(rule)  # 白名单校验

    pack = RulePack(
        pack_id=uuid.uuid4().hex[:12],
        version=next_version(store.list_packs()),
        name=name,
        rules=rules,
        proposal_ids=list(proposal_ids),
        created_by=created_by,
    )
    store.save_pack(pack)
    store.audit(created_by, "create_pack", pack.pack_id, {"version": pack.version, "rules": len(rules)})
    return pack


def run_publish_gate(store: RuleGovernanceStore | None = None) -> dict[str, Any]:
    """运行发布门禁：现有基准评测 + 回归。失败抛 PublicationBlockedError。"""
    from services.qualification.evaluator import run_evaluation

    try:
        report = run_evaluation("synthetic_baseline")
    except Exception as e:  # noqa: BLE001
        raise PublicationBlockedError(f"基准评测无法运行，禁止发布: {e}") from e

    accuracy = report.requirement_accuracy
    gate = {
        "dataset_name": report.dataset_name,
        "case_count": report.case_count,
        "requirement_accuracy": accuracy,
        "overall_accuracy": report.overall_accuracy,
        "evidence_invariant_violations": len(report.evidence_invariant_violations or []),
        "failed_cases": len(report.failed_cases or []),
        "passed": True,
    }
    if accuracy < 0.5:
        raise PublicationBlockedError(f"基准准确率过低 ({accuracy:.3f})，禁止发布", gate)
    if gate["evidence_invariant_violations"] > 0:
        raise PublicationBlockedError("基准存在证据不变式违规，禁止发布", gate)
    return gate


def publish_rule_pack(
    pack_id: str,
    published_by: str = "admin",
    store: RuleGovernanceStore | None = None,
) -> RulePack:
    """发布规则包：先校验 + 跑发布门禁；未通过 -> 拒绝发布。"""
    store = store or RuleGovernanceStore.instance()
    pack = store.get_pack(pack_id)
    if pack is None:
        raise ValueError(f"规则包不存在: {pack_id}")
    if pack.status == "published":
        raise ValueError(f"规则包已发布: {pack_id}")
    if pack.status == "rolled_back":
        raise ValueError(f"规则包已回滚，不能发布: {pack_id}")

    try:
        for rule in pack.rules:
            validate_rule(rule)
        gate = run_publish_gate(store)
    except PublicationBlockedError as e:
        store.audit(published_by, "block", pack_id, {"reason": str(e), "details": e.details})
        raise PublicationBlockedError(f"发布被门禁阻止: {e}", e.details) from e

    pack.status = "published"
    pack.published_at = _now_iso()
    pack.published_by = published_by
    pack.eval_summary = gate
    store.save_pack(pack)
    store.audit(published_by, "publish", pack_id, {"version": pack.version, "gate": gate})
    return pack


def rollback_rule_pack(
    pack_id: str,
    rolled_back_by: str = "admin",
    store: RuleGovernanceStore | None = None,
) -> RulePack:
    """回滚到上一个已发布版本。当前包标记 rolled_back，记录回滚目标。"""
    store = store or RuleGovernanceStore.instance()
    pack = store.get_pack(pack_id)
    if pack is None:
        raise ValueError(f"规则包不存在: {pack_id}")
    if pack.status != "published":
        raise ValueError(f"只有已发布规则包可回滚: {pack_id}")
    published = [p for p in store.list_packs() if p.status == "published" and p.pack_id != pack_id]
    target = None
    if published:
        target = sorted(published, key=lambda p: p.version, reverse=True)[0]
    pack.status = "rolled_back"
    pack.rolled_back_to = target.version if target else None
    store.save_pack(pack)
    store.audit(rolled_back_by, "rollback", pack_id, {"rolled_back_to": pack.rolled_back_to})
    return pack
