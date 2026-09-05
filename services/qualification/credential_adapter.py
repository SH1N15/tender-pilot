"""企业证明材料 -> Credential 候选抽取（候选抽取 + 人工确认，非自动认证）。

边界与红线：
1. 纯确定性，不调用 LLM / 网络；不得因文本出现某词就直接生成可支撑 met 的可信凭证。
2. 自动抽取的候选默认 evidence_refs 为空，needs_human_confirmation=true；
   只有人工确认并显式绑定来源引用（evidence_ref）后才可转换为正式 Credential。
3. 招标要求文本（"投标人须/要求/不低于..."等约束语境）属于 requirement，不是企业凭证，
   应跳过/ unresolved 并警告，绝不误当企业资质。
4. 不补造金额/日期；信息不完整只生成 low/medium 候选并列出 warnings。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from services.qualification.analysis_adapter import _PERSONNEL_TITLES, UnresolvedItem, _clean_region_name
from services.qualification.models import Credential

MAX_EXCERPT = 300
MAX_EVIDENCE_REF_LENGTH = 300

ConfidenceLevel = Literal["high", "medium", "low"]


class CredentialCandidate(BaseModel):
    candidate_id: str
    credential_type: str
    name: str | None = None
    certificate_name: str | None = None
    certificate_number: str | None = None
    issue_date: date | str | None = None
    expiry_date: date | str | None = None
    amount: float | str | None = None
    amount_text: str | None = None
    currency: str = "CNY"
    project_name: str | None = None
    contract_amount: float | str | None = None
    contract_amount_text: str | None = None
    completion_date: date | str | None = None
    personnel_title: str | None = None
    person_ref: str | None = None  # 姓名不可逆摘要，不保存真实姓名到飞轮
    region: str | None = None
    source_path: str | None = None
    source_excerpt: str = ""
    confidence_level: ConfidenceLevel = "low"
    needs_human_confirmation: bool = True
    warnings: list[str] = Field(default_factory=list)


class CredentialAdapterResult(BaseModel):
    candidates: list[CredentialCandidate] = Field(default_factory=list)
    unresolved_items: list[UnresolvedItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class InvalidEvidenceRefError(Exception):
    pass


# --------------------------------------------------------------------------- #
# 正则与辅助
# --------------------------------------------------------------------------- #

_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|亿|万|元)")
_DATE_CN_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_DATE_ISO_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")

_TENDER_CONSTRAINT_MARKERS = (
    "要求",
    "投标人",
    "必须",
    "须",
    "不低于",
    "不少于",
    "至少",
    "应具备",
    "须具备",
    "需具备",
    "均应",
    "应提供",
    "须提供",
)
_VAGUE_CERT_MARKERS = ("相关", "相应", "一定", "适当", "合理", "规定", "等", "要求")

_REGION_CONTEXT = ("注册地址", "营业执照", "住所", "经营场所", "注册地")
_REGION_SOFT_MARKERS = ("本地化", "优先", "倾向", "鼓励")

_CERT_NAME_RE = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9\-]{2,30}?(?:资质证书|认证证书|许可证|资格证书|登记证书))")
_CERT_NO_RE = re.compile(r"(?:证书编号|注册编号|统一编号|编号)[:：为]?\s*([A-Za-z0-9\-_]+)")
_CERT_EXPIRY_RE = re.compile(
    r"(?:有效期(?:至|到|截止)?|有效日期)[:：]?\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
)
_CAPITAL_RE = re.compile(r"(?:注册资本|注册资金)[:：为是]?\s*(?:人民币)?\s*(\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|亿|万|元)")
_PROJECT_TRIGGER_KEYWORDS = ("项目名称", "工程名称", "合同名称", "合同金额", "竣工", "完工")
_PROJECT_NAME_RE = re.compile(r"(?:项目名称|工程名称|合同名称)[:：为]?\s*([\u4e00-\u9fa5A-Za-z0-9\-]{2,30})")
_CONTRACT_AMOUNT_RE = re.compile(
    r"(?:合同金额|合同价|中标金额|合同价款)[:：为]?\s*(?:人民币)?\s*(\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|亿|万|元)"
)
_COMPLETION_DATE_RE = re.compile(
    r"(?:竣工日期|完工日期|完成日期|竣工验收日期|竣工时间)[:：为]?\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
)
_PERSONNEL_TITLE_RE = re.compile("(" + "|".join(sorted(_PERSONNEL_TITLES, key=len, reverse=True)) + ")")
_GENERIC_PERSONNEL_COUNT_RE = re.compile(
    r"(?P<title>(?:现场|项目)?[\u4e00-\u9fa5]{2,12}人员)\s*(?:名单|配置|团队)?[^\n。；;]{0,12}?"
    r"(?P<count>\d+)\s*[名人]"
)
_EVIDENCE_PERSONNEL_COUNT_RE = re.compile(
    r"(?:不少于|至少|现场(?:在岗)?实施团队(?:人数)?[^\d]{0,8})\s*(?P<count>\d+)\s*[名人]"
    r"|(?P<count2>\d+)\s*[名人][^\n。；;]{0,12}?(?:现场(?:在岗)?实施团队|实施人员)",
)
_PERSON_REF_SALT = "qualification-person:"


def _excerpt(text: str, limit: int = MAX_EXCERPT) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    m = _DATE_CN_RE.search(value) or _DATE_ISO_RE.search(value)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_amounts(text: str) -> list[tuple[float, str]]:
    seen: dict[float, str] = {}
    for m in _AMOUNT_RE.finditer(text):
        num = float(m.group(1).replace(",", "").replace("，", ""))
        unit = m.group(2)
        mult = {"亿元": 100_000_000.0, "万元": 10_000.0, "亿": 100_000_000.0, "万": 10_000.0, "元": 1.0}.get(unit, 1.0)
        seen.setdefault(num * mult, m.group(0))
    return list(seen.items())


def _is_tender_constraint(text: str) -> bool:
    """招标约束语境检测：要求/须/投标人应等 -> requirement 而非凭证。"""
    return any(marker in text for marker in _TENDER_CONSTRAINT_MARKERS)


def _person_ref(name: str) -> str:
    return hashlib.sha256(f"{_PERSON_REF_SALT}{name}".encode("utf-8")).hexdigest()[:16]


def _new_candidate_id() -> str:
    return uuid.uuid4().hex[:16]


def _candidate_key(candidate: CredentialCandidate) -> tuple:
    return (
        candidate.credential_type,
        candidate.certificate_name
        or candidate.name
        or candidate.project_name
        or candidate.personnel_title
        or candidate.region
        or "",
        candidate.certificate_number or str(candidate.amount or "") or candidate.person_ref or "",
    )


def _add_candidate(result: CredentialAdapterResult, candidate: CredentialCandidate) -> None:
    key = _candidate_key(candidate)
    if any(_candidate_key(c) == key for c in result.candidates):
        return  # 去重
    result.candidates.append(candidate)


def _add_unresolved(result: CredentialAdapterResult, text: str, reason: str, source_path: str | None) -> None:
    result.unresolved_items.append(
        UnresolvedItem(
            source_field=source_path or "text", source_path=source_path, source_text=_excerpt(text, 200), reason=reason
        )
    )
    warning = f"{source_path or 'text'}: {reason}"
    if warning not in result.warnings:
        result.warnings.append(warning)


# --------------------------------------------------------------------------- #
# 五类抽取
# --------------------------------------------------------------------------- #


def _try_certificate(line: str, source_path: str | None, result: CredentialAdapterResult) -> None:
    name_m = _CERT_NAME_RE.search(line)
    if not name_m:
        if re.search(r"(?:具备|具有|持有)?(?:相关|相应|一定|适当|规定)?资质", line):
            _add_unresolved(result, line, "仅有笼统资质表述（如'具备相关资质'），未识别到明确证书名称", source_path)
        return
    name = name_m.group(1)
    if any(k in name for k in _VAGUE_CERT_MARKERS):
        _add_unresolved(result, line, "仅有笼统资质表述（如'具备相关资质'），未识别到明确证书名称", source_path)
        return
    no_m = _CERT_NO_RE.search(line)
    exp_m = _CERT_EXPIRY_RE.search(line)
    warnings: list[str] = []
    if no_m and exp_m:
        level: ConfidenceLevel = "high"
    elif no_m or exp_m:
        level = "medium"
        warnings.append("缺少证书编号或有效期之一")
    else:
        level = "low"
        warnings.append("缺少证书编号与有效期")
    _add_candidate(
        result,
        CredentialCandidate(
            candidate_id=_new_candidate_id(),
            credential_type="certificate",
            certificate_name=name,
            certificate_number=no_m.group(1) if no_m else None,
            expiry_date=_parse_date(exp_m.group(1)) if exp_m else None,
            source_path=source_path,
            source_excerpt=_excerpt(line),
            confidence_level=level,
            warnings=warnings,
        ),
    )


def _try_capital(line: str, source_path: str | None, result: CredentialAdapterResult) -> None:
    if not _CAPITAL_RE.search(line):
        return
    amounts = _parse_amounts(line)
    if len(amounts) != 1:
        _add_unresolved(result, line, "注册资本/注册资金出现多个金额，无法确定唯一注册资本", source_path)
        return
    value, matched = amounts[0]
    _add_candidate(
        result,
        CredentialCandidate(
            candidate_id=_new_candidate_id(),
            credential_type="capital",
            name="注册资本",
            amount=value,
            amount_text=matched,
            source_path=source_path,
            source_excerpt=_excerpt(line),
            confidence_level="high",
        ),
    )


def _try_project_experience(block: str, source_path: str | None, result: CredentialAdapterResult) -> None:
    proj_m = _PROJECT_NAME_RE.search(block)
    amount_m = _CONTRACT_AMOUNT_RE.search(block)
    date_m = _COMPLETION_DATE_RE.search(block)
    if not proj_m:
        if amount_m or date_m:
            _add_unresolved(result, block, "识别到合同金额/日期但缺少项目或合同名称", source_path)
        return
    project_name = proj_m.group(1)
    warnings: list[str] = []
    if amount_m and date_m:
        level: ConfidenceLevel = "high"
    elif amount_m:
        level = "medium"
        warnings.append("缺少竣工/完成日期（不补造）")
    elif date_m:
        level = "medium"
        warnings.append("缺少合同金额（不补造）")
    else:
        level = "low"
        warnings.append("缺少合同金额与竣工/完成日期（不补造）")
    contract_value = None
    if amount_m:
        num = float(amount_m.group(1).replace(",", "").replace("，", ""))
        unit = amount_m.group(2)
        mult = {"亿元": 100_000_000.0, "万元": 10_000.0, "亿": 100_000_000.0, "万": 10_000.0, "元": 1.0}.get(unit, 1.0)
        contract_value = num * mult
    _add_candidate(
        result,
        CredentialCandidate(
            candidate_id=_new_candidate_id(),
            credential_type="project_experience",
            project_name=project_name,
            contract_amount=contract_value,
            contract_amount_text=amount_m.group(0) if amount_m else None,
            completion_date=_parse_date(date_m.group(1)) if date_m else None,
            source_path=source_path,
            source_excerpt=_excerpt(block),
            confidence_level=level,
            warnings=warnings,
        ),
    )


def _try_personnel(line: str, source_path: str | None, result: CredentialAdapterResult) -> None:
    title_m = _PERSONNEL_TITLE_RE.search(line)
    paren_titles = "|".join(sorted(_PERSONNEL_TITLES, key=len, reverse=True))
    name_m = re.search(r"([\u4e00-\u9fa5]{2,4})[（(]\s*(?:" + paren_titles + r")[）)]", line)
    if not name_m:
        name_m = re.search(r"(?:姓名|人员姓名)[:：为]?\s*([\u4e00-\u9fa5]{2,4})", line)
    # Common evidence format is "项目负责人：张三" or "技术负责人 张三".
    # The previous extractor only handled "张三（项目负责人）", causing
    # perfectly usable personnel lists to disappear from qualification match.
    if not name_m and title_m:
        tail = line[title_m.end() :]
        name_m = re.search(r"\s*[：:]?\s*([\u4e00-\u9fa5]{2,4})(?=[，,。；;\s]|$)", tail)
    if not name_m:
        return
    name = name_m.group(1)
    if not title_m:
        _add_unresolved(result, line, f"识别到姓名「{name}」但缺少岗位/证书编号，岗位缺失", source_path)
        return
    cert_m = re.search(r"(?:证书编号|执业证书|注册编号)[:：为]?\s*([A-Za-z0-9\-_]+)", line)
    warnings: list[str] = []
    if cert_m:
        level: ConfidenceLevel = "high"
    else:
        level = "medium"
        warnings.append("缺少人员证书编号")
    _add_candidate(
        result,
        CredentialCandidate(
            candidate_id=_new_candidate_id(),
            credential_type="personnel",
            name=name,
            person_ref=_person_ref(name),
            personnel_title=title_m.group(1),
            certificate_number=cert_m.group(1) if cert_m else None,
            source_path=source_path,
            source_excerpt=_excerpt(line),
            confidence_level=level,
            warnings=warnings,
        ),
    )


def _try_region(line: str, source_path: str | None, result: CredentialAdapterResult) -> None:
    if any(k in line for k in _REGION_SOFT_MARKERS) and ("本地" in line or "服务" in line or "优先" in line):
        _add_unresolved(result, line, "本地化/优先等表述不构成注册地证据，不生成 region 候选", source_path)
        return
    if not any(k in line for k in _REGION_CONTEXT):
        return
    m = re.search(r"([\u4e00-\u9fa5]{2,4}?(?:省|自治区|市|特别行政区))", line)
    if not m:
        _add_unresolved(result, line, "注册地址/营业执照语境中未解析到明确的省/市名称", source_path)
        return
    region = _clean_region_name(m.group(1))
    if len(region) < 2:
        _add_unresolved(result, line, "地区短语清理后为空", source_path)
        return
    _add_candidate(
        result,
        CredentialCandidate(
            candidate_id=_new_candidate_id(),
            credential_type="region",
            region=region,
            source_path=source_path,
            source_excerpt=_excerpt(line),
            confidence_level="high",
        ),
    )


# --------------------------------------------------------------------------- #
# 公共入口
# --------------------------------------------------------------------------- #


def extract_credentials(text: str | None, source_path: str | None = None) -> CredentialAdapterResult:
    result = CredentialAdapterResult()
    if not text or not text.strip():
        result.warnings.append("文本为空，未抽取任何候选")
        return result
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        # A project-uploaded staffing schedule can quote the tender threshold
        # (e.g. “不少于20人”) while simultaneously serving as the enterprise
        # evidence.  Evidence context must win over the generic constraint
        # marker, otherwise valid team-size proof is discarded before parsing.
        evidence_personnel_context = any(
            marker in line
            for marker in (
                "人员配置证明",
                "人员投入",
                "人员名单",
                "人天工作量",
                "人天投入",
                "配置与报价",
            )
        )
        if _is_tender_constraint(line) and not evidence_personnel_context:
            _add_unresolved(
                result, line, "招标约束语境（要求/须/投标人应等），属于 requirement 而非企业凭证，跳过", source_path
            )
            continue
        _try_certificate(line, source_path, result)
        _try_capital(line, source_path, result)
        _try_personnel(line, source_path, result)
        _try_region(line, source_path, result)
        # Preserve explicit team-size evidence (for example
        # "现场实施人员 22 人").  Qualification matching is count-based, so
        # materializing one candidate per named slot is the compatible model
        # until the credential schema grows a first-class quantity field.
        generic = _GENERIC_PERSONNEL_COUNT_RE.search(line)
        if not generic and evidence_personnel_context:
            generic = _EVIDENCE_PERSONNEL_COUNT_RE.search(line)
        if generic:
            title = generic.groupdict().get("title") or "现场在岗实施人员"
            if "现场" in title and "实施" in title:
                title = "现场在岗实施人员"
            raw_count = generic.groupdict().get("count") or generic.groupdict().get("count2") or "0"
            count = min(int(raw_count), 200)
            for index in range(count):
                _add_candidate(
                    result,
                    CredentialCandidate(
                        candidate_id=_new_candidate_id(),
                        credential_type="personnel",
                        name=f"{title}{index + 1}",
                        person_ref=_person_ref(f"{title}{index + 1}"),
                        personnel_title=title,
                        source_path=source_path,
                        source_excerpt=_excerpt(line),
                        confidence_level="high",
                    ),
                )
    # 业绩可能跨行：以"项目/合同名称"行为边界切成互不重叠的 block，每个 block 只抽取一次。
    # 既避免同一项目被重复扫描产生重复 unresolved，也不会把两个相邻项目合并成一段。
    segments: list[list[int]] = []  # 行号段
    current: list[int] = []
    for i, ln in enumerate(lines):
        is_name = bool(_PROJECT_NAME_RE.search(ln))
        if is_name:
            if current:
                segments.append(current)
            current = [i]
        elif any(k in ln for k in _PROJECT_TRIGGER_KEYWORDS):
            if not current:
                current = [i]
            else:
                current.append(i)
        elif current:
            current.append(i)
    if current:
        segments.append(current)
    for seg in segments:
        _try_project_experience("\n".join(lines[seg[0] : seg[-1] + 1]), source_path, result)
    return result


# --------------------------------------------------------------------------- #
# 候选确认
# --------------------------------------------------------------------------- #

_EVIDENCE_REF_RE = re.compile(r"^(document:[A-Za-z0-9_-]+(?:#[A-Za-z0-9_.:-]+)?|manual:[^/\\]+)$")


def validate_evidence_ref(evidence_ref: str) -> str:
    ref = (evidence_ref or "").strip()
    if not ref:
        raise InvalidEvidenceRefError("evidence_ref 不能为空")
    if len(ref) > MAX_EVIDENCE_REF_LENGTH:
        raise InvalidEvidenceRefError(f"evidence_ref 过长（上限 {MAX_EVIDENCE_REF_LENGTH} 字符）")
    if ".." in ref:
        raise InvalidEvidenceRefError("evidence_ref 不允许包含 '..'")
    if not _EVIDENCE_REF_RE.match(ref):
        raise InvalidEvidenceRefError("evidence_ref 格式非法，应为 document:<id>#pN 或 manual:<label>")
    return ref


def confirm_candidate(
    candidate: CredentialCandidate, evidence_ref: str, credential_id: str | None = None
) -> Credential:
    """人工确认候选并绑定证据引用，返回正式 Credential（不修改原始文本/文档）。

    确认后才允许带 evidence_refs=[evidence_ref]，才可用于支撑 met。
    """
    ref = validate_evidence_ref(evidence_ref)
    return Credential(
        credential_id=credential_id or uuid.uuid4().hex,
        credential_type=candidate.credential_type,
        name=candidate.name,
        certificate_name=candidate.certificate_name,
        issue_date=candidate.issue_date,
        expiry_date=candidate.expiry_date,
        amount=candidate.amount,
        amount_text=candidate.amount_text,
        currency=candidate.currency,
        project_name=candidate.project_name,
        contract_amount=candidate.contract_amount,
        contract_amount_text=candidate.contract_amount_text,
        completion_date=candidate.completion_date,
        personnel_title=candidate.personnel_title,
        certificate_number=candidate.certificate_number,
        region=candidate.region,
        evidence_refs=[ref],
        source=ref,
    )


__all__ = [
    "ConfidenceLevel",
    "CredentialCandidate",
    "CredentialAdapterResult",
    "InvalidEvidenceRefError",
    "extract_credentials",
    "validate_evidence_ref",
    "confirm_candidate",
    "MAX_EVIDENCE_REF_LENGTH",
]
