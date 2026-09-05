"""招标要求—企业能力资格预审：确定性规则匹配引擎。

设计原则：
1. 所有规则均为确定性判断（日期、金额、数量、地区），绝不调用 LLM。
2. 任何 "met" 结论都必须由"带证据引用(evidence_refs)"的证明材料支撑；
   没有证据引用时最多只能得到 insufficient，绝不输出 met。
3. 缺字段、类型错误、无法解析、冲突证据一律收敛为 insufficient / warnings，
   不向调用方抛出未处理异常。
"""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import ValidationError

from services.qualification.models import (
    Credential,
    MatchReport,
    MatchResult,
    MatchSummary,
    Requirement,
)

# --------------------------------------------------------------------------- #
# 解析辅助（确定性，不含 LLM）
# --------------------------------------------------------------------------- #

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日")

_AMOUNT_UNITS: dict[str, float] = {
    "亿元": 100_000_000.0,
    "万元": 10_000.0,
    "亿": 100_000_000.0,
    "万": 10_000.0,
    "元": 1.0,
}


def _parse_date(value: date | str | None) -> tuple[date | None, str | None]:
    """把 date / ISO 字符串解析为 date。返回 (date, error)。"""
    if value is None or value == "":
        return None, None
    if isinstance(value, date):
        return value, None
    text = str(value).strip()
    if not text:
        return None, None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date(), None
        except ValueError:
            continue
    # 兼容 yyyy-mm / yyyy
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1), None
    m = re.fullmatch(r"(\d{4})", text)
    if m:
        return date(int(m.group(1)), 1, 1), None
    return None, f"无法解析日期: {value!r}"


def _parse_amount(value: object) -> tuple[float | None, str | None]:
    """把数值 / 带单位字符串解析为元。返回 (金额(元), error)。"""
    if value is None or value == "":
        return None, None
    if isinstance(value, bool):
        return None, "金额字段类型错误(布尔值)"
    if isinstance(value, (int, float)):
        return float(value), None
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return None, None
    unit = ""
    for u in ("亿元", "万元", "亿", "万", "元"):
        if text.endswith(u):
            unit = u
            text = text[: -len(u)].strip()
            break
    m = re.fullmatch(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None, f"金额无法解析: {value!r}"
    return float(m.group(0)) * _AMOUNT_UNITS.get(unit, 1.0), None


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _has_evidence(cred: Credential) -> bool:
    return bool(cred.evidence_refs)


def _collect_refs(creds: list[Credential]) -> list[str]:
    refs: list[str] = []
    for c in creds:
        for r in c.evidence_refs:
            if r not in refs:
                refs.append(r)
    return refs


def _ids(creds: list[Credential]) -> list[str]:
    return [c.credential_id for c in creds]


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in err.get("loc", ()))
    return f"{loc}: {err.get('msg', '数据无效')}"


# --------------------------------------------------------------------------- #
# 各类型子匹配器
# --------------------------------------------------------------------------- #


def _match_certificate(req: Requirement, cands: list[Credential]) -> MatchResult:
    base = {"requirement_id": req.requirement_id, "requirement_type": req.requirement_type}
    name = (req.certificate_name or "").strip()
    if not name:
        return MatchResult(**base, status="insufficient", reason="缺少证书名称要求(certificate_name)")

    matching = [c for c in cands if _name_matches(c, name)]
    if not matching:
        return MatchResult(**base, status="unmet", reason=f"已核查证书类材料，但未找到名称为「{name}」的证书")

    # 未要求有效期：仅验证"存在且附证据"
    if req.valid_until is None or req.valid_until == "":
        verified = [c for c in matching if _has_evidence(c)]
        if verified:
            return MatchResult(
                **base,
                status="met",
                reason=f"已提供证书「{name}」（未要求有效期覆盖）",
                evidence_refs=_collect_refs(verified),
                matched_credential_ids=_ids(verified),
                warnings=(
                    [f"{len(matching) - len(verified)} 份证书缺少证据引用"] if len(verified) < len(matching) else []
                ),  # noqa: E501
            )
        return MatchResult(
            **base,
            status="insufficient",
            reason=f"证书「{name}」未附带任何证据引用(evidence_refs)，无法确认",
            matched_credential_ids=_ids(matching),
            warnings=["缺少证据引用"],
        )

    until, until_err = _parse_date(req.valid_until)
    if until_err:
        return MatchResult(
            **base,
            status="insufficient",
            reason="要求的证书有效期截止日(valid_until)无法解析",
            warnings=[until_err],
        )

    valid: list[Credential] = []
    expired: list[Credential] = []
    unknown: list[Credential] = []
    for c in matching:
        if not _has_evidence(c):
            unknown.append(c)
            continue
        exp, err = _parse_date(c.expiry_date)
        if err or exp is None:
            unknown.append(c)
            continue
        (valid if exp >= until else expired).append(c)

    if valid:
        warns: list[str] = []
        if expired:
            warns.append(f"{len(expired)} 份证书已过期（不影响通过结论）")
        if unknown:
            warns.append(f"{len(unknown)} 份证书缺少有效期或证据引用")
        return MatchResult(
            **base,
            status="met",
            reason=f"证书「{name}」有效期覆盖至 {until.isoformat()}",
            evidence_refs=_collect_refs(valid),
            matched_credential_ids=_ids(valid),
            warnings=warns,
        )

    if unknown:
        return MatchResult(
            **base,
            status="insufficient",
            reason=f"证书「{name}」缺少有效期或证据引用，无法确认是否覆盖 {until.isoformat()}",
            matched_credential_ids=_ids(unknown),
            warnings=[f"{len(unknown)} 份证书无法判定有效期"],
        )

    latest = max(_parse_date(c.expiry_date)[0] for c in expired)  # type: ignore[arg-type]
    return MatchResult(
        **base,
        status="unmet",
        reason=f"证书「{name}」最晚有效期至 {latest.isoformat()}，未覆盖要求的 {until.isoformat()}",
        evidence_refs=_collect_refs(expired),
        matched_credential_ids=_ids(expired),
    )


def _match_capital(req: Requirement, cands: list[Credential]) -> MatchResult:
    base = {"requirement_id": req.requirement_id, "requirement_type": req.requirement_type}
    min_amount, min_err = _parse_amount(req.min_amount)
    if min_err or min_amount is None:
        return MatchResult(
            **base,
            status="insufficient",
            reason="缺少最低注册资本要求(min_amount)",
            warnings=[min_err] if min_err else [],
        )

    parsed: list[tuple[float, Credential]] = []
    other_currency: list[Credential] = []
    unparseable: list[Credential] = []
    for c in cands:
        if not _has_evidence(c):
            unparseable.append(c)
            continue
        if c.currency not in (None, "", "CNY", "RMB", "人民币"):
            other_currency.append(c)
            continue
        raw = c.amount if c.amount is not None else c.amount_text
        amt, err = _parse_amount(raw)
        if err or amt is None:
            unparseable.append(c)
            continue
        parsed.append((amt, c))

    if not parsed:
        warns = [f"{len(unparseable)} 份材料金额缺失、无法解析或缺少证据引用"] if unparseable else []
        warns.extend([f"{len(other_currency)} 份材料币种非人民币，暂不支持"] if other_currency else [])
        return MatchResult(
            **base,
            status="insufficient",
            reason="注册资本证明材料金额缺失、无法解析或缺少证据引用，无法确认",
            matched_credential_ids=_ids(unparseable + other_currency),
            warnings=warns,
        )

    best = max(amt for amt, _ in parsed)
    conflict = len({round(amt, 4) for amt, _ in parsed}) > 1
    warns = ["不同材料注册资本金额不一致，取最大值参与判定"] if conflict else []
    if best >= min_amount:
        winners = [c for amt, c in parsed if amt == best]
        return MatchResult(
            **base,
            status="met",
            reason=f"注册资本 {best:,.2f} 元 ≥ 要求 {min_amount:,.2f} 元",
            evidence_refs=_collect_refs(winners),
            matched_credential_ids=_ids(winners),
            warnings=warns,
        )
    return MatchResult(
        **base,
        status="unmet",
        reason=f"注册资本 {best:,.2f} 元 < 要求 {min_amount:,.2f} 元",
        evidence_refs=_collect_refs([c for _, c in parsed]),
        matched_credential_ids=_ids([c for _, c in parsed]),
        warnings=warns,
    )


def _match_project_experience(req: Requirement, cands: list[Credential]) -> MatchResult:
    base = {"requirement_id": req.requirement_id, "requirement_type": req.requirement_type}
    min_count = req.min_count if req.min_count is not None else 1
    need_amount = req.min_amount is not None and req.min_amount != ""
    if need_amount:
        min_amount, min_err = _parse_amount(req.min_amount)
        if min_err or min_amount is None:
            return MatchResult(
                **base,
                status="insufficient",
                reason="最低业绩金额(min_amount)无法解析",
                warnings=[min_err] if min_err else [],
            )
    need_window = req.date_from is not None or req.date_to is not None
    if need_window and (req.date_from is None or req.date_to is None):
        return MatchResult(**base, status="insufficient", reason="业绩时间窗口不完整，须同时提供 date_from 与 date_to")

    date_from, date_from_err = _parse_date(req.date_from)
    date_to, date_to_err = _parse_date(req.date_to)
    if need_window and (date_from_err or date_to_err or date_from is None or date_to is None):
        return MatchResult(
            **base,
            status="insufficient",
            reason="业绩时间窗口无法解析",
            warnings=[w for w in (date_from_err, date_to_err) if w],
        )

    eligible: list[Credential] = []
    unknown: list[Credential] = []
    for c in cands:
        if not _has_evidence(c):
            unknown.append(c)
            continue
        if need_amount:
            amt, err = _parse_amount(c.contract_amount if c.contract_amount is not None else c.contract_amount_text)
            if err or amt is None:
                unknown.append(c)
                continue
            if amt < min_amount:  # type: ignore[operator]
                continue  # 明确不满足金额，不计数也不计未知
        if need_window:
            comp, cerr = _parse_date(c.completion_date)
            if cerr or comp is None:
                unknown.append(c)
                continue
            if not (date_from <= comp <= date_to):  # type: ignore[operator]
                continue  # 时间窗口外，明确不满足
        eligible.append(c)

    if len(eligible) >= min_count:
        return MatchResult(
            **base,
            status="met",
            reason=f"符合要求的业绩 {len(eligible)} 项 ≥ 要求 {min_count} 项",
            evidence_refs=_collect_refs(eligible),
            matched_credential_ids=_ids(eligible),
            warnings=[f"{len(unknown)} 项业绩信息不足"] if unknown else [],
        )
    if len(eligible) + len(unknown) >= min_count:
        return MatchResult(
            **base,
            status="insufficient",
            reason=f"已确认业绩 {len(eligible)} 项，另有 {len(unknown)} 项信息不足，无法确认是否达到 {min_count} 项",
            matched_credential_ids=_ids(unknown),
            warnings=[f"{len(unknown)} 项业绩缺少金额/日期/证据引用"],
        )
    return MatchResult(
        **base,
        status="unmet",
        reason=f"已确认业绩 {len(eligible)} 项 < 要求 {min_count} 项",
        evidence_refs=_collect_refs(eligible),
        matched_credential_ids=_ids(eligible),
    )


def _match_personnel(req: Requirement, cands: list[Credential]) -> MatchResult:
    base = {"requirement_id": req.requirement_id, "requirement_type": req.requirement_type}
    title = (req.personnel_title or "").strip()
    if not title:
        return MatchResult(**base, status="insufficient", reason="缺少人员岗位要求(personnel_title)")
    min_count = req.min_count if req.min_count is not None else 1

    matching = [c for c in cands if _name_matches(c, title) and _has_evidence(c)]
    unverified = [c for c in cands if _name_matches(c, title) and not _has_evidence(c)]
    unknown = [c for c in cands if not _name_matches(c, title) and not (c.personnel_title or c.name or "").strip()]

    if len(matching) >= min_count:
        return MatchResult(
            **base,
            status="met",
            reason=f"符合岗位「{title}」的人员 {len(matching)} 名 ≥ 要求 {min_count} 名",
            evidence_refs=_collect_refs(matching),
            matched_credential_ids=_ids(matching),
            warnings=[f"{len(unverified)} 份人员材料缺少证据引用"] if unverified else [],
        )
    if len(matching) + len(unknown) + len(unverified) >= min_count:
        unsure = len(unknown) + len(unverified)
        return MatchResult(
            **base,
            status="insufficient",
            reason=f"符合岗位「{title}」的人员 {len(matching)} 名，另有 {unsure} 名信息不足，无法确认是否达标",
            matched_credential_ids=_ids(unknown + unverified),
        )
    return MatchResult(
        **base,
        status="unmet",
        reason=f"符合岗位「{title}」的人员仅 {len(matching)} 名 < 要求 {min_count} 名",
        evidence_refs=_collect_refs(matching),
        matched_credential_ids=_ids(matching),
    )


def _match_region(req: Requirement, cands: list[Credential]) -> MatchResult:
    base = {"requirement_id": req.requirement_id, "requirement_type": req.requirement_type}
    region = (req.region or "").strip()
    if not region:
        return MatchResult(**base, status="insufficient", reason="缺少地区要求(region)")

    matching = [c for c in cands if _region_matches(c, region) and _has_evidence(c)]
    unverified = [c for c in cands if _region_matches(c, region) and not _has_evidence(c)]
    missing_region = [c for c in cands if not _region_matches(c, region) and not (c.region or "").strip()]

    if matching:
        return MatchResult(
            **base,
            status="met",
            reason=f"企业注册地满足地区要求「{region}」",
            evidence_refs=_collect_refs(matching),
            matched_credential_ids=_ids(matching),
            warnings=[f"{len(unverified)} 份地区材料缺少证据引用"] if unverified else [],
        )
    if unverified or missing_region:
        return MatchResult(
            **base,
            status="insufficient",
            reason=f"存在缺少注册地信息或证据引用的材料，无法确认是否满足「{region}」",
            matched_credential_ids=_ids(unverified + missing_region),
        )
    return MatchResult(**base, status="unmet", reason=f"企业注册地不满足地区要求「{region}」")


# --------------------------------------------------------------------------- #
# 名称/地区匹配（确定性）
# --------------------------------------------------------------------------- #


def _name_matches(cred: Credential, target: str) -> bool:
    """证书名称 / 岗位名称匹配：归一化后精确相等或互相包含。"""
    t = _norm(target)
    if not t:
        return False
    value = _norm(cred.certificate_name or cred.personnel_title or cred.name or "")
    return bool(value) and (value == t or t in value or value in t)


def _region_matches(cred: Credential, target: str) -> bool:
    t = _norm(target)
    if not t:
        return False
    value = _norm(cred.region or "")
    return bool(value) and (value == t or t in value or value in t)


# --------------------------------------------------------------------------- #
# 公共入口
# --------------------------------------------------------------------------- #

_MATCHERS = {
    "certificate": _match_certificate,
    "capital": _match_capital,
    "project_experience": _match_project_experience,
    "personnel": _match_personnel,
    "region": _match_region,
}


def match_requirement(req: Requirement, credentials: list[Credential]) -> MatchResult:
    """匹配单条资格要求（内部使用，供外部单测）。"""
    cands = [c for c in credentials if c.credential_type == req.requirement_type]
    if not cands:
        return MatchResult(
            requirement_id=req.requirement_id,
            requirement_type=req.requirement_type,
            status="insufficient",
            reason=f"未提供类型为 {req.requirement_type} 的证明材料，无法确认",
            warnings=[f"缺少 {req.requirement_type} 类证明材料"],
        )
    matcher = _MATCHERS.get(req.requirement_type)
    if matcher is None:
        return MatchResult(
            requirement_id=req.requirement_id,
            requirement_type=req.requirement_type,
            status="insufficient",
            reason=f"不支持的资格要求类型: {req.requirement_type}",
        )
    return matcher(req, cands)


def match_qualifications(
    requirements: list[dict | Requirement] | None,
    credentials: list[dict | Credential] | None,
) -> MatchReport:
    """入口：输入 requirements + credentials，输出结构化 MatchReport。

    - 不调用 LLM，纯确定性规则。
    - 单个无效要求/材料会被降级为 insufficient 结果或全局 warning，不抛异常。
    - 只有"要求数组/材料数组"本身类型错误时才抛 TypeError/ValueError（由上层转成 4xx）。
    """
    if not isinstance(requirements, list):
        raise TypeError("requirements 必须是数组")
    if not isinstance(credentials, list):
        raise TypeError("credentials 必须是数组")

    # P1-3：加载「已发布」规则包（无规则包时为空列表，行为与此前一致）
    published_rules = get_published_rules()

    warnings: list[str] = []
    creds: list[Credential] = []
    for raw in credentials:
        try:
            creds.append(raw if isinstance(raw, Credential) else Credential.model_validate(raw))
        except ValidationError as e:
            warnings.append(f"已忽略无效证明材料: {_first_error(e)}")

    results: list[MatchResult] = []
    for raw in requirements:
        try:
            req = raw if isinstance(raw, Requirement) else Requirement.model_validate(raw)
        except ValidationError as e:
            rid = raw.get("requirement_id", "?") if isinstance(raw, dict) else "?"
            results.append(
                MatchResult(
                    requirement_id=str(rid),
                    requirement_type="unknown",
                    status="insufficient",
                    reason="要求数据无效，无法参与匹配",
                    warnings=[_first_error(e)],
                )
            )
            continue
        req = _apply_rule_pre_match(req, published_rules)
        results.append(match_requirement(req, creds))

    if published_rules:
        results, warnings = _apply_rule_post_match(results, creds, published_rules, warnings)

    return _build_report(results, warnings)


def _build_report(results: list[MatchResult], warnings: list[str]) -> MatchReport:
    total = len(results)
    met = sum(1 for r in results if r.status == "met")
    unmet = sum(1 for r in results if r.status == "unmet")
    insufficient = total - met - unmet

    if total == 0:
        overall = "insufficient"
        warnings = [*warnings, "没有提供任何资格要求(requirements)"]
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
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# 已发布规则包叠加层（P1-3）
# --------------------------------------------------------------------------- #


def get_published_rules() -> list[dict]:
    """加载所有「已发布」状态规则包中的规则（草稿/已回滚不生效）。

    无规则包、存储不可用时返回空列表——matcher 行为与此前完全一致（向后兼容）。
    """
    try:
        from services.qualification.rule_governance import RuleGovernanceStore

        packs = RuleGovernanceStore.instance().published_packs()
        return [rule for pack in packs for rule in pack.rules]
    except Exception:  # noqa: BLE001
        return []


def _apply_rule_pre_match(req: Requirement, rules: list[dict]) -> Requirement:
    """匹配前叠加：capital_min_amount_multiplier 收紧金额下界。"""
    for rule in rules:
        if rule.get("template") == "capital_min_amount_multiplier" and req.requirement_type == "capital":
            req_type_scope = rule.get("requirement_type", "")
            if req_type_scope and req_type_scope != req.requirement_type:
                continue
            multiplier = rule.get("params", {}).get("multiplier")
            if isinstance(multiplier, (int, float)) and multiplier > 0:
                min_amount, err = _parse_amount(req.min_amount)
                if not err and min_amount is not None:
                    req = req.model_copy(
                        update={"min_amount": min_amount * multiplier},
                    )
    return req


def _apply_rule_post_match(
    results: list[MatchResult],
    creds: list[Credential],
    rules: list[dict],
    warnings: list[str],
) -> tuple[list[MatchResult], list[str]]:
    """匹配后叠加：require_evidence / certificate_name_min_length / insufficient_review_threshold。"""
    require_evidence_rules = [r for r in rules if r.get("template") == "require_evidence"]
    cert_min_len_rules = [r for r in rules if r.get("template") == "certificate_name_min_length"]
    threshold_rules = [r for r in rules if r.get("template") == "insufficient_review_threshold"]

    cred_by_id = {c.credential_id: c for c in creds}
    adjusted: list[MatchResult] = []
    for res in results:
        # require_evidence：met 必须有证据引用支撑，否则降级 insufficient
        for rule in require_evidence_rules:
            scope = rule.get("requirement_type", "")
            if scope and scope != res.requirement_type:
                continue
            if res.status == "met" and not res.evidence_refs:
                adjusted.append(
                    res.model_copy(
                        update={
                            "status": "insufficient",
                            "reason": f"{res.reason}；[规则包] 缺少证据引用，按 require_evidence 规则降级",
                            "warnings": [*(res.warnings or []), "规则包要求提供证据引用(require_evidence)"],
                        }
                    )
                )
                break
        else:
            # certificate_name_min_length：匹配到的证书名称过短 → 降级 insufficient
            downgraded = False
            for rule in cert_min_len_rules:
                scope = rule.get("requirement_type", "")
                if scope and scope != res.requirement_type:
                    continue
                min_length = rule.get("params", {}).get("min_length")
                if not isinstance(min_length, int) or min_length < 1:
                    continue
                if res.status == "met" and res.requirement_type == "certificate":
                    matched = [cred_by_id[i] for i in res.matched_credential_ids if i in cred_by_id]
                    short = [
                        c
                        for c in matched
                        if len((c.certificate_name or "").strip()) < min_length
                    ]
                    if short:
                        adjusted.append(
                            res.model_copy(
                                update={
                                    "status": "insufficient",
                                    "reason": (
                                        f"{res.reason}；[规则包] 证书名称长度不足（要求 ≥ {min_length} 字），"
                                        "按 certificate_name_min_length 规则降级"
                                    ),
                                    "warnings": [*(res.warnings or []), f"规则包：证书名称长度 ≥ {min_length}"],
                                }
                            )
                        )
                        downgraded = True
                        break
            if not downgraded:
                adjusted.append(res)

    # insufficient_review_threshold：insufficient 数量达到阈值 → 提示人工复核
    for rule in threshold_rules:
        threshold = rule.get("params", {}).get("threshold")
        if isinstance(threshold, int) and threshold >= 1:
            n_insufficient = sum(1 for r in adjusted if r.status == "insufficient")
            if n_insufficient >= threshold:
                warnings = [
                    *warnings,
                    f"[规则包] 信息不足项 {n_insufficient} 项 ≥ 阈值 {threshold}，建议人工复核",
                ]
                break

    return adjusted, warnings


__all__ = [
    "get_published_rules",
    "match_qualifications",
    "match_requirement",
    "MatchReport",
    "MatchResult",
]
