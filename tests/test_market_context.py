"""大盘环境（modules/market_context.py）测试。

从已删除的 test_daily_pipeline.py 迁移而来，并补上宽度层的用例
——宽度是首选数据源，原来的测试只覆盖了指数降级路径。
"""

from __future__ import annotations

import pytest

from modules import market_context as mc
from modules.market_context import compute_market_breadth, compute_market_context


def _write_index_rows(rows):
    """往 index_daily 写入 (ts_code, trade_date, close, pct_chg)。"""
    from modules.database import get_connection

    with get_connection() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO index_daily (ts_code, trade_date, close, pct_chg) VALUES (?, ?, ?, ?)",
            rows,
        )


def _write_market(trade_date, *, up, down, limit_up=0, limit_down=0, st=0, bse=0, industry="测试业"):
    """造一天的全市场行情。

    up/down 是普通可交易票的涨跌只数；st/bse 额外造 ST 与北交所标的，
    用于验证它们**不进入**宽度统计。
    """
    from modules.database import get_connection

    basics, klines = [], []

    def _add(code, name, pct, lu=0, ld=0):
        basics.append((code, name, industry))
        klines.append((code, trade_date, 10.0, 10.0, 10.0, 10.0, 1000.0, 10000.0, pct, lu, ld))

    n = 0
    for i in range(up):
        _add(f"6{n:05d}.SH", f"涨{i}", 1.5, 1 if i < limit_up else 0)
        n += 1
    for i in range(down):
        _add(f"6{n:05d}.SH", f"跌{i}", -1.5, 0, 1 if i < limit_down else 0)
        n += 1
    for i in range(st):
        _add(f"6{n:05d}.SH", f"*ST垃圾{i}", -9.0)
        n += 1
    for i in range(bse):
        _add(f"92{n:04d}.BJ", f"北交{i}", 25.0, 1)
        n += 1

    with get_connection() as conn:
        conn.executemany("INSERT OR REPLACE INTO stock_basic (ts_code, name, industry) VALUES (?, ?, ?)", basics)
        conn.executemany(
            """INSERT OR REPLACE INTO daily_kline
               (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, is_limit_up, is_limit_down)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            klines,
        )


# ==================== 市场宽度（首选数据源）====================


def test_breadth_needs_full_market_sample(temp_db):
    """样本不足说明当天不是全市场同步日，据此算宽度会得出错误结论。"""
    _write_market("20260807", up=100, down=100)
    b = compute_market_breadth("20260807")
    assert b["available"] is False
    assert str(mc._BREADTH_MIN_SAMPLE) in b["reason"]


def test_breadth_bullish(temp_db):
    _write_market("20260807", up=2800, down=1200, limit_up=100)
    b = compute_market_breadth("20260807")
    assert b["available"] is True
    assert b["market_dir"] == "LONG"
    assert b["up"] == 2800 and b["down"] == 1200
    assert b["up_ratio"] == pytest.approx(70.0)
    assert b["strength"] > 70


def test_breadth_bearish(temp_db):
    _write_market("20260807", up=1200, down=2800, limit_down=80)
    b = compute_market_breadth("20260807")
    assert b["market_dir"] == "SHORT"
    assert b["strength"] < 30


def test_breadth_neutral_when_split(temp_db):
    _write_market("20260807", up=2000, down=2000)
    assert compute_market_breadth("20260807")["market_dir"] == "NEUTRAL"


def test_breadth_excludes_st_and_bse(temp_db):
    """ST（±5%）与北交所（±30%）的涨跌停幅度都不是 ±10%，
    而 is_limit_up 阈值写死 9.9%——一个永远识别不到涨停、一个把涨 12% 误判成
    涨停，朝相反方向污染同一个统计，必须排除在宽度之外。"""
    _write_market("20260807", up=2000, down=2000, st=300, bse=200)
    b = compute_market_breadth("20260807")
    assert b["total"] == 4000, "ST 与北交所不应计入宽度样本"
    # 北交所那 200 只都标了 is_limit_up，若混入会把强度顶起来
    assert b["limit_up"] == 0


# ==================== 两层降级 ====================


def test_breadth_wins_over_index(temp_db):
    """宽度可用时以它为准——指数会被权重股绑架，宽度反映的是赚钱效应。"""
    _write_market("20260807", up=2800, down=1200)
    _write_index_rows([(mc.MARKET_PRIMARY_INDEX, "20260807", 4000.0, -3.0)])

    ctx = compute_market_context("20260807")
    assert ctx["detail"]["source"] == "breadth"
    assert ctx["market_dir"] == "LONG"
    # 指数仍作为参考记下来，但不参与定方向
    assert ctx["detail"]["index_ref"]["pct_chg"] == -3.0


def test_market_context_no_data_returns_neutral(temp_db):
    """宽度与指数都没有时降级为 NEUTRAL / 50。"""
    ctx = compute_market_context("20260807")
    assert ctx["market_dir"] == "NEUTRAL"
    assert ctx["market_pct_chg"] == 0.0
    assert ctx["market_strength"] == 50.0
    assert ctx["detail"]["source"] == "none"


# ==================== 指数补充层 ====================


def test_market_context_bullish_index(temp_db):
    """收盘价站上 MA5/MA20 且当日上涨 → LONG"""
    rows = []
    price = 3000.0
    for i in range(20):
        price *= 1.005
        rows.append((mc.MARKET_PRIMARY_INDEX, f"202607{i + 10:02d}", price, 0.5))
    rows[-1] = (mc.MARKET_PRIMARY_INDEX, rows[-1][1], rows[-1][2], 1.5)
    _write_index_rows(rows)

    ctx = compute_market_context(rows[-1][1])
    assert ctx["market_dir"] == "LONG"
    # 50 + min(1.5*10, 8) + 15 + 15 = 88
    assert ctx["market_strength"] == pytest.approx(88.0)
    assert ctx["detail"]["is_current"] is True


def test_market_context_bearish_index(temp_db):
    rows = []
    price = 4000.0
    for i in range(20):
        price *= 0.995
        rows.append((mc.MARKET_PRIMARY_INDEX, f"202607{i + 10:02d}", price, -0.5))
    rows[-1] = (mc.MARKET_PRIMARY_INDEX, rows[-1][1], rows[-1][2], -2.0)
    _write_index_rows(rows)

    ctx = compute_market_context(rows[-1][1])
    assert ctx["market_dir"] == "SHORT"
    assert ctx["market_strength"] == pytest.approx(12.0)


def test_pct_chg_alone_cannot_set_direction(temp_db):
    """_PCT_SCORE_CAP(8) < 方向阈值距离(10) 的回归测试。

    否则"跌势中的单日反弹"会被误判成 LONG。
    """
    _write_index_rows([(mc.MARKET_PRIMARY_INDEX, "20260807", 4000.0, 9.9)])
    ctx = compute_market_context("20260807")
    assert ctx["detail"]["ma5"] is None and ctx["detail"]["ma20"] is None
    assert ctx["market_strength"] == pytest.approx(58.0)
    assert ctx["market_dir"] == "NEUTRAL"


def test_falls_back_to_shanghai_index(temp_db):
    _write_index_rows([(mc.MARKET_FALLBACK_INDEX, "20260807", 3500.0, 0.8)])
    ctx = compute_market_context("20260807")
    assert ctx["detail"]["ts_code"] == mc.MARKET_FALLBACK_INDEX
    assert ctx["market_strength"] == pytest.approx(58.0)


def test_uses_latest_index_bar_when_date_missing(temp_db):
    _write_index_rows([(mc.MARKET_PRIMARY_INDEX, "20260801", 4000.0, 0.3)])
    ctx = compute_market_context("20260807")
    assert ctx["detail"]["latest_date"] == "20260801"
    assert ctx["detail"]["is_current"] is False
    assert "回退" in ctx["detail"]["reason"]


# ==================== CLI ====================


def _parse(*argv):
    from modules.cli import build_parser

    return build_parser().parse_args(list(argv))


def test_daily_run_command_is_gone():
    """每日编排器已删除，daily-run 不该还在。"""
    with pytest.raises(SystemExit):
        _parse("daily-run")


def test_cli_scan_defaults():
    args = _parse("scan")
    assert args.market_gate == "on"
    assert args.top_n == 5
    assert args.min_strength == 50.0
    assert args.limit == 0
    assert args.save is False


def test_cli_scan_flags():
    args = _parse(
        "scan", "--date", "20260807", "--market-gate", "off", "--top-n", "3",
        "--min-strength", "70", "--max-per-group", "1", "--limit", "200", "--save", "--json",
    )
    assert args.date == "20260807"
    assert args.market_gate == "off"
    assert args.top_n == 3 and args.min_strength == 70.0 and args.max_per_group == 1
    assert args.limit == 200 and args.save is True and args.json is True


def test_cli_buy_has_market_gate():
    assert _parse("buy").market_gate == "on"
    assert _parse("buy", "--market-gate", "off").market_gate == "off"


def test_cli_amv_subcommands():
    assert _parse("amv", "import", "a.csv").amv_action == "import"
    add = _parse("amv", "add", "20260810", "--close", "215000")
    assert add.amv_action == "add" and add.close == 215000.0 and add.pct is None
    assert _parse("amv", "add", "20260810", "--pct", "2.4").pct == 2.4
    assert _parse("amv", "status").segments == 8
    assert _parse("amv", "list", "--limit", "5").limit == 5
    assert _parse("amv", "verify").amv_action == "verify"


def test_cli_amv_in_handler_table():
    import inspect

    from modules import cli

    assert '"amv": cmd_amv' in inspect.getsource(cli.main)


def test_cli_scan_in_handler_table():
    import inspect

    from modules import cli

    assert '"scan": cmd_scan' in inspect.getsource(cli.main)
