"""招标解读结果 -> 资格预审 Requirement 的保守适配层。

输入：现有 Analysis.dimensions 风格 dict（如 tender_interpret 输出的
{"qualification": {...}, "project_info": {...}, "timeline": {...}, ...}）。

原则：
1. 只把能确定解析的字段结构化为 Requirement：
   - registered_capital -> capital（金额解析成功才结构化）
   - qualification_level / 证书列表 -> certificate（文本明确才结构化）
   - performance_requirement -> project_experience（数量/金额/时间窗至少解析出其一）
   - personnel_requirement -> personnel（岗位+人数都确定才结构化）
   - 地区限制 -> region（仅当措辞强制且解析到明确省/市名才结构化）
2. 模糊自然语言绝不强行判定为明确要求：**同时**进入 unresolved_items 与 warnings。
3. 来源追踪：每个 Requirement / unresolved 条目都保留 source_path（稳定路径，如
   qualification.registered_capital）、source_text（原文）与 source_refs（可选）。
4. 不调用 LLM / 数据库 / 网络。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from services.qualification.models import Requirement

# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #


class UnresolvedItem(BaseModel):
    """无法结构化的原始条目（保留证据定位，等待人工处理）。"""

    source_field: str
    source_text: str
    reason: str
    source_path: str | None = None


class AdapterResult(BaseModel):
    requirements: list[Requirement] = Field(default_factory=list)
    unresolved_items: list[UnresolvedItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 解析辅助（确定性正则，不含 LLM）
# --------------------------------------------------------------------------- #

_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|亿|万|元)")
_COUNT_RE = re.compile(r"(\d+)\s*[项个]")
_DIRECTION_DOWN = ("不高于", "不超过", "小于", "≤", "＜")

_PERSONNEL_TITLES = [
    "总监理工程师",
    "项目负责人",
    "技术负责人",
    "专职安全员",
    "造价工程师",
    "一级建造师",
    "二级建造师",
    "项目经理",
    "监理工程师",
    "安全员",
    "质检员",
    "建造师",
]
_VAGUE_MARKERS = (
    "详见",
    "见招标文件",
    "见采购文件",
    "相关规定",
    "符合国家",
    "满足招标文件",
    "按招标文件",
    "以招标文件",
    "招标文件要求",
    "法律法规",
    "有关要求",
    "相关",
    "相应",
    "一定",
    "适当",
    "合理",
    "规定",
    "一定等级",
    "相应等级",
)
_CERT_CONCRETE_MARKERS = (
    "资质",
    "证书",
    "认证",
    "许可证",
    "资格",
    "备案",
    "登记证",
    "ISO",
    "CMA",
    "CNAS",
    "总承包",
    "专业承包",
    "施工",
)

_REGION_KEYWORDS = ("注册地", "省内", "市内", "本地", "本地化", "属地", "在册")
_REGION_SOFT_MARKERS = ("优先", "优先考虑", "倾向", "鼓励", "加分", "适当", "酌情")
_REGION_MANDATORY_MARKERS = ("须", "必须", "需", "应", "要求", "注册地", "限于", "仅限", "限定", "指定", "需在", "须在")
_REGION_RE = re.compile(
    r"(?:注册地(?:为|在)|须在|需在|在|位于|仅限|限于|限定|指定|为|是)"
    r"([\u4e00-\u9fa5]{2,4}?(?:省|自治区|特别行政区|市))"
)
_DATE_CN_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_DATE_ISO_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NEAR_YEAR_RE = re.compile(r"近\s*(\d{1,2}|[一二两三四五六七八九十])\s*年")
_LEVEL_RE = re.compile(r"[一二三四五六七八九十]?级及以上|级及以?上|及以上|及以下|以下")
_LEVEL_GRADE_RE = re.compile(r"特级|一级|二级|三级|四级|甲级|乙级|丙级|丁级")
_FILLER_PREFIX_RE = re.compile(
    r"^(?:在|须在|须|位于|为|仅限|限定|限于|注册地|注册|本地|当地|属|是|企业|投标人|要求|具有|具备|持有|提供|需)+"
)


def _minus_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 2 月 29 日等日期不存在时退到 28 日
        return d.replace(year=d.year - years, day=28)


def _to_text(value: Any) -> str:
    """把 str/list/dict 字段值压平成便于解析的文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                for v in item.values():
                    t = _to_text(v)
                    if t:
                        parts.append(t)
            else:
                t = _to_text(item)
                if t:
                    parts.append(t)
        return "；".join(parts)
    if isinstance(value, dict):
        return "；".join(_to_text(v) for v in value.values() if _to_text(v))
    return str(value).strip()


def _clean_amount_num(text: str) -> float:
    return float(text.replace(",", "").replace("，", ""))


def _parse_amounts(text: str) -> list[tuple[float, str]]:
    """返回 [(金额(元), 原文片段), ...]，去重。"""
    seen: dict[float, str] = {}
    for m in _AMOUNT_RE.finditer(text):
        num = _clean_amount_num(m.group(1))
        unit = m.group(2)
        mult = {"亿元": 100_000_000.0, "万元": 10_000.0, "亿": 100_000_000.0, "万": 10_000.0, "元": 1.0}.get(unit, 1.0)
        seen.setdefault(num * mult, m.group(0))
    return list(seen.items())


def _parse_date_cn(text: str) -> date | None:
    m = _DATE_CN_RE.search(text) or _DATE_ISO_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _find_window(text: str, bid_deadline: date | None) -> tuple[date | None, date | None, str | None]:
    """解析业绩时间窗。返回 (date_from, date_to, error)。"""
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(?:至|到|~|—|-)\s*(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        try:
            return (
                date(int(m.group(1)), int(m.group(2)), int(m.group(3))),
                date(int(m.group(4)), int(m.group(5)), int(m.group(6))),
                None,
            )
        except ValueError:
            return None, None, "业绩时间窗日期无效"
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*(?:至|到|~)\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if m:
        try:
            return (
                date(int(m.group(1)), int(m.group(2)), int(m.group(3))),
                date(int(m.group(4)), int(m.group(5)), int(m.group(6))),
                None,
            )
        except ValueError:
            return None, None, "业绩时间窗日期无效"
    m = _NEAR_YEAR_RE.search(text)
    if m and bid_deadline is not None:
        raw = m.group(1)
        years = int(raw) if raw.isdigit() else _CN_NUM.get(raw, 0)
        if years <= 0:
            return None, None, "近N年年限无法识别"
        try:
            return _minus_years(bid_deadline, years), bid_deadline, None
        except OverflowError:
            return None, None, "近N年时间窗计算失败"
    if m:
        return None, None, "业绩年限要求无法解析（缺少 timeline.bid_deadline 作为基准）"
    return None, None, None


def _clean_certificate_name(text: str) -> str:
    """去掉前缀动词/等级/后缀，得到可匹配的基础证书名称。"""
    t = re.sub(r"^(具有|具备|持有|提供|须|需|要求|投标人|须具有|须具备)", "", text).strip()
    t = re.sub(r"[（(]含[^）)]*[）)]", "", t)
    t = _LEVEL_RE.sub("", t)
    t = _LEVEL_GRADE_RE.sub("", t)
    t = re.sub(r"(资质|证书|认证|资格)$", "", t)
    return t.strip(" ，、；：:()（）")


def _clean_region_name(text: str) -> str:
    """去掉地区短语前常见的功能词前缀，保留省/市名。"""
    prev = None
    while prev != text:
        prev = text
        text = _FILLER_PREFIX_RE.sub("", text)
    return text


def _add_unresolved(result: AdapterResult, source_path: str, text: str, reason: str) -> None:
    """模糊/无法解析条目：同时进入 unresolved_items 与 warnings。"""
    result.unresolved_items.append(
        UnresolvedItem(source_field=source_path, source_path=source_path, source_text=text, reason=reason)
    )
    warning = f"{source_path}: {reason}"
    if warning not in result.warnings:
        result.warnings.append(warning)


# --------------------------------------------------------------------------- #
# 各字段适配器
# --------------------------------------------------------------------------- #


def _adapt_capital(text: str, source_path: str, result: AdapterResult) -> None:
    if not text:
        return
    if re.search(r"不高于|不超过|(?<!不)低于|小于|≤|＜", text):
        _add_unresolved(result, source_path, text, "出现上限/约束类措辞，无法确定最低注册资本")
        return
    amounts = _parse_amounts(text)
    if not amounts:
        _add_unresolved(result, source_path, text, "未解析到明确金额")
        return
    if len(amounts) > 1:
        _add_unresolved(result, source_path, text, "出现多个金额，无法确定最低注册资本")
        return
    value, matched = amounts[0]
    result.requirements.append(
        Requirement(
            requirement_id=f"capital-{len(result.requirements) + 1}",
            requirement_type="capital",
            description=f"注册资本要求（原文：{text}）",
            min_amount=value,
            source_refs=[],
            source_text=text,
            source_path=source_path,
        )
    )
    result.warnings.append(f"{source_path}: 已按「{matched}」解析为最低注册资本要求")


def _adapt_certificate(text: str, source_path: str, result: AdapterResult) -> None:
    if not text:
        return
    name = None
    vague = any(k in text for k in _VAGUE_MARKERS)
    concrete = any(k in text for k in _CERT_CONCRETE_MARKERS)
    multi_clause = any(ch in text for ch in ("。", "；", "\n", "、")) or len(text) > 80
    if not vague and concrete and not multi_clause:
        name = _clean_certificate_name(text)

    if name:
        result.requirements.append(
            Requirement(
                requirement_id=f"certificate-{len(result.requirements) + 1}",
                requirement_type="certificate",
                description=f"资质/证书要求（原文：{text}）",
                certificate_name=name,
                source_refs=[],
                source_text=text,
                source_path=source_path,
            )
        )
        result.warnings.append(f"{source_path}: 已按「{name}」解析为证书名称要求（原文：{text}）")
        return

    # BUG-11 修复：长文本（≥50 字）资质条款未命中结构化模式时不再静默丢弃，
    # 与 BUG-8 相同方式降级为"待人工确认"要求（保留原文）。
    if len(text) >= _NEEDS_REVIEW_MIN_LEN:
        result.requirements.append(
            Requirement(
                requirement_id=f"certificate-{len(result.requirements) + 1}",
                requirement_type="certificate",
                description=f"{_NEEDS_REVIEW_PREFIX}资质/证书要求（原文：{text}）",
                source_refs=[],
                source_text=text,
                source_path=source_path,
            )
        )
        result.warnings.append(f"{source_path}: 资质条款未命中结构化模式，已降级为待人工确认要求")
        return

    if vague:
        _add_unresolved(result, source_path, text, "文本含模糊措辞，无法确定具体证书名称")
    elif not concrete:
        _add_unresolved(result, source_path, text, "文本缺少明确的资质/证书名称特征，不强行结构化")
    elif multi_clause:
        _add_unresolved(result, source_path, text, "文本为多句/多证书表述，不强行猜测单一证书名称")
    else:
        _add_unresolved(result, source_path, text, "清理后证书名称为空")


_NEEDS_REVIEW_PREFIX = "【待人工确认】"
_NEEDS_REVIEW_MIN_LEN = 50


def _adapt_performance(text: str, bid_deadline: date | None, source_path: str, result: AdapterResult) -> None:
    if not text:
        return
    count = None
    count_m = (
        re.search(r"(?:至少|不少于|不低于)\s*(\d+)\s*[项个]", text)
        or re.search(r"(\d+)\s*[项个]\s*(?:\(含\))?\s*(?:以上|及以上)", text)
        or re.search(r"[≥＞]\s*(\d+)\s*[项个]", text)
        or re.search(r"(\d+)\s*[项个]", text)
    )
    if count_m:
        count = int(count_m.group(1))

    amounts = _parse_amounts(text)
    min_amount = None
    if len(amounts) == 1:
        min_amount = amounts[0][0]
    elif len(amounts) > 1:
        _add_unresolved(result, source_path, text, "业绩文本出现多个金额，无法确定单项金额门槛")

    date_from, date_to, window_err = _find_window(text, bid_deadline)
    if window_err:
        _add_unresolved(result, source_path, text, window_err)

    if count is None and min_amount is None and date_from is None:
        if len(text) >= _NEEDS_REVIEW_MIN_LEN:
            # BUG-8 修复：散文式业绩条款不再静默丢弃，降级为"待人工确认"要求
            result.requirements.append(
                Requirement(
                    requirement_id=f"project_experience-{len(result.requirements) + 1}",
                    requirement_type="project_experience",
                    description=f"{_NEEDS_REVIEW_PREFIX}业绩要求（原文：{text}）",
                    source_refs=[],
                    source_text=text,
                    source_path=source_path,
                )
            )
            result.warnings.append(f"{source_path}: 散文式业绩条款未解析出结构化字段，已降级为待人工确认要求")
        else:
            _add_unresolved(result, source_path, text, "未解析到数量/金额/时间窗，无法结构化")
        return

    result.requirements.append(
        Requirement(
            requirement_id=f"project_experience-{len(result.requirements) + 1}",
            requirement_type="project_experience",
            description=f"业绩要求（原文：{text}）",
            min_count=count,
            min_amount=min_amount,
            date_from=date_from,
            date_to=date_to,
            source_refs=[],
            source_text=text,
            source_path=source_path,
        )
    )
    parsed_parts = [
        p
        for p in (
            "数量" if count is not None else None,
            "金额" if min_amount is not None else None,
            "时间窗" if date_from else None,
        )
        if p
    ]
    result.warnings.append(f"{source_path}: 已解析 {'、'.join(parsed_parts)}")


def _adapt_personnel(text: str, source_path: str, result: AdapterResult) -> None:
    if not text:
        return
    found_any = False
    produced_any = False
    accepted_spans: list[tuple[int, int]] = []
    for title in sorted(_PERSONNEL_TITLES, key=len, reverse=True):
        for m in re.finditer(re.escape(title), text):
            if any(not (m.end() <= s or m.start() >= e) for (s, e) in accepted_spans):
                continue  # 已被更长岗位名覆盖（如 专职安全员 覆盖 安全员）
            accepted_spans.append((m.start(), m.end()))
            found_any = True
            after = text[m.end() : m.end() + 12]
            count_m = re.match(r"\s*(?:不少于|不低于|至少)?\s*(\d+)\s*[名人]", after)
            if not count_m:
                before = text[max(0, m.start() - 12) : m.start()]
                count_m = re.search(r"(\d+)\s*[名人]\s*$", before)
            if not count_m:
                _add_unresolved(result, source_path, text, f"岗位「{title}」缺少明确人数")
                if len(text) >= _NEEDS_REVIEW_MIN_LEN:
                    result.requirements.append(
                        Requirement(
                            requirement_id=f"personnel-{len(result.requirements) + 1}",
                            requirement_type="personnel",
                            description=f"{_NEEDS_REVIEW_PREFIX}{title}人员要求（原文：{text}）",
                            personnel_title=title,
                            source_refs=[],
                            source_text=text,
                            source_path=source_path,
                        )
                    )
                    result.warnings.append(f"{source_path}: 已提取岗位「{title}」，人数待人工确认")
                    produced_any = True
                continue
            result.requirements.append(
                Requirement(
                    requirement_id=f"personnel-{len(result.requirements) + 1}",
                    requirement_type="personnel",
                    description=f"人员要求（原文：{text}）",
                    personnel_title=title,
                    min_count=int(count_m.group(1)),
                    source_refs=[],
                    source_text=text,
                    source_path=source_path,
                )
            )
            produced_any = True
    for count_match in re.finditer(
        r"(?P<title>[\u4e00-\u9fff]{2,14}人员)\s*(?:数量)?(?:不少于|不低于|至少)\s*(?P<count>\d+)\s*[名人]",
        text,
    ):
        title = re.sub(r"^(?:需确保|确保|配备|安排)", "", count_match.group("title"))
        count = int(count_match.group("count"))
        if any(r.personnel_title == title and r.min_count == count for r in result.requirements):
            continue
        result.requirements.append(
            Requirement(
                requirement_id=f"personnel-{len(result.requirements) + 1}",
                requirement_type="personnel",
                description=f"人员要求（原文：{text}）",
                personnel_title=title,
                min_count=count,
                source_refs=[],
                source_text=text,
                source_path=source_path,
            )
        )
        result.warnings.append(f"{source_path}: 已解析「{title}不少于{count}人」")
        produced_any = True
    if not produced_any and len(text) >= _NEEDS_REVIEW_MIN_LEN:
        # BUG-8 修复：散文式人员条款（含明确证书清单）不再静默丢弃，降级为"待人工确认"要求
        result.requirements.append(
            Requirement(
                requirement_id=f"personnel-{len(result.requirements) + 1}",
                requirement_type="personnel",
                description=f"{_NEEDS_REVIEW_PREFIX}人员要求（原文：{text}）",
                source_refs=[],
                source_text=text,
                source_path=source_path,
            )
        )
        result.warnings.append(f"{source_path}: 散文式人员条款未解析出岗位+人数，已降级为待人工确认要求")
        return
    if not found_any:
        _add_unresolved(result, source_path, text, "未识别到已知岗位名称，无法结构化")


def _adapt_region(text: str, source_path: str, result: AdapterResult) -> None:
    if not text:
        return
    if not any(k in text for k in _REGION_KEYWORDS):
        return  # 无地区语境，跳过
    if any(k in text for k in _REGION_SOFT_MARKERS):
        _add_unresolved(result, source_path, text, "地区表述为非强制（优先/倾向等），不生成硬性地区要求")
        return
    if not any(k in text for k in _REGION_MANDATORY_MARKERS):
        _add_unresolved(result, source_path, text, "地区表述未含强制措辞（须/必须/注册地在等），不强行判定为硬性要求")
        return
    m = _REGION_RE.search(text)
    if not m:
        _add_unresolved(result, source_path, text, "提到地区限制但未解析到明确的省/市名称")
        return
    region = _clean_region_name(m.group(1))
    if len(region) < 2:
        _add_unresolved(result, source_path, text, "地区短语清理后为空或非明确省/市名称")
        return
    result.requirements.append(
        Requirement(
            requirement_id=f"region-{len(result.requirements) + 1}",
            requirement_type="region",
            description=f"地区要求（原文：{text}）",
            region=region,
            source_refs=[],
            source_text=text,
            source_path=source_path,
        )
    )
    result.warnings.append(f"{source_path}: 已按「{region}」解析为注册地要求")


# --------------------------------------------------------------------------- #
# 公共入口
# --------------------------------------------------------------------------- #


def _get_dim(dimensions: dict, *ids: str) -> dict | None:
    for dim_id in ids:
        value = dimensions.get(dim_id)
        if isinstance(value, dict):
            return value
    return None


def _field(dim: dict | None, *keys: str) -> Any:
    if not isinstance(dim, dict):
        return None
    for key in keys:
        if key in dim and dim[key] not in (None, ""):
            return dim[key]
    return None


def adapt_analysis(dimensions: dict | None) -> AdapterResult:
    """把 Analysis.dimensions dict 保守转换为资格预审 requirements。"""
    result = AdapterResult()
    if not isinstance(dimensions, dict) or not dimensions:
        result.warnings.append("dimensions 为空，未解析出任何资格要求")
        return result

    qual = _get_dim(dimensions, "qualification")
    timeline = _get_dim(dimensions, "timeline")
    project_info = _get_dim(dimensions, "project_info")

    bid_deadline = _parse_date_cn(_to_text(_field(timeline, "bid_deadline", "投标截止日期", "投标截止时间")))

    # --- capital ---
    cap_text = _to_text(_field(qual, "registered_capital", "注册资金要求", "registeredCapital", "注册资金", "注册资本"))
    _adapt_capital(cap_text, "qualification.registered_capital", result)

    # --- certificate ---
    cert_value = _field(
        qual,
        "qualification_level",
        "资质等级要求",
        "qualificationLevel",
        "资质要求",
        "证书要求",
        "certificates",
        "certificate_list",
        "证书列表",
    )
    cert_texts = cert_value if isinstance(cert_value, list) else [cert_value]
    for i, item in enumerate(cert_texts):
        _adapt_certificate(_to_text(item), f"qualification.qualification_level[{i}]", result)

    # --- project_experience ---
    perf_text = _to_text(_field(qual, "performance_requirement", "业绩要求", "performanceRequirement"))
    _adapt_performance(perf_text, bid_deadline, "qualification.performance_requirement", result)

    # --- personnel ---
    pers_text = _to_text(
        _field(qual, "personnel_requirement", "人员要求", "personnelRequirement", "人员要求（项目经理等）")
    )
    _adapt_personnel(pers_text, "qualification.personnel_requirement", result)

    # --- region：扫描资格/风险/项目信息中的地区限制 ---
    region_fields = [
        (_field(qual, "other_requirements", "其他资格要求", "otherRequirements"), "qualification.other_requirements"),
        (
            _field(
                _get_dim(dimensions, "risk"),
                "unreasonable_requirements",
                "不合理要求",
                "exclusivity_clauses",
                "排他性条款",
            ),
            "risk.unreasonable_requirements",
        ),
        (_field(project_info, "project_overview", "项目概况简述", "projectOverview"), "project_info.project_overview"),
    ]
    for value, source_path in region_fields:
        text = _to_text(value)
        if text:
            _adapt_region(text, source_path, result)

    if not result.requirements and not result.unresolved_items:
        result.warnings.append("dimensions 中未找到可适配的资格要求字段")

    return result


__all__ = ["AdapterResult", "UnresolvedItem", "adapt_analysis"]
