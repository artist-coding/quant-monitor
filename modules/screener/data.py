"""选股数据获取层（支持 DataSource 注入）。"""

from ..database import get_db_connection
from ..datasource import DataSource, get_datasource
from ..indicators import DailyData


def get_all_stocks(datasource: DataSource | None = None) -> list[dict]:
    """
    获取所有可交易股票的基本信息（已排除 ST 与北交所，见 modules/universe.py）

    优先从注入的 datasource 获取，为空时回退到本地 SQLite
    """
    from ..universe import is_tradable

    if datasource is None:
        datasource = get_datasource()

    stocks = datasource.get_stock_list()
    if stocks:
        # market 白名单挡不住 ST——ST 遍布主板/创业板/科创板（实测 147/43/14 只），
        # 必须按名称另判一次。市场字段缺失（None）时也走同一套规则。
        return [
            s
            for s in stocks
            if s.get("market") in ("主板", "创业板", "科创板", None)
            and is_tradable(str(s.get("ts_code", "")), s.get("name"))
        ]

    # 回退到本地
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ts_code, name, industry, market
        FROM stock_basic
        WHERE market IN ('主板', '创业板', '科创板')
          AND name NOT LIKE '%ST%'
        ORDER BY ts_code
    """)
    stocks = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return stocks


def get_recent_klines(ts_code: str, days: int = 60, datasource: DataSource | None = None) -> list[DailyData]:
    """
    获取近期 K 线数据

    从注入的 datasource 获取并转换为 DailyData（升序）
    """
    if datasource is None:
        datasource = get_datasource()

    rows = datasource.get_kline_dicts(ts_code, days=days)
    if not rows:
        return []

    return _dict_to_daily(rows)


def _dict_to_daily(klines: list[dict]) -> list[DailyData]:
    """将 dict 格式 K 线转为 DailyData 列表"""
    result = []
    for i, row in enumerate(klines):
        prev_close = klines[i - 1]["close"] if i > 0 else row["close"]
        result.append(
            DailyData(
                ts_code=row["ts_code"],
                trade_date=row["trade_date"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                vol=row["vol"],
                amount=row.get("amount", row["close"] * row["vol"]),
                pct_chg=row.get("pct_chg", 0.0),
                prev_close=prev_close,
            )
        )
    return result
