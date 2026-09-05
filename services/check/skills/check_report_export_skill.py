from __future__ import annotations

import json
import logging
from datetime import datetime

from core.skill_engine.base import Skill, SkillContext, SkillResult

logger = logging.getLogger(__name__)

# 检查项名称兜底链：LLM 各检查项字段名不统一，逐一回退，最终给出占位标题
_NAME_KEYS = (
    "check_name",
    "check_type",
    "name",
    "title",
    "content",
    "scoring_item",
    "requirement",
    "item",
    "category",
)


def _check_name(check: dict) -> str:
    for key in _NAME_KEYS:
        val = check.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, (int, float)) and str(val).strip():
            return str(val)
    return "未命名检查项"


class CheckReportExportSkill(Skill):
    name = "check_report_export"
    description = "检查报告导出(Markdown/PDF/HTML)"
    category = "check"
    version = "1.0.0"
    triggers = ["导出报告", "报告导出", "检查报告"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        report_data = ctx.parameters.get("report_data", {})
        format_type = ctx.parameters.get("format", "markdown")
        project_name = ctx.parameters.get("project_name", "未命名项目")

        if not report_data:
            return SkillResult(success=False, error="无报告数据")

        if format_type == "markdown":
            content = self._to_markdown(report_data, project_name)
        elif format_type == "html":
            content = self._to_html(report_data, project_name)
        elif format_type == "json":
            content = json.dumps(report_data, ensure_ascii=False, indent=2)
        else:
            content = self._to_markdown(report_data, project_name)

        # Worker J（净化层）：MD/HTML 交付物统一过导出净化——剥离章节正文引用
        # 进报告后带出的【n】锚点/拒收原因等生成期内部痕迹（json 为数据接口，保留原样）。
        if format_type != "json":
            from core.agent_engine.export_sanitizer import sanitize_export_text

            content, _report = sanitize_export_text(content)

        return SkillResult(
            success=True,
            data={
                "content": content,
                "format": format_type,
                "size": len(content.encode("utf-8")),
                "generated_at": datetime.now().isoformat(),
            },
        )

    def _extract_sections(self, data: dict) -> list[tuple[str, dict]]:
        """从报告中提取 (小节标题, 内层结果字典) 列表。

        支持三种存储形态：
        1. 单项检查信封: {success, data: {checks/risk_level/...}}
        2. 全量检查映射: {check_type: {success, data: {...}}}
        3. 扁平结果: {checks: [...], risk_level: ...}
        """
        sections: list[tuple[str, dict]] = []
        if not isinstance(data, dict):
            return sections
        if "checks" in data or "risk_level" in data:
            return [("", data)]
        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            inner = val.get("data") if isinstance(val.get("data"), dict) else val
            if isinstance(inner, dict) and any(
                k in inner for k in ("checks", "items", "categories", "dimensions", "risk_level", "overall_risk")
            ):
                sections.append((str(key), inner))
            elif val.get("success") is False and val.get("error"):
                # P4：执行异常的检查项也透出小节与异常摘要（不改判定，仅透出原因）
                sections.append((str(key), {"execution_error": str(val["error"])}))
        return sections

    def _render_detail(self, result: dict, lines: list) -> None:
        """渲染单个检查结果中的明细（checks / items / categories / dimensions）。"""
        if result.get("execution_error"):
            lines.append(f"- ⛔ 执行异常: {result['execution_error']}")
            lines.append("")
            return
        checks = result.get("checks")
        if not isinstance(checks, list) and isinstance(result.get("items"), list):
            checks = result.get("items")
        if not isinstance(checks, list) and isinstance(result.get("categories"), list):
            checks = [
                item
                for cat in result["categories"]
                if isinstance(cat, dict) and isinstance(cat.get("items"), list)
                for item in cat["items"]
            ]
        if not isinstance(checks, list):
            checks = []

        summary = result.get("summary", {}) if isinstance(result.get("summary"), dict) else {}
        risk_level = result.get("risk_level") or result.get("overall_risk") or "unknown"
        risk_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(str(risk_level), "⚪")
        lines.append(f"- 风险等级: {risk_emoji} {risk_level}")
        if summary:
            lines.append(f"- 总检查项: {summary.get('total', len(checks))}")
            lines.append(f"- 通过: {summary.get('passed', summary.get('passed_items', 0))}")
            lines.append(f"- 不通过: {summary.get('failed', 0)}")
            lines.append(f"- 警告: {summary.get('warning', 0)}")
        lines.append("")

        if checks:
            lines.append("### 详细检查结果")
            lines.append("")
            lines.append("| 检查项 | 要求 | 实际 | 状态 | 说明 |")
            lines.append("|--------|------|------|------|------|")
            for check in checks:
                if not isinstance(check, dict):
                    continue
                name = _check_name(check)
                required = str(
                    check.get(
                        "required",
                        check.get(
                            "required_by_tender",
                            check.get("scoring_criteria", check.get("clause_content", check.get("requirement", ""))),
                        ),
                    )
                )[:40]
                actual = str(check.get("actual", check.get("found_in_bid", check.get("response_content", ""))))[:40]
                status = check.get("status", check.get("response_status", check.get("response_quality", "unknown")))
                detail = str(check.get("detail", check.get("suggestion", check.get("gap_analysis", ""))))[:50]
                icon_map = {
                    "pass": "✅", "fail": "❌", "warning": "⚠️",
                    "compliant": "✅", "answered": "✅", "missing": "❌", "partial": "⚠️",
                }
                status_icon = icon_map.get(str(status), "")
                lines.append(f"| {name} | {required} | {actual} | {status_icon} {status} | {detail} |")
            lines.append("")
        elif isinstance(result.get("dimensions"), dict):
            lines.append("| 维度 | 得分 | 权重 | 说明 |")
            lines.append("|------|------|------|------|")
            for dim, info in result["dimensions"].items():
                if isinstance(info, dict):
                    desc = str(info.get("description", ""))[:60]
                    lines.append(f"| {dim} | {info.get('score', '')} | {info.get('weight', '')} | {desc} |")
            lines.append("")

        failed = [c for c in checks if isinstance(c, dict) and c.get("status") in ("fail", "missing", "non_compliant")]
        if failed:
            lines.append("#### ❌ 不通过项详情")
            lines.append("")
            for i, check in enumerate(failed, 1):
                title = _check_name(check)
                lines.append(f"**{i}. {title}**")
                lines.append("")
                required = check.get("required", check.get("scoring_criteria", check.get("clause_content")))
                if required:
                    lines.append(f"- **招标要求**: {required}")
                if check.get("actual", check.get("response_content")):
                    lines.append(f"- **投标文件**: {check.get('actual', check.get('response_content'))}")
                if check.get("detail"):
                    lines.append(f"- **详细说明**: {check.get('detail')}")
                if check.get("suggestion"):
                    lines.append(f"- **修改建议**: {check.get('suggestion')}")
                lines.append("")

    def _to_markdown(self, data: dict, project_name: str) -> str:
        lines = []
        lines.append("# 投标文件检查报告")
        lines.append("")
        lines.append(f"**项目名称**: {project_name}")
        lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        sections = self._extract_sections(data)
        if sections and "checks" not in data:
            # 多小节报告（全量检查映射或信封结构）
            risk_order = {"high": 3, "medium": 2, "low": 1}
            overall = "low"
            for _, s in sections:
                rl = str(s.get("risk_level") or s.get("overall_risk") or "low")
                if s.get("has_critical_issues") or s.get("disqualification_risk"):
                    rl = "high"
                if risk_order.get(rl, 0) > risk_order.get(overall, 0):
                    overall = rl
            risk_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(overall, "⚪")
            lines.append("## 检查概要")
            lines.append("")
            lines.append(f"- 综合风险等级: {risk_emoji} {overall}")
            lines.append(f"- 检查小节数: {len(sections)}")
            lines.append("")
            for title, section in sections:
                lines.append(f"## {title or '未命名小节'}")
                lines.append("")
                self._render_detail(section, lines)
            lines.append("---")
            lines.append("*报告由投标智航 / TenderPilot 自动生成*")
            return "\n".join(lines)

        checks = data.get("checks", [])
        if isinstance(checks, list):
            summary = data.get("summary", {})
            risk_level = data.get("risk_level", "unknown")

            risk_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(risk_level, "⚪")
            lines.append("## 检查概要")
            lines.append("")
            lines.append(f"- 风险等级: {risk_emoji} {risk_level}")
            if summary:
                lines.append(f"- 总检查项: {summary.get('total', len(checks))}")
                lines.append(f"- 通过: {summary.get('passed', 0)}")
                lines.append(f"- 不通过: {summary.get('failed', 0)}")
                lines.append(f"- 警告: {summary.get('warning', 0)}")
            lines.append("")

            lines.append("## 详细检查结果")
            lines.append("")
            lines.append("| 检查项 | 要求 | 实际 | 状态 | 说明 |")
            lines.append("|--------|------|------|------|------|")
            for check in checks:
                if isinstance(check, dict):
                    name = _check_name(check)
                    required = str(check.get("required", ""))[:30]
                    actual = str(check.get("actual", ""))[:30]
                    status = check.get("status", "unknown")
                    detail = str(check.get("detail", check.get("suggestion", "")))[:40]
                    status_icon = {"pass": "✅", "fail": "❌", "warning": "⚠️"}.get(status, status)
                    lines.append(f"| {name} | {required} | {actual} | {status_icon} {status} | {detail} |")
            lines.append("")

            failed = [c for c in checks if isinstance(c, dict) and c.get("status") == "fail"]
            if failed:
                lines.append("## ❌ 不通过项详情")
                lines.append("")
                for i, check in enumerate(failed, 1):
                    if isinstance(check, dict):
                        lines.append(f"### {i}. {_check_name(check)}")
                        lines.append("")
                        lines.append(f"- **招标要求**: {check.get('required', '')}")
                        lines.append(f"- **投标文件**: {check.get('actual', '')}")
                        lines.append(f"- **详细说明**: {check.get('detail', '')}")
                        suggestion = check.get("suggestion", "")
                        if suggestion:
                            lines.append(f"- **修改建议**: {suggestion}")
                        lines.append("")

        elif isinstance(data, dict):
            for key, value in data.items():
                if key in ("checks", "summary", "risk_level", "has_critical_issues"):
                    continue
                lines.append(f"## {key}")
                lines.append("")
                if isinstance(value, (list, dict)):
                    lines.append("```json")
                    lines.append(json.dumps(value, ensure_ascii=False, indent=2)[:2000])
                    lines.append("```")
                else:
                    lines.append(f"{value}")
                lines.append("")

        lines.append("---")
        lines.append("*报告由投标智航 / TenderPilot 自动生成*")

        return "\n".join(lines)

    def _to_html(self, data: dict, project_name: str) -> str:
        md_content = self._to_markdown(data, project_name)

        html_parts = [
            "<!DOCTYPE html>",
            "<html><head><meta charset='utf-8'>",
            f"<title>检查报告 - {project_name}</title>",
            "<style>",
            "body { font-family: 'Microsoft YaHei', sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; }",
            "h1 { color: #1a56db; border-bottom: 2px solid #1a56db; padding-bottom: 8px; }",
            "h2 { color: #374151; margin-top: 24px; }",
            "table { border-collapse: collapse; width: 100%; margin: 12px 0; }",
            "th, td { border: 1px solid #d1d5db; padding: 8px 12px; text-align: left; }",
            "th { background: #f3f4f6; font-weight: 600; }",
            ".risk-high { color: #dc2626; font-weight: 700; }",
            ".risk-medium { color: #d97706; font-weight: 700; }",
            ".risk-low { color: #059669; font-weight: 700; }",
            ".pass { color: #059669; } .fail { color: #dc2626; } .warning { color: #d97706; }",
            "hr { border: none; border-top: 1px solid #e5e7eb; margin: 24px 0; }",
            "</style></head><body>",
        ]

        for line in md_content.split("\n"):
            if line.startswith("# "):
                html_parts.append(f"<h1>{line[2:]}</h1>")
            elif line.startswith("## "):
                html_parts.append(f"<h2>{line[3:]}</h2>")
            elif line.startswith("### "):
                html_parts.append(f"<h3>{line[4:]}</h3>")
            elif line.startswith("- "):
                html_parts.append(f"<li>{line[2:]}</li>")
            elif line.startswith("| ") and "|" in line[1:]:
                cells = [c.strip() for c in line.split("|")[1:-1]]
                row = "".join(f"<td>{c}</td>" for c in cells)
                html_parts.append(f"<tr>{row}</tr>")
            elif line.startswith("---"):
                html_parts.append("<hr>")
            elif line.strip():
                html_parts.append(f"<p>{line}</p>")

        html_parts.append("</body></html>")
        return "\n".join(html_parts)
