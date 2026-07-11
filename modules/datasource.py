"""
统一数据源抽象层

定义 DataSource Protocol，并封装 Tushare、Bridge、SQLite 以及自动回退的 Composite 数据源。
"""

from typing import Protocol, runtime_checkable

import pandas as pd

from .bridge_client import (
    BridgeConfig,
    get_all_stocks_bridge_first,
    get_daily_klines,
    is_bridge_available,
    set_bridge_config,
)
from .database import get_connection
from .tushare_client import TushareClient


@runtime_checkable
class DataSource(Protocol):
    """统一数据源协议，所有数据源实现必须满足此接口。"""

    @property
    def name(self) -> str: ...

    def health_check(self) -> bool: ...

    def get_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None: ...

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None: ...

    def get_realtime_quote(self, ts_codes: list[str]) -> pd.DataFrame | None: ...

    def get_moneyflow(self, ts_code: str, trade_date: str) -> pd.DataFrame | None: ...

    def get_daily_basic(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None: ...

    def get_stk_factor(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None: ...

    def get_stock_basic(self, ts_code: str | None = None, name: str | None = None) -> pd.DataFrame | None: ...

    def get_trade_cal(self, exchange: str, start_date: str, end_date: str) -> pd.DataFrame | None: ...

    def get_stock_list(self, exchange: str | None = None) -> list[dict]: ...

    def get_kline_dicts(
        self,
        ts_code: str,
        days: int = 60,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]: ...


class TushareDataSource:
    """Tushare Pro API 数据源封装。"""

    def __init__(self, token: str | None = None):
        self._client = TushareClient(token)

    @property
    def name(self) -> str:
        return "tushare"

    def health_check(self) -> bool:
        return self._client.check_connection()

    def get_daily(
        self, ts_code: str, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame | None:
        return self._client.get_daily(ts_code, start_date, end_date)

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return self._client.get_index_daily(ts_code, start_date, end_date)

    def get_realtime_quote(self, ts_codes: list[str]) -> pd.DataFrame | None:
        return self._client.get_realtime_quote(ts_codes)

    def get_moneyflow(self, ts_code: str, trade_date: str) -> pd.DataFrame | None:
        return self._client.get_moneyflow(ts_code, trade_date)

    def get_daily_basic(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        if self._client._pro is None:
            return None
        try:
            return self._client._pro.daily_basic(ts_code=ts_code, start_date=start_date, end_date=end_date)
        except Exception:
            return None

    def get_stk_factor(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        if self._client._pro is None:
            return None
        try:
            return self._client._pro.stk_factor(ts_code=ts_code, start_date=start_date, end_date=end_date)
        except Exception:
            return None

    def get_stock_basic(self, ts_code: str | None = None, name: str | None = None) -> pd.DataFrame | None:
        return self._client.get_stock_basic(ts_code, name)

    def get_trade_cal(self, exchange: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return self._client.get_trade_cal(exchange, start_date, end_date)

    def get_stock_list(self, exchange: str | None = None) -> list[dict]:
        df = self.get_stock_basic()
        if df is None or df.empty:
            return []
        columns = ["ts_code", "name", "industry", "market"]
        available = [c for c in columns if c in df.columns]
        return df[available].to_dict("records")

    def get_kline_dicts(
        self,
        ts_code: str,
        days: int = 60,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        df = self.get_daily(ts_code, start_date, end_date)
        if df is None or df.empty:
            return []
        records = df.to_dict("records")
        records.sort(key=lambda x: x.get("trade_date", ""))
        if not start_date and days > 0:
            records = records[-days:]
        return records


class BridgeDataSource:
    """Tushare Data Bridge HTTP API 数据源封装。

    支持传入实例级 ``BridgeConfig``，不会修改全局 bridge 配置。
    若未传 config，则使用当前全局配置。
    """

    def __init__(self, config: BridgeConfig | None = None):
        self._config = config

    @property
    def name(self) -> str:
        return "bridge"

    def health_check(self) -> bool:
        return is_bridge_available(self._config)

    def get_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return None

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return None

    def get_realtime_quote(self, ts_codes: list[str]) -> pd.DataFrame | None:
        return None

    def get_moneyflow(self, ts_code: str, trade_date: str) -> pd.DataFrame | None:
        return None

    def get_daily_basic(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return None

    def get_stk_factor(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return None

    def get_stock_basic(self, ts_code: str | None = None, name: str | None = None) -> pd.DataFrame | None:
        return None

    def get_trade_cal(self, exchange: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return None

    def get_stock_list(self, exchange: str | None = None) -> list[dict]:
        return get_all_stocks_bridge_first(exchange, config=self._config)

    def get_kline_dicts(
        self,
        ts_code: str,
        days: int = 60,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        return get_daily_klines(ts_code, days=days, start_date=start_date, end_date=end_date, config=self._config)


class SqliteDataSource:
    """本地 SQLite 数据源封装。"""

    @property
    def name(self) -> str:
        return "sqlite"

    def health_check(self) -> bool:
        try:
            with get_connection() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def get_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return None

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return None

    def get_realtime_quote(self, ts_codes: list[str]) -> pd.DataFrame | None:
        return None

    def get_moneyflow(self, ts_code: str, trade_date: str) -> pd.DataFrame | None:
        return None

    def get_daily_basic(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return None

    def get_stk_factor(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return None

    def get_stock_basic(self, ts_code: str | None = None, name: str | None = None) -> pd.DataFrame | None:
        return None

    def get_trade_cal(self, exchange: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return None

    def get_stock_list(self, exchange: str | None = None) -> list[dict]:
        with get_connection() as conn:
            cursor = conn.cursor()
            sql = "SELECT ts_code, name, industry, market FROM stock_basic"
            params: list = []
            if exchange:
                sql += " WHERE exchange = ?"
                params.append(exchange)
            sql += " ORDER BY ts_code"
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_kline_dicts(
        self,
        ts_code: str,
        days: int = 60,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        with get_connection() as conn:
            cursor = conn.cursor()
            params: list = [ts_code]
            sql = """
                SELECT ts_code, trade_date, open, high, low, close, vol, amount, pct_chg
                FROM daily_kline
                WHERE ts_code = ?
            """
            if start_date:
                sql += " AND trade_date >= ?"
                params.append(start_date)
            if end_date:
                sql += " AND trade_date <= ?"
                params.append(end_date)
            sql += " ORDER BY trade_date DESC"
            if not start_date and days > 0:
                sql += " LIMIT ?"
                params.append(days)
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return [dict(row) for row in reversed(rows)]


class CompositeDataSource:
    """组合数据源：按配置优先级自动回退。

    当前实现中，bridge / SQLite 仅提供 ``get_stock_list`` 与 ``get_kline_dicts``
    两个接口的完整回退；其余 DataSource 方法（如 ``get_daily``、
    ``get_stk_factor``、``get_moneyflow`` 等）仅在 ``preferred="tushare"``
    时生效，其他 preferred 模式下返回 ``None``。这是由底层 bridge/SQLite
    能力范围决定的，后续如需完整 Composite 可在此扩展。
    """

    def __init__(self, preferred: str = "auto"):
        self._preferred = preferred
        self._bridge = BridgeDataSource()
        self._sqlite = SqliteDataSource()
        self._tushare: TushareDataSource | None = None

    @property
    def _tushare_source(self) -> TushareDataSource:
        if self._tushare is None:
            self._tushare = TushareDataSource()
        return self._tushare

    @property
    def name(self) -> str:
        return f"composite({self._preferred})"

    def health_check(self) -> bool:
        if self._preferred == "bridge":
            return self._bridge.health_check()
        if self._preferred == "sqlite":
            return self._sqlite.health_check()
        if self._preferred == "tushare":
            return self._tushare_source.health_check()
        return self._bridge.health_check() or self._sqlite.health_check()

    def get_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        if self._preferred == "tushare":
            return self._tushare_source.get_daily(ts_code, start_date, end_date)
        return None

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        if self._preferred == "tushare":
            return self._tushare_source.get_index_daily(ts_code, start_date, end_date)
        return None

    def get_realtime_quote(self, ts_codes: list[str]) -> pd.DataFrame | None:
        if self._preferred == "tushare":
            return self._tushare_source.get_realtime_quote(ts_codes)
        return None

    def get_moneyflow(self, ts_code: str, trade_date: str) -> pd.DataFrame | None:
        if self._preferred == "tushare":
            return self._tushare_source.get_moneyflow(ts_code, trade_date)
        return None

    def get_daily_basic(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        if self._preferred == "tushare":
            return self._tushare_source.get_daily_basic(ts_code, start_date, end_date)
        return None

    def get_stk_factor(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        if self._preferred == "tushare":
            return self._tushare_source.get_stk_factor(ts_code, start_date, end_date)
        return None

    def get_stock_basic(self, ts_code: str | None = None, name: str | None = None) -> pd.DataFrame | None:
        if self._preferred == "tushare":
            return self._tushare_source.get_stock_basic(ts_code, name)
        return None

    def get_trade_cal(self, exchange: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        if self._preferred == "tushare":
            return self._tushare_source.get_trade_cal(exchange, start_date, end_date)
        return None

    def get_stock_list(self, exchange: str | None = None) -> list[dict]:
        sources: list[DataSource] = []
        if self._preferred == "auto":
            sources = [self._bridge, self._sqlite]
        elif self._preferred == "bridge":
            sources = [self._bridge]
        elif self._preferred == "sqlite":
            sources = [self._sqlite]
        elif self._preferred == "tushare":
            sources = [self._tushare_source]

        for source in sources:
            try:
                data = source.get_stock_list(exchange)
                if data:
                    return data
            except Exception:
                continue
        return []

    def get_kline_dicts(
        self,
        ts_code: str,
        days: int = 60,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        sources: list[DataSource] = []
        if self._preferred == "auto":
            sources = [self._bridge, self._sqlite]
        elif self._preferred == "bridge":
            sources = [self._bridge]
        elif self._preferred == "sqlite":
            sources = [self._sqlite]
        elif self._preferred == "tushare":
            sources = [self._tushare_source]

        for source in sources:
            try:
                data = source.get_kline_dicts(ts_code, days=days, start_date=start_date, end_date=end_date)
                if data:
                    return data
            except Exception:
                continue
        return []


def get_datasource(preferred: str = "auto") -> DataSource:
    """数据源工厂函数。"""
    if preferred == "tushare":
        return TushareDataSource()
    if preferred == "bridge":
        return BridgeDataSource()
    if preferred == "sqlite":
        return SqliteDataSource()
    return CompositeDataSource("auto")
