"""每日编排器（modules/daily_pipeline.py）回归测试。

覆盖：
- 非交易日 → status="skipped"，不执行任何同步
- 各 skip_* 开关生效
- 评分写入 daily_scores 且重跑幂等（INSERT OR REPLACE）
- compute_market_context 在有/无 index_daily 数据两种情况下的行为
- 单步抛异常不中断整条链，错误被收集进 errors

全部用 temp_db fixture + monkeypatch，不打真实网络。
"""

from __future__ import annotations

import json

import pytest

from modules import daily_pipeline
from modules.daily_pipeline import compute_market_context, run_daily_pipeline
from modules.screener.models import StockScore


# ==================== 测试替身 ====================


class FakeSyncer:
    """DataSyncer 的测试替身：只记录调用，不触网。"""

    def __init__(self, is_open: bool | None = True):
        self._is_open = is_open
        self.calls: list[str] = []

    def is_trade_day(self, trade_date, exchange="SSE"):
        self.calls.append(f"is_trade_day:{trade_date}")
        return self._is_open

    def sync_trade_cal(self, start_date, end_date, exchange="SSE"):
        self.calls.append(f"sync_trade_cal:{start_date}~{end_date}")
        return 365

    def sync_market_daily(self, trade_date=None, refresh_stock_basic=True, check_trade_calendar=True):
        self.calls.append(f"sync_market_daily:{trade_date}")
        return {
            "status": "success",
            "trade_date": trade_date,
            "stock_basic_rows": 10,
            "market_rows": 5000,
            "db_rows_for_date": 5000,
            "price_mode": "raw",
            "message": "ok",
        }

    def sync_all_index_daily(self, ts_codes=None, start_date=None, end_date=None):
        self.calls.append("sync_all_index_daily")
        return {"000300.SH": 3, "000001.SH": 3}

    def sync_daily_kline(self, ts_code, start_date=None, end_date=None):
        self.calls.append(f"sync_daily_kline:{ts_code}")
        return 1

    def sync_indicator_cache(self, ts_code, days=120):
        self.calls.append(f"sync_indicator_cache:{ts_code}:{days}")
        return days


def _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH", "600519.SH"), scores=None, data_date="20260807"):
    """把编排器的三个外部依赖（syncer / 票池 / 评分）全部替换成可控替身。

    同时给票池各票铺好 K 线：评分落库要用真实的最新 K 线日期做主键，
    库里没有 K 线时会被判为"无数据"而跳过落库。data_date=None 表示不铺，
    用于测试"数据没同步上来"的场景。
    """
    monkeypatch.setattr(daily_pipeline, "_build_syncer", lambda: syncer)

    if data_date:
        _write_klines(codes, data_date=data_date)

    import modules.watchlist as watchlist_mod

    monkeypatch.setattr(watchlist_mod, "list_watch", lambda *a, **kw: [{"ts_code": c} for c in codes])

    score_map = scores or {}

    def _fake_analyze(ts_code, *a, **kw):
        if ts_code in score_map:
            return score_map[ts_code]
        return StockScore(
            ts_code=ts_code,
            name=f"名称{ts_code[:3]}",
            score=70.0,
            b1_score=20.0,
            trend_score=25.0,
            volume_score=15.0,
            risk_score=10.0,
            reasons=["理由A", "理由B"],
            warnings=["警告A"],
        )

    import modules.screener as screener_mod

    monkeypatch.setattr(screener_mod, "analyze_stock", _fake_analyze)


def _write_klines(codes, data_date="20260807", n=3):
    """给每只票写入 n 根 K 线，最后一根落在 data_date。

    评分步骤会取每只票的最新 K 线日期作为 daily_scores 的主键
    （screener.analyze_stock 固定用"库里最新 150 根"，拿命令行的 target 打标
    会把陈旧评分冒充成当天的），因此没有 K 线就不落库——测试必须先铺数据。
    """
    from datetime import datetime, timedelta

    from modules.database import get_connection

    end = datetime.strptime(data_date, "%Y%m%d")
    rows = []
    for code in codes:
        for i in range(n):
            d = (end - timedelta(days=n - 1 - i)).strftime("%Y%m%d")
            rows.append((code, d, 10.0, 10.5, 9.5, 10.0, 1000.0, 10000.0, 0.0))
    with get_connection() as conn:
        # 顺带登记 stock_basic：可交易性过滤用 INNER JOIN stock_basic，
        # 没有基础信息的代码会被判为"无法确认是否 ST"而整体排除。
        conn.executemany(
            "INSERT OR IGNORE INTO stock_basic (ts_code, name, industry) VALUES (?, ?, '')",
            [(c, c) for c in codes],
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO daily_kline
            (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _write_stock_basic(pairs):
    """写入 (ts_code, industry)。主线排名的行业参照系要用它。"""
    from modules.database import get_connection

    with get_connection() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO stock_basic (ts_code, name, industry) VALUES (?, ?, ?)",
            [(c, c, ind) for c, ind in pairs],
        )


def _write_index_rows(rows):
    """往 index_daily 写入 (ts_code, trade_date, close, pct_chg) 元组列表。"""
    from modules.database import get_connection

    with get_connection() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO index_daily (ts_code, trade_date, close, pct_chg) VALUES (?, ?, ?, ?)",
            rows,
        )


def _read_daily_scores(trade_date):
    from modules.database import get_connection

    with get_connection() as conn:
        return conn.execute(
            """
            SELECT ts_code, name, score, rating, market_dir, market_pct_chg,
                   market_strength, reasons, warnings
            FROM daily_scores WHERE trade_date = ? ORDER BY ts_code
            """,
            (trade_date,),
        ).fetchall()


# ==================== 非交易日 ====================


def test_non_trading_day_returns_skipped(temp_db, monkeypatch):
    """is_trade_day=False 时整条链跳过，且不调用任何同步接口"""
    syncer = FakeSyncer(is_open=False)
    _install_fake_pipeline(monkeypatch, syncer)

    result = run_daily_pipeline("20260808")

    assert result["status"] == "skipped"
    assert result["trade_date"] == "20260808"
    assert result["is_trade_day"] is False
    assert result["steps"]["trade_day_check"]["status"] == "skipped"
    # 除交易日判断外不应有任何同步调用
    assert syncer.calls == ["is_trade_day:20260808"]
    assert _read_daily_scores("20260808") == []


def test_unknown_trade_day_continues_with_warning(temp_db, monkeypatch):
    """is_trade_day=None（日历不可用）时记 warning 并继续执行"""
    syncer = FakeSyncer(is_open=None)
    _install_fake_pipeline(monkeypatch, syncer)

    result = run_daily_pipeline("20260807")

    assert result["status"] != "skipped"
    assert result["is_trade_day"] is None
    assert any("无法确认" in w for w in result["warnings"])
    assert "sync_market_daily:20260807" in syncer.calls


# ==================== skip 开关 ====================


def test_full_run_executes_all_steps(temp_db, monkeypatch):
    """默认全开时 5 个步骤都执行"""
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)

    result = run_daily_pipeline("20260807")

    assert result["status"] == "success"
    assert result["errors"] == []
    steps = result["steps"]
    assert steps["market_daily"]["status"] == "success"
    assert steps["market_daily"]["rows"] == 5000
    assert steps["index_daily"]["status"] == "success"
    assert steps["index_daily"]["rows"] == 6
    assert steps["watchlist_indicators"]["status"] == "success"
    assert steps["daily_scores"]["status"] == "success"
    assert steps["daily_scores"]["rows"] == 2
    assert result["watchlist_count"] == 2


def test_skip_market(temp_db, monkeypatch):
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)

    result = run_daily_pipeline("20260807", skip_market=True)

    assert result["steps"]["market_daily"]["status"] == "skipped"
    assert result["steps"]["market_daily"]["message"] == "--skip-market"
    assert not any(c.startswith("sync_market_daily") for c in syncer.calls)


def test_skip_index(temp_db, monkeypatch):
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)

    result = run_daily_pipeline("20260807", skip_index=True)

    assert result["steps"]["index_daily"]["status"] == "skipped"
    assert "sync_all_index_daily" not in syncer.calls


def test_skip_indicators(temp_db, monkeypatch):
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)

    result = run_daily_pipeline("20260807", skip_indicators=True)

    assert result["steps"]["watchlist_indicators"]["status"] == "skipped"
    assert not any(c.startswith("sync_indicator_cache") for c in syncer.calls)
    # 评分步骤不受影响
    assert result["steps"]["daily_scores"]["status"] == "success"


def test_skip_scores(temp_db, monkeypatch):
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)

    result = run_daily_pipeline("20260807", skip_scores=True)

    assert result["steps"]["daily_scores"]["status"] == "skipped"
    assert _read_daily_scores("20260807") == []
    # 即使跳过评分，大盘环境仍然会算（便于排障）
    assert result["market"]["market_dir"] in ("LONG", "NEUTRAL", "SHORT")


def test_watchlist_days_passed_through(temp_db, monkeypatch):
    """watchlist_days 必须原样透传给 sync_indicator_cache（双线战法需 ≥115）"""
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH",))

    run_daily_pipeline("20260807", watchlist_days=300)

    assert "sync_indicator_cache:600487.SH:300" in syncer.calls


def test_default_watchlist_days_is_250(temp_db, monkeypatch):
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH",))

    run_daily_pipeline("20260807")

    assert "sync_indicator_cache:600487.SH:250" in syncer.calls


def test_empty_watchlist_skips_stock_steps(temp_db, monkeypatch):
    """票池为空时后两步标记 skipped，而不是 failed"""
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer, codes=())

    result = run_daily_pipeline("20260807")

    assert result["watchlist_count"] == 0
    assert result["steps"]["watchlist_indicators"]["status"] == "skipped"
    assert result["steps"]["daily_scores"]["status"] == "skipped"
    assert result["status"] == "success"


# ==================== daily_scores 落库与幂等 ====================


def test_scores_written_to_daily_scores(temp_db, monkeypatch):
    syncer = FakeSyncer()
    scores = {
        "600487.SH": StockScore(
            ts_code="600487.SH",
            name="中天科技",
            score=82.0,
            b1_score=30.0,
            trend_score=25.0,
            volume_score=17.0,
            risk_score=10.0,
            reasons=["B1买点", "缩量"],
            warnings=[],
        )
    }
    _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH",), scores=scores)

    run_daily_pipeline("20260807")

    rows = _read_daily_scores("20260807")
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "600487.SH"
    assert row[1] == "中天科技"
    assert row[2] == pytest.approx(82.0)
    # rating 是 property，必须被落库
    assert "强烈推荐" in row[3]
    assert row[4] in ("LONG", "NEUTRAL", "SHORT")
    # reasons / warnings 存 JSON 数组字符串
    assert json.loads(row[7]) == ["B1买点", "缩量"]
    assert json.loads(row[8]) == []


def test_rerun_is_idempotent(temp_db, monkeypatch):
    """同一交易日重跑走 INSERT OR REPLACE：行数不翻倍，内容被覆盖"""
    syncer = FakeSyncer()
    first = {"600487.SH": StockScore(ts_code="600487.SH", name="中天科技", score=40.0)}
    _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH",), scores=first)
    run_daily_pipeline("20260807")

    rows = _read_daily_scores("20260807")
    assert len(rows) == 1
    assert rows[0][2] == pytest.approx(40.0)

    # 第二次跑，评分变了
    second = {"600487.SH": StockScore(ts_code="600487.SH", name="中天科技", score=88.0)}
    _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH",), scores=second)
    run_daily_pipeline("20260807")

    rows = _read_daily_scores("20260807")
    assert len(rows) == 1, "重跑不应产生重复行"
    assert rows[0][2] == pytest.approx(88.0), "重跑应覆盖旧评分"


def test_top_scores_sorted_desc(temp_db, monkeypatch):
    syncer = FakeSyncer()
    scores = {
        "600001.SH": StockScore(ts_code="600001.SH", score=30.0),
        "600002.SH": StockScore(ts_code="600002.SH", score=90.0),
        "600003.SH": StockScore(ts_code="600003.SH", score=60.0),
    }
    _install_fake_pipeline(monkeypatch, syncer, codes=tuple(scores), scores=scores)

    result = run_daily_pipeline("20260807")

    assert [t["ts_code"] for t in result["top_scores"]] == ["600002.SH", "600003.SH", "600001.SH"]


# ==================== compute_market_context ====================


def test_market_context_no_data_returns_neutral(temp_db):
    """index_daily 全空时降级为 NEUTRAL / 50"""
    ctx = compute_market_context("20260807")

    assert ctx["market_dir"] == "NEUTRAL"
    assert ctx["market_pct_chg"] == 0.0
    assert ctx["market_strength"] == 50.0
    assert "index_daily" in ctx["detail"]["reason"]


def test_market_context_bullish(temp_db):
    """收盘价站上 MA5/MA20 且当日上涨 → LONG"""
    # 20 天单调上行，最后一天收盘远高于两条均线
    rows = []
    price = 3000.0
    for i in range(20):
        price *= 1.005
        rows.append((daily_pipeline.MARKET_PRIMARY_INDEX, f"202607{i + 10:02d}", price, 0.5))
    # 最后一天涨 1.5%
    rows[-1] = (daily_pipeline.MARKET_PRIMARY_INDEX, rows[-1][1], rows[-1][2], 1.5)
    _write_index_rows(rows)

    ctx = compute_market_context(rows[-1][1])

    assert ctx["market_dir"] == "LONG"
    assert ctx["market_pct_chg"] == pytest.approx(1.5)
    # 50 + min(1.5*10, 8) + 15 + 15 = 88
    # 涨跌幅项封顶 8 分（< 方向阈值所需的 10 分），保证方向必须由均线决定
    assert ctx["market_strength"] == pytest.approx(88.0)
    assert ctx["detail"]["ts_code"] == daily_pipeline.MARKET_PRIMARY_INDEX
    assert ctx["detail"]["is_current"] is True
    assert ctx["detail"]["bars"] == 20


def test_market_context_bearish(temp_db):
    """收盘价跌破 MA5/MA20 且当日下跌 → SHORT"""
    rows = []
    price = 4000.0
    for i in range(20):
        price *= 0.995
        rows.append((daily_pipeline.MARKET_PRIMARY_INDEX, f"202607{i + 10:02d}", price, -0.5))
    rows[-1] = (daily_pipeline.MARKET_PRIMARY_INDEX, rows[-1][1], rows[-1][2], -2.0)
    _write_index_rows(rows)

    ctx = compute_market_context(rows[-1][1])

    assert ctx["market_dir"] == "SHORT"
    # 50 - min(2.0*10, 8) - 15 - 15 = 12
    assert ctx["market_strength"] == pytest.approx(12.0)


def test_market_context_pct_chg_alone_cannot_set_direction(temp_db):
    """涨跌幅单项不足以定方向：只有 1 根 bar（无 MA）时必须保持 NEUTRAL

    这是 _PCT_SCORE_CAP(8) < 方向阈值距离(10) 这条不变量的回归测试——
    否则"跌势中的单日反弹"会被误判成 LONG。
    """
    _write_index_rows([(daily_pipeline.MARKET_PRIMARY_INDEX, "20260807", 4000.0, 9.9)])

    ctx = compute_market_context("20260807")

    assert ctx["detail"]["ma5"] is None
    assert ctx["detail"]["ma20"] is None
    assert ctx["market_strength"] == pytest.approx(58.0)  # 50 + 8(封顶)
    assert ctx["market_dir"] == "NEUTRAL"


def test_market_context_falls_back_to_shanghai_index(temp_db):
    """沪深300 无数据时降级到上证指数"""
    _write_index_rows([(daily_pipeline.MARKET_FALLBACK_INDEX, "20260807", 3500.0, 0.8)])

    ctx = compute_market_context("20260807")

    assert ctx["detail"]["ts_code"] == daily_pipeline.MARKET_FALLBACK_INDEX
    # 只有 1 根 K 线：MA5/MA20 均为 None，只有涨跌幅贡献 50 + 8 = 58
    assert ctx["detail"]["ma5"] is None
    assert ctx["detail"]["ma20"] is None
    assert ctx["market_strength"] == pytest.approx(58.0)
    assert ctx["market_dir"] == "NEUTRAL"


def test_market_context_uses_latest_available_when_date_missing(temp_db):
    """目标日无指数数据时回退到最近一根，并在 detail 里说明"""
    _write_index_rows([(daily_pipeline.MARKET_PRIMARY_INDEX, "20260801", 4000.0, 0.3)])

    ctx = compute_market_context("20260807")

    assert ctx["detail"]["latest_date"] == "20260801"
    assert ctx["detail"]["is_current"] is False
    assert "回退" in ctx["detail"]["reason"]


def test_market_context_written_into_daily_scores(temp_db, monkeypatch):
    """大盘三列必须真正落进 daily_scores（且不是默认的 NEUTRAL/50）"""
    # 20 根上行 bar，收盘站上 MA5/MA20，末根涨 2%
    rows = []
    price = 3000.0
    for i in range(19):
        price *= 1.005
        rows.append((daily_pipeline.MARKET_PRIMARY_INDEX, f"202607{i + 10:02d}", price, 0.5))
    rows.append((daily_pipeline.MARKET_PRIMARY_INDEX, "20260807", price * 1.02, 2.0))
    _write_index_rows(rows)
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH",))

    run_daily_pipeline("20260807")

    row = _read_daily_scores("20260807")[0]
    assert row[4] == "LONG"  # market_dir
    assert row[5] == pytest.approx(2.0)  # market_pct_chg
    assert row[6] == pytest.approx(88.0)  # market_strength = 50 + 8(封顶) + 15 + 15


# ==================== 单步失败不中断整条链 ====================


def test_market_sync_exception_does_not_break_chain(temp_db, monkeypatch):
    """全市场日线抛异常，后续指数/指标/评分照常跑完"""
    syncer = FakeSyncer()

    def _boom(*a, **kw):
        raise RuntimeError("Tushare 限流")

    monkeypatch.setattr(syncer, "sync_market_daily", _boom)
    _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH",))

    result = run_daily_pipeline("20260807")

    assert result["status"] == "partial"
    assert result["steps"]["market_daily"]["status"] == "failed"
    assert "Tushare 限流" in result["steps"]["market_daily"]["message"]
    assert any("Tushare 限流" in e for e in result["errors"])
    # 后续步骤仍然成功
    assert result["steps"]["index_daily"]["status"] == "success"
    assert result["steps"]["daily_scores"]["status"] == "success"
    assert len(_read_daily_scores("20260807")) == 1


def test_index_sync_exception_collected(temp_db, monkeypatch):
    syncer = FakeSyncer()
    monkeypatch.setattr(syncer, "sync_all_index_daily", lambda *a, **kw: (_ for _ in ()).throw(ValueError("网络错误")))
    _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH",))

    result = run_daily_pipeline("20260807")

    assert result["steps"]["index_daily"]["status"] == "failed"
    assert any("网络错误" in e for e in result["errors"])
    assert result["steps"]["daily_scores"]["status"] == "success"


def test_index_all_zero_with_empty_table_is_failed(temp_db, monkeypatch):
    """7 个指数全返回 0 且 index_daily 为空 → 步骤报 failed，但只记 warning 不记 error。

    实测该中转数据源的 index_daily 接口长期不可用（7 个指数在 65 秒间隔下全部返回空），
    这一步几乎天天失败。计入 errors 会让整条链天天 partial，把真正的故障淹没在噪音里；
    而大盘环境的首选数据源已是全市场宽度，不依赖指数，所以降级为 warning。
    """
    syncer = FakeSyncer()
    monkeypatch.setattr(syncer, "sync_all_index_daily", lambda *a, **kw: dict.fromkeys(range(7), 0))
    _install_fake_pipeline(monkeypatch, syncer, codes=())

    result = run_daily_pipeline("20260807")

    step = result["steps"]["index_daily"]
    assert step["status"] == "failed"
    assert step["covered_indexes"] == 0
    assert "全部同步失败" in step["message"]
    # 进 warnings 而不是 errors，整条链不因此降级
    assert any("全部同步失败" in w for w in result["warnings"])
    assert not any("全部同步失败" in e for e in result["errors"])
    assert result["status"] == "success"


def test_index_all_zero_but_table_populated_is_success(temp_db, monkeypatch):
    """本地已是最新（返回 0 但库里有数据）→ 正常 success，不误报"""
    _write_index_rows([(daily_pipeline.MARKET_PRIMARY_INDEX, "20260807", 4000.0, 0.5)])
    syncer = FakeSyncer()
    monkeypatch.setattr(syncer, "sync_all_index_daily", lambda *a, **kw: dict.fromkeys(range(7), 0))
    _install_fake_pipeline(monkeypatch, syncer, codes=())

    result = run_daily_pipeline("20260807")

    step = result["steps"]["index_daily"]
    assert step["status"] == "success"
    assert step["covered_indexes"] == 1
    assert step["latest_index_date"] == "20260807"
    assert result["status"] == "success"


def test_single_stock_scoring_failure_does_not_block_others(temp_db, monkeypatch):
    """票池里某一只评分抛异常，其余仍然落库"""
    syncer = FakeSyncer()
    monkeypatch.setattr(daily_pipeline, "_build_syncer", lambda: syncer)

    import modules.watchlist as watchlist_mod

    monkeypatch.setattr(
        watchlist_mod, "list_watch", lambda *a, **kw: [{"ts_code": "600001.SH"}, {"ts_code": "600002.SH"}]
    )
    # 两只票都要有 K 线：评分落库按真实数据日打标，没 K 线会先一步被判为"无数据"
    _write_klines(("600001.SH", "600002.SH"), data_date="20260807")

    def _analyze(ts_code, *a, **kw):
        if ts_code == "600001.SH":
            raise RuntimeError("K线缺失")
        return StockScore(ts_code=ts_code, name="正常票", score=55.0)

    import modules.screener as screener_mod

    monkeypatch.setattr(screener_mod, "analyze_stock", _analyze)

    result = run_daily_pipeline("20260807")

    assert result["status"] == "partial"
    assert any("K线缺失" in e for e in result["errors"])
    rows = _read_daily_scores("20260807")
    assert len(rows) == 1
    assert rows[0][0] == "600002.SH"


def test_indicator_failure_for_one_stock_is_collected(temp_db, monkeypatch):
    syncer = FakeSyncer()

    def _boom(ts_code, days=120):
        if ts_code == "600001.SH":
            raise RuntimeError("指标计算崩了")
        return days

    monkeypatch.setattr(syncer, "sync_indicator_cache", _boom)
    _install_fake_pipeline(monkeypatch, syncer, codes=("600001.SH", "600002.SH"))

    result = run_daily_pipeline("20260807")

    assert result["steps"]["watchlist_indicators"]["status"] == "success"
    assert any("指标计算崩了" in e for e in result["errors"])
    assert result["status"] == "partial"


def test_syncer_construction_failure_returns_failed(temp_db, monkeypatch):
    """连 DataSyncer 都构造不出来（如 token 缺失）时直接 failed，不抛异常"""

    def _boom():
        raise RuntimeError("TUSHARE_TOKEN 未配置")

    monkeypatch.setattr(daily_pipeline, "_build_syncer", _boom)

    result = run_daily_pipeline("20260807")

    assert result["status"] == "failed"
    assert any("TUSHARE_TOKEN" in e for e in result["errors"])


def test_trade_cal_skipped_when_year_already_cached(temp_db, monkeypatch):
    """整年日历已缓存时跳过 sync_trade_cal（trade_cal 接口 1 次/分钟限流）"""
    from datetime import datetime, timedelta

    from modules.database import get_connection

    # 铺满 2026 全年
    rows = []
    day = datetime(2026, 1, 1)
    while day.year == 2026:
        rows.append(("SSE", day.strftime("%Y%m%d"), 1, None))
        day += timedelta(days=1)
    with get_connection() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO trade_cal (exchange, cal_date, is_open, pretrade_date) VALUES (?, ?, ?, ?)",
            rows,
        )

    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer, codes=())

    result = run_daily_pipeline("20260807")

    assert result["steps"]["trade_cal"]["status"] == "skipped"
    assert not any(c.startswith("sync_trade_cal") for c in syncer.calls)


def test_trade_cal_synced_when_missing(temp_db, monkeypatch):
    """日历未缓存时按整年拉取一次"""
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer, codes=())

    result = run_daily_pipeline("20260807")

    assert result["steps"]["trade_cal"]["status"] == "success"
    assert "sync_trade_cal:20260101~20261231" in syncer.calls


def test_trade_cal_not_retried_when_is_trade_day_already_failed(temp_db, monkeypatch):
    """is_trade_day 返回 None 说明它内部已拉过整年日历并失败。

    trade_cal 限流 1 次/分钟，同一分钟内重试必然再失败，本步必须跳过而不是重打接口。
    """
    syncer = FakeSyncer(is_open=None)
    _install_fake_pipeline(monkeypatch, syncer, codes=())

    result = run_daily_pipeline("20260807")

    step = result["steps"]["trade_cal"]
    assert step["status"] == "skipped"
    assert "不重复调用" in step["message"]
    assert not any(c.startswith("sync_trade_cal") for c in syncer.calls)
    # 只有交易日历不可用的 warning，不应额外制造一条 error
    assert not any("交易日历同步失败" in e for e in result["errors"])


# ==================== 摘要渲染 ====================


def test_format_pipeline_summary_does_not_crash(temp_db, monkeypatch):
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)

    result = run_daily_pipeline("20260807")
    text = daily_pipeline.format_pipeline_summary(result)

    assert "每日流水线 20260807" in text
    assert "market_daily" in text
    assert "【大盘环境】" in text


def test_format_pipeline_summary_on_skipped(temp_db, monkeypatch):
    syncer = FakeSyncer(is_open=False)
    _install_fake_pipeline(monkeypatch, syncer)

    result = run_daily_pipeline("20260808")
    text = daily_pipeline.format_pipeline_summary(result)

    assert "status=skipped" in text


# ==================== CLI 接线（argparse 注册，不触网）====================


def _parse(*argv):
    from modules.cli import build_parser

    return build_parser().parse_args(list(argv))


def test_cli_registers_daily_run():
    args = _parse("daily-run")
    assert args.command == "daily-run"
    assert args.date is None
    assert args.watchlist_days == 250
    assert args.skip_market is False
    assert args.skip_index is False
    assert args.skip_indicators is False
    assert args.skip_scores is False


def test_cli_daily_run_flags():
    args = _parse(
        "daily-run",
        "--date",
        "20260807",
        "--json",
        "--skip-market",
        "--skip-index",
        "--skip-indicators",
        "--skip-scores",
        "--watchlist-days",
        "300",
    )
    assert args.date == "20260807"
    assert args.json is True
    assert args.skip_market and args.skip_index and args.skip_indicators and args.skip_scores
    assert args.watchlist_days == 300


def test_cli_daily_run_in_handler_table():
    """daily-run 必须在 main() 的调度表里，否则会 KeyError"""
    import inspect

    from modules import cli

    assert '"daily-run": cmd_daily_run' in inspect.getsource(cli.main)


def test_cli_sync_trade_cal_action():
    args = _parse("sync", "trade-cal", "--start", "20260101", "--end", "20261231", "--exchange", "SZSE", "--json")
    assert args.sync_action == "trade-cal"
    assert args.start == "20260101"
    assert args.end == "20261231"
    assert args.exchange == "SZSE"
    assert args.json is True


def test_cli_sync_trade_cal_defaults():
    args = _parse("sync", "trade-cal")
    assert args.exchange == "SSE"
    assert args.start is None and args.end is None


def test_cli_sync_index_action():
    args = _parse("sync", "index", "--ts-code", "000300.SH", "--start", "20260101", "--json")
    assert args.sync_action == "index"
    assert args.ts_code == "000300.SH"
    assert args.start == "20260101"
    assert args.json is True


def test_cli_sync_index_defaults_to_all():
    args = _parse("sync", "index")
    assert args.ts_code is None


# ==================== 阶段1：主线强度 + 买点确认（步骤 7 / 8）====================


def test_theme_and_buy_steps_run_by_default(monkeypatch, temp_db):
    """默认应跑满 8 步，主线排名与买点确认都要有结果。"""
    syncer = FakeSyncer()
    codes = ("600487.SH", "600519.SH")
    _install_fake_pipeline(monkeypatch, syncer, codes=codes)
    _write_stock_basic([(c, "半导体") for c in codes])

    result = run_daily_pipeline("20260807")

    assert result["steps"]["theme_strength"]["status"] in ("success", "failed")
    assert result["steps"]["buy_decisions"]["status"] == "success"
    assert len(result["buy_decisions"]) == len(codes)
    assert set(result["buy_decisions"][0]) >= {"ts_code", "action", "score", "vetoes"}


def test_skip_themes_and_skip_buy(monkeypatch, temp_db):
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)
    result = run_daily_pipeline("20260807", skip_themes=True, skip_buy=True)

    assert result["steps"]["theme_strength"]["status"] == "skipped"
    assert result["steps"]["buy_decisions"]["status"] == "skipped"
    assert result["buy_decisions"] == []


def test_theme_step_warns_when_no_theme_imported(monkeypatch, temp_db):
    """一条主线都没导入时要明确提示会退回行业兜底，而不是静默降级。"""
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)
    result = run_daily_pipeline("20260807", skip_buy=True)
    assert any("尚未导入任何主线成员" in w for w in result["warnings"])


def test_theme_step_ranks_imported_themes(monkeypatch, temp_db):
    from modules import themes as th

    syncer = FakeSyncer()
    codes = tuple(f"6000{i:02d}.SH" for i in range(6))
    _install_fake_pipeline(monkeypatch, syncer, codes=codes)
    # rank_themes 的窗口要多几根 K 线才有意义
    _write_klines(codes, data_date="20260807", n=6)
    th.import_members([{"theme": "算力硬件", "ts_code": c} for c in codes])

    result = run_daily_pipeline("20260807", skip_buy=True)

    ranking = result["theme_ranking"]
    assert ranking["lookback"] == 5
    assert [t["theme"] for t in ranking["themes"]] == ["算力硬件"]
    assert result["steps"]["theme_strength"]["rows"] >= 1


def test_theme_lookback_is_threaded_through(monkeypatch, temp_db):
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)
    result = run_daily_pipeline("20260807", skip_buy=True, theme_lookback=3)
    assert result["theme_ranking"]["lookback"] == 3
    assert len(result["theme_ranking"]["window"]) <= 3


def test_buy_decisions_are_persisted(monkeypatch, temp_db):
    from modules.database import get_connection

    syncer = FakeSyncer()
    codes = ("600487.SH", "600519.SH")
    _install_fake_pipeline(monkeypatch, syncer, codes=codes)

    run_daily_pipeline("20260807")
    with get_connection() as conn:
        rows = conn.execute("SELECT ts_code, trade_date, action FROM buy_decisions").fetchall()
    assert {r[0] for r in rows} == set(codes)
    assert all(r[1] == "20260807" for r in rows)


def test_buy_decisions_rerun_is_idempotent(monkeypatch, temp_db):
    from modules.database import get_connection

    syncer = FakeSyncer()
    codes = ("600487.SH",)
    _install_fake_pipeline(monkeypatch, syncer, codes=codes)

    run_daily_pipeline("20260807")
    run_daily_pipeline("20260807")
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM buy_decisions").fetchone()[0]
    assert count == 1, "同一天重跑必须覆盖而不是新增"


def test_buy_step_failure_does_not_break_chain(monkeypatch, temp_db):
    """买点确认整体炸掉时，前面步骤的结果必须保留，整条链降级为 partial。"""
    import modules.buy_decision as bd_mod

    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)
    monkeypatch.setattr(
        bd_mod, "confirm_buy_batch", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("模拟买点确认崩溃"))
    )

    result = run_daily_pipeline("20260807")

    assert result["steps"]["buy_decisions"]["status"] == "failed"
    assert result["steps"]["daily_scores"]["status"] == "success"
    assert result["status"] == "partial"
    assert any("买点确认异常" in e for e in result["errors"])


def test_theme_step_failure_does_not_break_buy_step(monkeypatch, temp_db):
    """主线排名炸掉时买点确认仍要跑完——主线只是加减分项，不是前置条件。"""
    import modules.themes as th_mod

    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)
    monkeypatch.setattr(
        th_mod, "rank_themes", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("模拟主线排名崩溃"))
    )

    result = run_daily_pipeline("20260807")

    assert result["steps"]["theme_strength"]["status"] == "failed"
    assert result["steps"]["buy_decisions"]["status"] == "success"


def test_buy_step_reports_data_date_drift(monkeypatch, temp_db):
    """买点确认基于库里的真实数据日；与 target 不一致时必须报出来。"""
    syncer = FakeSyncer()
    codes = ("600487.SH",)
    _install_fake_pipeline(monkeypatch, syncer, codes=codes, data_date="20260805")

    result = run_daily_pipeline("20260807")

    assert any("买点确认基于其最新数据日" in w for w in result["warnings"])


def test_summary_renders_theme_and_buy_sections(monkeypatch, temp_db):
    from modules.daily_pipeline import format_pipeline_summary

    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer)
    text = format_pipeline_summary(run_daily_pipeline("20260807"))
    assert "主线强度" in text
    assert "买点确认" in text


# ==================== 阶段1 CLI ====================


def test_cli_daily_run_new_skip_flags():
    args = _parse("daily-run", "--skip-themes", "--skip-buy", "--theme-lookback", "10")
    assert args.skip_themes and args.skip_buy
    assert args.theme_lookback == 10


def test_cli_daily_run_theme_lookback_default():
    assert _parse("daily-run").theme_lookback == 5


def test_cli_theme_subcommands():
    assert _parse("theme", "add", "商业航天", "--description", "x").theme_action == "add"
    assert _parse("theme", "list", "--active-only").active_only is True
    assert _parse("theme", "import", "a.json", "--replace").replace is True
    assert _parse("theme", "import", "a.json").source == "kimi-swarm"
    rank = _parse("theme", "rank", "--lookback", "3", "--dry-run")
    assert rank.lookback == 3 and rank.dry_run is True


def test_cli_buy_command():
    args = _parse("buy", "600487.SH,600519.SH", "--date", "20260807", "--save", "--json")
    assert args.codes == "600487.SH,600519.SH"
    assert args.date == "20260807"
    assert args.save is True and args.json is True


def test_cli_buy_codes_optional():
    """省略代码时走票池，argparse 不能因此报错。"""
    assert _parse("buy").codes is None


def test_cli_theme_and_buy_in_handler_table():
    import inspect

    from modules import cli

    src = inspect.getsource(cli.main)
    assert '"theme": cmd_theme' in src
    assert '"buy": cmd_buy' in src


def test_pipeline_warns_about_dropped_themes(monkeypatch, temp_db):
    """成员不足的主线被跳过时，编排器要把它报进 warnings。"""
    from modules import themes as th

    syncer = FakeSyncer()
    codes = ("600487.SH", "600519.SH")
    _install_fake_pipeline(monkeypatch, syncer, codes=codes)
    _write_klines(codes, data_date="20260807", n=6)
    th.import_members([{"theme": "太小的主线", "ts_code": codes[0]}])

    result = run_daily_pipeline("20260807", skip_buy=True)
    assert any("主线未参与排名" in w and "太小的主线" in w for w in result["warnings"])


# ==================== 阶段1 第二阶段：主线/行业筛选 ====================


def _fake_buy(monkeypatch, decisions):
    """替换第一阶段，直接给定决策，专测编排器里的第二阶段接线。"""
    import modules.buy_decision as bd_mod

    monkeypatch.setattr(bd_mod, "confirm_buy_batch", lambda *a, **kw: list(decisions))


def _decision(code, score, group, strength, kind="theme", action="BUY"):
    from modules.buy_decision import BuyDecision

    return BuyDecision(
        ts_code=code,
        trade_date="20260807",
        name=code,
        action=action,
        score=score,
        base_strategy="B1",
        theme={"theme": group, "kind": kind, "strength": strength, "rank": 1},
    )


def test_pipeline_runs_second_stage_and_persists_pick_rank(monkeypatch, temp_db):
    from modules.database import get_connection

    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH", "600519.SH"))
    _fake_buy(
        monkeypatch,
        [
            _decision("600487.SH", 70.0, "强主线", 90.0),
            _decision("600519.SH", 95.0, "弱主线", 20.0),
        ],
    )

    result = run_daily_pipeline("20260807")

    # 强主线里的低分票入选，弱主线里的高分票落选
    assert [p["ts_code"] for p in result["final_picks"]] == ["600487.SH"]
    with get_connection() as conn:
        ranks = dict(conn.execute("SELECT ts_code, pick_rank FROM buy_decisions").fetchall())
    assert ranks["600487.SH"] == 1
    assert ranks["600519.SH"] == 0


def test_pipeline_warns_about_buy_that_missed_the_cut(monkeypatch, temp_db):
    """买点成立却被板块判断刷掉的票必须报出来，不能看起来像"根本没通过买点确认"。"""
    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH",))
    _fake_buy(monkeypatch, [_decision("600487.SH", 95.0, "弱主线", 10.0)])

    result = run_daily_pipeline("20260807")

    assert result["final_picks"] == []
    assert any("买点成立" in w and "未入选" in w for w in result["warnings"])


def test_pipeline_pick_params_are_threaded_through(monkeypatch, temp_db):
    syncer = FakeSyncer()
    codes = tuple(f"6000{i:02d}.SH" for i in range(4))
    _install_fake_pipeline(monkeypatch, syncer, codes=codes)
    _fake_buy(monkeypatch, [_decision(c, 80.0, "同一条主线", 90.0) for c in codes])

    capped = run_daily_pipeline("20260807", pick_top_n=2)
    assert len(capped["final_picks"]) == 2

    diversified = run_daily_pipeline("20260807", pick_max_per_group=1)
    assert len(diversified["final_picks"]) == 1

    strict = run_daily_pipeline("20260807", pick_min_group_strength=95.0)
    assert strict["final_picks"] == []


def test_summary_renders_final_picks(monkeypatch, temp_db):
    from modules.daily_pipeline import format_pipeline_summary

    syncer = FakeSyncer()
    _install_fake_pipeline(monkeypatch, syncer, codes=("600487.SH",))
    _fake_buy(monkeypatch, [_decision("600487.SH", 80.0, "商业航天", 90.0)])

    text = format_pipeline_summary(run_daily_pipeline("20260807"))
    assert "最终选股" in text
    assert "商业航天" in text


def test_cli_daily_run_pick_flags():
    args = _parse("daily-run", "--pick-top-n", "3", "--pick-min-strength", "60", "--pick-max-per-group", "1")
    assert args.pick_top_n == 3
    assert args.pick_min_strength == 60.0
    assert args.pick_max_per_group == 1


def test_cli_daily_run_pick_defaults():
    args = _parse("daily-run")
    assert args.pick_top_n == 5
    assert args.pick_min_strength == 50.0
    assert args.pick_max_per_group is None


def test_cli_buy_pick_flags():
    args = _parse("buy", "--top-n", "3", "--min-strength", "70", "--max-per-group", "2", "--include-watch")
    assert args.top_n == 3
    assert args.min_strength == 70.0
    assert args.max_per_group == 2
    assert args.include_watch is True
