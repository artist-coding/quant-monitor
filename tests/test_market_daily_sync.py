"""收盘后全市场日线同步测试。"""

from __future__ import annotations

import pandas as pd

from modules.cli import build_parser
from modules.data_sync import DataSyncer
from modules.data_sync import syncer as syncer_module
from modules.database import get_connection


class FakeMarketDataSource:
    def __init__(
        self,
        *,
        is_open: int = 1,
        daily: pd.DataFrame | None = None,
        empty_before: int = 0,
    ):
        """
        Args:
            empty_before: 前 N 次 get_daily_by_trade_date 返回空 DataFrame，
                之后才返回 ``daily``。用于模拟中转 API 限流时的静默空响应。
                ``daily`` 为空时退化为「永远返回空」。
        """
        self.is_open = is_open
        self.daily = daily if daily is not None else pd.DataFrame()
        self.empty_before = empty_before
        self.market_calls = 0

    @property
    def name(self) -> str:
        return "fake-market"

    def get_trade_cal(self, exchange: str, start_date: str, end_date: str):
        # 真实 trade_cal 会返回区间内的每一天，这里照样铺满整段区间
        dates = pd.date_range(start_date, end_date).strftime("%Y%m%d").tolist()
        return pd.DataFrame(
            {
                "exchange": [exchange] * len(dates),
                "cal_date": dates,
                "is_open": [self.is_open] * len(dates),
            }
        )

    def get_daily_by_trade_date(self, trade_date: str):
        self.market_calls += 1
        if self.market_calls <= self.empty_before:
            return pd.DataFrame()
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


def test_market_daily_empty_response_retries_then_fails(temp_db, monkeypatch):
    """空结果必须重试后才判失败。

    中转 API 被限流时不报错、直接返回空 DataFrame，与「非交易日」无法区分。
    以前一次空就记 failed 并放过，整月被限流就等于整月静默漏数据——库里
    2019-2026 的大段缺口正是这么来的。现在必须重试满 3 次才允许失败。
    """
    monkeypatch.setattr(syncer_module, "_EMPTY_RETRY_BACKOFFS", (0, 0, 0))
    source = FakeMarketDataSource(is_open=1, daily=pd.DataFrame())
    result = DataSyncer(datasource=source).sync_market_daily("20260115", refresh_stock_basic=False)
    assert result["status"] == "failed"
    assert "连续 3 次返回空" in result["message"]
    assert source.market_calls == 3, f"应重试 3 次，实际 {source.market_calls} 次"


def test_market_daily_empty_then_success_recovers(temp_db, monkeypatch):
    """前两次返回空、第三次拿到数据时，应当正常入库而不是判失败。"""
    monkeypatch.setattr(syncer_module, "_EMPTY_RETRY_BACKOFFS", (0, 0, 0))
    source = FakeMarketDataSource(is_open=1, daily=_market_frame(), empty_before=2)
    result = DataSyncer(datasource=source).sync_market_daily("20260115", refresh_stock_basic=False)
    assert result["status"] == "success", result
    assert result["market_rows"] > 0


def test_market_daily_cli_parser():
    args = build_parser().parse_args(
        ["sync", "market-daily", "--date", "20260115", "--no-refresh-stock-basic", "--json"]
    )
    assert args.sync_action == "market-daily"
    assert args.date == "20260115"
    assert args.no_refresh_stock_basic is True
    assert args.json is True
