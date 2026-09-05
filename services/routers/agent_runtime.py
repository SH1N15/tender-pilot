"""退役说明（G-5，2026-09-01）：/api/agent 旁路已摘除挂载（services/main.py），本文件保留不删。

原因：主链路多 Agent 编排已统一由 /api/graph 主编排图承载；前端/MCP/A2A/AG-UI 对
/api/agent/* 全部无引用（G-5 任务书 T2 grep 证明）。本模块的 run_pipeline/run_single_agent
助手（services/agent_runtime_helpers.py）与 core/agent_framework 组件继续以库形式可用，
若需恢复旁路，在 main.py 重新挂载本 router 即可。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.agent_framework.checkpoint import CheckpointManager
from services.agent_runtime_helpers import (
    run_pipeline as _run_pipeline_helper,
)
from services.agent_runtime_helpers import (
    run_single_agent,
)
from services.database import get_db

router = APIRouter(prefix="/agent", tags=["agent"])


class RunPipelineRequest(BaseModel):
    project_id: str
    pipeline_type: str = "bid_generation"
    params: dict[str, Any] = {}


class RunStepRequest(BaseModel):
    project_id: str
    agent_name: str
    task: str
    params: dict[str, Any] = {}


@router.post("/run")
async def run_pipeline(req: RunPipelineRequest, db: AsyncSession = Depends(get_db)):
    """启动完整的多Agent流程（vNext：复用共享运行时助手）"""
    try:
        result = await _run_pipeline_helper(
            req.project_id,
            req.pipeline_type,
            db,
        )
        if not result.success:
            return {
                "success": False,
                "message": result.error or "流程执行失败",
                "data": result.data,
            }
        return {
            "success": True,
            "message": "流程执行完成",
            "data": result.data,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/run_step")
async def run_step(req: RunStepRequest, db: AsyncSession = Depends(get_db)):
    """执行单个Agent步骤"""
    try:
        result = await run_single_agent(
            req.project_id,
            req.agent_name,
            req.task,
            db,
            params=req.params,
        )
        return {
            "success": result.success,
            "message": result.error or "ok",
            "data": result.data,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{project_id}")
async def get_agent_status(project_id: str):
    """查询Agent执行状态"""
    try:
        checkpoint = CheckpointManager()
        data = await checkpoint.load_latest(project_id)
        if data is None:
            return {"success": True, "status": "not_started", "data": None}
        return {
            "success": True,
            "status": data.get("status", "unknown"),
            "completed_steps": data.get("completed_steps", []),
            "current_step": data.get("current_step", ""),
            "errors": data.get("errors", []),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/resume/{project_id}")
async def resume_pipeline(project_id: str, db: AsyncSession = Depends(get_db)):
    """从检查点恢复执行"""
    try:
        from core.agent_framework.checkpoint import CheckpointManager

        checkpoint = CheckpointManager()

        async def _resume(pid, cp):
            return await run_pipeline(pid, cp.get("pipeline_type", "bid_generation"), db)

        result = await checkpoint.resume_from_checkpoint(project_id, _resume)
        return {"success": result.success, "message": result.error or "ok", "data": result.data}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
