"""复盘案例库（review_memory）测试。

不依赖 B1 是否真的触发——归因回放的正确性由 buy_decision 自己的测试保证，
这里验证的是案例层的职责：录入、归因字段结构、前瞻收益口径、结算与幂等。
"""

import json

import pytest

from tests.conftest import generate_uptrend_klines, write_klines_to_db, write_stock_basic


def _add_amv_regime(db_conn, trade_date, regime="多头区间", close=100000.0):
    db_conn.execute(
        "INSERT OR REPLACE INTO amv_daily (trade_date, close, regime) VALUES (?, ?, ?)",
        (trade_date, close, regime),
    )
    db_conn.commit()


def _setup_stock(db_conn, n=120, ts_code="600519.SH", start_date="20250601"):
    rows = generate_uptrend_klines(n=n, ts_code=ts_code, start_date=start_date)
    write_klines_to_db(db_conn, rows)
    write_stock_basic(db_conn, ts_code=ts_code)
    return rows


class TestAddCase:
    def test_basic_attribution_and_forward(self, temp_db, db_conn):
        from modules import review_memory as rm

        rows = _setup_stock(db_conn)
        case_date = rows[80]["trade_date"]  # 前 80 根历史够判定，后 39 根够结算 30 日
        case = rm.add_case("600519.SH", case_date, note="测试案例", tags="缩量回踩")

        assert case["ts_code"] == "600519.SH"
        assert case["name"] == "贵州茅台"
        assert case["case_date"] == case_date
        assert case["decision_date"] == case_date
        assert case["source"] == "manual"
        assert case["note"] == "测试案例"
        assert case["stopped_at"] in ("excluded", "veto", "no_trigger", "scored", "no_data")
        assert case["action"] in ("BUY", "WATCH", "NONE")
        # decision_json 已解析为 decision 字典，含完整明细
        assert isinstance(case["decision"], dict)
        assert case["decision"]["ts_code"] == "600519.SH"
        assert "detail" in case["decision"]
        # 上升趋势里次日开盘买入，各窗口收益为正且已结清
        assert case["entry_date"] == rows[81]["trade_date"]
        assert case["entry_price"] == pytest.approx(rows[81]["open"])
        for h in (5, 10, 20, 30):
            assert case[f"ret_{h}"] > 0
        assert case["ret_peak_30"] >= case["ret_30"]
        assert case["settled"] == 1

    def test_gate_blocked_without_amv(self, temp_db, db_conn):
        from modules import review_memory as rm

        rows = _setup_stock(db_conn)
        case = rm.add_case("600519.SH", rows[80]["trade_date"], precompute_theme=False)
        assert case["regime"] == ""
        assert case["gate_blocked"] == 1

    def test_gate_open_with_regime_carried_forward(self, temp_db, db_conn):
        from modules import review_memory as rm

        rows = _setup_stock(db_conn)
        _add_amv_regime(db_conn, rows[0]["trade_date"], "多头区间")  # 只录第一天，靠沿用铺满
        case = rm.add_case("600519.SH", rows[80]["trade_date"], precompute_theme=False)
        assert case["regime"] == "多头区间"
        assert case["gate_blocked"] == 0

    def test_input_normalization(self, temp_db, db_conn):
        from modules import review_memory as rm

        rows = _setup_stock(db_conn)
        d = rows[80]["trade_date"]
        dashed = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        case = rm.add_case("600519", dashed, precompute_theme=False)
        assert case["ts_code"] == "600519.SH"
        assert case["case_date"] == d

    def test_non_trading_day_falls_back(self, temp_db, db_conn):
        from modules import review_memory as rm

        rows = _setup_stock(db_conn, n=60)
        last = rows[-1]["trade_date"]
        case = rm.add_case("600519.SH", "20991231", precompute_theme=False)
        assert case["case_date"] == "20991231"
        assert case["decision_date"] == last  # 回退到不晚于请求日的最近一根

    def test_date_before_data_raises(self, temp_db, db_conn):
        from modules import review_memory as rm

        _setup_stock(db_conn)
        with pytest.raises(ValueError, match="早于"):
            rm.add_case("600519.SH", "19900101", precompute_theme=False)

    def test_upsert_keeps_id_and_note_semantics(self, temp_db, db_conn):
        from modules import review_memory as rm

        rows = _setup_stock(db_conn)
        d = rows[80]["trade_date"]
        first = rm.add_case("600519.SH", d, note="第一版", precompute_theme=False)
        second = rm.add_case("600519.SH", d, note="第二版", precompute_theme=False)
        assert second["id"] == first["id"]
        assert second["note"] == "第二版"
        # 空 note 重录不抹掉已有记录
        third = rm.add_case("600519.SH", d, precompute_theme=False)
        assert third["note"] == "第二版"
        assert len(rm.list_cases()) == 1


class TestSettle:
    def test_partial_then_settle(self, temp_db, db_conn):
        from modules import review_memory as rm

        rows = _setup_stock(db_conn)
        d = rows[100]["trade_date"]  # 只剩 19 根未来 K 线：+5/+10 可算，+20/+30 待结算
        case = rm.add_case("600519.SH", d, precompute_theme=False)
        assert case["ret_5"] is not None
        assert case["ret_10"] is not None
        assert case["ret_20"] is None
        assert case["ret_30"] is None
        assert case["settled"] == 0

        # 补 40 根后结算
        more = generate_uptrend_klines(n=40, start_date="20251001", start_price=rows[-1]["close"])
        write_klines_to_db(db_conn, more)
        updated = rm.settle_open_cases()
        assert len(updated) == 1
        settled = updated[0]
        assert settled["id"] == case["id"]
        assert settled["ret_30"] is not None
        assert settled["ret_peak_30"] is not None
        assert settled["settled"] == 1
        # 再跑一次没有待结算的
        assert rm.settle_open_cases() == []

    def test_today_case_waits_for_next_bar(self, temp_db, db_conn):
        from modules import review_memory as rm

        rows = _setup_stock(db_conn)
        case = rm.add_case("600519.SH", rows[-1]["trade_date"], precompute_theme=False)
        assert case["entry_date"] == ""
        assert case["settled"] == 0


class TestQueryAndRender:
    def test_list_show_and_format(self, temp_db, db_conn):
        from modules import review_memory as rm

        rows = _setup_stock(db_conn)
        case = rm.add_case("600519.SH", rows[80]["trade_date"], note="图形复盘", precompute_theme=False)

        cases = rm.list_cases(source="manual")
        assert len(cases) == 1
        assert rm.list_cases(source="missed") == []

        got = rm.get_case(case["id"])
        assert got["id"] == case["id"]
        assert rm.get_case(99999) is None

        text = rm.format_case(got)
        assert "框架归因" in text and "前瞻收益" in text and "图形复盘" in text
        assert rm.format_case_list(cases)
        assert "案例库为空" in rm.format_case_list([])

    def test_decision_json_survives_roundtrip(self, temp_db, db_conn):
        from modules import review_memory as rm
        from modules.database import get_connection

        rows = _setup_stock(db_conn)
        rm.add_case("600519.SH", rows[80]["trade_date"], precompute_theme=False)
        with get_connection() as conn:
            raw = conn.execute("SELECT decision_json FROM review_cases").fetchone()[0]
        payload = json.loads(raw)
        assert payload["ts_code"] == "600519.SH"
        assert "confirms" in payload and "vetoes" in payload
