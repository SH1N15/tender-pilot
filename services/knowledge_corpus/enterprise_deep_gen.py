# -*- coding: utf-8 -*-
"""P-B 企业库语料深化（B2 第二步）：40 份文档逐份按章节深化至 25-45k 字。

- 每文档 12 个章节指令，逐章 LLM 生成（temperature 0.4，max_tokens 4096），append 到原文档；
- 章节 checkpoint：data/corpus_raw/enterprise_sections/<doc>__<sec_idx>.txt，续跑跳过已完成；
- 并发受 asyncio.Semaphore 限制（默认 4），LLM 网关自带全局并发闸门；
- 完成后重新执行 ingest --kind enterprise（重建式幂等）即可达到 ≥2000 chunks。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

OUT_DIR = Path("data/corpus_raw/enterprise")
SEC_DIR = Path("data/corpus_raw/enterprise_sections")

SECTIONS: list[str] = [
    "第一部分：背景与需求分析（项目/业务背景、痛点、需求清单表）",
    "第二部分：总体架构与技术路线（架构图文字描述、子系统划分、技术选型理由）",
    "第三部分：设备与材料清单（表格：名称/型号示例/数量/单位/关键参数，示例口径）",
    "第四部分：关键参数与标准规范对照（引用国标 GB/GBT 编号与条款要求、逐条响应写法）",
    "第五部分：实施与施工组织（阶段划分、人员分工表、进度计划表、现场管理）",
    "第六部分：质量控制与测试验收（测试项表、验收标准、隐蔽工程、资料清单）",
    "第七部分：安全保障与风险应对（施工安全、数据安全、应急预案表）",
    "第八部分：典型场景与案例分析（2-3 个场景细节：用户规模、配置、效果数据示例）",
    "第九部分：成本构成与报价测算说明（科目表、费率示例口径、测算方法）",
    "第十部分：培训与知识转移（分对象课程表、课时、考核方式）",
    "第十一部分：售后服务与运维细节（SLA 表、巡检项目表、备件清单、驻场安排）",
    "第十二部分：附录（表单模板、自查清单、常用应答话术摘录）",
]

SYSTEM = (
    "你是招投标领域资深顾问。为广州市科旭电子有限公司（教育装备与智能电子系统集成商，示例口径）"
    "编写企业文档的一个章节。要求：结构化、可用表格、内容具体（金额/数量/参数用合理示例值）、"
    "1200-2500 字、直接输出正文不要寒暄。整体属于 AI 生成演示语料。"
)


async def _gen_section(gw, doc_title: str, doc_text_head: str, section: str, sem: asyncio.Semaphore) -> str:
    user = (
        f"文档标题：{doc_title}\n\n文档已有内容开头（保持口径一致）：\n{doc_text_head[:1500]}\n\n"
        f"请撰写章节：{section}"
    )
    async with sem:
        for attempt in range(3):
            try:
                return await gw.chat(
                    [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.4,
                    max_tokens=4096,
                )
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
                await asyncio.sleep(5 * (attempt + 1))
    return ""


async def main(concurrency: int = 4) -> None:
    from services.llm_factory import get_llm_gateway

    gw = get_llm_gateway()
    SEC_DIR.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)
    docs = sorted(OUT_DIR.glob("*.txt"))
    print(f"{len(docs)} docs x {len(SECTIONS)} sections")

    # 先完成本轮全部章节任务（文档间并发=1 文档内串行，保证连续性；跨文档并行度=concurrency）
    async def expand_doc(path: Path) -> None:
        doc_text = path.read_text(encoding="utf-8", errors="ignore")
        pending: list[tuple[int, str]] = []
        # 断点：已完成的章节文件（用 stem 去扩展名；曾用 name 得到 "00.txt" 与 key "00" 永不匹配→重启全量重做）
        done_secs = {p.stem.split("__")[1] for p in SEC_DIR.glob(f"{path.stem}__*.txt")}
        for idx, sec in enumerate(SECTIONS):
            key = f"{idx:02d}"
            if key in done_secs:
                continue
            pending.append((idx, sec))
        if not pending:
            return
        base = doc_text
        for idx, sec in pending:
            try:
                text = await _gen_section(gw, path.stem, base, sec, sem)
            except Exception as exc:  # noqa: BLE001
                print(f"[fail] {path.stem} sec{idx:02d}: {str(exc)[:80]}")
                continue
            (SEC_DIR / f"{path.stem}__{idx:02d}.txt").write_text(f"\n\n【{sec}】\n{text}", encoding="utf-8")
            base = (base + text)[:4000]  # 续写上下文
        print(f"[doc] {path.stem} sections done ({len(pending)} new)")

    t0 = time.time()
    for i in range(0, len(docs), concurrency):
        await asyncio.gather(*(expand_doc(d) for d in docs[i : i + concurrency]))
        print(f"batch {i // concurrency + 1} done, {time.time() - t0:.0f}s")

    # 组装：章节追加回主文档
    for path in docs:
        secs = sorted(SEC_DIR.glob(f"{path.stem}__*.txt"))
        if not secs:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for sp in secs:
            body = sp.read_text(encoding="utf-8", errors="ignore")
            if body.strip()[:200] not in text:
                text += body
        path.write_text(text, encoding="utf-8")
    total_chars = sum(len(p.read_text(encoding="utf-8", errors="ignore")) for p in docs)
    print(f"assembled {len(docs)} docs, total {total_chars} chars (~{total_chars // 800} chunks)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    asyncio.run(main(concurrency=args.concurrency))
