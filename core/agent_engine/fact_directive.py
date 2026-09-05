"""G-7 收尾：生成路径事实注入公共件。

G7-5 曾把"定向检索 + extra_docs 台账 + 事实消费指令"三件套接到 repair 路径，
但全量生成的 chapter_gen B 模式仍是"章节标题+全大纲树 JSON"稀释检索、无 extra_docs、
无事实消费指令——导致检索命中事实但正文不带【n】锚点，被 Grounding 硬门拒收成
【待补充】（终验样例：367 处）。本模块把三件套抽成公共件，供生成图与修复 runner 共用。

来源：services/check/repair_runner.py 的 _FACT_DIRECTIVE / _PLACEHOLDER_DIRECTIVE
（此处改为公共名，repair_runner 保留原名别名以兼容既有引用）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 事实消费指令：库内有据的编号/金额/日期/人名必须带【n】锚点引用；无据才写待补充；
# 编号禁止 XXX/待定占位。
FACT_DIRECTIVE = (
    "\n【企业事实库定向检索结果（本章节相关，必须消费）】\n"
    "以下材料是按本章节定向检索到的企业事实（检索词：{query}）。"
    "撰写正文时必须引用其中与本章节相关的具体硬事实（证书编号、统一社会信用代码、"
    "合同金额、人员资格证号、日期、人名等），并按引用规则标注其编号；不得再留占位符，"
    "也不得编造库外数值。与本章节无关的材料可忽略。\n{facts}"
)

# 占位符/商务要件/编号真实性/划型一致性纪律（与修复路径同口径）。
PLACEHOLDER_DIRECTIVE = (
    "\n【占位符填写纪律（复检必查）】正文中的占位符（____、[待补充]、[投标人名称]、"
    "[具体天数]、（知识库无据，待补充）等）必须替换为编号参考材料中的真实值；"
    "金额/日期/天数/编号/名称必须逐字取自材料并标注引用编号，"
    "仅线下签章/落款空栏可保留，其余信息性占位符一律不得保留。"
    "\n【金额与商务要件强制填写】若编号材料给出预算金额（如 6,500,000.00 元）、工期天数、"
    "质保年限、投标有效期，则必须直接采用这些数值填写正文（报价金额取材料中的预算金额，"
    "且投标函/开标一览表/报价汇总表/明细表四处金额必须为同一数值、合计=分项之和）；"
    "投标人名称/法定代表人/信用代码等企业信息必须用企业事实材料中的全称填齐，"
    "绝不写'待补充'。"
    "\n【编号真实性纪律（废标级）】审批/申请/合同/证书类编号禁止输出含 XXX、××、"
    "待定等占位值（如 KX-SQ-2026-XXX）；编号材料（企业事实/招标文件）有据则逐字引用，"
    "无据则整句省略，不得保留任何占位编号。"
    "\n【政策适用性一致（废标级）】中小企业/小微企业等政策扶持或声明，必须与企业事实"
    "材料中的财务数据划型一致（如年营收超过 1 亿元即不属于中小微企业）；"
    "与事实矛盾时必须删除该声明或改写为实际情况，禁止保留自相矛盾的表述。"
)


def find_outline_node(outline_tree: Any, chapter_id: str) -> dict:
    """在大纲树 {chapters:[{id,title,children:[...]}]} 中按章节 id 找节点（含中间层）。"""
    if not isinstance(outline_tree, dict):
        return {}
    stack = list(outline_tree.get("chapters") or [])
    while stack:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        if str(node.get("id")) == str(chapter_id):
            return node
        stack.extend(node.get("children") or [])
    return {}


def build_chapter_retrieval_query(chapter_id: str, chapter_title: str, outline_tree: Any) -> str:
    """定向检索查询：章节标题 + 该章子树小节标题（去掉全大纲树 JSON 稀释）。

    叶子章（无 children）回退为自身标题；找不到节点时回退为标题。
    """
    parts: list[str] = [str(chapter_title or "").strip()]
    node = find_outline_node(outline_tree, chapter_id)
    children = [c for c in (node.get("children") or []) if isinstance(c, dict)] if node else []

    def _titles(nodes: list[dict], depth: int = 0) -> list[str]:
        out: list[str] = []
        for c in nodes:
            t = str(c.get("title") or "").strip()
            if t:
                out.append(t)
            if depth < 1:
                out.extend(_titles([g for g in (c.get("children") or []) if isinstance(g, dict)], depth + 1))
        return out

    parts.extend(_titles(children))
    query = " ".join(p for p in parts if p)
    return query or str(chapter_title or "")


def _is_ent(doc: dict) -> bool:
    return str((doc.get("metadata") or {}).get("collection", "")).startswith("kb_ent")


async def retrieve_generation_facts(
    knowledge_base: Any, query: str, top_k: int = 6
) -> tuple[list[dict], str]:
    """生成路径定向检索：企业域（kb_ent_*）命中优先，返回 (注入用 docs, 事实文本)。

    检索失败降级为空，不阻断生成（与修复路径同口径）。
    """
    if knowledge_base is None or not hasattr(knowledge_base, "retrieve") or not str(query or "").strip():
        return [], ""
    try:
        hits = await knowledge_base.retrieve(query=str(query), top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        logger.warning("生成定向检索失败（降级为无事实注入）: %s", exc)
        return [], ""
    ent = [h for h in hits if isinstance(h, dict) and _is_ent(h)]
    picked = (ent + [h for h in hits if isinstance(h, dict) and not _is_ent(h)])[:top_k]
    facts_text = "\n\n".join(
        f"[fact{i}] collection={(d.get('metadata') or {}).get('collection', '')} "
        f"source={(d.get('metadata') or {}).get('source', '')}:\n{str(d.get('text', ''))[:600]}"
        for i, d in enumerate(picked, start=1)
    )
    return picked, facts_text


def build_fact_directive(query: str, facts_text: str) -> str:
    """有事实时拼 FACT_DIRECTIVE；无事实返回空串（指令不空转）。"""
    if not str(facts_text or "").strip():
        return ""
    return FACT_DIRECTIVE.format(query=str(query)[:200], facts=facts_text)


# 项目/采购人已知名（Worker J 缺陷③根因修复：chapter_gen 此前从不注入
# Analysis.dimensions，模型拿不到买方单位名，"致：[采购人名称]"永远填不齐）。
_BUYER_NAME_KEYS = ("unit_name", "name", "buyer_name")
_PINFO_NAME_KEYS = ("procurement_unit", "buyer_name", "purchaser")


def build_project_brief(analysis_dimensions: dict | None) -> str:
    """由解读结果 dimensions 构建『本项目已确认要素』注入块（无据返回空串）。

    只收录解读维度中实际存在的字段；提示模型用其填充采购人/招标人/项目名称/
    编号类占位符，禁止再留 [采购人名称] 式脚手架。
    """
    if not isinstance(analysis_dimensions, dict):
        return ""
    buyer = analysis_dimensions.get("buyer_info")
    pinfo = analysis_dimensions.get("project_info")
    buyer = buyer if isinstance(buyer, dict) else {}
    pinfo = pinfo if isinstance(pinfo, dict) else {}

    def _first(src: dict, keys: tuple[str, ...]) -> str:
        for k in keys:
            v = str(src.get(k) or "").strip()
            if v:
                return v
        return ""

    rows: list[tuple[str, str]] = []
    purchaser = _first(buyer, _BUYER_NAME_KEYS) or _first(pinfo, _PINFO_NAME_KEYS)
    if purchaser:
        rows.append(("采购人/招标人（买方单位全称）", purchaser))
    for label, key in (
        ("项目名称", "project_name"),
        ("项目编号/招标编号", "project_code"),
        ("预算金额", "budget_amount"),
        ("采购方式", "procurement_method"),
    ):
        v = str(pinfo.get(key) or "").strip()
        if v:
            rows.append((label, v))
    if not rows:
        return ""
    lines = "\n".join(f"- {k}：{v}" for k, v in rows)
    return (
        "\n【本项目已确认要素（解读结果，必须用于填写正文）】\n"
        f"{lines}\n"
        "正文中出现采购人/招标人/项目名称/项目编号等需要上述要素的位置时，"
        "必须直接填写上述真实值（如“致：采购人全称”），"
        "禁止保留 [采购人名称]、[招标人名称]、[项目名称] 等方括号占位脚手架。\n"
    )
