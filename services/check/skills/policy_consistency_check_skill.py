"""G-7 终验否决修复③：政策划型一致性检查（确定性，无 LLM）。

正文声称适用中小企业/小微企业等政策扶持时，与企业事实库的财务数据
（近三年营业收入）做工信部划型一致性校验：软信业年营收超 1 亿元即非
中小微企业，正文仍声明中小企业 → fail（真实场景废标风险），修复建议
"删除该声明或改写为实际情况"。企业库不可达/无财务事实时显式 skipped。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from core.skill_engine.base import Skill, SkillContext, SkillResult

logger = logging.getLogger(__name__)

# 划型门槛：软件和信息技术服务业营业收入 1 亿元（工信部划型，单位：万元）
_SME_REVENUE_THRESHOLD_WAN = 10000.0

_CLAIM_RE = re.compile(r"中小企业|小微企业|中型企业|小型企业|微利企业")

# ── G-7 终验收口（2026-09-03，负责人终读缺陷①修复）──────────────────────
# 旧实现把"出现中小企业字样"一律当声明，导致"不适用/不附"等正确表述误报，
# 且 graph_adapter 截断 4000 字后投标函政策声明在窗口外 → 漏检假阴性。
# 现改为句级识别"划型声明句"：
#   命中（任一）：确认/符合中小企业扶持条件；填写/提供/出具/附/填报《中小企业声明函》；
#                适用中小企业（价格扣除）扶持政策；我方/我司/本公司 属于/是 中小微企业
#   排除：否定表述（不附/不出具/不适用/大型企业…）、条件句（如/若/未来…符合）、
#         法规转述（供应商应当…）
_CLAIM_PATTERNS = [
    re.compile(r"符合中小企业(?:政策扶持)?条件"),
    re.compile(r"(?:我方|我司|我单位|本公司|投标人)(?:属于|是|为)(?:中小|小微|小型|中型|微利)企业"),
    re.compile(r"(?:已|现|特此|按要求)[^。；\n]{0,12}填写[了]?《?中小企业声明函"),
    re.compile(r"(?:已|现|特此)[^。；\n]{0,12}(?:提供|出具|附|填报)[了]?《?中小企业声明函"),
    re.compile(r"(?:提供|出具|附|填报)[了]?中小企业声明函"),
    re.compile(r"适用中小企业(?:价格扣除)?(?:扶持|优惠)政策"),
]
_CLAIM_NEGATION_RE = re.compile(
    r"不附|不出具|不提供|未出具|不得|不适用|不主张|无需|并非|不属于|大型企业|不涉及"
    r"|未填写|不填写|未附|不填报|未填报|未提供"
)
_CLAIM_CONDITIONAL_RE = re.compile(r"(?:如|若|无论|未来)[^。；\n]{0,12}符合")
_CLAIM_REGULATION_CITE_RE = re.compile(r"供应商应当|供应商应按规定")

# 营收数值：支持 23,108.4 万 / 23108.4万元 / 2.3 亿 / 230,000,000元
_REVENUE_WAN_RE = re.compile(r"((?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?)\s*万")
_REVENUE_YI_RE = re.compile(r"((?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?)\s*亿")
_REVENUE_YUAN_RE = re.compile(r"(\d{1,3}(?:,\d{3}){2,}|\d{7,})\s*元")


def _to_wan(raw: str, unit: str) -> float:
    value = float(raw.replace(",", ""))
    if unit == "亿":
        return value * 10000.0
    if unit == "元":
        return value / 10000.0
    return value  # 万


def _max_revenue_wan(text: str) -> float:
    candidates: list[float] = []
    for m in _REVENUE_WAN_RE.finditer(text):
        candidates.append(_to_wan(m.group(1), "万"))
    for m in _REVENUE_YI_RE.finditer(text):
        candidates.append(_to_wan(m.group(1), "亿"))
    for m in _REVENUE_YUAN_RE.finditer(text):
        candidates.append(_to_wan(m.group(1), "元"))
    return max(candidates) if candidates else 0.0


def _extract_sme_claims(text: str) -> list[str]:
    """句级提取"划型声明句"（G-7 终验收口）：命中声明模式、无否定/条件/法规转述。"""
    claims: list[str] = []
    for sent in re.split(r"[。；;！!？?\n]", text or ""):
        s = sent.strip()
        if not s:
            continue
        if not any(p.search(s) for p in _CLAIM_PATTERNS):
            continue
        if _CLAIM_NEGATION_RE.search(s):
            continue
        if _CLAIM_CONDITIONAL_RE.search(s):
            continue
        if _CLAIM_REGULATION_CITE_RE.search(s):
            continue
        claims.append(s[:80])
    return sorted(set(claims))


class PolicyConsistencyCheckSkill(Skill):
    name = "policy_consistency_check"
    description = "政策划型一致性检查(确定性)：中小企业声明与企业营收划型校验"
    category = "check"
    version = "1.0.0"
    triggers = ["政策核查", "划型核查"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        bid_text = str(ctx.parameters.get("bid_text") or "")
        supplemental = str(ctx.parameters.get("supplemental_evidence") or "")
        if not bid_text:
            return SkillResult(success=False, error="投标文件内容为空")

        claims = _extract_sme_claims(bid_text)
        if not claims:
            return SkillResult(
                success=True,
                data={
                    "checks": [
                        {
                            "check_name": "政策划型一致性检查",
                            "status": "pass",
                            "detail": "正文未声明适用中小企业/小微企业政策，无需划型校验",
                        }
                    ]
                },
            )

        # 企业财务事实：项目证据优先；只有项目证据没有可核算事实时，
        # 才回退到全局企业库。这样补充材料能真正参与当前运行，且不
        # 会被旧企业库中的同主题片段抢先命中。
        revenue_wan = 0.0
        evidence = ""
        for source in (supplemental, bid_text):
            rev = _max_revenue_wan(source)
            if rev > revenue_wan:
                revenue_wan, evidence = rev, source[:200]
        try:
            if revenue_wan <= 0:
                from core.agent_engine.generate_graph import get_default_knowledge_base_cached

                kb = await get_default_knowledge_base_cached()
                if kb is not None and hasattr(kb, "retrieve"):
                    hits = await kb.retrieve(query="近三年 营业收入 净利润 财务 审计 划型", top_k=6)
                    for h in hits:
                        text = str(h.get("text") or "")
                        rev = _max_revenue_wan(text)
                        if rev > revenue_wan:
                            revenue_wan, evidence = rev, text[:200]
        except Exception as exc:  # noqa: BLE001
            logger.warning("政策划型检查检索企业库失败: %s", exc)

        if revenue_wan <= 0:
            return SkillResult(
                success=True,
                data={
                    "checks": [
                        {
                            "check_name": "政策划型一致性检查",
                            "status": "skipped",
                            "detail": "企业库中未检索到可核算的营业收入事实，无法做划型一致性校验",
                        }
                    ]
                },
            )

        is_sme = revenue_wan < _SME_REVENUE_THRESHOLD_WAN
        if is_sme:
            return SkillResult(
                success=True,
                data={
                    "checks": [
                        {
                            "check_name": "政策划型一致性检查",
                            "status": "pass",
                            "detail": f"企业库营收 {revenue_wan:,.1f} 万元低于划型门槛，中小企业声明与事实一致",
                        }
                    ]
                },
            )

        return SkillResult(
            success=True,
            data={
                "checks": [
                    {
                        "check_name": "政策划型一致性检查",
                        "status": "fail",
                        "severity": "critical",
                        "location_a": "正文政策声明",
                        "value_a": "、".join(claims),
                        "location_b": "企业财务事实",
                        "value_b": f"年营业收入 {revenue_wan:,.1f} 万元（超过 1 亿元划型门槛）",
                        "detail": (
                            f"正文声称适用中小企业/小微企业政策（{'、'.join(claims)}），但企业事实为"
                            f"年营收 {revenue_wan:,.1f} 万元，按工信部划型不属于中小微企业，"
                            "中小企业声明自相矛盾，真实投标场景构成废标风险"
                        ),
                        "suggestion": "删除该中小企业声明或改写为实际情况（如按大型企业身份响应相应政策条款）",
                        "evidence_excerpt": evidence,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            },
        )


__all__ = ["PolicyConsistencyCheckSkill", "_max_revenue_wan", "_SME_REVENUE_THRESHOLD_WAN"]
