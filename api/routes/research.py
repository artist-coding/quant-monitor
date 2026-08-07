"""Kimi Swarm 股票调研路由"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from api.models.research import (
    ResearchCreateRequest,
    ResearchHistoryResponse,
    ResearchTaskListResponse,
    ResearchTaskResponse,
)
from api.services.research_service import ResearchBusyError, research_service

router = APIRouter()


@router.post("/", response_model=ResearchTaskResponse, status_code=202)
def create_research(req: ResearchCreateRequest):
    try:
        return research_service.create(req.ts_code)
    except ResearchBusyError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"创建调研任务失败: {exc}")


@router.get("/recent", response_model=ResearchTaskListResponse)
def list_recent_research(limit: int = Query(default=10, ge=1, le=50)):
    return {"tasks": research_service.list_recent(limit)}


@router.get("/history", response_model=ResearchHistoryResponse)
def list_research_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    status: Literal["queued", "running", "completed", "failed"] | None = Query(default=None),
    keyword: str = Query(default="", max_length=80),
):
    return research_service.list_history(page=page, page_size=page_size, status=status, keyword=keyword)


@router.get("/{task_id}", response_model=ResearchTaskResponse)
def get_research(task_id: str):
    task = research_service.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="调研任务不存在")
    return task
