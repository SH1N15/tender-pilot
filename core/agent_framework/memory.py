from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentMemory:
    """标书场景记忆管理：静态上下文 + 滑动窗口。

    双层设计：
    - 静态上下文 (static_context)：存储 DB 恢复的摘要信息，永不随窗口滑动丢失
    - 滑动窗口 (working_memory)：ReAct 循环的短期上下文，超过 window_size 自动丢弃
    """

    window_size: int = 10
    db: Any = None
    _working_memory: deque = field(default_factory=deque, init=False, repr=False)
    _static_context: list[dict] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self):
        self._working_memory = deque(maxlen=self.window_size)

    def add_message(self, role: str, content: str):
        """添加消息到滑动窗口（会随窗口满被丢弃）"""
        self._working_memory.append({"role": role, "content": content})

    def add_static_context(self, role: str, content: str):
        """添加静态上下文（永不丢弃，用于DB恢复的摘要信息）"""
        self._static_context.append({"role": role, "content": content})

    def add_exchange(self, user: str, assistant: str):
        """添加一次问答"""
        self._working_memory.append({"role": "user", "content": user})
        self._working_memory.append({"role": "assistant", "content": assistant})

    def get_context(self) -> list[dict]:
        """获取完整上下文：静态上下文 + 滑动窗口（静态上下文始终在前）"""
        return self._static_context + list(self._working_memory)

    async def restore_from_db(self, project_id: str):
        """从DB恢复静态上下文——不会被滑动窗口挤出。

        与滑动窗口的区别：静态上下文始终保留在消息列表头部，
        确保 Agent 在长 ReAct 循环中不会忘记已有状态。
        """
        if not self.db:
            return
        from sqlalchemy import select

        from services.models import Analysis, Chapter, Outline

        # 清空旧静态上下文（恢复时重建）
        self._static_context.clear()

        result = await self.db.execute(select(Analysis).where(Analysis.project_id == project_id))
        analysis = result.scalar_one_or_none()
        if analysis and analysis.dimensions:
            self.add_static_context("system", f"已有解读结果，包含维度：{list(analysis.dimensions.keys())}")

        result = await self.db.execute(select(Outline).where(Outline.project_id == project_id))
        outline = result.scalar_one_or_none()
        if outline and outline.tree:
            chapters = outline.tree if isinstance(outline.tree, list) else []
            self.add_static_context("system", f"已有大纲，共 {len(chapters)} 个章节")

        result = await self.db.execute(select(Chapter).where(Chapter.project_id == project_id))
        chapters = result.scalars().all()
        if chapters:
            completed = [c for c in chapters if c.status == "generated"]
            self.add_static_context("system", f"已有 {len(completed)}/{len(chapters)} 个章节已生成")


class LongTermMemory:
    """长期记忆（P-D2 先行版，分层：工作记忆 AgentMemory / 长期记忆本类 / 会话摘要本期不做）。

    - 读路径1：restore_from_db（既有能力，历史决策/大纲/章节摘要）；
    - 读路径2：经 P-C 向量库（kb_adapter，覆盖全部 kb_* 业务库，只读）按查询
      召回历史事实/决策相关 chunk，产物带 chunk_id/source 锚点；
    - 写路径：record 结构化事实写入专用 memory collection，不写业务知识库；
    - retriever 可注入（测试用 fake），缺省经 build_default_knowledge_base 构造。
    """

    def __init__(self, knowledge_base: Any = None):
        self._kb = knowledge_base
        self._kb_built = knowledge_base is not None

    async def _ensure_kb(self):
        if not self._kb_built:
            self._kb_built = True
            try:
                from core.rag_engine.kb_adapter import build_default_knowledge_base

                self._kb = await build_default_knowledge_base()
            except Exception:  # noqa: BLE001
                self._kb = None
        return self._kb

    async def recall(self, query: str, top_k: int = 3) -> list[dict]:
        """按查询召回历史事实/决策相关 chunk（带 chunk_id/source 锚点）。"""
        kb = await self._ensure_kb()
        if kb is None or not (query or "").strip():
            return []
        try:
            return await kb.retrieve(query=query, top_k=top_k)
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def format_context(items: list[dict], limit: int = 1500) -> str:
        """把召回结果格式化为可注入 prompt 的长期记忆上下文（带 chunk_id 锚点）。"""
        lines = []
        for doc in items or []:
            meta = doc.get("metadata") or {}
            lines.append(
                f"- (chunk_id={meta.get('chunk_id', '')}, source={meta.get('source', '')}) "
                f"{str(doc.get('text') or '')[:300]}"
            )
        return "\n".join(lines)[:limit]

    async def recall_context(self, query: str, top_k: int = 3) -> str:
        return self.format_context(await self.recall(query, top_k=top_k))

    async def record(
        self,
        *,
        project_id: str,
        source_type: str,
        fact: dict[str, Any],
        collection: str | None = None,
    ) -> dict:
        """写入一条结构化长期事实。

        记忆写入只允许进入专用 collection，永不复用业务知识库 collection。
        UAT/EVAL_UAT 项目使用独立 collection，避免测试污染生产记忆。
        """
        if not project_id or not project_id.strip():
            raise ValueError("project_id 必填")
        if not source_type or not source_type.strip():
            raise ValueError("source_type 必填")
        if not isinstance(fact, dict) or not fact:
            raise ValueError("fact 必须是非空对象")
        kb = await self._ensure_kb()
        if kb is None or not hasattr(kb, "record"):
            raise RuntimeError("长期记忆写入适配器不可用")
        return await kb.record(
            project_id=project_id,
            source_type=source_type,
            fact=fact,
            collection=collection,
        )

    async def clear_project(self, project_id: str) -> int:
        """清理指定项目的长期记忆，返回删除条数。"""
        kb = await self._ensure_kb()
        if kb is None or not hasattr(kb, "clear_project"):
            return 0
        return await kb.clear_project(project_id)
