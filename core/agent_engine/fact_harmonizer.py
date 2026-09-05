"""Deterministic chapter fact harmonization.

LLM prompts are useful guidance, but they cannot be the only mechanism that
decides whether a chapter uses the current project facts.  This module keeps
that last mile deterministic and generic: it derives canonical values from
the retrieved evidence ledger, selects values relevant to a chapter's
semantic topic, and replaces stale placeholders or conflicting values while
adding the matching evidence anchor.
"""

from __future__ import annotations

import re
from typing import Any

_PROFILES: dict[str, tuple[str, ...]] = {
    "identity": ("营业执照", "投标人", "企业名称", "统一社会信用代码", "法定代表人", "注册资本", "营业期限"),
    "qualification": ("资质", "资格", "证书", "营业执照", "软件著作权", "认证"),
    "authorization": ("授权", "委托", "法定代表人", "授权代表", "受托人"),
    "validity": ("有效期", "投标有效期", "工期", "实施周期", "质保", "服务承诺"),
    "deposit": ("保证金", "保函", "银行回单", "到账"),
    "pricing": ("报价", "投标总价", "总报价", "报价汇总", "价格", "金额", "费用", "分项", "税率"),
    "personnel": ("项目负责人", "技术负责人", "人员", "社保", "团队"),
}

_MONEY_TOKEN_RE = re.compile(
    # Require a monetary shape (grouped/4+ digit integer or decimal).  This
    # deliberately excludes ordinary day counts such as “90日” that can
    # appear on the same business-summary line as a price.
    r"(?<![\d.])(?:\d{1,3}(?:[,，]\d{3})+|\d{4,}|\d+\.\d{1,2})\s*(?:万?元|元|万元)?"
)
_ATTACHMENT_HEADING_RE = re.compile(r"(?m)^\s*\d+\.\s+([^\n]+\.(?:txt|pdf|docx|xlsx))\s*$", re.I)


def classify_profile(title: str) -> set[str]:
    text = str(title or "")
    return {name for name, words in _PROFILES.items() if any(word in text for word in words)} or {"identity"}


def _first_match(entries: list[tuple[int, str]], patterns: tuple[str, ...]) -> tuple[str, int | None]:
    """Choose a canonical value from overlapping project uploads.

    Project evidence is intentionally append-only.  Taking the first hit lets
    an older attachment override a later consolidated sheet.  We therefore
    count normalized candidates and use the strongest consensus, with ledger
    order as the deterministic tie-breaker.
    """
    candidates: list[tuple[str, int]] = []
    for number, text in entries:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I)
            if not match:
                continue
            value = next((g for g in match.groups()[::-1] if g), match.group(0))
            cleaned = str(value).strip(" ：:，,。；;")
            if cleaned:
                candidates.append((cleaned, number))
            break
    if not candidates:
        return "", None
    counts: dict[str, int] = {}
    for value, _ in candidates:
        counts[value.casefold()] = counts.get(value.casefold(), 0) + 1
    best = max(candidates, key=lambda item: (counts[item[0].casefold()], -item[1]))
    return best


def extract_canonical_facts(ledger: dict[int, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Extract a small canonical fact set, preferring project evidence.

    Ledger order is already project-first in the generation/repair paths.  A
    project collection is still explicitly preferred in case a caller builds
    a ledger in another order.
    """
    rows = []
    for number, entry in (ledger or {}).items():
        try:
            n = int(number)
        except (TypeError, ValueError):
            continue
        text = str(entry.get("text") or "")
        if text.strip():
            collection = str(entry.get("collection") or entry.get("source") or "")
            priority = 0 if "kb_proj" in collection or "项目" in collection else 1
            rows.append((priority, n, text))
    rows.sort(key=lambda item: (item[0], item[1]))
    entries = [(n, text) for _, n, text in rows]

    specs: dict[str, tuple[str, ...]] = {
        # ``名称`` alone is intentionally excluded: tender evidence commonly
        # contains ``项目名称`` immediately before the bidder name and the
        # broad pattern used to misclassify the project as the company.
        "company_name": (r"(?:企业名称|投标人名称|供应商名称|供应商|投标人)\s*[：:]\s*([^\n；;。]+)",),
        "credit_code": (r"统一社会信用代码\s*[：:]\s*([0-9A-Z]{18})",),
        "legal_representative": (r"(?:法定代表人|法人)\s*[：:]\s*([^\n；;。,，]+)",),
        "registered_capital": (r"注册资本\s*[：:]\s*(人民币)?\s*([\d,.，]+\s*万元?|[\d,.，]+\s*元)",),
        "validity_days": (r"投标有效期\s*[：:]?[^\n。；;]{0,20}?([0-9０-９]+)\s*日",),
        "implementation_days": (r"(?:实施|交付)[^\n。；;]{0,20}?([0-9０-９]+)\s*个?日历(?:天|日)",),
        "warranty_months": (r"(?:质量保证期|质保期)[^\n。；;]{0,20}?([0-9０-９]+)\s*个月",),
        "certificate_expiry": (
            r"(?:有效期(?:至|截止日期?)|证书有效期)\s*[：:]?\s*"
            r"((?:19|20)\d{2}年\d{1,2}月\d{1,2}日)",
        ),
        "revenue_wan": (
            r"(?:营业收入|主营业务收入)[：:]?\s*(?:为|是|约为|约是)?\s*(?:人民币\s*)?"
            r"([\d,.，]+(?:\.\d+)?)\s*万元?",
        ),
        "deposit_exempt": (r"(?:投标保证金|保证金)\s*[：:]\s*((?:本项目)?(?:不收取|无需缴纳|不要求缴纳)[^\n。；;]*)",),
        "deposit_amount": (r"保证金金额\s*[：:]?[^\n。；;]{0,20}?([\d,.，]+(?:\.\d+)?)\s*元",),
        "deposit_receipt": (r"(?:银行回单编号|回单编号)\s*[：:]\s*([^\n；;。]+)",),
        "total_price": (
            r"(?:投标总价|投标总报价)(?:（含税）|\(含税\))?\s*[：：:]\s*(?:人民币\s*)?"
            r"([\d,.，]+(?:\.\d+)?)\s*元",
        ),
        "total_price_upper": (
            r"(?:投标总价|投标总报价|总报价)[^\n。；;]{0,100}?(?:大写|人民币大写)\s*[：:]\s*"
            r"([零壹贰叁肆伍陆柒捌玖拾佰仟万亿元整点]+)",
        ),
        "direct_labor_cost": (
            r"(?:人员人工及直接服务成本合计|直接人工及直接服务成本合计|直接人工成本合计)\s*[：:]?\s*"
            r"([\d,.，]+(?:\.\d+)?)\s*元",
        ),
        "tax_rate": (r"税率\s*[：:]\s*([^\n；;。]+)",),
        "authorized_person": (r"(?:受托人|授权代表|代理人)\s*[：:]\s*([^\n；;。,，]+)",),
        "ca_certificate": (r"(?:数字证书|电子签章)[^\n]{0,20}?([A-Z]{2,}-CA-[0-9-]+)",),
    }
    facts: dict[str, dict[str, Any]] = {}
    for key, patterns in specs.items():
        value, n = _first_match(entries, patterns)
        if value:
            facts[key] = {"value": value, "anchor": n}
    return facts


def _append_anchor(value: str, anchor: int | None) -> str:
    if anchor is None or re.search(rf"【{anchor}】", value):
        return value
    return f"{value}【{anchor}】"


def harmonize_content(
    content: str, chapter_title: str, ledger: dict[int, dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    """Apply relevant canonical facts to generated chapter content.

    The function is intentionally conservative: replacements are limited to
    explicit placeholders and sentences containing the field's semantic
    keywords, so technical prose and unrelated numeric values remain intact.
    """
    text = str(content or "")
    facts = extract_canonical_facts(ledger)
    # A generated chapter can carry an appendix block whose topic is broader
    # than its title.  Include a bounded content probe so a pricing chapter
    # containing a guarantee clause receives the same canonicalization as a
    # chapter explicitly titled “保证金”.
    profiles = classify_profile(f"{chapter_title}\n{text}")
    changed: list[str] = []

    # Remove competing numeric pricing paths before any fact-specific pass.
    # These rows are formatter/LLM artifacts; canonical totals and item rows
    # come from the project evidence ledger below.
    if "pricing" in profiles:
        cleaned: list[str] = []
        for line in text.splitlines(keepends=True):
            if re.search(r"报价让利|安装调试与项目管理服务合计", line) and (
                "|" in line or re.search(r"\d[\d,.，]*\s*元", line)
            ):
                changed.append("remove_stale_pricing_row")
                continue
            cleaned.append(line)
        text = "".join(cleaned)

    # Formatter passes can accidentally prepend a money token to a Markdown
    # heading (for example ``### 6,480,000.00元.1 投标总价...``).  A chapter's
    # persisted title is the structural source of truth, so repair only
    # headings carrying unmistakable money/suffix corruption and preserve the
    # original heading level.  This is title- and project-agnostic.
    canonical_title = re.sub(r"\s+", " ", str(chapter_title or "")).strip()
    if canonical_title:
        heading_re = re.compile(r"(?m)^(?P<indent>\s*)(?P<marks>#{1,6})\s+(?P<title>[^\n]+)$")

        def _repair_heading(match: re.Match[str]) -> str:
            heading = match.group("title").strip()
            malformed_money = bool(
                re.search(r"(?:元(?:\.00)+元?|\d[\d,，]*\.\d{2}元\.\d+|^\d[\d,，]*元(?:\.\d+)?)", heading)
            )
            if malformed_money and not re.search(r"(?:元|万元|金额|报价)\s*$", canonical_title):
                changed.append("chapter_heading_cleanup")
                return f"{match.group('indent')}{match.group('marks')} {canonical_title}"
            return match.group(0)

        text = heading_re.sub(_repair_heading, text)

    # Normalize malformed certificate dates against the current project
    # evidence.  This handles OCR/template artifacts such as “120日” without
    # embedding any customer-specific date or certificate number.
    expiry_by_month: dict[str, str] = {}
    for _, entry in (ledger or {}).items():
        source_text = str((entry or {}).get("text") or "") if isinstance(entry, dict) else str(entry or "")
        for match in re.finditer(r"有效期至\s*[：:]?\s*(\d{4})年(\d{1,2})月(\d{1,2})日", source_text):
            expiry_by_month[f"{match.group(1)}-{int(match.group(2)):02d}"] = match.group(0).split("：")[-1].strip()
    if expiry_by_month:

        def _fix_expiry(match: re.Match[str]) -> str:
            key = f"{match.group(1)}-{int(match.group(2)):02d}"
            replacement = expiry_by_month.get(key)
            return f"有效期至：{replacement}" if replacement else match.group(0)

        fixed = re.sub(r"有效期至\s*[：:]?\s*(\d{4})年(\d{1,2})月\d{1,3}日", _fix_expiry, text)
        if fixed != text:
            changed.append("certificate_expiry_date")
            text = fixed

    # OCR/template passes can truncate a certificate date (for example
    # ``有效期至：...202...``).  Once project evidence contains a complete
    # date, repair only certificate/date lines and never invent a date.
    certificate_expiry = facts.get("certificate_expiry", {}).get("value")
    if certificate_expiry and re.search(r"证书|认证|ISO|有效期", text, re.I):
        repaired = re.sub(
            r"(?m)^([^\n]*(?:证书|认证|ISO)[^\n]*有效期至\s*[：:]?\s*)(?:[^\n]*)$",
            lambda m: f"{m.group(1)}{certificate_expiry}",
            text,
        )
        if repaired != text:
            changed.append("certificate_expiry_truncation")
            text = repaired

    def replace_field(key: str, placeholders: tuple[str, ...], sentence_words: tuple[str, ...], pattern: str) -> None:
        nonlocal text
        fact = facts.get(key)
        if not fact or not fact.get("value"):
            return
        raw_value = str(fact["value"])
        if key == "validity_days" and not raw_value.endswith("日"):
            raw_value += "日"
        elif key == "implementation_days" and "日历" not in raw_value:
            raw_value += "个日历日"
        elif key == "warranty_months" and not raw_value.endswith("个月"):
            raw_value += "个月"
        elif key in {"deposit_amount", "total_price"} and not raw_value.endswith("元"):
            raw_value += "元"
        value = _append_anchor(raw_value, fact.get("anchor"))
        before = text
        for placeholder in placeholders:
            text = text.replace(placeholder, value)
        # Replace stale values only in the matching semantic sentence.
        pieces = re.split(r"(?<=[。；;\n])", text)
        for i, sentence in enumerate(pieces):
            if sentence.lstrip().startswith("#"):
                continue
            if any(word in sentence for word in sentence_words) and re.search(pattern, sentence):

                def _replace(match: re.Match[str]) -> str:
                    matched = match.group(0)
                    numeric_keys = {
                        "validity_days",
                        "implementation_days",
                        "warranty_months",
                        "deposit_amount",
                        "total_price",
                    }
                    if key in numeric_keys:
                        return re.sub(
                            r"\d[\d,.，]*\s*(?:个?日历(?:天|日)|日|个月|元)",
                            value,
                            matched,
                            count=1,
                        )
                    return value

                pieces[i] = re.sub(pattern, _replace, sentence, count=1)
        text = "".join(pieces)
        if text != before:
            changed.append(key)

    if profiles & {"identity", "qualification", "authorization"}:
        replace_field(
            "company_name",
            ("[投标人名称]", "[企业名称]", "____公司", "投标人名称：____"),
            ("投标人", "企业名称", "公司名称"),
            r"(?:投标人|企业名称|公司名称)\s*[：:]\s*(?:\S+)",
        )
        replace_field(
            "credit_code",
            ("[统一社会信用代码]", "统一社会信用代码：____"),
            ("统一社会信用代码",),
            r"统一社会信用代码\s*[：:]\s*\S+",
        )
        replace_field(
            "legal_representative",
            ("[法定代表人]", "[法人]"),
            ("法定代表人", "法人"),
            r"(?:法定代表人|法人)\s*[：:]\s*\S+",
        )
        replace_field(
            "registered_capital",
            ("[注册资本]",),
            ("注册资本",),
            r"注册资本\s*[：:]\s*\S+",
        )
    if profiles & {"validity", "pricing", "deposit"}:
        replace_field(
            "validity_days",
            ("[投标有效期]", "[具体天数]", "____日"),
            ("投标有效期",),
            r"投标有效期[^。；;\n]{0,20}?\d+\s*日",
        )
        replace_field(
            "implementation_days",
            ("[实施周期]", "[工期]", "____个日历日"),
            ("实施", "交付", "工期"),
            r"(?:实施|交付|工期)[^。；;\n]{0,20}?\d+\s*个?日历(?:天|日)",
        )
        replace_field(
            "warranty_months",
            ("[质保期]", "[质量保证期]"),
            ("质保", "质量保证"),
            r"(?:质保期|质量保证期)[^。；;\n]{0,20}?\d+\s*个月",
        )
    if "deposit" in profiles:
        exempt = facts.get("deposit_exempt")
        if exempt and exempt.get("value"):
            canonical = f"投标保证金：{exempt['value']}".replace("投标保证金：本项目本项目", "投标保证金：本项目")
            pieces = re.split(r"(?<=[。；;\n])", text)
            updated: list[str] = []
            for sentence in pieces:
                if (
                    any(word in sentence for word in ("投标保证金", "保证金金额", "缴纳保证金", "保证金凭证"))
                    and re.search(r"\d[\d,.，]*\s*元|银行转账|保函|回单编号|到账", sentence)
                    and "不收取" not in sentence
                    and "无需缴纳" not in sentence
                ):
                    updated.append(canonical + "。")
                    changed.append("deposit_exempt")
                else:
                    updated.append(sentence)
            text = "".join(updated)
        if not exempt:
            replace_field(
                "deposit_amount",
                ("[保证金金额]",),
                ("保证金",),
                r"保证金(?:金额)?[^。；;\n]{0,20}?\d[\d,.，]*\s*元",
            )
        replace_field(
            "deposit_receipt",
            ("[回单编号]",),
            ("回单", "到账"),
            r"(?:银行)?回单编号\s*[：:]\s*\S+",
        )
    if "pricing" in profiles:
        replace_field(
            "total_price",
            ("[投标总价]", "[报价总额]"),
            ("总价", "报价", "金额"),
            r"(?:投标总价|投标报价|报价总额|总报价)[^。；;\n]{0,20}?\d[\d,.，]*\s*元",
        )
        replace_field(
            "tax_rate",
            ("[税率]",),
            ("税率",),
            r"税率\s*[：:]\s*[^。；;\n]+",
        )
        labor = facts.get("direct_labor_cost")
        if labor and labor.get("value"):
            labor_value = str(labor["value"])
            if not labor_value.endswith("元"):
                labor_value += "元"
            text = re.sub(
                r"((?:直接人工(?:及直接服务)?成本|人员人工及直接服务成本)"
                r"[^。；;\n]{0,20}?\s*)[\d,.，]+(?:\.\d+)?\s*元",
                rf"\g<1>{labor_value}",
                text,
            )
    if "authorization" in profiles:
        replace_field(
            "authorized_person",
            ("[授权代表]", "[受托人]"),
            ("授权代表", "受托人"),
            r"(?:授权代表|受托人)\s*[：:]\s*\S+",
        )
    # A known-material placeholder is never a valid final value.
    if facts and profiles:
        text = re.sub(r"（知识库无据，待补充[^）]*）", "", text)
        text = re.sub(r"\(知识库无据，待补充[^)]*\)", "", text)
        # Repair diagnostics are internal orchestration metadata and must not
        # leak into persisted bid chapters or the exported document.
        text = re.sub(
            r"(?m)^\s*-\s*kind=[^\n]*(?:硬事实未携带引用标记|请为该断言补标)[^\n]*\n?",
            "",
            text,
        )

    # Generated chapters frequently contain Markdown tables or pasted
    # attachment excerpts.  They do not have sentence punctuation, so the
    # sentence-level replacements above cannot see stale totals.  Reconcile
    # only rows that explicitly describe a total/aggregate price; item-level
    # prices remain untouched unless their own canonical field is available.
    if "pricing" in profiles and facts.get("total_price", {}).get("value"):
        canonical_total = str(facts["total_price"]["value"])
        if not canonical_total.endswith("元"):
            canonical_total += "元"
        canonical_upper = str(facts.get("total_price_upper", {}).get("value") or "")
        lines: list[str] = []
        for line in text.splitlines(keepends=True):
            if re.search(r"投标总价|投标总报价|投标报价|总报价|报价总额|报价合计|分项(?:报价)?合计", line):

                def _table_money(match: re.Match[str]) -> str:
                    start = max(0, match.start() - 40)
                    context = line[start : match.start() + 1]
                    return (
                        canonical_total
                        if re.search(
                            r"投标总价|投标总报价|投标报价|总报价|报价总额|报价合计|分项(?:报价)?合计",
                            context,
                        )
                        else match.group(0)
                    )

                updated = _MONEY_TOKEN_RE.sub(
                    _table_money,
                    line,
                )
                # The amount is often in a separate Markdown cell, so the
                # proximity replacement above may intentionally find none.
                if updated == line and _MONEY_TOKEN_RE.search(line):
                    updated = _MONEY_TOKEN_RE.sub(canonical_total, line, count=1)
                if canonical_upper and re.search(r"(?:大写|人民币大写)\s*[：:]", updated):
                    updated = re.sub(
                        r"((?:大写|人民币大写)\s*[：:])\s*[零壹贰叁肆伍陆柒捌玖拾佰仟万亿元整点]+",
                        rf"\1{canonical_upper}",
                        updated,
                    )
                if updated != line:
                    changed.append("table_total")
                line = updated
            lines.append(line)
        text = "".join(lines)

        # Normalize common generated-document artifacts using project facts.
        # Scope is limited to pricing semantics, so unrelated technical
        # quantities remain untouched.
        no_hardware = any(
            "不包含硬件" in str((entry or {}).get("text") or "")
            for entry in (ledger or {}).values()
            if isinstance(entry, dict)
        )
        cleaned_lines: list[str] = []
        for line in text.splitlines(keepends=True):
            if no_hardware and re.search(r"网关|服务器|交换机|硬件设备|面板集成|打印机", line):
                changed.append("remove_hardware_pricing_line")
                continue
            if re.search(r"合计|总价|报价|分项和|让利", line):
                fixed_line = re.sub(r"元(?:\.00元)+", "元", line)
                # Aggregate rows are semantically different from item rows:
                # replace only the amount immediately following the aggregate
                # label, never the first item amount on the same line.
                if facts.get("total_price", {}).get("value") and not re.search(r"最高限价|预算", fixed_line):
                    fixed_line = re.sub(
                        r"((?:分项)?(?:报价)?合计|总计)\s*[：:]?\s*"
                        r"[\d,，]+(?:\.\d+)?\s*元?",
                        lambda m: f"{m.group(1)}：{canonical_total}",
                        fixed_line,
                        count=1,
                    )
                if facts.get("tax_rate", {}).get("value") and "税率" in fixed_line:
                    fixed_line = re.sub(
                        r"税率\s*[：:]\s*[^。；;\n]+",
                        f"税率：{facts['tax_rate']['value']}",
                        fixed_line,
                    )
                if "让利" in fixed_line and re.search(r"\d[\d,.，]*\s*元", fixed_line):
                    # Numeric discount rows are a stale competing total, not
                    # a project fact.  Remove them instead of leaving a
                    # second arithmetic path for the checker to follow.
                    fixed_line = ""
                if fixed_line != line:
                    changed.append("pricing_format_cleanup")
                line = fixed_line
            if no_hardware and re.search(r"(?:税率|增值税).*(?:13%|9%)", line):
                changed.append("remove_inapplicable_tax_rate")
                continue
            cleaned_lines.append(line)
        text = "".join(cleaned_lines)

        # Normalize suffix corruption produced by repeated formatter passes,
        # e.g. ``6,480,000.00元.00`` or ``6,480,000.00元.00元``.  This is
        # deliberately amount-shape based and applies to any project.
        normalized_suffix = re.sub(r"元(?:\.00)+(?:元)?", "元", text)
        # A formatter may leave a unit outside the replaced token, e.g.
        # ``6,480,000.00元【1】** 元``.  Collapse only this adjacent unit
        # pattern so ordinary prose and unrelated numbers remain untouched.
        normalized_suffix = re.sub(
            r"(\d[\d,，]*(?:\.\d+)?\s*元(?:【\d+】)?)(?:\*\*)?\s*元",
            r"\1",
            normalized_suffix,
        )
        normalized_suffix = re.sub(r"(?:\.00元){2,}", ".00元", normalized_suffix)
        # Repeated citation/suffix pairs are a formatter idempotency bug, not
        # meaningful evidence.  Collapse only consecutive identical anchors.
        normalized_suffix = re.sub(r"(?P<a>【\d+】)(?:(?P=a))+(?:\.00元)+", "\\g<a>", normalized_suffix)
        normalized_suffix = re.sub(r"(?P<a>【\d+】)(?:\.00元(?P=a))+(?:\.00元)?", "\\g<a>", normalized_suffix)
        normalized_suffix = re.sub(r"(?P<a>【\d+】)(?:(?:\.00元)?【\d+】)+(?:\.00元)?", "\\g<a>", normalized_suffix)
        normalized_suffix = re.sub(r"(?P<a>【\d+】)(?:(?:\.00元)?【\d+】)+(?:\.00元)?", "\\g<a>", normalized_suffix)
        # Adjacent duplicate policy lines are another idempotency artifact.
        text = re.sub(r"(?m)^(价格让利：不单列让利项目，分项报价合计即投标总价。\s*\n){2,}", r"\1", text)
        normalized_suffix = re.sub(r"(?P<a>【\d+】)(?:(?P=a))+", "\\g<a>", normalized_suffix)
        if normalized_suffix != text:
            changed.append("money_suffix_cleanup")
            text = normalized_suffix

        # A hard-fact placeholder must not survive once the project evidence
        # contains the canonical value.  Remove only standalone placeholder
        # lines so surrounding explanatory prose remains intact.
        text_without_placeholders = re.sub(
            r"(?m)^\s*[【\[]?待补充[】\]]?[^\n]*(?:投标总价|报价总额|税率|金额)[^\n]*\n?",
            "",
            text,
        )
        text_without_placeholders = re.sub(r"(?m)^\s*[^\n]*待补充[^\n]*\n?", "", text_without_placeholders)
        if text_without_placeholders != text:
            changed.append("resolved_pricing_placeholder")
            text = text_without_placeholders

    # A project whose current evidence explicitly says no deposit must not
    # retain stale transfer/account/receipt fields from an older attachment.
    # Keep the canonical exemption statement and remove only operational
    # deposit fields, never unrelated bank information in technical prose.
    if "deposit" in profiles and facts.get("deposit_exempt", {}).get("value"):
        kept: list[str] = []
        for line in text.splitlines(keepends=True):
            if re.search(r"保证金金额|缴纳形式|付款账户|到账时间|回单编号|银行转账|保证金凭证", line) and not re.search(
                r"不收取|无需缴纳|不要求缴纳", line
            ):
                changed.append("deposit_legacy_fields")
                continue
            kept.append(line)
        text = "".join(kept)

    # Pasted source excerpts can repeat the same attachment block several
    # times.  Drop later identical filename blocks while retaining the first
    # occurrence, so the final document and integrity checks have one source
    # of truth.  This is filename- and project-agnostic.
    matches = list(_ATTACHMENT_HEADING_RE.finditer(text))
    if matches:
        seen_files: set[str] = set()
        rebuilt: list[str] = []
        cursor = 0
        for index, match in enumerate(matches):
            rebuilt.append(text[cursor : match.start()])
            filename = match.group(1).strip().casefold()
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            block = text[match.start() : next_start]
            if filename in seen_files:
                changed.append("duplicate_attachment_block")
            else:
                seen_files.add(filename)
                rebuilt.append(block)
            cursor = next_start
        rebuilt.append(text[cursor:])
        text = "".join(rebuilt)
    # Policy declarations are mutually exclusive with a confirmed large-
    # enterprise fact.  Prefer an explicit evidence statement and remove only
    # contradictory declaration sentences; no customer name or project id is
    # embedded here.
    policy_large = (
        any(
            re.search(
                r"(?:大型企业|不属于中小|不属于小微|不出具.{0,12}中小企业声明函)",
                str((entry or {}).get("text") or ""),
                re.I,
            )
            for entry in (ledger or {}).values()
            if isinstance(entry, dict)
        )
        or float(str(facts.get("revenue_wan", {}).get("value") or "0").replace(",", "")) >= 10000
    )
    if policy_large and re.search(r"中小企业声明函|小微企业|中小微企业", text):
        sentences = re.split(r"(?<=[。；;\n])", text)
        rebuilt_sentences: list[str] = []
        for sentence in sentences:
            if re.search(
                r"(?:符合|属于|适用|出具|提供|按|享受|申请|选择|促进|扶持).{0,40}(?:中小企业|小微企业|中小微企业)",
                sentence,
            ) or (
                re.search(r"(?:中小企业声明函|小微企业|中小微企业|中小企业价格扣除)", sentence)
                and not re.search(r"不属于|不适用|不出具|不提供|不享受|非专门", sentence)
            ):
                changed.append("policy_declaration")
                continue
            rebuilt_sentences.append(sentence)
        text = "".join(rebuilt_sentences)
        if "不属于中小企业" not in text:
            text += "\n本企业不属于中小企业，不出具《中小企业声明函》，按采购文件规定以大型企业身份响应。"
    return text, {"changed": changed, "facts": sorted(facts), "profiles": sorted(profiles)}


__all__ = ["classify_profile", "extract_canonical_facts", "harmonize_content"]
