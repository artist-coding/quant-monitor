"""Data fetcher wrapping a DataSource."""

from __future__ import annotations
from typing import Any

import pandas as pd

from ..datasource import DataSource


class DataFetcher:
    """Encapsulates raw data pulling from a DataSource."""

    def __init__(self, datasource: DataSource):
        self.datasource = datasource

    def fetch_stock_basic(self) -> pd.DataFrame | None:
        return self.datasource.get_stock_basic()

    def fetch_daily_kline(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return self.datasource.get_daily(ts_code, start_date, end_date)

    def fetch_market_daily(self, trade_date: str) -> pd.DataFrame | None:
        return self.datasource.get_daily_by_trade_date(trade_date)

    def fetch_trade_cal(self, exchange: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return self.datasource.get_trade_cal(exchange, start_date, end_date)

    def fetch_index_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        """拉取指数日线行情（Tushare index_daily 接口）。"""
        return self.datasource.get_index_daily(ts_code, start_date, end_date)

    def fetch_stk_factor(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return self.datasource.get_stk_factor(ts_code, start_date, end_date)

    def fetch_daily_basic(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        return self.datasource.get_daily_basic(ts_code, start_date, end_date)

    def fetch_moneyflow(self, ts_code: str, trade_date: str) -> pd.DataFrame | None:
        return self.datasource.get_moneyflow(ts_code, trade_date)
