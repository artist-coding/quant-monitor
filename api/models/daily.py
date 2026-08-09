"""每日选股（活跃市值 + 全市场扫描 + Kimi 复核）模型。"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AmvAddRequest(BaseModel):
    trade_date: str = Field(min_length=6, max_length=12, description="交易日，YYYYMMDD 或 YYYY-MM-DD")
    close: float | None = Field(default=None, gt=0, description="活跃市值收盘价（首选）")
    pct_chg: float | None = Field(default=None, ge=-99, le=1000, description="日涨幅%（备选，边界处精度不足）")

    @model_validator(mode="after")
    def _need_one(self):
        if self.close is None and self.pct_chg is None:
            raise ValueError("收盘价与涨幅至少填一个；优先填收盘价")
        return self


class AmvDayItem(BaseModel):
    trade_date: str
    close: float
    pct_chg: float | None = None
    regime: str = ""


class AmvSegment(BaseModel):
    regime: str
    start: str
    end: str
    days: int


class AmvStatusResponse(BaseModel):
    available: bool = False
    trade_date: str = ""
    close: float = 0.0
    pct_chg: float | None = None
    regime: str = ""
    can_select: bool = False
    bull_threshold: float = 4.0
    bear_threshold: float = -2.3
    segments: list[AmvSegment] = Field(default_factory=list)
    recent: list[AmvDayItem] = Field(default_factory=list)
    precision_warning: str = ""


class ThemeItem(BaseModel):
    name: str
    description: str = ""
    active: int = 1
    member_count: int = 0
    updated_at: str = ""


class ThemeListResponse(BaseModel):
    themes: list[ThemeItem] = Field(default_factory=list)


class ThemeUpsertRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=200)
    active: bool = True


class ThemeMembersRequest(BaseModel):
    codes: list[str] = Field(default_factory=list, max_length=500)


class ThemeMembersResponse(BaseModel):
    theme: str
    members: list[str] = Field(default_factory=list)


class ThemeStrengthItem(BaseModel):
    theme: str
    kind: str
    strength: float
    excess: float
    rank: int
    member_count: int
    median_pct_chg: float
    limit_up_count: int


class ThemeRankingResponse(BaseModel):
    trade_date: str = ""
    lookback: int = 5
    window: list[str] = Field(default_factory=list)
    themes: list[ThemeStrengthItem] = Field(default_factory=list)
    industries: list[ThemeStrengthItem] = Field(default_factory=list)
    dropped_themes: list[str] = Field(default_factory=list)
    reason: str = ""


class ScanCreateRequest(BaseModel):
    trade_date: str = Field(default="", max_length=12, description="留空用库内最新交易日")
    market_gate: Literal["on", "off"] = "on"
    top_n: int = Field(default=5, ge=1, le=20)
    min_group_strength: float = Field(default=50.0, ge=0, le=100)
    max_per_group: int | None = Field(default=None, ge=1, le=10)
    include_watch: bool = False
    theme_lookback: int = Field(default=5, ge=1, le=60)
    save: bool = True


class PickItem(BaseModel):
    rank: int = 0
    ts_code: str
    name: str = ""
    score: float = 0
    base_strategy: str = ""
    group: str = ""
    group_kind: str = ""
    group_strength: float = 0
    triggers: list[dict[str, Any]] = Field(default_factory=list)
    confirms: list[str] = Field(default_factory=list)


class RejectedItem(BaseModel):
    ts_code: str
    name: str = ""
    score: float = 0
    reason: str = ""


class ScanResponse(BaseModel):
    scan_id: str
    status: Literal["queued", "running", "completed", "failed"]
    progress: int = 0
    message: str = ""
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    trade_date: str = ""
    amv: dict[str, Any] | None = None
    position_hint: dict[str, Any] = Field(default_factory=dict)
    market: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    blocked: str = ""
    scanned: int = 0
    elapsed: float = 0
    counts: dict[str, int] = Field(default_factory=dict)
    stopped: dict[str, int] = Field(default_factory=dict)
    picks: list[PickItem] = Field(default_factory=list)
    rejected: list[RejectedItem] = Field(default_factory=list)
    theme_ranking: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    review_task_id: str = ""


class ScanSummary(BaseModel):
    scan_id: str
    trade_date: str = ""
    status: str = ""
    created_at: str = ""
    blocked: str = ""
    pick_count: int = 0
    buy_count: int = 0


class ScanListResponse(BaseModel):
    scans: list[ScanSummary] = Field(default_factory=list)
