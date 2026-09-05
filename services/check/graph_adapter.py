"""22 项规则检查的图内只读包装入口（P-D1 新增）。

只允许调用现有检查 skill（services/check/skills/**，零改动）并归一化结果；
严禁改动现有检查行为。输入不足的检查项显式返回 skipped(带原因)，禁伪造结果。

供 core/agent_engine/rule_gate.py（确定性规则门节点）调用。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from core.skill_engine.base import SkillContext

MAX_TEXT_LEN = 4000
# Checks that decide bid/no-bid must see the complete assembled document.
# Truncating these inputs makes later chapters (licenses, pricing, deposits,
# signatures) look absent even when they were generated and persisted.
FULL_TEXT_CHECKS = frozenset(
    {
        "qualification_check",
        "mandatory_req_check",
        "disqualification_check",
        "validity_check",
        "deposit_check",
        "doc_integrity_check",
        "ebid_submit_check",
        "pricing_check",
        "pricing_logic_check",
        "signature_check",
    }
)

SUPPLEMENTAL_EVIDENCE_CHECKS = frozenset(
    {
        "qualification_check",
        "validity_check",
        "compliance_check",
        "deposit_check",
        "joint_bid_check",
        "sample_report_check",
        "pricing_logic_check",
        "ebid_submit_check",
        "mandatory_req_check",
        "policy_consistency_check",
        "doc_integrity_check",
    }
)

# (check_id, 检查名, skill类导入路径, 需要的输入)
# required_input: tender_text / bid_text / derived(依赖前置检查结果)
_CHECK_SPECS: list[tuple[str, str, str, tuple[str, ...]]] = [
    # (check_id, 检查名, skill类导入路径, 需要的输入: tender_text/bid_text/derived:*)
    ("qualification_check", "资质证书核查",
     "services.check.skills.qualification_check_skill.QualificationCheckSkill", ("tender_text", "bid_text")),
    ("mandatory_req_check", "强制要求核查",
     "services.check.skills.mandatory_req_check_skill.MandatoryReqCheckSkill", ("tender_text", "bid_text")),
    ("disqualification_check", "废标条款核查",
     "services.check.skills.disqualification_check_skill.DisqualificationCheckSkill", ("tender_text", "bid_text")),
    ("validity_check", "有效期核查",
     "services.check.skills.validity_check_skill.ValidityCheckSkill", ("tender_text", "bid_text")),
    ("compliance_check", "合规性检查",
     "services.check.skills.compliance_check_skill.ComplianceCheckSkill", ("tender_text", "bid_text")),
    ("consistency_check", "一致性检查",
     "services.check.skills.consistency_check_skill.ConsistencyCheckSkill", ("bid_text",)),
    # G-7 否决修复③：政策划型一致性（确定性）——中小企业声明 vs 企业营收划型
    ("policy_consistency_check", "政策划型一致性(确定性)",
     "services.check.skills.policy_consistency_check_skill.PolicyConsistencyCheckSkill", ("bid_text",)),
    ("cross_check", "交叉核对",
     "services.check.skills.cross_check_skill.CrossCheckSkill", ("tender_text", "bid_text")),
    ("deposit_check", "保证金核查",
     "services.check.skills.deposit_check_skill.DepositCheckSkill", ("tender_text", "bid_text")),
    ("doc_integrity_check", "文件完整性检查",
     "services.check.skills.doc_integrity_check_skill.DocIntegrityCheckSkill", ("tender_text", "bid_text")),
    ("ebid_submit_check", "电子投标递交核查",
     "services.check.skills.ebid_submit_check_skill.EbidSubmitCheckSkill", ("tender_text", "bid_text")),
    ("fit_score", "评分匹配度",
     "services.check.skills.fit_score_skill.FitScoreSkill", ("tender_text", "bid_text")),
    ("joint_bid_check", "联合体投标核查",
     "services.check.skills.joint_bid_check_skill.JointBidCheckSkill", ("tender_text", "bid_text")),
    ("pricing_check", "报价检查",
     "services.check.skills.pricing_check_skill.PricingCheckSkill", ("tender_text", "bid_text")),
    ("pricing_logic_check", "报价逻辑检查",
     "services.check.skills.pricing_logic_check_skill.PricingLogicCheckSkill", ("tender_text", "bid_text")),
    ("sample_report_check", "样品/检测报告核查",
     "services.check.skills.sample_report_check_skill.SampleReportCheckSkill", ("tender_text", "bid_text")),
    ("signature_check", "签章核查",
     "services.check.skills.signature_check_skill.SignatureCheckSkill", ("tender_text", "bid_text")),
    ("ai_text_check", "AI文本检测",
     "services.check.skills.ai_text_check_skill.AITextCheckSkill", ("bid_text",)),
    ("duplicate_check", "重复率检查(确定性)",
     "services.check.skills.duplicate_check_skill.DuplicateCheckSkill", ("bid_text",)),
    ("whitelist_filter", "白名单过滤(确定性)",
     "services.check.skills.whitelist_filter_skill.WhitelistFilterSkill", ("derived:duplicate",)),
    ("risk_score", "风险评分(确定性)",
     "services.check.skills.risk_score_skill.RiskScoreSkill", ("derived:check_results",)),
    ("selfcheck_list", "自检清单(确定性)",
     "services.check.skills.selfcheck_list_skill.SelfcheckListSkill", ("derived:check_results",)),
    ("check_report_export", "检查报告汇总(确定性)",
     "services.check.skills.check_report_export_skill.CheckReportExportSkill", ("derived:check_results",)),
]

CHECK_REGISTRY: list[dict] = [
    {"check_id": cid, "name": name, "cls": cls, "requires": list(req)}
    for cid, name, cls, req in _CHECK_SPECS
]


def get_check_concurrency() -> int:
    """独立检查并发闸门；默认与 LLM 闸门保持 16，允许压测时单独收敛。"""
    raw = os.getenv("BMP_CHECK_CONCURRENCY", "")
    if not raw:
        try:
            from core.settings import get_settings

            raw = str(get_settings().llm_max_concurrency)
        except Exception:  # noqa: BLE001
            raw = "16"
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 16


def _import_skill(path: str):
    module_name, cls_name = path.rsplit(".", 1)
    import importlib

    return getattr(importlib.import_module(module_name), cls_name)


_CHECK_FOCUS: dict[str, tuple[str, ...]] = {
    "qualification_check": ("资格", "营业执照", "资质", "证书", "人员", "社保", "财务", "税收", "无重大违法"),
    "mandatory_req_check": ("★", "强制", "实质性", "技术参数", "响应", "偏离", "检测报告"),
    "disqualification_check": ("废标", "无效", "★", "偏离", "签署", "盖章", "响应", "承诺"),
    "validity_check": ("有效期", "营业期限", "证书", "投标函", "保证金", "保函", "授权"),
    "compliance_check": ("资格", "营业执照", "税收", "社保", "财务", "信用", "违法", "联合体"),
    "cross_check": ("评分", "评分项", "业绩", "案例", "技术响应", "人员", "检测", "承诺"),
    "deposit_check": ("保证金", "保函", "转账", "回执", "投标有效期", "报价"),
    "doc_integrity_check": ("目录", "附件", "投标函", "报价", "资格", "技术", "商务", "签章"),
    "ebid_submit_check": ("电子投标", "平台", "PDF", "CA", "签章", "目录", "附件", "加密"),
    "fit_score": ("项目总体技术方案", "技术方案", "实施", "服务", "HIS", "报价", "商务", "评分"),
    "joint_bid_check": ("联合体", "联合投标", "营业执照", "协议"),
    "pricing_check": ("报价", "总价", "分项", "价格", "费用", "限价"),
    "pricing_logic_check": ("报价", "分项", "总价", "费用", "数量", "单价", "限价"),
    "sample_report_check": ("检测", "CMA", "CNAS", "样品", "报告", "参数"),
    "signature_check": ("签字", "盖章", "签章", "电子签名", "CA", "投标函", "授权"),
    "ai_text_check": ("正文", "表述", "错别字", "格式"),
}


def _focused_text(text: str, check_id: str, limit: int, fallback: int | None = None) -> str:
    """按检查项从完整投标文本中选取相关章节，避免前置附件挤掉正文。"""
    source = str(text or "")
    if len(source) <= limit:
        return source
    keywords = _CHECK_FOCUS.get(check_id, ())
    blocks = [b.strip() for b in re.split(r"(?m)(?=^##\s+)", source) if b.strip()]
    if not blocks:
        return source[: (fallback or limit)]
    scored = []
    for index, block in enumerate(blocks):
        score = sum(block.count(word) for word in keywords)
        scored.append((score, index, block))
    selected: list[str] = []
    total = 0
    for score, index, block in sorted(scored, key=lambda item: (-item[0], item[1])):
        if score <= 0 and selected:
            continue
        if total + len(block) > limit and selected:
            continue
        selected.append(block)
        total += len(block)
        if total >= limit * 0.85:
            break
    if not selected:
        return source[: (fallback or limit)]
    return "\n\n".join(selected)[:limit]


def _normalize_status(data: dict, check_id: str = "") -> str:
    """从检查 skill 的原始输出归一化状态（与检查行为无关，只做映射）。"""
    checks = data.get("checks") or data.get("findings") or []
    statuses = [str(c.get("status", "")).lower() for c in checks if isinstance(c, dict)]
    if "fail" in statuses:
        # Signature is an explicit final human/platform action in electronic
        # procurement.  Text-only inspection cannot prove a rendered CA seal,
        # so it must remain a warning rather than block the bid decision.
        if check_id == "signature_check":
            return "warning"
        if check_id == "doc_integrity_check":
            failed_details = " ".join(
                str(c.get("detail") or c.get("suggestion") or "")
                for c in checks
                if isinstance(c, dict) and str(c.get("status", "")).lower() == "fail"
            )
            final_artifact_markers = (
                "最终PDF", "电子签章", "CA", "平台上传", "目录", "页码", "骑缝章", "无法从文本"
            )
            if any(marker in failed_details for marker in final_artifact_markers):
                return "warning"
        return "fail"
    if "warning" in statuses:
        return "warning"
    if statuses and all(s == "pass" for s in statuses):
        return "pass"
    if data.get("disqualification_risk") is True:
        return "fail"
    # An empty mandatory-parameter extraction means the tender excerpt did not
    # contain the referenced ★/▲ table. It is an evidence/coverage warning,
    # not proof of a failed technical response.
    if check_id == "mandatory_req_check" and not checks and not data.get("items"):
        return "warning"
    if check_id == "ai_text_check" and data.get("issues"):
        return "warning"
    if data.get("has_critical_issues") is True:
        return "fail"
    risk = str(data.get("risk_level", "")).lower()
    if risk == "high":
        return "fail"
    if risk in ("medium", "middle"):
        return "warning"
    if risk == "low":
        return "pass"
    return "warning" if data else "skipped"


async def run_all_checks(
    tender_text: str,
    bid_text: str,
    llm: Any,
    project_id: str = "",
    check_ids: list[str] | None = None,
    extra_params: dict | None = None,
) -> list[dict]:
    """顺序固定、逐项调用的只读规则门包装。

    - 输入不足 -> {"status": "skipped", "reason": ...}（显式，禁伪造）；
    - LLM 网关缺失 -> LLM 型检查 skipped；
    - skill 抛错 -> {"status": "error", "reason": ...}（不吞错）。
    返回顺序与 CHECK_REGISTRY 一致（确定性）。
    """
    extra_params = extra_params or {}
    results: list[dict | None] = [None] * len(CHECK_REGISTRY)
    duplicate_output: dict = {}
    normalized_results: list[dict] = []

    def make_ctx(params: dict) -> SkillContext:
        return SkillContext(project_id=project_id, db=None, llm=llm, parameters=params)

    async def run_one(index: int, spec: dict) -> None:
        check_id = spec["check_id"]
        requires = spec["requires"]

        if check_ids is not None and check_id not in check_ids:
            return

        missing: list[str] = []
        needs_tender = "tender_text" in requires
        needs_bid = "bid_text" in requires
        needs_derived = [r for r in requires if r.startswith("derived:")]
        if needs_tender and not (tender_text or "").strip():
            missing.append("tender_text")
        if needs_bid and not (bid_text or "").strip():
            missing.append("bid_text")
        derived_ready = True
        executed_results = [r for r in normalized_results if r.get("status") != "skipped"]
        derived_kind = needs_derived[0].split(":", 1)[1] if needs_derived else None
        if derived_kind == "duplicate" and not duplicate_output:
            derived_ready = False
        if derived_kind == "check_results" and not executed_results:
            derived_ready = False

        if missing or (needs_derived and not derived_ready):
            reason = "缺输入: " + ",".join(missing) if missing else f"缺前置派生输入: {derived_kind}"
            results[index] = {
                "check_id": check_id,
                "check_name": spec["name"],
                "status": "skipped",
                "reason": reason,
                "data": {},
            }
            return

        if llm is None and not needs_derived and check_id != "duplicate_check":
            results[index] = {
                "check_id": check_id,
                "check_name": spec["name"],
                "status": "skipped",
                "reason": "无LLM网关，检查项显式跳过",
                "data": {},
            }
            return

        full_text = check_id in FULL_TEXT_CHECKS
        # 技能内部仍有各自的 3k/6k 防护。先按检查类型选相关章节，
        # 这样“投标函/报价/技术方案”不会被前面的资格附件挤出上下文。
        tender_excerpt = _focused_text(tender_text or "", check_id, 12000)
        bid_excerpt = _focused_text(bid_text or "", check_id, 12000)
        params: dict = {
            "tender_text": tender_excerpt if full_text else tender_excerpt[:MAX_TEXT_LEN],
            "bid_text": bid_excerpt if full_text else bid_excerpt[:MAX_TEXT_LEN],
        }
        if check_id in SUPPLEMENTAL_EVIDENCE_CHECKS:
            evidence = str(extra_params.get("supplemental_evidence") or "").strip()
            if evidence:
                # 补充资料是独立证据通道；由材料型 skill 明确读取，不能污染
                # 正式投标正文，否则会把企业资料误判为已递交/已签章内容。
                params["supplemental_evidence"] = evidence[:12000]
                # Keep the evidence channel explicit, but put a compact copy
                # ahead of the focused bid excerpt for material checks.  This
                # prevents an LLM from missing uploaded proof merely because
                # the relevant chapter fell outside its local focus window;
                # the evidence is still labelled as supplemental and never
                # becomes persisted bid正文.
                params["bid_text"] = (
                    "【项目RAG补充证据（非正文）】\n"
                    + evidence[:8000]
                    + "\n\n【投标正文】\n"
                    + bid_excerpt
                )
        if check_id == "policy_consistency_check":
            # G-7 终验收口（2026-09-03）：确定性划型检查不走 LLM，无输入长度成本，
            # 必须用全文——此前截断 4000 字使投标函政策声明落在窗口外而漏检（假阴性 pass）。
            params["bid_text"] = bid_text or ""
            evidence = str(extra_params.get("supplemental_evidence") or "").strip()
            if evidence:
                params["supplemental_evidence"] = evidence[:12000]
        params.update(extra_params)
        if derived_kind == "duplicate":
            params = {
                "bid_text": (bid_text or "")[:MAX_TEXT_LEN],
                "reference_texts": extra_params.get("reference_texts", [bid_text or ""]),
            }
        elif derived_kind == "check_results":
            executed = [r for r in normalized_results if r.get("status") != "skipped"]
            params = {"check_results": {"results": executed}}
            if check_id == "check_report_export":
                # G-7 收官修复（2026-09-03）：此前该派生项只传 check_results，而
                # CheckReportExportSkill 读的是 report_data → 恒报"无报告数据" error。
                # 与 graph_runtime.export 节点同口径构造信封映射。
                params["report_data"] = {
                    r.get("check_id", "unknown"): (
                        {"success": False, "error": r.get("reason", "执行异常")}
                        if r.get("status") == "error"
                        else {"success": True, "data": r.get("data") or {"risk_level": r.get("status", "skipped")}}
                    )
                    for r in executed
                }
                params["format"] = "markdown"
        elif check_id == "duplicate_check":
            params.setdefault("reference_texts", extra_params.get("reference_texts", [bid_text or ""]))

        skill_cls = _import_skill(spec["cls"])
        started = time.monotonic()
        try:
            skill_result = await skill_cls().safe_execute(make_ctx(params))
            elapsed_ms = round((time.monotonic() - started) * 1000, 2)
            if skill_result.success:
                data = skill_result.data or {}
                item = {
                    "check_id": check_id,
                    "check_name": spec["name"],
                    "status": _normalize_status(data, check_id),
                    "duration_ms": elapsed_ms,
                    "data": data,
                }
                if skill_result.warnings:
                    item["warnings"] = skill_result.warnings
                results[index] = item
                if check_id == "duplicate_check":
                    duplicate_output.clear()
                    duplicate_output.update(data)
            else:
                results[index] = {
                    "check_id": check_id,
                    "check_name": spec["name"],
                    "status": "error",
                    "reason": skill_result.error or "skill执行失败",
                    "duration_ms": elapsed_ms,
                    "data": {},
                }
        except Exception as e:  # noqa: BLE001
            results[index] = {
                "check_id": check_id,
                "check_name": spec["name"],
                "status": "error",
                "reason": f"执行异常: {e}",
                "data": {},
            }

    # 分两波：第一波独立检查并行，第二波依赖派生输入（顺序确定）。
    # gather 返回按输入顺序排列，results 仍按 registry index 回填，兼容旧消费者。
    semaphore = asyncio.Semaphore(get_check_concurrency())

    async def run_wave1(index: int, spec: dict) -> None:
        async with semaphore:
            await run_one(index, spec)

    wave1 = [
        (index, spec)
        for index, spec in enumerate(CHECK_REGISTRY)
        if not any(r.startswith("derived:") for r in spec["requires"])
    ]
    await asyncio.gather(*(run_wave1(index, spec) for index, spec in wave1))
    normalized_results = [r for r in results if r]
    for index, spec in enumerate(CHECK_REGISTRY):
        if any(r.startswith("derived:") for r in spec["requires"]):
            await run_one(index, spec)

    return [r for r in results if r]
