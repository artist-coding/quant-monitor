"""收盘后全市场日线同步测试。"""

from __future__ import annotations

import pandas as pd

from modules.cli import build_parser
from modules.data_sync import DataSyncer
from modules.database import get_connection


class FakeMarketDataSource:
    def __init__(self, *, is_open: int = 1, daily: pd.DataFrame | None = None):
        self.is_open = is_open
        self.daily = daily if daily is not None else pd.DataFrame()
        self.market_calls = 0

    @property
    def name(self) -> str:
        return "fake-market"

    def get_trade_cal(self, exchange: str, start_date: str, end_date: str):
        return pd.DataFrame({"exchange": [exchange], "cal_date": [start_date], "is_open": [self.is_open]})

    def get_daily_by_trade_date(self, trade_date: str):
        self.market_calls += 1
        return self.daily.copy()

    def get_stock_basic(self, ts_code=None, name=None):
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "name": "平安银行",
                    "area": "深圳",
                    "industry": "银行",
                    "market": "主板",
                    "list_date": "19910403",
                    "is_hs": "S",
                },
                {
                    "ts_code": "300001.SZ",
                    "name": "特锐德",
                    "area": "山东",
                    "industry": "电气设备",
                    "market": "创业板",
                    "list_date": "20091030",
                    "is_hs": "N",
                },
            ]
        )


def _market_frame(close_1: float = 10.5) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260115",
                "open": 10.0,
                "high": 10.8,
                "low": 9.9,
                "close": close_1,
                "vol": 1000,
                "amount": 10500,
                "pct_chg": 5.0,
            },
            {
                "ts_code": "300001.SZ",
                "trade_date": "20260115",
                "open": 20.0,
                "high": 24.0,
                "low": 19.8,
                "close": 24.0,
                "vol": 2000,
                "amount": 48000,
                "pct_chg": 20.0,
            },
        ]
    )


def test_market_daily_sync_upserts_all_rows_and_is_idempotent(temp_db):
    source = FakeMarketDataSource(daily=_market_frame())
    syncer = DataSyncer(datasource=source)

    first = syncer.sync_market_daily("20260115")
    assert first["status"] == "success"
    assert first["market_rows"] == 2
    assert first["db_rows_for_date"] == 2
    assert first["price_mode"] == "raw"

    source.daily = _market_frame(close_1=10.7)
    second = syncer.sync_market_daily("20260115")
    assert second["status"] == "success"
    assert second["db_rows_for_date"] == 2

    with get_connection() as conn:
        row = conn.execute(
            "SELECT close FROM daily_kline WHERE ts_code = ? AND trade_date = ?",
            ("000001.SZ", "20260115"),
        ).fetchone()
        stock_count = conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
        growth_limit = conn.execute(
            "SELECT is_limit_up FROM daily_kline WHERE ts_code = ? AND trade_date = ?",
            ("300001.SZ", "20260115"),
        ).fetchone()[0]
    assert row["close"] == 10.7
    assert stock_count == 2
    assert growth_limit == 1


def test_market_daily_skips_non_trading_day(temp_db):
    source = FakeMarketDataSource(is_open=0, daily=_market_frame())
    result = DataSyncer(datasource=source).sync_market_daily("20260118")
    assert result["status"] == "skipped"
    assert result["market_rows"] == 0
    assert source.market_calls == 0


def test_market_daily_empty_response_is_failure(temp_db):
    source = FakeMarketDataSource(is_open=1, daily=pd.DataFrame())
    result = DataSyncer(datasource=source).sync_market_daily("20260115", refresh_stock_basic=False)
    assert result["status"] == "failed"
    assert "日线为空" in result["message"]


def test_market_daily_cli_parser():
    args = build_parser().parse_args(
        ["sync", "market-daily", "--date", "20260115", "--no-refresh-stock-basic", "--json"]
    )
    assert args.sync_action == "market-daily"
    assert args.date == "20260115"
    assert args.no_refresh_stock_basic is True
    assert args.json is True
