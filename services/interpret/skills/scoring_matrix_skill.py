from __future__ import annotations

import json
import re

from core.skill_engine.base import Skill, SkillContext, SkillResult

# 类别映射关键词：评分项名称 -> 类别
_CATEGORY_KEYWORDS = (
    ("价格", "价格"),
    ("报价", "价格"),
    ("商务", "商务"),
    ("技术", "技术"),
)
# 从 description 提取分值（如 "商务部分15.0分"、"满分25分"）
_SCORE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*分")

_WEIGHT_KEYS = (
    ("business_weight", "商务"),
    ("technical_weight", "技术"),
    ("price_weight", "价格"),
)


def _classify(name: str) -> str:
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in name:
            return category
    return "未分类"


def _extract_score(description: str) -> float | None:
    """从评分项描述中提取分值。优先取"满分N分"，其次取首个"N分"。"""
    m = re.search(r"满分\s*(\d+(?:\.\d+)?)\s*分", description)
    if m:
        return float(m.group(1))
    matches = _SCORE_RE.findall(description)
    if matches:
        return float(matches[0])
    return None


def build_matrix_rows(scoring_data: dict) -> list[dict]:
    """BUG-9 修复：确定性消费 LLM 实际输出的 scoring_items 数组。

    每个 scoring_item 产出一行（seq/category/item/score/criteria/response_section/status）。
    description 中可提取分值则提取，否则 score=None 由上游汇总时按 0 处理。
    不调用 LLM，纯确定性规则，便于离线测试。
    """
    if not isinstance(scoring_data, dict):
        return []
    items = scoring_data.get("scoring_items")
    if not isinstance(items, list):
        return []
    rows: list[dict] = []
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        description = str(item.get("description") or item.get("criteria") or "").strip()
        if not name and not description:
            continue
        rows.append(
            {
                "seq": i,
                "category": _classify(name),
                "item": name or description[:20],
                "score": _extract_score(description),
                "criteria": description,
                "response_section": None,
                "status": "pending",
            }
        )
    return rows


def _weights_category_scores(scoring_data: dict) -> dict[str, float]:
    """从 business/technical/price_weight 权重字段构建类别分汇总（仅当按类别汇总为空时使用）。"""
    scores: dict[str, float] = {}
    if not isinstance(scoring_data, dict):
        return scores
    for key, category in _WEIGHT_KEYS:
        value = scoring_data.get(key)
        if isinstance(value, (int, float)):
            scores[category] = scores.get(category, 0.0) + float(value)
    return scores


class ScoringMatrixSkill(Skill):
    name = "scoring_matrix"
    description = "构建评分矩阵"
    category = "interpret"
    version = "1.1.0"
    triggers = ["评分矩阵", "评分标准"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        scoring_data = ctx.parameters.get("scoring_data", {})
        if not scoring_data:
            return SkillResult(success=False, error="评分数据为空")

        messages = [
            {
                "role": "system",
                "content": """你是招标文件评分矩阵构建专家。
将评分标准转换为结构化矩阵，每个评分项一行。
返回JSON数组，每项包含：
- seq: 序号
- category: 类别(商务/技术/价格)
- item: 评分项名称
- score: 分值
- criteria: 评分标准描述
- response_section: 建议应答章节(如"3.1技术方案")
- status: 状态(pending)""",
            },
            {
                "role": "user",
                "content": f"评分标准数据：\n{json.dumps(scoring_data, ensure_ascii=False)}",
            },
        ]

        result = await ctx.llm.collect_json(messages=messages, temperature=0.1)
        if isinstance(result, list):
            matrix_rows = result
        elif isinstance(result, dict):
            matrix_rows = result.get("rows", result.get("items", []))
            if not isinstance(matrix_rows, list):
                matrix_rows = []
        else:
            matrix_rows = []

        # BUG-9 修复：LLM 输出为空/未解析出行时，确定性回退消费 scoring_items + 权重字段，
        # 保证 rows 非空、不再返回空矩阵。
        fallback_rows: list[dict] = []
        if not matrix_rows:
            fallback_rows = build_matrix_rows(scoring_data)

        all_rows = matrix_rows if matrix_rows else fallback_rows

        total_score = sum(row.get("score", 0) or 0 for row in all_rows)
        category_scores: dict[str, float] = {}
        for row in all_rows:
            cat = row.get("category", "未分类")
            category_scores[cat] = category_scores.get(cat, 0) + (row.get("score", 0) or 0)
        # 回退模式：权重字段（business/technical/price_weight）是权威的类别分/总分来源，
        # 优先于从行分数累加（行分数可能含类别下细分项，与类别权重重叠）。
        if not matrix_rows:
            weights = _weights_category_scores(scoring_data)
            if weights:
                category_scores = weights
                total_score = sum(weights.values())

        return SkillResult(
            success=True,
            data={
                "rows": all_rows,
                "total_score": total_score,
                "category_scores": category_scores,
                "row_count": len(all_rows),
                "source": "llm" if matrix_rows else "scoring_items_fallback",
            },
        )
