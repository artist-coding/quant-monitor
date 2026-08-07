"""自选股模型"""

from pydantic import BaseModel


class WatchlistAddRequest(BaseModel):
    ts_code: str
    tags: str = ""
    notes: str = ""


class WatchlistItem(BaseModel):
    id: int = 0
    ts_code: str
    name: str = ""
    tags: str = ""
    notes: str = ""
    added_date: str = ""
    alert_enabled: bool = True
    trade_date: str = ""
    price: float | None = None
    pct_chg: float | None = None
    vol: float | None = None
    amount: float | None = None
    kline_count: int = 0
    data_ready: bool = False
    score: float = 0
    b1_score: float = 0
    trend_score: float = 0
    volume_score: float = 0
    risk_score: float = 0
    rating: str = "数据不足"
    j: float | None = None
    vol_ratio: float | None = None
    macd_status: str = "--"
    trend_status: str = "待补历史"
    signal: str = "--"


class WatchlistAddResponse(BaseModel):
    status: str
    message: str = ""
    item: WatchlistItem


class WatchlistListResponse(BaseModel):
    count: int
    items: list[WatchlistItem] = []


class WatchAlertItem(BaseModel):
    ts_code: str
    name: str = ""
    alert_type: str = ""
    level: str = ""
    message: str = ""


class WatchlistScanResponse(BaseModel):
    total: int = 0
    b1_count: int = 0
    b2_count: int = 0
    exit_count: int = 0
    break_count: int = 0
    abnormal_count: int = 0
    alerts: list[WatchAlertItem] = []


class WatchlistReportResponse(BaseModel):
    report: str


class WatchlistRefreshResponse(BaseModel):
    status: str
    stocks: int = 0
    kline_rows: int = 0
    indicator_rows: int = 0
    failures: list[str] = []
