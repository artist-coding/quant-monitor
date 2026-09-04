"""选股筛选模型"""

from pydantic import BaseModel, Field


class ScreenRequest(BaseModel):
    strategy: str = Field(default="B1", description="策略名称（B1/B2/B3/完美图形/超级B1/长安战法/建仓波/吸筹/安全/超跌/突破）")
    limit: int = Field(default=20, ge=1, le=500, description="返回数量上限（只截断结果，不影响扫描范围）")
    # 扫描范围必须和"返回几条"分开：旧版把 limit 直接当 max_stocks 传给
    # screen_stocks，于是选 20 条 = 只扫 20 只股票，页面上写着"扫描全市场"
    # 实际连 1% 都没扫到。0=全量，保留正数是为了排查时缩小范围。
    max_stocks: int = Field(default=0, ge=0, le=10000, description="最大扫描数量，0=全量")
    use_parallel: bool = Field(default=True, description="是否启用多进程")


class StockScoreItem(BaseModel):
    ts_code: str
    name: str = ""
    score: float = 0
    b1_score: float = 0
    trend_score: float = 0
    volume_score: float = 0
    risk_score: float = 0
    rating: str = ""
    reasons: list[str] = []
    warnings: list[str] = []


class ScreenResponse(BaseModel):
    strategy: str
    criteria: str
    scanned: int = 0  # 实际扫描只数
    hits: int = 0  # 命中总数（未被 limit 截断）
    count: int  # 本次返回条数 = min(hits, limit)
    stocks: list[StockScoreItem] = []


class StrategyInfo(BaseModel):
    alias: str
    criteria: str
    description: str = ""
