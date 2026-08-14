"""
watchlist.py 自选股观察池测试
"""

from modules.watchlist import (
    add_watch,
    remove_watch,
    list_watch,
    normalize_ts_code,
    scan_watchlist,
)


class TestWatchlistCRUD:
    def test_normalize_bare_code_and_resolve_name(self, temp_db, db_conn):
        from tests.conftest import write_stock_basic

        write_stock_basic(db_conn, "600487.SH", "亨通光电")

        add_watch("600487")
        watches = list_watch()

        assert normalize_ts_code("SH600487") == "600487.SH"
        assert watches[0]["ts_code"] == "600487.SH"
        assert watches[0]["name"] == "亨通光电"

    def test_list_includes_latest_market_snapshot(self, temp_db, db_conn):
        from tests.conftest import make_kline_row, write_klines_to_db, write_stock_basic

        write_stock_basic(db_conn, "600487.SH", "亨通光电")
        row = make_kline_row("600487.SH", "20260717", 18.5, 123456)
        row["close"] = 18.88
        row["pct_chg"] = 2.03
        write_klines_to_db(db_conn, [row])

        add_watch("600487")
        item = list_watch()[0]

        assert item["price"] == 18.88
        assert item["pct_chg"] == 2.03
        assert item["trade_date"] == "20260717"
        assert item["kline_count"] == 1

    def test_add_service_recalls_complete_item_from_database(self, temp_db, db_conn):
        from api.services.watchlist_service import add_to_watchlist
        from tests.conftest import make_kline_row, write_klines_to_db, write_stock_basic

        write_stock_basic(db_conn, "600519.SH", "贵州茅台")
        row = make_kline_row("600519.SH", "20260717", 1259.0, 88888)
        row["close"] = 1253.0
        row["pct_chg"] = -0.4758
        write_klines_to_db(db_conn, [row])

        result = add_to_watchlist("600519")

        assert result["status"] == "ok"
        assert result["item"]["ts_code"] == "600519.SH"
        assert result["item"]["name"] == "贵州茅台"
        assert result["item"]["price"] == 1253.0
        assert result["item"]["pct_chg"] == -0.4758

    def test_add_and_list(self, temp_db, db_conn):
        from tests.conftest import write_stock_basic

        write_stock_basic(db_conn, "600519.SH", "贵州茅台")

        wid = add_watch("600519.SH", name="贵州茅台", tags="波段")
        assert wid > 0

        watches = list_watch()
        assert len(watches) == 1
        assert watches[0]["ts_code"] == "600519.SH"
        assert watches[0]["tags"] == "波段"

    def test_remove(self, temp_db, db_conn):
        from tests.conftest import write_stock_basic

        write_stock_basic(db_conn, "000001.SZ", "平安银行")

        add_watch("000001.SZ", name="平安银行")
        assert len(list_watch()) == 1

        assert remove_watch("000001.SZ") is True
        assert len(list_watch()) == 0

    def test_list_by_tags(self, temp_db, db_conn):
        from tests.conftest import write_stock_basic

        write_stock_basic(db_conn, "600519.SH", "贵州茅台")
        write_stock_basic(db_conn, "000001.SZ", "平安银行")

        add_watch("600519.SH", tags="波段")
        add_watch("000001.SZ", tags="短线")

        band = list_watch(tags="波段")
        assert len(band) == 1
        assert band[0]["ts_code"] == "600519.SH"


class TestScanWatchlist:
    def test_empty(self, temp_db):
        result = scan_watchlist()
        assert result["summary"]["total"] == 0
        assert result["alerts"] == []


class TestScanWatchlistAlerts:
    """破位预警 / 异动预警 / 逃顶信号全量扫描（历史上均为死分支或被截断）"""

    @staticmethod
    def _patch_scan(monkeypatch, ind, signals=()):
        """替换 watchlist 内部的 analyze_stock / detect_all_strategies，隔离出告警逻辑"""
        import modules.watchlist as wl

        monkeypatch.setattr(wl, "analyze_stock", lambda ts_code, days=60: ind)
        monkeypatch.setattr(wl, "detect_all_strategies", lambda ts_code, days=60: list(signals))

    @staticmethod
    def _make_ind(**kwargs):
        from modules.indicators import IndicatorResult

        base = dict(ts_code="600487.SH", trade_date="20260717", close=0.0, pct_chg=0.0)
        base.update(kwargs)
        return IndicatorResult(**base)

    def test_break_bbi_alert_triggers(self, temp_db, db_conn, monkeypatch):
        from tests.conftest import write_stock_basic

        write_stock_basic(db_conn, "600487.SH", "亨通光电")
        add_watch("600487")
        self._patch_scan(monkeypatch, self._make_ind(close=90.0, bbi=100.0))

        result = scan_watchlist()
        breaks = [a for a in result["alerts"] if a.alert_type == "BREAK"]

        assert result["summary"]["break_count"] == 1
        assert breaks[0].message == "跌破BBI"
        assert breaks[0].level == "WARNING"

    def test_break_bbi_not_triggered_when_above(self, temp_db, db_conn, monkeypatch):
        from tests.conftest import write_stock_basic

        write_stock_basic(db_conn, "600487.SH", "亨通光电")
        add_watch("600487")
        self._patch_scan(monkeypatch, self._make_ind(close=99.0, bbi=100.0))

        assert scan_watchlist()["summary"]["break_count"] == 0

    def test_empty_indicator_result_does_not_false_alarm(self, temp_db, db_conn, monkeypatch):
        """analyze_stock 无数据时 close=0，不能误报跌破 BBI"""
        from tests.conftest import write_stock_basic

        write_stock_basic(db_conn, "600487.SH", "亨通光电")
        add_watch("600487")
        self._patch_scan(monkeypatch, self._make_ind(trade_date="", close=0.0, bbi=100.0))

        result = scan_watchlist()

        assert result["summary"]["break_count"] == 0
        assert result["summary"]["abnormal_count"] == 0

    def test_pct_chg_abnormal_triggers(self, temp_db, db_conn, monkeypatch):
        from tests.conftest import write_stock_basic

        write_stock_basic(db_conn, "600487.SH", "亨通光电")
        add_watch("600487")
        self._patch_scan(monkeypatch, self._make_ind(close=58.39, pct_chg=-9.96, vol_ratio=1.2))

        result = scan_watchlist()
        abnormal = [a for a in result["alerts"] if a.alert_type == "ABNORMAL"]

        assert result["summary"]["abnormal_count"] == 1
        assert "涨跌幅-9.96%" in abnormal[0].message
        assert "量比" not in abnormal[0].message
        assert abnormal[0].data["pct_chg"] == -9.96

    def test_vol_ratio_abnormal_message_distinguishes(self, temp_db, db_conn, monkeypatch):
        from tests.conftest import write_stock_basic

        write_stock_basic(db_conn, "600487.SH", "亨通光电")
        add_watch("600487")
        self._patch_scan(monkeypatch, self._make_ind(close=58.39, pct_chg=1.0, vol_ratio=4.5))

        abnormal = [a for a in scan_watchlist()["alerts"] if a.alert_type == "ABNORMAL"]

        assert abnormal[0].message == "异动 量比4.5"

    def test_both_abnormal_reasons_listed(self, temp_db, db_conn, monkeypatch):
        from tests.conftest import write_stock_basic

        write_stock_basic(db_conn, "600487.SH", "亨通光电")
        add_watch("600487")
        self._patch_scan(monkeypatch, self._make_ind(close=58.39, pct_chg=6.5, vol_ratio=4.5))

        abnormal = [a for a in scan_watchlist()["alerts"] if a.alert_type == "ABNORMAL"]

        assert abnormal[0].message == "异动 量比4.5 涨跌幅+6.50%"

    def test_exit_signal_beyond_first_three_is_reported(self, temp_db, db_conn, monkeypatch):
        """逃顶是 CRITICAL，排在第 4 位之后也不能漏报"""
        from tests.conftest import write_stock_basic
        from modules.strategies import StrategySignal, StrategyType

        write_stock_basic(db_conn, "600487.SH", "亨通光电")
        add_watch("600487")

        def sig(strategy, action, date):
            return StrategySignal(
                ts_code="600487.SH",
                trade_date=date,
                strategy=strategy,
                confidence=0.8,
                description="",
                action=action,
            )

        signals = [
            sig(StrategyType.WATCH, "WATCH", "20260717"),
            sig(StrategyType.XISHOU, "WATCH", "20260716"),
            sig(StrategyType.PAIFA, "SELL", "20260715"),
            sig(StrategyType.WATCH, "WATCH", "20260714"),
            sig(StrategyType.S1, "SELL", "20260713"),
            sig(StrategyType.S3, "SELL", "20260710"),
        ]
        self._patch_scan(monkeypatch, self._make_ind(close=58.39), signals)

        result = scan_watchlist()
        exits = [a for a in result["alerts"] if a.alert_type == "EXIT"]

        assert result["summary"]["exit_count"] == 2
        # 消息带上信号日期：同一战法在不同交易日各触发一次是常态，不带日期会看起来像重复刷屏
        assert {a.message for a in exits} == {"20260713 S1逃顶信号", "20260710 S3逃顶信号"}

    def test_same_strategy_different_dates_not_deduped(self, temp_db, db_conn, monkeypatch):
        """同一战法不同日期是两条独立信号，不能被去重合并"""
        from tests.conftest import write_stock_basic
        from modules.strategies import StrategySignal, StrategyType

        write_stock_basic(db_conn, "600487.SH", "亨通光电")
        add_watch("600487")

        def sig(date):
            return StrategySignal(
                ts_code="600487.SH",
                trade_date=date,
                strategy=StrategyType.S1,
                confidence=0.8,
                description="",
                action="SELL",
            )

        # 三条同战法不同日期 + 一条完全重复的（应被去重掉）
        signals = [sig("20260717"), sig("20260716"), sig("20260715"), sig("20260715")]
        self._patch_scan(monkeypatch, self._make_ind(close=58.39), signals)

        result = scan_watchlist()
        exits = [a for a in result["alerts"] if a.alert_type == "EXIT"]

        assert result["summary"]["exit_count"] == 3
        assert {a.message for a in exits} == {
            "20260717 S1逃顶信号",
            "20260716 S1逃顶信号",
            "20260715 S1逃顶信号",
        }

    def test_buy_signals_still_capped_at_three(self, temp_db, db_conn, monkeypatch):
        from tests.conftest import write_stock_basic
        from modules.strategies import StrategySignal, StrategyType

        write_stock_basic(db_conn, "600487.SH", "亨通光电")
        add_watch("600487")

        signals = [
            StrategySignal(
                ts_code="600487.SH",
                trade_date=f"2026071{i}",
                strategy=StrategyType.B1,
                confidence=0.8,
                description="",
                action="BUY",
            )
            for i in range(5)
        ]
        self._patch_scan(monkeypatch, self._make_ind(close=58.39), signals)

        result = scan_watchlist()

        assert result["summary"]["b1_count"] == 3
