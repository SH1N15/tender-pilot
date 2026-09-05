"""可暂停、可人工确认（HITL）的资格预审 Workflow。

设计原则：
1. run() 先调用纯规则 matcher：overall_status 为 met/unmet 且无 warning -> completed；
   存在 insufficient 或 warnings -> waiting_human，并生成 review_items 供人工逐条决策。
2. approve() 只记录人工决策并重建最终报告，绝不改写原始 credentials；
   每条人工审批都会留下 decision 记录（requirement_id / decision / reviewer / note / decided_at）。
3. 不伪造模型置信度：信息不足就是 insufficient，人工确认只是背书，不会升级为 met。
4. 内存存储（WorkflowStore 单例），不依赖数据库 / LLM / 网络。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from services.qualification.matcher import match_qualifications
from services.qualification.models import Credential, MatchReport, MatchResult, MatchSummary, Requirement

WorkflowStatus = Literal["waiting_human", "resumed", "completed"]
ReviewDecision = Literal["confirm", "reject", "mark_insufficient"]
VALID_DECISIONS: tuple[str, ...] = ("confirm", "reject", "mark_insufficient")

# --------------------------------------------------------------------------- #
# 领域异常
# --------------------------------------------------------------------------- #


class WorkflowError(Exception):
    """Workflow 领域错误基类。"""


class WorkflowNotFoundError(WorkflowError):
    pass


class WorkflowIdConflictError(WorkflowError):
    pass


class UnknownRequirementError(WorkflowError):
    pass


class DuplicateDecisionError(WorkflowError):
    pass


class InvalidDecisionError(WorkflowError):
    pass


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


class ReviewItem(BaseModel):
    """一条等待人工决策的评审项。"""

    requirement_id: str
    status: str  # 规则引擎原始状态
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    requirement_type: str = ""
    category_label: str = "资格要求"
    title: str = "资格要求待确认"
    requirement_text: str = ""
    source_path: str | None = None
    source_label: str = ""
    source_refs: list[str] = Field(default_factory=list)
    expected_evidence: str = ""
    matched_evidence_summary: str = ""
    recommendation: str = ""
    matched_credential_ids: list[str] = Field(default_factory=list)
    decision: str | None = None  # confirm / reject / mark_insufficient，人工填写


class WorkflowDecision(BaseModel):
    """人工对某条评审项的决策。"""

    requirement_id: str
    decision: ReviewDecision
    reviewer: str = ""
    note: str = ""


class WorkflowApproveRequest(BaseModel):
    decisions: list[WorkflowDecision] = Field(default_factory=list)


class QualificationWorkflow(BaseModel):
    workflow_id: str
    status: WorkflowStatus = "waiting_human"
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    project_id: str | None = None
    entrypoint: str = "manual"  # manual / from_analysis / from_project
    report: MatchReport
    review_items: list[ReviewItem] = Field(default_factory=list)
    # 人工审批记录（追加式，永不修改原始 credentials）
    decisions: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 内存存储
# --------------------------------------------------------------------------- #


class WorkflowStore:
    """进程内内存存储。测试可用 reset() 重置。"""

    _instance: "WorkflowStore | None" = None

    def __init__(self) -> None:
        self._workflows: dict[str, QualificationWorkflow] = {}

    @classmethod
    def instance(cls) -> "WorkflowStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def get(self, workflow_id: str) -> QualificationWorkflow | None:
        return self._workflows.get(workflow_id)

    def save(self, workflow: QualificationWorkflow) -> None:
        self._workflows[workflow.workflow_id] = workflow


# --------------------------------------------------------------------------- #
# 状态推进
# --------------------------------------------------------------------------- #


def _needs_review(report: MatchReport) -> bool:
    if report.warnings:
        return True
    return any(r.status == "insufficient" or r.warnings for r in report.results)


_REQUIREMENT_LABELS = {
    "certificate": "资质证书",
    "capital": "注册资本",
    "project_experience": "项目业绩",
    "personnel": "人员配置",
    "region": "注册地区",
}

_EXPECTED_EVIDENCE = {
    "certificate": "有效期内的证书扫描件，以及可核验的证书编号或查询记录",
    "capital": "营业执照或工商登记信息中能够证明注册资本的页面",
    "project_experience": "合同关键页、金额页、签章页和验收/完工证明",
    "personnel": "人员名单、劳动/社保证明、岗位证书及证书编号",
    "region": "营业执照或工商登记信息中的注册地址页面",
}


def _review_title(requirement: Requirement) -> str:
    label = _REQUIREMENT_LABELS.get(requirement.requirement_type, "资格要求")
    if requirement.requirement_type == "personnel":
        if requirement.personnel_title and requirement.min_count:
            return f"{label}：{requirement.personnel_title}不少于 {requirement.min_count} 人"
        if requirement.personnel_title:
            return f"{label}：{requirement.personnel_title}"
        return "人员资格要求（系统未提取到具体岗位和人数）"
    if requirement.requirement_type == "certificate" and requirement.certificate_name:
        return f"{label}：{requirement.certificate_name}"
    if requirement.requirement_type == "capital" and requirement.min_amount is not None:
        return f"{label}：不低于 {requirement.min_amount}"
    if requirement.requirement_type == "project_experience" and requirement.min_count:
        return f"{label}：不少于 {requirement.min_count} 项"
    if requirement.requirement_type == "region" and requirement.region:
        return f"{label}：{requirement.region}"
    return label


def _source_label(source_path: str | None, requirement_type: str) -> str:
    field_labels = {
        "qualification_level": "资质证书要求",
        "registered_capital": "注册资本要求",
        "performance_requirement": "项目业绩要求",
        "personnel_requirement": "人员配置要求",
        "region_requirement": "注册地区要求",
        "other_requirements": "其他资格要求",
    }
    if source_path:
        field = source_path.rsplit(".", 1)[-1].split("[", 1)[0]
        if field in field_labels:
            return f"招标文件 · {field_labels[field]}"
    return f"招标文件 · {_REQUIREMENT_LABELS.get(requirement_type, '资格要求')}"


def _humanize_reason(reason: str, requirement_type: str) -> str:
    label = _REQUIREMENT_LABELS.get(requirement_type, "资格")
    if reason.startswith(f"未提供类型为 {requirement_type} 的证明材料"):
        return f"系统未找到可核验的{label}证明材料，暂时无法确认"
    field_labels = {
        "certificate_name": "证书名称",
        "personnel_title": "人员岗位",
        "region": "注册地区",
        "project_experience": "项目业绩",
        "capital": "注册资本",
        "certificate": "资质证书",
        "personnel": "人员配置",
    }
    text = reason
    for internal, readable in field_labels.items():
        text = text.replace(f"({internal})", f"（{readable}）")
    return text


def _credential_summary(result: MatchResult, credentials: dict[str, Credential]) -> str:
    summaries: list[str] = []
    for credential_id in result.matched_credential_ids:
        credential = credentials.get(credential_id)
        if credential is None:
            continue
        name = (
            credential.certificate_name
            or credential.project_name
            or credential.personnel_title
            or credential.name
            or credential.region
            or credential_id
        )
        refs = "、".join(credential.evidence_refs)
        summaries.append(f"{name}{f'（{refs}）' if refs else ''}")
    return "；".join(summaries) or "当前企业资料库未找到可直接匹配的证明材料"


def _build_review_item(
    result: MatchResult,
    requirements: dict[str, Requirement],
    credentials: dict[str, Credential],
) -> ReviewItem:
    requirement = requirements.get(result.requirement_id)
    requirement_type = requirement.requirement_type if requirement else result.requirement_type
    label = _REQUIREMENT_LABELS.get(requirement_type, "资格要求")
    requirement_text = ""
    source_path = None
    source_refs: list[str] = []
    if requirement is not None:
        requirement_text = requirement.source_text or requirement.description or ""
        source_path = requirement.source_path
        source_refs = list(requirement.source_refs)
    generic_personnel = requirement_type == "personnel" and (
        requirement is None or not requirement.personnel_title or not requirement.min_count
    )
    recommendation = (
        "系统未提取到可执行的岗位和人数条件。请回看招标原文；无法确认时选择“暂缺材料/信息”。"
        if generic_personnel
        else "核对下方招标原文与企业材料；证据充分选“材料满足”，明确不满足选“不满足”，缺少证明选“暂缺材料/信息”。"
    )
    return ReviewItem(
        requirement_id=result.requirement_id,
        requirement_type=requirement_type,
        category_label=label,
        title=_review_title(requirement) if requirement else label,
        requirement_text=requirement_text,
        source_path=source_path,
        source_label=_source_label(source_path, requirement_type),
        source_refs=source_refs,
        expected_evidence=_EXPECTED_EVIDENCE.get(requirement_type, "能够直接证明该项资格条件的原始材料"),
        matched_evidence_summary=_credential_summary(result, credentials),
        recommendation=recommendation,
        status=result.status,
        reason=_humanize_reason(result.reason, requirement_type),
        evidence_refs=list(result.evidence_refs),
        matched_credential_ids=list(result.matched_credential_ids),
    )


def _build_review_items(
    report: MatchReport,
    requirements: list[Requirement],
    credentials: list[Credential],
    force_fallback: bool = False,
) -> list[ReviewItem]:
    requirements_by_id = {item.requirement_id: item for item in requirements}
    credentials_by_id = {item.credential_id: item for item in credentials}
    targeted = [
        _build_review_item(r, requirements_by_id, credentials_by_id)
        for r in report.results
        if r.status == "insufficient" or r.warnings
    ]
    if targeted:
        return targeted
    # 存在全局 warning，或因外部上下文（如适配 unresolved）强制人工复核时，
    # 退化为全部结果供人工确认，避免空 review_items 死锁
    if report.warnings or force_fallback:
        return [_build_review_item(r, requirements_by_id, credentials_by_id) for r in report.results]
    return []


def _validation_error_text(exc: ValidationError) -> str:
    err = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in err.get("loc", ()))
    return f"{loc}: {err.get('msg', '数据无效')}"


def _coerce_decision(raw: WorkflowDecision | dict) -> WorkflowDecision:
    if isinstance(raw, WorkflowDecision):
        return raw
    try:
        return WorkflowDecision.model_validate(raw)
    except ValidationError as e:
        raise InvalidDecisionError(f"决策数据无效: {_validation_error_text(e)}") from e


def run_qualification_workflow(
    requirements: list[dict | Requirement] | None,
    credentials: list[dict | Credential] | None,
    workflow_id: str | None = None,
    extra_warnings: list[str] | None = None,
    force_review: bool = False,
    project_id: str | None = None,
    entrypoint: str = "manual",
) -> QualificationWorkflow:
    """先跑纯规则 matcher，再按结果进入 completed / waiting_human。

    - extra_warnings：外部上下文（如适配层 unresolved/warnings）并入 workflow.warnings；
    - force_review：强制进入 waiting_human（即使规则结果全部 met），
      用于存在需要人工处理的未解析项时，不吞掉 unresolved。
    """
    parsed_requirements = [
        item if isinstance(item, Requirement) else Requirement.model_validate(item)
        for item in (requirements or [])
    ]
    parsed_credentials = [
        item if isinstance(item, Credential) else Credential.model_validate(item)
        for item in (credentials or [])
    ]
    report = match_qualifications(parsed_requirements, parsed_credentials)
    wid = workflow_id or str(uuid.uuid4())
    store = WorkflowStore.instance()
    if store.get(wid) is not None:
        raise WorkflowIdConflictError(f"Workflow '{wid}' 已存在")

    extra = list(extra_warnings or [])
    needs_review = _needs_review(report) or force_review
    wf = QualificationWorkflow(
        workflow_id=wid,
        status="waiting_human" if needs_review else "completed",
        project_id=project_id,
        entrypoint=entrypoint,
        report=report,
        review_items=_build_review_items(
            report,
            parsed_requirements,
            parsed_credentials,
            force_fallback=force_review,
        ),
        warnings=list(report.warnings) + extra,
    )
    store.save(wf)
    return wf


def get_qualification_workflow(workflow_id: str) -> QualificationWorkflow:
    wf = WorkflowStore.instance().get(workflow_id)
    if wf is None:
        raise WorkflowNotFoundError(f"Workflow '{workflow_id}' 不存在")
    return wf


def approve_qualification_workflow(
    workflow_id: str,
    decisions: list[WorkflowDecision | dict],
) -> QualificationWorkflow:
    """人工审批 review_items。

    - 校验 workflow_id 与 requirement_id：未知项直接报错，不静默接受；
    - 相同决策重复提交 -> 幂等（不重复记录、不报错）；
    - 已决策项提交不同决策 -> DuplicateDecisionError（拒绝覆盖）；
    - 全部 review_items 决策完成 -> completed，并用决策重建最终报告；否则 -> resumed。
    """
    store = WorkflowStore.instance()
    wf = store.get(workflow_id)
    if wf is None:
        raise WorkflowNotFoundError(f"Workflow '{workflow_id}' 不存在")

    parsed = [_coerce_decision(d) for d in decisions]

    item_ids = {item.requirement_id for item in wf.review_items}
    unknown = [d.requirement_id for d in parsed if d.requirement_id not in item_ids]
    if unknown:
        raise UnknownRequirementError("审批中包含未知的要求ID: " + ", ".join(unknown))

    existing = {rec["requirement_id"]: rec["decision"] for rec in wf.decisions}
    applied_any = False
    for d in parsed:
        if d.requirement_id in existing:
            if existing[d.requirement_id] != d.decision:
                raise DuplicateDecisionError(
                    f"要求 '{d.requirement_id}' 已审批为 {existing[d.requirement_id]}，不能改为 {d.decision}"
                )
            continue  # 幂等：相同决策不重复记录
        wf.decisions.append(
            {
                "requirement_id": d.requirement_id,
                "decision": d.decision,
                "reviewer": d.reviewer,
                "note": d.note,
                "decided_at": datetime.now().isoformat(),
            }
        )
        for item in wf.review_items:
            if item.requirement_id == d.requirement_id:
                item.decision = d.decision
        applied_any = True

    if applied_any:
        if all(item.decision for item in wf.review_items):
            wf.report = _apply_decisions(wf.report, wf.decisions)
            wf.status = "completed"
        else:
            wf.status = "resumed"
        wf.updated_at = datetime.now()
        store.save(wf)
    return wf


def _apply_decisions(report: MatchReport, decisions: list[dict]) -> MatchReport:
    """按人工决策重建最终报告（不改动原始 credentials）。"""
    by_id = {rec["requirement_id"]: rec["decision"] for rec in decisions}
    results: list[MatchResult] = []
    for r in report.results:
        dec = by_id.get(r.requirement_id)
        if dec is None:
            results.append(r)
            continue
        status = r.status
        warnings = list(r.warnings)
        if dec == "reject":
            status = "unmet"
            warnings.append("人工否决该要求")
        elif dec == "mark_insufficient":
            status = "insufficient"
            warnings.append("人工标记信息不足")
        else:  # confirm
            warnings.append("人工确认该判定")
        results.append(
            MatchResult(
                requirement_id=r.requirement_id,
                requirement_type=r.requirement_type,
                status=status,
                reason=r.reason,
                evidence_refs=list(r.evidence_refs),
                matched_credential_ids=list(r.matched_credential_ids),
                warnings=warnings,
            )
        )

    total = len(results)
    met = sum(1 for r in results if r.status == "met")
    unmet = sum(1 for r in results if r.status == "unmet")
    insufficient = total - met - unmet
    if total == 0:
        overall = "insufficient"
    elif unmet > 0:
        overall = "unmet"
    elif insufficient > 0:
        overall = "insufficient"
    else:
        overall = "met"

    return MatchReport(
        overall_status=overall,
        summary=MatchSummary(total=total, met=met, unmet=unmet, insufficient=insufficient),
        results=results,
        warnings=list(report.warnings),
    )


__all__ = [
    "WorkflowStatus",
    "ReviewDecision",
    "VALID_DECISIONS",
    "WorkflowError",
    "WorkflowNotFoundError",
    "WorkflowIdConflictError",
    "UnknownRequirementError",
    "DuplicateDecisionError",
    "InvalidDecisionError",
    "ReviewItem",
    "WorkflowDecision",
    "WorkflowApproveRequest",
    "QualificationWorkflow",
    "WorkflowStore",
    "run_qualification_workflow",
    "get_qualification_workflow",
    "approve_qualification_workflow",
]
