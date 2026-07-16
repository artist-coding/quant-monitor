"""REST contracts for end-of-day candidate-pool refreshes."""

from typing import Literal

from pydantic import BaseModel, Field


class MarketContextInput(BaseModel):
    score: float = Field(ge=0, le=100)
    version: str = Field(default="explicit-market-context-v1", min_length=1)
    source_hash: str = ""


class CandidatePoolRefreshRequest(BaseModel):
    as_of_date: str = Field(description="Signal date in YYYYMMDD or YYYY-MM-DD")
    universe: Literal["WATCHLIST", "EXPLICIT"] = "WATCHLIST"
    ts_codes: list[str] = Field(default_factory=list)
    lookback_bars: int = Field(default=180, ge=120, le=500)
    max_position_pct: float = Field(default=0.10, gt=0, le=1)
    minimum_buy_score: float = Field(default=60, ge=0, le=100)
    candidate_statuses: list[Literal["CANDIDATE", "CONFIRMED"]] = Field(
        default_factory=lambda: ["CANDIDATE", "CONFIRMED"]
    )
    top_n: int = Field(default=100, ge=1, le=1000)
    minimum_market_coverage: int = Field(default=1000, ge=1)
    allow_partial: bool = False
    market_context: MarketContextInput | None = None


class CandidatePoolIssueItem(BaseModel):
    ts_code: str
    issue_type: str
    reason: str


class MarketContextItem(BaseModel):
    trade_date: str
    score: float
    version: str
    source_hash: str


class DailyCandidateItem(BaseModel):
    rank: int
    trade_date: str
    ts_code: str
    name: str = ""
    buy_score: float
    sell_score: float
    buy_point_status: str
    desired_action: str
    primary_variant: str = ""
    reference_close: float
    planned_stop_loss: float | None = None
    estimated_risk_pct: float | None = None
    buy_contributions: dict[str, float] = Field(default_factory=dict)
    rule_qualification: str
    strategy_version: str
    parameter_version: str
    parameter_fingerprint: str


class CandidatePoolRefreshResponse(BaseModel):
    schema_version: str
    run_id: str
    status: str
    trade_date: str
    universe: str
    requested_count: int
    scored_count: int
    candidate_count: int
    skipped_count: int
    failed_count: int
    market_context: MarketContextItem
    strategy_version: str
    parameter_version: str
    parameter_fingerprint: str
    candidates: list[DailyCandidateItem] = Field(default_factory=list)
    issues: list[CandidatePoolIssueItem] = Field(default_factory=list)


class CandidatePoolListResponse(BaseModel):
    schema_version: str
    run_id: str
    status: str
    trade_date: str
    universe: str
    count: int
    total_candidates: int
    market_context: MarketContextItem
    candidates: list[DailyCandidateItem] = Field(default_factory=list)


class CandidatePoolRunResponse(BaseModel):
    run_id: str
    status: str
    trade_date: str
    universe: str
    requested_count: int
    scored_count: int
    candidate_count: int
    skipped_count: int
    failed_count: int
    error_message: str = ""
    started_at: str = ""
    completed_at: str = ""
    issues: list[CandidatePoolIssueItem] = Field(default_factory=list)


__all__ = [
    "CandidatePoolListResponse",
    "CandidatePoolRefreshRequest",
    "CandidatePoolRefreshResponse",
    "CandidatePoolRunResponse",
    "DailyCandidateItem",
    "MarketContextInput",
]
