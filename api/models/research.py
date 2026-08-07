"""Kimi Swarm 股票调研模型"""

from typing import Literal

from pydantic import BaseModel, Field


class ResearchCreateRequest(BaseModel):
    ts_code: str = Field(min_length=1, max_length=80, description="A 股代码或名称，如 600519.SH、贵州茅台")


class ResearchTaskResponse(BaseModel):
    task_id: str
    ts_code: str
    status: Literal["queued", "running", "completed", "failed"]
    progress: int = Field(default=0, ge=0, le=100)
    message: str = ""
    report: str = ""
    error: str = ""
    created_at: str
    started_at: str = ""
    completed_at: str = ""
    engine: str = "Kimi Code CLI"
    mode: str = "Swarm"
    skill: str = "zettaranc-perspective"
    trace_id: str = ""
    trace_available: bool = False
    trace_schema_version: int = 1
    agent_count: int = 0
    expected_agent_count: int = 5
    partial_result: bool = False
    partial_reason: str = ""
    last_activity_at: str = ""
    target_type: str = "stock"


class ResearchTaskListResponse(BaseModel):
    tasks: list[ResearchTaskResponse] = Field(default_factory=list)


class ResearchTaskSummary(BaseModel):
    task_id: str
    ts_code: str
    status: Literal["queued", "running", "completed", "failed"]
    progress: int = Field(default=0, ge=0, le=100)
    message: str = ""
    error: str = ""
    created_at: str
    started_at: str = ""
    completed_at: str = ""
    agent_count: int = 0
    expected_agent_count: int = 5
    partial_result: bool = False
    target_type: str = "stock"
    has_report: bool = False
    report_excerpt: str = ""


class ResearchHistoryResponse(BaseModel):
    tasks: list[ResearchTaskSummary] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    status_counts: dict[str, int] = Field(default_factory=dict)
