"""交易日历本地缓存 + 指数日线同步测试（阶段0 地基改造）。"""

from __future__ import annotations

import pandas as pd
import pytest

from modules.data_sync import DataSyncer
from modules.data_sync.syncer import DEFAULT_INDEXES
from modules.database import get_connection


class FakeCalendarSource:
    """可控的假数据源：记录 trade_cal 调用次数，可模拟接口异常。"""

    def __init__(self, *, is_open: int = 1, raise_on_cal: bool = False, empty_cal: bool = False):
        self.is_open = is_open
        self.raise_on_cal = raise_on_cal
        self.empty_cal = empty_cal
        self.cal_calls = 0
        self.market_calls = 0
        self.index_calls = 0
        self.index_frame: pd.DataFrame | None = None
        self.daily: pd.DataFrame = pd.DataFrame()

    @property
    def name(self) -> str:
        return "fake-calendar"

    def get_trade_cal(self, exchange: str, start_date: str, end_date: str):
        self.cal_calls += 1
        if self.raise_on_cal:
            raise RuntimeError("抱歉，您每分钟最多访问该接口1次")
        if self.empty_cal:
            return pd.DataFrame()
        dates = pd.date_range(start_date, end_date).strftime("%Y%m%d").tolist()
        return pd.DataFrame(
            {
                "exchange": [exchange] * len(dates),
                "cal_date": dates,
                "is_open": [self.is_open] * len(dates),
                "pretrade_date": [None] * len(dates),
            }
        )

    def get_daily_by_trade_date(self, trade_date: str):
        self.market_calls += 1
        return self.daily.copy()

    def get_index_daily(self, ts_code: str, start_date: str, end_date: str):
        self.index_calls += 1
        if self.index_frame is None:
            return pd.DataFrame()
        return self.index_frame.copy()

    def get_stock_basic(self, ts_code=None, name=None):
        return pd.DataFrame()


def _market_frame(trade_date: str = "20260115") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": 10.0,
                "high": 10.8,
                "low": 9.9,
                "close": 10.5,
                "vol": 1000,
                "amount": 10500,
                "pct_chg": 5.0,
            }
        ]
    )


def _index_frame(ts_code: str = "000001.SH") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": ts_code,
                "trade_date": "20260115",
                "close": 3200.5,
                "open": 3180.0,
                "high": 3210.0,
                "low": 3175.0,
                "pre_close": 3175.5,
                "change": 25.0,
                "pct_chg": 0.79,
                "vol": 380000000.0,
                "amount": 450000000.0,
            },
            {
                "ts_code": ts_code,
                "trade_date": "20260116",
                "close": 3230.0,
                "open": 3200.5,
                "high": 3240.0,
                "low": 3198.0,
                "pre_close": 3200.5,
                "change": 29.5,
                "pct_chg": 0.92,
                "vol": 400000000.0,
                "amount": 470000000.0,
            },
        ]
    )


# ==================== 建表 ====================


def test_init_database_creates_new_tables(temp_db):
    """init_database 必须创建 trade_cal / index_daily / daily_scores 三张表"""
    with get_connection() as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"trade_cal", "index_daily", "daily_scores"}.issubset(names)


def test_init_database_is_idempotent(temp_db):
    """对已存在的库重复执行 init_database 不应报错，且不丢数据"""
    from modules.database import init_database

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO trade_cal (exchange, cal_date, is_open, pretrade_date) VALUES (?, ?, ?, ?)",
            ("SSE", "20260115", 1, "20260114"),
        )

    init_database(verbose=False)
    init_database(verbose=False)

    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM trade_cal").fetchone()[0]
    assert count == 1


# ==================== 交易日历 ====================


def test_sync_trade_cal_upserts_and_is_idempotent(temp_db):
    """sync_trade_cal 入库交易日历，重复执行幂等（主键去重）"""
    source = FakeCalendarSource(is_open=1)
    syncer = DataSyncer(datasource=source)

    first = syncer.sync_trade_cal("20260101", "20260131")
    assert first == 31

    second = syncer.sync_trade_cal("20260101", "20260131")
    assert second == 31

    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM trade_cal WHERE exchange = 'SSE'").fetchone()[0]
        row = conn.execute(
            "SELECT is_open FROM trade_cal WHERE exchange = ? AND cal_date = ?",
            ("SSE", "20260115"),
        ).fetchone()
        log_status = conn.execute(
            "SELECT status FROM sync_log WHERE data_type = 'trade_cal' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert total == 31
    assert row[0] == 1
    assert log_status == "success"


def test_is_trade_day_hits_local_cache(temp_db):
    """首次查询触发一次整年拉取，之后命中本地缓存不再打 API"""
    source = FakeCalendarSource(is_open=1)
    syncer = DataSyncer(datasource=source)

    assert syncer.is_trade_day("20260115") is True
    assert source.cal_calls == 1

    # 同一年的其它日期已在缓存中，不应再次请求
    assert syncer.is_trade_day("20260620") is True
    assert syncer.is_trade_day("20261231") is True
    assert source.cal_calls == 1


def test_is_trade_day_returns_false_for_non_trading_day(temp_db):
    source = FakeCalendarSource(is_open=0)
    syncer = DataSyncer(datasource=source)
    assert syncer.is_trade_day("20260118") is False


def test_is_trade_day_returns_none_when_fetch_fails(temp_db):
    """API 限流/异常时返回 None（未知），不抛异常"""
    source = FakeCalendarSource(raise_on_cal=True)
    syncer = DataSyncer(datasource=source)

    assert syncer.is_trade_day("20260115") is None


def test_is_trade_day_returns_none_when_calendar_empty(temp_db):
    source = FakeCalendarSource(empty_cal=True)
    syncer = DataSyncer(datasource=source)

    assert syncer.is_trade_day("20260115") is None


def test_sync_market_daily_continues_when_calendar_unknown(temp_db):
    """交易日历不可用时不再 raise，继续走同步流程"""
    source = FakeCalendarSource(raise_on_cal=True)
    source.daily = _market_frame()
    syncer = DataSyncer(datasource=source)

    result = syncer.sync_market_daily("20260115", refresh_stock_basic=False)

    assert result["status"] == "success"
    assert result["market_rows"] == 1
    assert source.market_calls == 1
    # 返回结构的 key 保持不变
    assert set(result) == {
        "status",
        "trade_date",
        "stock_basic_rows",
        "market_rows",
        "db_rows_for_date",
        "price_mode",
        "message",
    }


def test_sync_market_daily_reuses_cached_calendar(temp_db):
    """本地已有日历缓存时，sync_market_daily 不应再请求 trade_cal 接口"""
    source = FakeCalendarSource(is_open=1)
    source.daily = _market_frame()
    syncer = DataSyncer(datasource=source)

    syncer.sync_trade_cal("20260101", "20261231")
    assert source.cal_calls == 1

    result = syncer.sync_market_daily("20260115", refresh_stock_basic=False)
    assert result["status"] == "success"
    assert source.cal_calls == 1


# ==================== 指数日线 ====================


def test_default_indexes_fits_daily_quota():
    """指数清单必须留在 index_daily 的 5 次/天配额内，且以沪深300 打头。

    实测该中转源 index_daily 限额 5 次/天。一次同步 = 一个指数一次调用，
    清单超过 5 个时后面的必然失败；这里卡在 ≤4 以便给失败重跑留余量。
    """
    assert len(DEFAULT_INDEXES) <= 4, f"指数数量 {len(DEFAULT_INDEXES)} 超出 index_daily 的 5 次/天配额余量"
    assert DEFAULT_INDEXES[0] == "000300.SH"  # 沪深300 代表性最强，配额不够时优先保它
    assert len(set(DEFAULT_INDEXES)) == len(DEFAULT_INDEXES), "指数清单有重复，白白浪费配额"
    assert {"000300.SH", "000001.SH"}.issubset(set(DEFAULT_INDEXES))


def test_rate_limit_error_is_not_retried(temp_db):
    """限流错误必须立刻抛出：重试只会多烧配额，1s/2s 退避也等不过任何限流窗口"""
    calls = []

    def _boom():
        calls.append(1)
        raise RuntimeError("抱歉，您访问接口(index_daily)频率超限(5次/天)，具体频次详情：...")

    syncer = DataSyncer(datasource=FakeCalendarSource())
    with pytest.raises(RuntimeError, match="频率超限"):
        syncer._call_api_with_retry("index_daily", _boom)

    assert len(calls) == 1, f"限流错误被重试了 {len(calls)} 次，会加速耗尽配额"


def test_non_rate_limit_error_still_retries(temp_db):
    """普通错误（网络抖动等）仍然要重试，不能被上面的改动误伤"""
    calls = []

    def _flaky():
        calls.append(1)
        raise RuntimeError("connection reset by peer")

    syncer = DataSyncer(datasource=FakeCalendarSource())
    with pytest.raises(RuntimeError):
        syncer._call_api_with_retry("daily", _flaky)

    assert len(calls) == 3


def test_sync_index_daily_inserts_and_is_idempotent(temp_db):
    source = FakeCalendarSource()
    source.index_frame = _index_frame()
    syncer = DataSyncer(datasource=source)

    first = syncer.sync_index_daily("000001.SH", "20260101", "20260131")
    assert first == 2

    second = syncer.sync_index_daily("000001.SH", "20260101", "20260131")
    assert second == 2

    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM index_daily").fetchone()[0]
        row = conn.execute(
            "SELECT close, pct_chg, pre_close FROM index_daily WHERE ts_code = ? AND trade_date = ?",
            ("000001.SH", "20260116"),
        ).fetchone()
        last_date = conn.execute(
            "SELECT last_date FROM sync_log WHERE data_type = 'index_daily' AND status = 'success' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert total == 2
    assert row["close"] == 3230.0
    assert row["pct_chg"] == 0.92
    assert row["pre_close"] == 3200.5
    assert last_date == "20260116"


def test_sync_index_daily_incremental_uses_last_sync_date(temp_db):
    """start_date=None 时从 sync_log 最后成功日期次日开始"""
    captured: dict[str, str] = {}

    source = FakeCalendarSource()
    source.index_frame = _index_frame()
    syncer = DataSyncer(datasource=source)
    syncer.sync_index_daily("000001.SH", "20260101", "20260131")

    original = source.get_index_daily

    def _spy(ts_code, start_date, end_date):
        captured["start_date"] = start_date
        return original(ts_code, start_date, end_date)

    source.get_index_daily = _spy
    syncer.sync_index_daily("000001.SH")
    assert captured["start_date"] == "20260117"


def test_sync_index_daily_empty_returns_zero(temp_db):
    source = FakeCalendarSource()
    source.index_frame = None
    syncer = DataSyncer(datasource=source)
    assert syncer.sync_index_daily("000001.SH", "20260101", "20260131") == 0


def test_sync_index_daily_error_returns_zero_and_logs_failure(temp_db):
    class BoomSource(FakeCalendarSource):
        def get_index_daily(self, ts_code, start_date, end_date):
            raise RuntimeError("接口挂了")

    syncer = DataSyncer(datasource=BoomSource())
    assert syncer.sync_index_daily("000001.SH", "20260101", "20260131") == 0

    with get_connection() as conn:
        status = conn.execute(
            "SELECT status FROM sync_log WHERE data_type = 'index_daily' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert status == "failed"


def test_sync_all_index_daily_defaults_to_default_indexes(temp_db):
    source = FakeCalendarSource()
    source.index_frame = _index_frame()
    syncer = DataSyncer(datasource=source)

    results = syncer.sync_all_index_daily(start_date="20260101", end_date="20260131")

    assert set(results) == set(DEFAULT_INDEXES)
    assert all(count == 2 for count in results.values())
