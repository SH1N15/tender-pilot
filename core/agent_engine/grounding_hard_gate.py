"""P-G G-2：Grounding 硬门（citation_ledger 硬约束，G-0 收束方案）。

背景（G-0 结论）：机制侧全部接通但 qwen3.7-flash 对输出里标【N】服从性差
（节点级 3 轮实测 before 0/6→after 0/1、0/20、0/1）。本模块把 Grounding 从
"提示"升级为"图内确定性门"：

1. 正文生成后确定性后处理校验：硬事实断言必须携带【n】且 n 在 ledger 内，
   且断言值在所引 chunk 原文命中（复用 evidence_gate.ground_hard_facts 判定）；
2. 未过校验的硬事实 → `suggest_anchor` 确定性反查：在 ledger 中找
   "归一化断言值命中"的 chunk，给出建议编号【n】（不依赖模型自己挑证据）；
3. 携带建议编号触发一次修正重生成（LLM 只做句子改写/删除，不做新断言）；
4. 修正后仍不过 → 确定性降级：该句替换为【待补充】(原因；待补充：值)，
   绝不编造（与 evidence_gate.ground_hard_facts 同一替换语义）；
5. 锚点完整性审计：最终文本中的【n】全部必须在 ledger 内（防编造引用编号）。

判定全部确定性可单测；LLM 修正通道可注入（测试用 FakeLLM）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from core.agent_engine.evidence_gate import (
    CITATION_MARKER_RE,
    ground_hard_facts,
    normalize_value,
)

# 门判定结论（三态）
VERDICT_PASS = "pass"  # 初稿即全过或无硬事实
VERDICT_REVISED = "revised"  # 一次修正重生成后全过
VERDICT_DEGRADED = "degraded"  # 最终确定性降级（残余拒收句已替换为【待补充】）

ReviseFunc = Callable[[str, list[dict]], Awaitable[str]]


def suggest_anchor(value: str, ledger: dict) -> int | None:
    """确定性反查：断言值（归一化）命中的 ledger chunk 编号；无命中返回 None。"""
    needle = normalize_value(str(value or ""))
    if not needle:
        return None
    for n, entry in sorted(ledger.items()):
        try:
            n_int = int(n)
        except (TypeError, ValueError):
            continue
        if needle in normalize_value(str(entry.get("text", ""))):
            return n_int
    return None


def build_revise_hints(rejected: list[dict], ledger: dict) -> list[dict]:
    """由拒收明细构建修正提示（kind/value/sentence + 确定性建议编号【n】）。"""
    hints: list[dict] = []
    for fact in rejected or []:
        n = suggest_anchor(fact.get("value", ""), ledger)
        hints.append(
            {
                "kind": fact.get("kind", ""),
                "value": fact.get("value", ""),
                "sentence": fact.get("sentence", ""),
                "reason": fact.get("reason", ""),
                "suggest_n": n,
            }
        )
    return hints


def audit_anchors(text: str, ledger: dict) -> dict:
    """锚点完整性审计（确定性）：最终文本中【n】是否全部在 ledger 内。"""
    valid = {int(n) for n, _ in (ledger or {}).items() if str(n).lstrip("-").isdigit()}
    found = [int(m.group(1)) for m in CITATION_MARKER_RE.finditer(text or "")]
    fabricated = sorted({n for n in found if n not in valid})
    return {"anchors": found, "fabricated": fabricated}


def _strip_fabricated(text: str, fabricated: list[int]) -> str:
    """删除编造引用编号的标记（保留句子文字，只摘掉越界【n】标记）。"""
    out = text or ""
    for n in fabricated or []:
        out = out.replace(f"【{n}】", "", 1)
    return out


def build_revise_messages(text: str, hints: list[dict], ledger: dict) -> list[dict]:
    """一次修正重生成的消息（编号建议确定性给出，模型只改写句子）。"""
    hint_lines = []
    for h in hints or []:
        line = f"- kind={h['kind']} 值=『{h['value']}』原因={h['reason']}"
        if h.get("suggest_n"):
            line += f"；依据在参考材料【{h['suggest_n']}】——请改写该句并在句末标注【{h['suggest_n']}】"
        else:
            line += "；库内无据——请删除该数值并将该处写为（知识库无据，待补充）"
        hint_lines.append(line)
    materials = "\n".join(
        f"【引用{n}】(chunk_id={e.get('chunk_id')}, source={e.get('source')}):\n{str(e.get('text', ''))[:600]}"
        for n, e in sorted((ledger or {}).items())
    )
    user_content = (
        f"正文：\n{text[:6000]}\n\n未过校验断言：\n{'\n'.join(hint_lines)}\n\n编号参考材料：\n{materials[:8000]}"
    )
    return [
        {
            "role": "system",
            "content": (
                "你是引用修正器。对给出正文中的未过校验硬事实断言逐条处理：\n"
                "1. 给了建议编号【n】且证据支持：改写该句并在句末标注【n】；\n"
                "2. 库内无据：删除该数值，写（知识库无据，待补充），绝不编造；\n"
                "3. 其余句子原样保留。直接输出修正后的完整正文，不要 JSON、不要解释。"
            ),
        },
        {"role": "user", "content": user_content},
    ]


async def run_hard_gate(
    content: str,
    ledger: dict,
    revise: ReviseFunc | None = None,
) -> dict:
    """Grounding 硬门主流程（确定性可单测；revise 可注入）。

    返回：
    {
        "verdict": pass | revised | degraded,
        "rounds": 0|1,
        "before": 初稿 grounding stats {total, passed, rejected}（模型服从性）,
        "after": 门后 stats {total, passed, degraded, rejected}（rejected 恒 0）,
        "text": 门后放行文本,
        "degraded": 降级明细列表,
        "anchor_audit": {anchors, fabricated},
        "revise_hints": 修正提示（修正轮触发时）,
    }
    """
    # 第 1 轮：初稿确定性校验（evidence_gate 同源判定）
    first = ground_hard_facts(content or "", ledger)
    before = dict(first["stats"])
    audit = audit_anchors(first["text"], ledger)
    if audit["fabricated"]:
        first["text"] = _strip_fabricated(first["text"], audit["fabricated"])

    if before["rejected"] == 0:
        return {
            "verdict": VERDICT_PASS,
            "rounds": 0,
            "before": before,
            "after": {"total": before["total"], "passed": before["passed"], "degraded": 0, "rejected": 0},
            "text": first["text"],
            "degraded": [],
            "anchor_audit": audit_anchors(first["text"], ledger),
            "revise_hints": [],
        }

    hints = build_revise_hints(first["rejected"], ledger)
    current_text = first["text"]
    after_stats = {"total": before["total"], "passed": before["passed"], "degraded": len(hints), "rejected": 0}

    if revise is not None:
        # 第 2 轮：携带确定性建议编号的一次修正重生成
        try:
            revised_text = await revise(current_text, hints)
        except Exception:  # noqa: BLE001  # 修正通道异常→走确定性降级，绝不阻断放行
            revised_text = ""
        if revised_text:
            second = ground_hard_facts(revised_text, ledger)
            audit2 = audit_anchors(second["text"], ledger)
            if audit2["fabricated"]:
                second["text"] = _strip_fabricated(second["text"], audit2["fabricated"])
            if second["stats"]["rejected"] == 0:
                # 修正成功：放行修正文本（初稿已降级的句不追溯，以修正文本为准）
                return {
                    "verdict": VERDICT_REVISED,
                    "rounds": 1,
                    "before": before,
                    "after": {
                        "total": second["stats"]["total"],
                        "passed": second["stats"]["passed"],
                        "degraded": 0,
                        "rejected": 0,
                    },
                    "text": second["text"],
                    "degraded": [],
                    "anchor_audit": audit_anchors(second["text"], ledger),
                    "revise_hints": hints,
                }
            # 修正后仍有拒收 → 确定性降级（ground_hard_facts 已替换为【待补充】）
            current_text = second["text"]
            after_stats = {
                "total": before["total"],
                "passed": before["passed"] + second["stats"]["passed"],
                "degraded": len(second["rejected"]),
                "rejected": 0,
            }

    # 无修正通道或修正后仍不过：初稿/二稿中拒收句已被替换为【待补充】——最终放行
    return {
        "verdict": VERDICT_DEGRADED,
        "rounds": 1 if revise is not None else 0,
        "before": before,
        "after": after_stats,
        "text": current_text,
        "degraded": first["rejected"] if revise is None else hints,
        "anchor_audit": audit_anchors(current_text, ledger),
        "revise_hints": hints,
    }


def gate_pass_rate(before: dict, after: dict) -> dict:
    """门前后通过率对照（汇报用数字；确定性）。"""
    b_total = int(before.get("total", 0) or 0)
    a_total = int(after.get("total", 0) or 0)
    return {
        "before_rate": round(before.get("passed", 0) / b_total, 4) if b_total else 1.0,
        "after_rate": round(after.get("passed", 0) / a_total, 4) if a_total else 1.0,
        "integrity": 1.0 if int(after.get("rejected", 0) or 0) == 0 else 0.0,
    }


__all__ = [
    "VERDICT_PASS",
    "VERDICT_REVISED",
    "VERDICT_DEGRADED",
    "suggest_anchor",
    "build_revise_hints",
    "audit_anchors",
    "build_revise_messages",
    "run_hard_gate",
    "gate_pass_rate",
]
