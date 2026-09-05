"""AG-UI SSE 端点：标准 text/event-stream，事件由官方 ag-ui-protocol SDK 编码。"""

from __future__ import annotations

import uuid
from typing import Any

from ag_ui.encoder import EventEncoder
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from services.agui.service import (
    resume_qualification_events,
    run_agent_events,
    run_qualification_events,
)

router = APIRouter(prefix="/agui", tags=["AG-UI"])

_encoder = EventEncoder()


class AGUIRunRequest(BaseModel):
    thread_id: str = ""
    run_id: str = ""
    project_id: str = ""
    task: str = ""
    agent: str | None = None
    pipeline_type: str = "bid_generation"
    mode: str = "agent"  # agent | qualification
    requirements: list[dict[str, Any]] = []
    credentials: list[dict[str, Any]] = []


class AGUIResumeRequest(BaseModel):
    thread_id: str = ""
    run_id: str = ""
    workflow_id: str = ""
    decisions: list[dict[str, Any]] = []


async def _sse_stream(events):
    async for event in events:
        yield _encoder.encode(event)


@router.post("/run")
async def agui_run(body: AGUIRunRequest):
    thread_id = body.thread_id or f"thread_{uuid.uuid4().hex[:8]}"
    run_id = body.run_id or f"run_{uuid.uuid4().hex[:8]}"

    if body.mode == "qualification":
        events = run_qualification_events(
            thread_id=thread_id,
            run_id=run_id,
            requirements=body.requirements,
            credentials=body.credentials,
            project_id=body.project_id or None,
        )
    else:
        if not body.project_id:
            raise HTTPException(status_code=400, detail="agent 模式需要 project_id")
        events = run_agent_events(
            thread_id=thread_id,
            run_id=run_id,
            project_id=body.project_id,
            task=body.task,
            db=None,
            agent_name=body.agent,
            pipeline_type=body.pipeline_type,
        )

    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/resume")
async def agui_resume(body: AGUIResumeRequest):
    thread_id = body.thread_id or f"thread_{uuid.uuid4().hex[:8]}"
    run_id = body.run_id or f"run_{uuid.uuid4().hex[:8]}"
    if not body.workflow_id:
        raise HTTPException(status_code=400, detail="缺少 workflow_id")
    events = resume_qualification_events(
        thread_id=thread_id,
        run_id=run_id,
        workflow_id=body.workflow_id,
        decisions=body.decisions,
    )
    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
