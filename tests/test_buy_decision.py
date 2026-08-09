"""买点确认引擎（modules/buy_decision.py）测试。

分两层测：
- **各评分层单测**：MACD / 成交量 / 大盘 / 主线 / 共振，直接喂结构化输入，
  不依赖能触发特定形态的 K 线数据。
- **主流程集成**：用 monkeypatch 替换否决层与触发层，验证五层的串联顺序、
  短路行为、落库与幂等。

不打网络、不读生产库。
"""

from __future__ import annotations

import json

import pytest

from modules import buy_decision as bd
from modules.buy_decision import BuyDecision, confirm_buy, save_buy_decisions
from modules.indicators import DailyData


NEUTRAL_MARKET = {"market_dir": "NEUTRAL", "market_pct_chg": 0.0, "market_strength": 50.0}


def _make_klines(n=60, start="20260601", close=10.0):
    """造 n 根平淡的 K 线，只用于让流程走通（形态判定由替身接管）。"""
    from datetime import datetime, timedelta

    base = datetime.strptime(start, "%Y%m%d")
    out = []
    for i in range(n):
        out.append(
            DailyData(
                ts_code="600000.SH",
                trade_date=(base + timedelta(days=i)).strftime("%Y%m%d"),
                open=close,
                high=close * 1.02,
                low=close * 0.98,
                close=close,
                vol=1000.0,
                amount=close * 1000.0,
                pct_chg=0.0,
                prev_close=close,
            )
        )
    return out


def _trigger(strategy="B1", confidence=0.6, trade_date="20260730", bars_ago=0):
    return {
        "strategy": strategy,
        "trade_date": trade_date,
        "confidence": confidence,
        "description": f"{strategy}测试信号",
        "bars_ago": bars_ago,
        "stop_loss": 9.5,
    }


def _install(monkeypatch, *, vetoes=(), triggers=(), macd_sig=None):
    """替换否决层与触发层，让主流程可控。"""
    detail = {"macd": {}, "_macd_sig": macd_sig or {}}
    monkeypatch.setattr(bd, "_collect_vetoes", lambda k, i: (list(vetoes), dict(detail)))
    monkeypatch.setattr(bd, "_collect_triggers", lambda k, i: list(triggers))
    monkeypatch.setattr(bd, "_lookup_name", lambda c: "测试股")


# ==================== 各评分层单测 ====================


def test_score_macd_rewards_bottom_divergence():
    delta, notes = bd._score_macd({"is_bottom_divergence": True})
    assert delta == bd._MACD_BOTTOM_DIVERGENCE
    assert "底背离" in notes[0]


def test_score_macd_punishes_top_divergence_hardest():
    """顶背离是最重的负项——此时买入等于接盘。"""
    top, _ = bd._score_macd({"is_top_divergence": True})
    dead, _ = bd._score_macd({"macd_dead_cross": True})
    assert top < dead < 0


def test_score_macd_gold_trap_costs_more_than_gold_cross_gains():
    """金叉空在语料里是"最恶毒的诱多"，扣分必须大于金叉的加分。"""
    trap, _ = bd._score_macd({"is_gold_fake": True})
    gold, _ = bd._score_macd({"macd_gold_cross": True})
    assert abs(trap) > gold


def test_score_macd_accumulates_multiple_signals():
    delta, notes = bd._score_macd({"macd_gold_cross": True, "is_dif_positive": True})
    assert delta == bd._MACD_GOLD_CROSS + bd._MACD_DIF_POSITIVE
    assert len(notes) == 2


def test_score_macd_empty_is_neutral():
    assert bd._score_macd({}) == (0.0, [])


def test_score_market_penalizes_short_more_than_it_rewards_long():
    """不对称是有意的：宁可错过，不可在跌势里加仓。"""
    long_delta, _ = bd._score_market({"market_dir": "LONG", "market_strength": 50})
    short_delta, _ = bd._score_market({"market_dir": "SHORT", "market_strength": 50})
    assert long_delta > 0 > short_delta
    assert abs(short_delta) > long_delta


def test_score_market_strength_tilt_is_capped():
    hi, _ = bd._score_market({"market_dir": "NEUTRAL", "market_strength": 100})
    lo, _ = bd._score_market({"market_dir": "NEUTRAL", "market_strength": 0})
    assert hi == pytest.approx(bd._MARKET_STRENGTH_CAP)
    assert lo == pytest.approx(-bd._MARKET_STRENGTH_CAP)


def test_score_market_ignores_tiny_tilt():
    """强度只偏离中性一点点时不写噪音进 confirms。"""
    delta, notes = bd._score_market({"market_dir": "NEUTRAL", "market_strength": 52})
    assert delta == 0.0
    assert notes == []


def test_score_theme_scales_with_strength():
    strong, _ = bd._score_theme({"theme": "A", "kind": "theme", "strength": 100, "rank": 1, "total": 10})
    weak, _ = bd._score_theme({"theme": "B", "kind": "theme", "strength": 0, "rank": 10, "total": 10})
    mid, _ = bd._score_theme({"theme": "C", "kind": "theme", "strength": 50, "rank": 5, "total": 10})
    assert strong == pytest.approx(bd._THEME_WEIGHT)
    assert weak == pytest.approx(-bd._THEME_WEIGHT)
    assert mid == pytest.approx(0.0)


def test_score_theme_industry_fallback_weighs_less():
    """行业只是兜底参照，不是真正的炒作主线，权重必须小于主线。"""
    theme, _ = bd._score_theme({"theme": "A", "kind": "theme", "strength": 100, "rank": 1, "total": 5})
    industry, notes = bd._score_theme({"theme": "A", "kind": "industry", "strength": 100, "rank": 1, "total": 5})
    assert industry < theme
    assert "行业(兜底)" in notes[0]


def test_score_theme_none_is_neutral():
    assert bd._score_theme(None) == (0.0, [])


def test_score_volume_rewards_attack(monkeypatch):
    monkeypatch.setattr(
        bd_indicators := __import__("modules.indicators", fromlist=["x"]),
        "detect_volume_attack",
        lambda k: {"is_attack": True, "desc": "量比攻击：量比4.0涨幅5.0%，强势信号"},
    )
    monkeypatch.setattr(bd_indicators, "detect_double_gun", lambda k: {"is_double_gun": False})
    klines = _make_klines(30)
    delta, notes, _ = bd._score_volume(klines, 29, [])
    assert delta == bd._VOL_ATTACK
    assert "量比攻击" in notes[0]


def test_score_volume_b1_suoliang_bonus_only_for_b1(monkeypatch):
    ind = __import__("modules.indicators", fromlist=["x"])
    monkeypatch.setattr(ind, "detect_volume_attack", lambda k: {"is_attack": False})
    monkeypatch.setattr(ind, "detect_double_gun", lambda k: {"is_double_gun": False})

    klines = _make_klines(30)
    klines[29].is_suoliang = True

    with_b1, notes, _ = bd._score_volume(klines, 29, [_trigger("B1")])
    without, _, _ = bd._score_volume(klines, 29, [_trigger("平行重炮")])
    assert with_b1 == bd._VOL_B1_SUOLIANG
    assert without == 0.0
    assert "缩量" in notes[0]


def test_score_volume_penalizes_fangliang_yinxian(monkeypatch):
    ind = __import__("modules.indicators", fromlist=["x"])
    monkeypatch.setattr(ind, "detect_volume_attack", lambda k: {"is_attack": False})
    monkeypatch.setattr(ind, "detect_double_gun", lambda k: {"is_double_gun": False})

    klines = _make_klines(30)
    klines[29].is_fangliang_yinxian = True
    delta, _, _ = bd._score_volume(klines, 29, [])
    assert delta == bd._VOL_FANGLIANG_YIN


def test_score_volume_does_not_use_volume_ratio_strategy(monkeypatch):
    """量比战法 6 场景矩阵是分钟级口径，实测日线下买入侧 0 触发，必须不被引用。"""
    ind = __import__("modules.indicators", fromlist=["x"])
    called = []
    monkeypatch.setattr(ind, "detect_volume_attack", lambda k: {"is_attack": False})
    monkeypatch.setattr(ind, "detect_double_gun", lambda k: {"is_double_gun": False})
    monkeypatch.setattr(
        ind, "detect_volume_ratio_strategy", lambda k: called.append(1) or {"scenario": "攻击日"}
    )
    bd._score_volume(_make_klines(30), 29, [])
    assert called == [], "买点确认不应调用量比战法 6 场景矩阵"


# ==================== 主流程 ====================


def test_veto_short_circuits_before_scoring(monkeypatch, temp_db):
    """一票否决优先于一切加分：即便有高置信度买点信号也不买。"""
    _install(monkeypatch, vetoes=["MACD一票否决（DIF=-1.0<0 且无底背离）"], triggers=[_trigger("SB1", 0.9)])
    d = confirm_buy("600000.SH", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert d.action == "NONE"
    assert d.score == 0.0
    assert d.detail["stopped_at"] == "veto"
    # 触发的战法仍要记录下来，供复盘时看"当时否决掉的是什么"
    assert d.triggers[0]["strategy"] == "SB1"


def test_no_trigger_yields_none_with_reason(monkeypatch, temp_db):
    _install(monkeypatch, triggers=[])
    d = confirm_buy("600000.SH", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert d.action == "NONE"
    assert d.detail["stopped_at"] == "no_trigger"
    assert str(bd.FRESH_BARS) in d.detail["reason"]


def test_strong_trigger_becomes_buy(monkeypatch, temp_db):
    _install(monkeypatch, triggers=[_trigger("SB1", 0.9)])
    d = confirm_buy("600000.SH", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert d.action == "BUY"
    assert d.score >= bd.SCORE_BUY
    assert d.base_strategy == "SB1"
    assert d.detail["breakdown"]["base"] == pytest.approx(90.0)


def test_weak_trigger_becomes_watch(monkeypatch, temp_db):
    _install(monkeypatch, triggers=[_trigger("B1", 0.5)])
    d = confirm_buy("600000.SH", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert d.action == "WATCH"
    assert bd.SCORE_WATCH <= d.score < bd.SCORE_BUY


def test_short_market_can_demote_a_trigger(monkeypatch, temp_db):
    """同一个买点信号，在空头大盘下应被降级。"""
    _install(monkeypatch, triggers=[_trigger("B1", 0.66)])
    kl = _make_klines(60)
    up = confirm_buy("600000.SH", klines=kl, market={"market_dir": "LONG", "market_strength": 75})
    down = confirm_buy("600000.SH", klines=kl, market={"market_dir": "SHORT", "market_strength": 25})
    assert up.score > down.score
    assert up.action == "BUY"
    assert down.action != "BUY"


def test_resonance_counts_distinct_strategies_only(monkeypatch, temp_db):
    """同一战法连报三天是一个证据，不是三个。"""
    same = [_trigger("B3", 0.6, "20260728"), _trigger("B3", 0.6, "20260729"), _trigger("B3", 0.6, "20260730")]
    diff = [_trigger("B1", 0.6), _trigger("长安战法", 0.6), _trigger("娜娜图形", 0.6)]
    kl = _make_klines(60)

    _install(monkeypatch, triggers=same)
    one = confirm_buy("600000.SH", klines=kl, market=NEUTRAL_MARKET)
    _install(monkeypatch, triggers=diff)
    three = confirm_buy("600000.SH", klines=kl, market=NEUTRAL_MARKET)

    assert one.detail["breakdown"]["resonance"] == 0.0
    assert three.detail["breakdown"]["resonance"] == pytest.approx(2 * bd._RESONANCE_PER_EXTRA)


def test_resonance_is_capped(monkeypatch, temp_db):
    many = [_trigger(f"战法{i}", 0.6) for i in range(10)]
    _install(monkeypatch, triggers=many)
    d = confirm_buy("600000.SH", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert d.detail["breakdown"]["resonance"] == bd._RESONANCE_CAP


def test_base_uses_highest_confidence_trigger(monkeypatch, temp_db):
    _install(monkeypatch, triggers=[_trigger("B1", 0.55), _trigger("SB1", 0.9)])
    d = confirm_buy("600000.SH", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert d.base_strategy == "SB1"


def test_score_is_clamped_to_100(monkeypatch, temp_db):
    _install(
        monkeypatch,
        triggers=[_trigger("SB1", 0.98), _trigger("B1", 0.9), _trigger("长安战法", 0.9), _trigger("娜娜图形", 0.9)],
        macd_sig={"is_bottom_divergence": True, "macd_gold_cross": True, "is_dif_positive": True},
    )
    d = confirm_buy("600000.SH", klines=_make_klines(60), market={"market_dir": "LONG", "market_strength": 95})
    assert d.score <= 100.0
    assert d.confidence <= 1.0


# ==================== 数据边界 ====================


def test_no_klines_returns_empty_decision(monkeypatch, temp_db):
    d = confirm_buy("600000.SH", klines=[], market=NEUTRAL_MARKET)
    assert d.action == "NONE"
    assert d.detail["reason"] == "无 K 线数据"
    assert d.trade_date == ""


def test_future_date_falls_back_to_latest_bar(monkeypatch, temp_db):
    """目标日还没有数据时回退到最近一根，并如实带上那根的真实日期。

    绝不能按目标日落库——阶段0 在 daily_scores 上踩过这个坑：
    日期存疑的脏行永远不会被正确重跑覆盖。
    """
    _install(monkeypatch, triggers=[_trigger("B1", 0.7)])
    kl = _make_klines(60)
    d = confirm_buy("600000.SH", "20991231", klines=kl, market=NEUTRAL_MARKET)
    assert d.trade_date == kl[-1].trade_date != "20991231"


def test_date_before_all_data_is_not_persistable(monkeypatch, temp_db):
    """目标日早于全部数据时不产出可落库的决策。"""
    d = confirm_buy("600000.SH", "19900101", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert d.action == "NONE"
    assert d.trade_date == ""
    assert save_buy_decisions([d]) == 0


def test_insufficient_history_is_reported(monkeypatch, temp_db):
    """战法检测普遍需要 20 根以上历史，不足时要说清楚而不是静默给 NONE。"""
    d = confirm_buy("600000.SH", klines=_make_klines(10), market=NEUTRAL_MARKET)
    assert d.action == "NONE"
    assert "不足以检测战法" in d.detail["reason"]


def test_historical_date_uses_that_bar(monkeypatch, temp_db):
    """指定历史日期时必须在该根 K 线上判定，不能悄悄用最新一根。"""
    _install(monkeypatch, triggers=[_trigger("B1", 0.7)])
    kl = _make_klines(60)
    target = kl[40].trade_date
    d = confirm_buy("600000.SH", target, klines=kl, market=NEUTRAL_MARKET)
    assert d.trade_date == target


# ==================== 落库 ====================


def test_save_and_reload_is_idempotent(temp_db):
    d = BuyDecision(
        ts_code="600000.SH",
        trade_date="20260807",
        name="浦发银行",
        action="BUY",
        score=77.5,
        confidence=0.775,
        base_strategy="B1",
        triggers=[_trigger()],
        confirms=["B1触发"],
        vetoes=[],
        market=NEUTRAL_MARKET,
        theme={"theme": "大金融", "kind": "theme", "strength": 60.0, "rank": 2},
        detail={"breakdown": {"base": 60.0}},
    )
    assert save_buy_decisions([d]) == 1
    assert save_buy_decisions([d]) == 1

    from modules.database import get_connection

    with get_connection() as conn:
        rows = conn.execute("SELECT ts_code, action, score, theme, triggers FROM buy_decisions").fetchall()
    assert len(rows) == 1, "重跑必须覆盖而不是新增"
    assert rows[0][1] == "BUY"
    assert rows[0][3] == "大金融"
    assert json.loads(rows[0][4])[0]["strategy"] == "B1"


def test_save_skips_decisions_without_trade_date(temp_db):
    """拿不到数据日的决策写进去没法复盘，直接不写。"""
    good = BuyDecision(ts_code="600000.SH", trade_date="20260807", action="NONE")
    bad = BuyDecision(ts_code="600001.SH", trade_date="", action="NONE")
    assert save_buy_decisions([good, bad]) == 1


def test_save_empty_is_noop(temp_db):
    assert save_buy_decisions([]) == 0


def test_batch_survives_single_stock_failure(monkeypatch, temp_db):
    """一只票炸了不能带崩整批。"""
    real = bd.confirm_buy

    def _flaky(ts_code, *a, **kw):
        if ts_code == "BOOM.SH":
            raise RuntimeError("模拟异常")
        return real(ts_code, *a, **kw)

    monkeypatch.setattr(bd, "confirm_buy", _flaky)
    out = bd.confirm_buy_batch(["BOOM.SH", "600000.SH"], "20260807", market=NEUTRAL_MARKET)
    assert len(out) == 2
    assert out[0].detail["error"] == "模拟异常"


# ==================== 渲染 ====================


def test_format_decision_shows_veto(temp_db):
    d = BuyDecision(ts_code="600000.SH", trade_date="20260807", action="NONE", vetoes=["MACD一票否决"])
    text = bd.format_buy_decision(d)
    assert "一票否决" in text
    assert "[不买]" in text


def test_format_summary_counts_actions(temp_db):
    ds = [
        BuyDecision(ts_code="A", action="BUY", score=80),
        BuyDecision(ts_code="B", action="WATCH", score=50),
        BuyDecision(ts_code="C", action="NONE", score=0),
    ]
    text = bd.format_buy_summary(ds)
    assert "买入 1" in text and "观察 1" in text and "不买 1" in text
    # BUY 必须排在最前
    assert text.index("\nA ") < text.index("\nB ") < text.index("\nC ")


def test_format_summary_marks_industry_fallback(temp_db):
    d = BuyDecision(
        ts_code="A", action="BUY", score=70, theme={"theme": "半导体", "kind": "industry", "strength": 80}
    )
    assert "(行业)" in bd.format_buy_summary([d])
