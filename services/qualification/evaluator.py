"""资格预审离线回归评测：纯确定性，不调用 LLM / DB / 网络。

定位：使用独立、版本化的合成/公开脱敏评测集验证 matcher 行为。
Trace 只包含聚合字段（无法反推 credentials/requirements），因此本评测**不能**从 Trace 重放企业数据；
Trace 的人工介入/改判指标仅用于指导后续补充哪些 case。

数据边界：
- 评测集位于仓库 data/qualification_eval/ 下，内置白名单名称；
- loader 限制最大文件大小与最大 case 数，损坏 JSONL 行进入 invalid_cases；
- 不接受任意用户文件路径（只按白名单名称在固定目录内解析）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from services.qualification.flywheel import MATCHER_VERSION
from services.qualification.matcher import match_qualifications
from services.qualification.models import Credential, Requirement

VALID_STATUSES = ("met", "unmet", "insufficient")

# 内置数据集注册表（白名单）：生产 API 只允许运行这些名称
DATASET_REGISTRY: dict[str, dict[str, str]] = {
    "synthetic_baseline": {
        "version": "1.0.0",
        "description": (
            "合成资格预审基准集：覆盖 certificate/capital/project_experience/personnel/region，"
            "met/unmet/insufficient 混合（非生产数据）"
        ),
    },
}

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "qualification_eval"
MAX_DATASET_FILE_SIZE = 2 * 1024 * 1024  # 2MB
MAX_DATASET_CASES = 200
_FAILED_REASON_MAX = 200


class DatasetNotFoundError(Exception):
    pass


class DatasetLoadError(Exception):
    pass


class EvalCase(BaseModel):
    case_id: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    credentials: list[Credential] = Field(default_factory=list)
    expected_results: dict[str, str] = Field(default_factory=dict)
    expected_overall_status: str = ""


class FailedCaseSummary(BaseModel):
    """失败案例摘要：只含定位与期望/实际/原因，不复制 credentials/source_text/evidence_refs。"""

    case_id: str
    requirement_id: str
    expected: str
    actual: str
    reason: str


class EvalReport(BaseModel):
    dataset_name: str = ""
    dataset_version: str = ""
    matcher_version: str = ""
    case_count: int = 0
    valid_case_count: int = 0
    invalid_case_count: int = 0
    requirement_accuracy: float = 0.0
    overall_accuracy: float = 0.0
    confusion_matrix: dict[str, dict[str, int]] = Field(
        default_factory=lambda: {s: {a: 0 for a in VALID_STATUSES} for s in VALID_STATUSES}
    )
    evidence_invariant_violations: list[dict[str, str]] = Field(default_factory=list)
    by_requirement_type: dict[str, dict[str, Any]] = Field(default_factory=dict)
    by_tag: dict[str, dict[str, Any]] = Field(default_factory=dict)
    failed_cases: list[FailedCaseSummary] = Field(default_factory=list)
    invalid_cases: list[dict[str, Any]] = Field(default_factory=list)


def _round4(value: float) -> float:
    return round(value, 4)


def _validate_case(case: EvalCase, seen_ids: set[str]) -> list[str]:
    problems: list[str] = []
    if not case.case_id:
        problems.append("case_id 为空")
    if case.case_id in seen_ids:
        problems.append(f"case_id 重复: {case.case_id}")
    if not case.requirements:
        problems.append("requirements 为空")
    expected_ids = set(case.expected_results.keys())
    actual_ids = {r.requirement_id for r in case.requirements}
    if expected_ids != actual_ids:
        missing = sorted(actual_ids - expected_ids)
        extra = sorted(expected_ids - actual_ids)
        if missing:
            problems.append(f"expected_results 缺少要求: {', '.join(missing)}")
        if extra:
            problems.append(f"expected_results 含未知要求: {', '.join(extra)}")
    bad_statuses = [k for k, v in case.expected_results.items() if v not in VALID_STATUSES]
    if bad_statuses:
        problems.append(f"expected_results 状态非法: {', '.join(bad_statuses)}")
    if case.expected_overall_status not in VALID_STATUSES:
        problems.append("expected_overall_status 非法")
    return problems


def evaluate_cases(
    cases: list[EvalCase],
    dataset_name: str = "",
    dataset_version: str = "",
    matcher_version: str = MATCHER_VERSION,
) -> EvalReport:
    """纯确定性评测：校验 case、运行 match_qualifications、聚合指标。

    - 单条坏 case 进入 invalid_cases，不影响整批；
    - 相同输入输出一致（按 case_id 排序后处理，结果字段确定性排序）。
    """
    ordered = sorted(cases, key=lambda c: c.case_id)  # 确定性排序
    seen: set[str] = set()
    valid_cases: list[EvalCase] = []
    invalid_cases: list[dict[str, Any]] = []
    for case in ordered:
        problems = _validate_case(case, seen)
        if case.case_id:
            seen.add(case.case_id)
        if problems:
            invalid_cases.append({"case_id": case.case_id or "(empty)", "reason": "; ".join(problems)})
            continue
        valid_cases.append(case)

    requirement_total = 0
    requirement_correct = 0
    evaluated = 0
    overall_correct = 0
    confusion: dict[str, dict[str, int]] = {s: {a: 0 for a in VALID_STATUSES} for s in VALID_STATUSES}
    evidence_violations: list[dict[str, str]] = []
    by_type: dict[str, dict[str, int]] = {}
    failed: list[FailedCaseSummary] = []

    for case in valid_cases:
        try:
            report = match_qualifications(case.requirements, case.credentials)
        except Exception as e:  # 单条坏 case 不拖垮整批
            invalid_cases.append({"case_id": case.case_id, "reason": f"运行异常: {str(e)[:120]}"})
            continue
        evaluated += 1
        if report.overall_status == case.expected_overall_status:
            overall_correct += 1
        for result in report.results:
            expected = case.expected_results.get(result.requirement_id, "unknown")
            actual = result.status
            requirement_total += 1
            if expected == actual:
                requirement_correct += 1
            else:
                failed.append(
                    FailedCaseSummary(
                        case_id=case.case_id,
                        requirement_id=result.requirement_id,
                        expected=expected,
                        actual=actual,
                        reason=result.reason[:_FAILED_REASON_MAX],
                    )
                )
            confusion.setdefault(expected, {a: 0 for a in VALID_STATUSES})[actual] += 1
            if actual == "met" and not result.evidence_refs:
                evidence_violations.append({"case_id": case.case_id, "requirement_id": result.requirement_id})
            entry = by_type.setdefault(result.requirement_type, {"total": 0, "correct": 0})
            entry["total"] += 1
            if expected == actual:
                entry["correct"] += 1

    # 按 tag 聚合（case 级 overall 准确率）
    by_tag: dict[str, dict[str, Any]] = {}
    all_tags = sorted({t for c in valid_cases for t in c.tags})
    for tag in all_tags:
        tagged = [c for c in valid_cases if tag in c.tags]
        ok = 0
        for case in tagged:
            try:
                report = match_qualifications(case.requirements, case.credentials)
            except Exception:
                continue
            if report.overall_status == case.expected_overall_status:
                ok += 1
        by_tag[tag] = {
            "case_count": len(tagged),
            "overall_correct": ok,
            "overall_accuracy": _round4(ok / len(tagged)) if tagged else 0.0,
        }

    by_type_clean = {
        key: {
            "total": v["total"],
            "correct": v["correct"],
            "accuracy": _round4(v["correct"] / v["total"]) if v["total"] else 0.0,
        }
        for key, v in sorted(by_type.items())
    }
    failed.sort(key=lambda f: (f.case_id, f.requirement_id))

    return EvalReport(
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        matcher_version=matcher_version,
        case_count=len(cases),
        valid_case_count=evaluated,
        invalid_case_count=len(invalid_cases),
        requirement_accuracy=_round4(requirement_correct / requirement_total) if requirement_total else 0.0,
        overall_accuracy=_round4(overall_correct / evaluated) if evaluated else 0.0,
        confusion_matrix=confusion,
        evidence_invariant_violations=evidence_violations,
        by_requirement_type=by_type_clean,
        by_tag=by_tag,
        failed_cases=failed,
        invalid_cases=invalid_cases,
    )


def _resolve_dataset_path(dataset_name: str, base_dir: str | Path | None = None) -> Path:
    """只允许白名单名称，在固定内置目录内解析，不接受任意用户路径。"""
    if dataset_name not in DATASET_REGISTRY:
        raise DatasetNotFoundError(f"未知数据集: {dataset_name}")
    base = Path(base_dir) if base_dir else DATA_DIR
    return base / f"{dataset_name}.jsonl"


def _parse_lines(path: Path) -> tuple[list[EvalCase], list[dict[str, Any]]]:
    cases: list[EvalCase] = []
    invalid: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            cases.append(EvalCase.model_validate(json.loads(line)))
        except Exception as e:
            invalid.append({"line_no": line_no, "reason": f"JSON 解析失败: {str(e)[:120]}"})
    if len(cases) > MAX_DATASET_CASES:
        raise DatasetLoadError(f"数据集 case 数超过上限 {MAX_DATASET_CASES}")
    return cases, invalid


def run_evaluation(dataset_name: str, base_dir: str | Path | None = None) -> EvalReport:
    """加载白名单数据集并评测（大小/数量受限，损坏行进入 invalid_cases）。"""
    if dataset_name not in DATASET_REGISTRY:
        raise DatasetNotFoundError(f"未知数据集: {dataset_name}")
    path = _resolve_dataset_path(dataset_name, base_dir)
    if not path.exists():
        raise DatasetLoadError(f"数据集文件不存在: {path.name}")
    if path.stat().st_size > MAX_DATASET_FILE_SIZE:
        raise DatasetLoadError(f"数据集文件超过大小上限: {path.name}")
    cases, corrupt_lines = _parse_lines(path)
    meta = DATASET_REGISTRY[dataset_name]
    report = evaluate_cases(cases, dataset_name, meta["version"], MATCHER_VERSION)
    report.case_count += len(corrupt_lines)
    report.invalid_case_count += len(corrupt_lines)
    report.invalid_cases = corrupt_lines + report.invalid_cases
    return report


def list_datasets(base_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """列出内置白名单数据集（名称/版本/描述/case 数）。"""
    base = Path(base_dir) if base_dir else DATA_DIR
    result: list[dict[str, Any]] = []
    for name in sorted(DATASET_REGISTRY.keys()):
        meta = DATASET_REGISTRY[name]
        count = 0
        try:
            path = base / f"{name}.jsonl"
            if path.exists() and path.stat().st_size <= MAX_DATASET_FILE_SIZE:
                cases, _ = _parse_lines(path)
                count = len(cases)
        except Exception:
            count = 0
        result.append(
            {"name": name, "version": meta["version"], "description": meta["description"], "case_count": count}
        )
    return result


__all__ = [
    "VALID_STATUSES",
    "DATASET_REGISTRY",
    "DATA_DIR",
    "MAX_DATASET_FILE_SIZE",
    "MAX_DATASET_CASES",
    "DatasetNotFoundError",
    "DatasetLoadError",
    "EvalCase",
    "EvalReport",
    "FailedCaseSummary",
    "evaluate_cases",
    "run_evaluation",
    "list_datasets",
]
