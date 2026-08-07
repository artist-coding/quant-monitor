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
