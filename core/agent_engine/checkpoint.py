"""P-D1 持久 checkpointer。

选型说明（任务书第 5 节要求报告）：
- PG 优先：自研 JSONB 异步 saver（`PGCheckpointSaver`），走现有 SQLAlchemy async
  引擎（services.database.async_session / DATABASE_URL），表 graph_checkpoints /
  graph_checkpoint_writes 由 Alembic 迁移（见 db/migrations/versions/）创建；
  checkpoint 载荷用 langgraph-checkpoint 自带 JsonPlusSerializer 序列化。
- 测试/内存场景用 langgraph 自带 InMemorySaver（langgraph.checkpoint.memory）。
- 短期会话摘要（Memurai）本期不做（任务书第 5 节）。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

SERDE = JsonPlusSerializer()


class PGCheckpointSaver(BaseCheckpointSaver):
    """自研 JSONB checkpointer：表结构见 Alembic revision pd1_graph_checkpoints。"""

    def __init__(self, session_factory: Any):
        super().__init__()
        self._session_factory = session_factory

    # ---- helpers ----

    def _dump(self, obj: Any) -> str:
        payload = SERDE.dumps_typed(obj)
        # dumps_typed -> (type_str, bytes)；存 type+base64
        import base64

        type_str, data = payload
        return json.dumps({"t": type_str, "d": base64.b64encode(data).decode()})

    def _load(self, raw: str | dict | None) -> Any:
        if not raw:
            return None
        import base64

        # asyncpg 把 JSONB 反序列化为 dict；文本驱动（sqlite 等）返回 str
        wrapper = raw if isinstance(raw, dict) else json.loads(raw)
        return SERDE.loads_typed((wrapper["t"], base64.b64decode(wrapper["d"])))

    async def aget_tuple(self, config: dict) -> CheckpointTuple | None:
        from sqlalchemy import text

        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT checkpoint_id, parent_checkpoint_id, checkpoint, metadata "
                        "FROM graph_checkpoints WHERE thread_id=:t AND checkpoint_ns=:ns "
                        "ORDER BY checkpoint_id DESC LIMIT 1"
                    ),
                    {"t": thread_id, "ns": checkpoint_ns},
                )
            ).first()
            if not row:
                return None
            pending_writes = (
                await session.execute(
                    text(
                        "SELECT task_id, idx, channel, type, blob FROM graph_checkpoint_writes "
                        "WHERE thread_id=:t AND checkpoint_ns=:ns AND checkpoint_id=:cid "
                        "ORDER BY task_id, idx"
                    ),
                    {"t": thread_id, "ns": checkpoint_ns, "cid": row[0]},
                )
            ).all()
        checkpoint = self._load(row[2])
        metadata = self._load(row[3]) or {}
        writes = [(w[0], w[2], self._load(w[4])) for w in pending_writes]
        return CheckpointTuple(
            config={"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": row[0]}},
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": row[1]}}
                if row[1]
                else None
            ),
            pending_writes=writes or None,
        )

    async def alist(
        self, config: dict | None, *, filter: dict | None = None, before: dict | None = None, limit: int | None = None
    ) -> AsyncIterator[CheckpointTuple]:
        from sqlalchemy import text

        thread_id = (config or {}).get("configurable", {}).get("thread_id")
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT checkpoint_id, parent_checkpoint_id, checkpoint, metadata FROM graph_checkpoints "
                        "WHERE (:t IS NULL OR thread_id=:t) ORDER BY checkpoint_id DESC LIMIT :lim"
                    ),
                    {"t": thread_id, "lim": limit or 100},
                )
            ).all()
        for row in rows:
            yield CheckpointTuple(
                config={"configurable": {"thread_id": thread_id, "checkpoint_id": row[0]}},
                checkpoint=self._load(row[2]),
                metadata=self._load(row[3]) or {},
            )

    async def alist_latest_snapshots(self, limit: int = 100) -> list[tuple[str, dict]]:
        """Return the newest state snapshot for each graph thread.

        The in-memory run registry is intentionally ephemeral, so callers that
        render a run history can use this read-only projection after a restart.
        """
        from sqlalchemy import text

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT thread_id, checkpoint, metadata, created_at FROM ("
                        "  SELECT DISTINCT ON (thread_id) thread_id, checkpoint, metadata, checkpoint_id, created_at "
                        "  FROM graph_checkpoints WHERE thread_id NOT LIKE '%:%' "
                        "  ORDER BY thread_id, checkpoint_id DESC"
                        ") latest ORDER BY checkpoint_id DESC LIMIT :lim"
                    ),
                    {"lim": max(1, min(int(limit), 500))},
                )
            ).all()
        snapshots: list[tuple[str, dict]] = []
        for row in rows:
            snapshot = self._load(row[1])
            persisted_ts = snapshot.get("ts") if isinstance(snapshot, dict) else None
            if isinstance(snapshot, dict) and isinstance(snapshot.get("channel_values"), dict):
                snapshot = snapshot["channel_values"]
                if persisted_ts and "ts" not in snapshot:
                    snapshot["ts"] = persisted_ts
            if isinstance(snapshot, dict):
                # checkpoint 行时间是进程重启后仍可用的历史排序依据；旧快照
                # 未写入业务 created_at 时由运行列表适配器消费该字段。
                if row[3] is not None and "_checkpoint_created_at" not in snapshot:
                    snapshot["_checkpoint_created_at"] = (
                        row[3].isoformat() if hasattr(row[3], "isoformat") else str(row[3])
                    )
                snapshots.append((str(row[0]), snapshot))
        return snapshots

    async def aput(
        self,
        config: dict,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict:
        from sqlalchemy import text

        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        parent_id = config["configurable"].get("checkpoint_id")
        checkpoint_id = checkpoint["id"]
        async with self._session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO graph_checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
                    "checkpoint, metadata, created_at) VALUES (:t,:ns,:cid,:pid,:ck,:md, NOW()) "
                    "ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO UPDATE "
                    "SET checkpoint=EXCLUDED.checkpoint, metadata=EXCLUDED.metadata"
                ),
                {
                    "t": thread_id,
                    "ns": checkpoint_ns,
                    "cid": checkpoint_id,
                    "pid": parent_id,
                    "ck": self._dump(checkpoint),
                    "md": self._dump(dict(metadata or {})),
                },
            )
            await session.commit()
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(self, config: dict, writes: list[tuple[str, Any]], task_id: str) -> None:
        from sqlalchemy import text

        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"].get("checkpoint_id", "")
        async with self._session_factory() as session:
            for idx, (channel, value) in enumerate(writes):
                await session.execute(
                    text(
                        "INSERT INTO graph_checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, "
                        "channel, type, blob) VALUES (:t,:ns,:cid,:tid,:idx,:ch,:ty,:bl) "
                        "ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, idx) DO NOTHING"
                    ),
                    {
                        "t": thread_id,
                        "ns": checkpoint_ns,
                        "cid": checkpoint_id,
                        "tid": task_id,
                        "idx": idx,
                        "ch": channel,
                        "ty": "json",
                        "bl": self._dump(value),
                    },
                )
            await session.commit()

    async def adelete_thread(self, thread_id: str) -> None:
        from sqlalchemy import text

        async with self._session_factory() as session:
            await session.execute(text("DELETE FROM graph_checkpoints WHERE thread_id=:t"), {"t": thread_id})
            await session.execute(text("DELETE FROM graph_checkpoint_writes WHERE thread_id=:t"), {"t": thread_id})
            await session.commit()
