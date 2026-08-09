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


def _register(code="600000.SH", name="测试股"):
    """在 stock_basic 里登记一只可交易的票。

    买点确认的第零层要查 stock_basic 判断是否 ST；查不到会被判为
    "无法确认是否 ST"而直接排除，所以不走 _install 的测试得自己登记。
    """
    from modules.database import get_connection

    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO stock_basic (ts_code, name) VALUES (?, ?)", (code, name))


def _seed_amv(regime="bull", trade_date="20260807"):
    """在 temp_db 里铺出指定的活跃市值区间。

    活跃市值是选股总开关，库里没有它 confirm_buy 会在第一层就拦下，
    所以任何要走到个股判定的测试都得先铺。
    """
    from modules.amv import recompute_regimes
    from modules.database import get_connection

    # 从 20260501 起铺，覆盖所有测试用到的日期区间（_make_klines 从 20260601 开始）。
    # 两根就够：基准 → 触发。多头用 +5%（≥4），空头用 -3%（< -2.3）。
    base = 1000.0
    trigger = base * (1.05 if regime == "bull" else 0.97)
    rows = [("20260501", base), ("20260502", trigger), (trade_date, trigger)]
    with get_connection() as conn:
        conn.executemany("INSERT OR REPLACE INTO amv_daily (trade_date, close) VALUES (?, ?)", rows)
    recompute_regimes()


def _install(monkeypatch, *, vetoes=(), triggers=(), macd_sig=None, amv="bull"):
    """替换否决层与触发层，让主流程可控；并铺好活跃市值区间。"""
    if amv:
        _seed_amv(amv)
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


@pytest.mark.parametrize("regime,gate,blocked", [("bull", "on", False), ("bear", "on", True), ("bear", "off", False)])
def test_market_gate_follows_amv_regime(temp_db, regime, gate, blocked):
    """选股总开关是活跃市值区间，不再是全市场宽度。"""
    _seed_amv(regime)
    reason, _warn, day = bd.check_market_gate("20260807", gate)
    assert bool(reason) is blocked
    assert day is not None
    assert day.regime == ("多头区间" if regime == "bull" else "空头区间")


def test_market_gate_blocks_when_amv_missing(temp_db):
    """活跃市值现在是总开关，没有数据就拦下——宁可提示补数据，也不在未知区间开仓。"""
    reason, _warn, day = bd.check_market_gate("20260807")
    assert "活跃市值无数据" in reason
    assert day is None


def test_market_gate_warns_when_amv_is_stale(temp_db):
    _seed_amv("bull", trade_date="20260801")
    reason, warnings, day = bd.check_market_gate("20260820")
    assert reason == ""  # 区间是沿用状态机，旧数据仍放行
    assert any("落后" in w for w in warnings)


def test_position_hint_scales_with_breadth(temp_db):
    """大盘宽度只影响仓位建议，不影响能否选股。"""
    levels = [bd.suggest_position({"market_strength": s})["level"] for s in (85, 62, 50, 35, 15)]
    assert levels == ["重仓", "偏重", "半仓", "轻仓", "空仓观望"]
    assert bd.suggest_position(None)["level"] == "未知"


def test_describe_theme_is_informational_only():
    """主线只挂说明、不打分——它属于第二阶段。"""
    notes = bd._describe_theme({"theme": "商业航天", "kind": "theme", "strength": 92.0, "rank": 1, "total": 8})
    assert len(notes) == 1
    assert "商业航天" in notes[0]
    assert "不计入确认分" in notes[0]


def test_describe_theme_marks_industry_fallback():
    notes = bd._describe_theme({"theme": "半导体", "kind": "industry", "strength": 60.0, "rank": 3, "total": 90})
    assert "行业(兜底)" in notes[0]


def test_describe_theme_none_is_empty():
    assert bd._describe_theme(None) == []


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


def test_breadth_no_longer_changes_the_score(monkeypatch, temp_db):
    """大盘宽度已降级为建仓参考，不再影响确认分，更不决定能否选股。"""
    _install(monkeypatch, triggers=[_trigger("B1", 0.66)])
    kl = _make_klines(60)
    up = confirm_buy("600000.SH", klines=kl, market={"market_dir": "LONG", "market_strength": 75})
    down = confirm_buy("600000.SH", klines=kl, market={"market_dir": "SHORT", "market_strength": 25})
    assert up.score == down.score
    assert up.action == down.action


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
    _register()
    _seed_amv()
    d = confirm_buy("600000.SH", klines=[], market=NEUTRAL_MARKET)
    assert d.action == "NONE"
    assert d.detail["reason"] == "无 K 线数据"
    assert d.trade_date == ""


def test_future_date_falls_back_to_latest_bar(monkeypatch, temp_db):
    """目标日还没有数据时回退到最近一根，并如实带上那根的真实日期。

    绝不能按目标日落库——踩过这个坑：
    日期存疑的脏行永远不会被正确重跑覆盖。
    """
    _install(monkeypatch, triggers=[_trigger("B1", 0.7)])
    kl = _make_klines(60)
    d = confirm_buy("600000.SH", "20991231", klines=kl, market=NEUTRAL_MARKET)
    assert d.trade_date == kl[-1].trade_date != "20991231"


def test_date_before_all_data_is_not_persistable(monkeypatch, temp_db):
    """目标日早于全部数据时不产出可落库的决策。"""
    _register()
    _seed_amv()
    d = confirm_buy("600000.SH", "19900101", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert d.action == "NONE"
    assert d.trade_date == ""
    assert save_buy_decisions([d]) == 0


def test_insufficient_history_is_reported(monkeypatch, temp_db):
    """战法检测普遍需要 20 根以上历史，不足时要说清楚而不是静默给 NONE。"""
    _register()
    _seed_amv()
    d = confirm_buy("600000.SH", klines=_make_klines(10), market=NEUTRAL_MARKET)
    assert d.action == "NONE"
    assert "无法完整判定" in d.detail["reason"]
    assert str(bd._MIN_BARS) in d.detail["reason"]


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

    _seed_amv()
    monkeypatch.setattr(bd, "confirm_buy", _flaky)
    out, blocked = bd.confirm_buy_batch(["BOOM.SH", "600000.SH"], "20260807", market=NEUTRAL_MARKET)
    assert blocked == ""
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
    assert text.index("A ") < text.index("B ") < text.index("C ")


def test_format_summary_marks_final_picks():
    """入选第二阶段的票要在汇总表里带 #名次。"""
    ds = [
        BuyDecision(ts_code="A", action="BUY", score=80, pick_rank=1),
        BuyDecision(ts_code="B", action="BUY", score=70),
    ]
    lines = bd.format_buy_summary(ds).splitlines()
    assert any(line.startswith("#1 ") for line in lines)


def test_format_summary_marks_industry_fallback(temp_db):
    d = BuyDecision(
        ts_code="A", action="BUY", score=70, theme={"theme": "半导体", "kind": "industry", "strength": 80}
    )
    assert "(行业)" in bd.format_buy_summary([d])


# ==================== 第二阶段：主线/行业筛选 ====================


def _buy(code, score, group="默认主线", kind="theme", strength=70.0, action="BUY"):
    """造一条已完成第一阶段的决策。group=None 表示没有任何主线/行业归属。"""
    return BuyDecision(
        ts_code=code,
        trade_date="20260807",
        name=code,
        action=action,
        score=score,
        base_strategy="B1",
        theme=None if group is None else {"theme": group, "kind": kind, "strength": strength, "rank": 1},
    )


def test_theme_no_longer_affects_first_stage_score(monkeypatch, temp_db):
    """主线强度不得再影响买点确认分——否则一条强主线能把不合格的买点推过阈值。"""
    _install(monkeypatch, triggers=[_trigger("B1", 0.6)])
    kl = _make_klines(60)

    def _theme(code, date, lookback):
        return {"theme": "超强主线", "kind": "theme", "strength": 100.0, "rank": 1, "total": 9}

    import modules.themes as th_mod

    monkeypatch.setattr(th_mod, "get_stock_theme_strength", _theme)
    strong = confirm_buy("600000.SH", klines=kl, market=NEUTRAL_MARKET)

    monkeypatch.setattr(th_mod, "get_stock_theme_strength", lambda *a, **kw: None)
    none_theme = confirm_buy("600000.SH", klines=kl, market=NEUTRAL_MARKET)

    assert strong.score == none_theme.score
    assert "theme" not in strong.detail["breakdown"]
    # 但主线信息本身要留着，供第二阶段和展示用
    assert strong.theme["theme"] == "超强主线"


def test_select_only_considers_buy_by_default(temp_db):
    ds = [_buy("A", 80), _buy("B", 50, action="WATCH"), _buy("C", 0, action="NONE")]
    sel = bd.select_final_picks(ds, min_group_strength=0)
    assert sel["candidates"] == 1
    assert [e["decision"].ts_code for e in sel["picks"]] == ["A"]


def test_select_can_include_watch(temp_db):
    ds = [_buy("A", 80), _buy("B", 50, action="WATCH")]
    sel = bd.select_final_picks(ds, min_group_strength=0, include_watch=True)
    assert sel["candidates"] == 2


def test_select_sorts_by_group_strength_first(temp_db):
    """同一条强主线里的第二名，好过一条弱主线里的第一名。"""
    ds = [
        _buy("弱主线里的高分", 95, group="弱", strength=55),
        _buy("强主线里的低分", 66, group="强", strength=90),
    ]
    sel = bd.select_final_picks(ds)
    assert [e["decision"].ts_code for e in sel["picks"]] == ["强主线里的低分", "弱主线里的高分"]


def test_select_ties_broken_by_score(temp_db):
    ds = [_buy("低分", 66, group="同一条", strength=80), _buy("高分", 88, group="同一条", strength=80)]
    sel = bd.select_final_picks(ds)
    assert [e["decision"].ts_code for e in sel["picks"]] == ["高分", "低分"]


def test_select_rejects_weak_groups(temp_db):
    """买点再漂亮，票在整体走弱的板块里也是逆水行舟。"""
    ds = [_buy("A", 95, group="弱板块", strength=30)]
    sel = bd.select_final_picks(ds, min_group_strength=50)
    assert sel["picks"] == []
    assert "低于门槛" in sel["rejected"][0]["reason"]


def test_industry_fallback_gets_stricter_threshold(temp_db):
    """行业口径粗、噪音大，用它筛选时门槛加严一档。"""
    strength = bd.DEFAULT_MIN_GROUP_STRENGTH + bd.INDUSTRY_STRENGTH_PENALTY / 2
    as_theme = bd.select_final_picks([_buy("A", 80, group="X", kind="theme", strength=strength)])
    as_industry = bd.select_final_picks([_buy("A", 80, group="X", kind="industry", strength=strength)])
    assert len(as_theme["picks"]) == 1
    assert len(as_industry["picks"]) == 0


def test_select_reports_missing_group(temp_db):
    """没有任何主线/行业归属时要说清楚——多半是 stock_basic 缺 industry 或排名那步挂了。"""
    sel = bd.select_final_picks([_buy("A", 90, group=None)])
    assert sel["picks"] == []
    assert "无主线/行业归属" in sel["rejected"][0]["reason"]


def test_select_respects_top_n(temp_db):
    ds = [_buy(f"S{i}", 90 - i, group=f"G{i}", strength=90 - i) for i in range(8)]
    sel = bd.select_final_picks(ds, top_n=3)
    assert len(sel["picks"]) == 3
    assert [e["rank"] for e in sel["picks"]] == [1, 2, 3]
    assert all("名额已满" in e["reason"] for e in sel["rejected"])


def test_select_allows_concentration_by_default(temp_db):
    """默认不限制每组只数——主线思维本来就要集中。"""
    ds = [_buy(f"S{i}", 90 - i, group="同一条主线", strength=88) for i in range(4)]
    sel = bd.select_final_picks(ds)
    assert len(sel["picks"]) == 4


def test_max_per_group_enforces_diversification(temp_db):
    ds = [_buy(f"S{i}", 90 - i, group="同一条主线", strength=88) for i in range(4)]
    sel = bd.select_final_picks(ds, max_per_group=2)
    assert len(sel["picks"]) == 2
    assert any("分散约束" in e["reason"] for e in sel["rejected"])


def test_apply_picks_writes_rank_back(temp_db):
    ds = [_buy("A", 90, group="G", strength=80), _buy("B", 80, group="弱", strength=10)]
    sel = bd.select_final_picks(ds)
    bd.apply_picks(ds, sel)
    assert ds[0].pick_rank == 1
    assert ds[1].pick_rank == 0
    assert "低于门槛" in ds[1].pick_reason


def test_pick_rank_is_persisted(temp_db):
    ds = [_buy("600000.SH", 90, group="G", strength=80)]
    bd.apply_picks(ds, bd.select_final_picks(ds))
    assert bd.save_buy_decisions(ds) == 1

    from modules.database import get_connection

    with get_connection() as conn:
        row = conn.execute("SELECT pick_rank, pick_reason FROM buy_decisions").fetchone()
    assert row[0] == 1
    assert "强度" in row[1]


def test_format_final_picks_lists_rejections(temp_db):
    ds = [_buy("A", 90, group="强", strength=80), _buy("B", 88, group="弱", strength=10)]
    text = bd.format_final_picks(bd.select_final_picks(ds))
    assert "入选 1 只" in text
    assert "落选 1 只" in text
    assert "B" in text


def test_format_final_picks_when_nothing_selected(temp_db):
    text = bd.format_final_picks(bd.select_final_picks([]))
    assert "无入选标的" in text


# ==================== 第零层：可交易性过滤（ST / 北交所）====================


@pytest.mark.parametrize(
    "code,name,expect",
    [
        ("600000.SH", "浦发银行", ""),
        ("000001.SZ", "平安银行", ""),
        ("000010.SZ", "*ST美丽", "ST"),
        ("000078.SZ", "ST海王", "ST"),
        ("920002.BJ", "万达轴承", "北交所"),
        ("920008.BJ", "ST某北交票", "北交所"),  # 两条都中时先报北交所
        ("600001.SH", None, "无 stock_basic"),
    ],
)
def test_exclusion_reason_rules(code, name, expect):
    from modules.universe import exclusion_reason

    reason = exclusion_reason(code, name)
    if expect:
        assert expect in reason
    else:
        assert reason == ""


def test_filter_tradable_splits_and_keeps_order(temp_db):
    from modules.database import get_connection
    from modules.universe import filter_tradable

    with get_connection() as conn:
        conn.executemany(
            "INSERT INTO stock_basic (ts_code, name) VALUES (?, ?)",
            [("600000.SH", "浦发银行"), ("000010.SZ", "*ST美丽"), ("600519.SH", "贵州茅台")],
        )
    kept, excluded = filter_tradable(["600000.SH", "000010.SZ", "920002.BJ", "600519.SH", "999999.SZ"])
    assert kept == ["600000.SH", "600519.SH"]
    assert set(excluded) == {"000010.SZ", "920002.BJ", "999999.SZ"}


def test_st_stock_is_excluded_with_reason(monkeypatch, temp_db):
    """票池里放了 ST 的话要看到"被排除了"，而不是这只票凭空消失。"""
    _register("000010.SZ", "*ST美丽")
    _install(monkeypatch, triggers=[_trigger("SB1", 0.95)])
    monkeypatch.setattr(bd, "_lookup_name", lambda c: "*ST美丽")

    d = confirm_buy("000010.SZ", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert d.action == "NONE"
    assert d.detail["stopped_at"] == "excluded"
    assert "ST" in d.vetoes[0]
    # 不可交易标的不该落库——它不是"某一天的判断"，会污染按日归因
    assert d.trade_date == ""
    assert save_buy_decisions([d]) == 0


def test_bse_stock_is_excluded(monkeypatch, temp_db):
    _register("920002.BJ", "某北交票")
    _install(monkeypatch, triggers=[_trigger("SB1", 0.95)])
    monkeypatch.setattr(bd, "_lookup_name", lambda c: "某北交票")

    d = confirm_buy("920002.BJ", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert d.action == "NONE"
    assert "北交所" in d.vetoes[0]


def test_exclusion_happens_before_any_analysis(monkeypatch, temp_db):
    """排除要发生在取 K 线之前——不可交易的票不值得跑一遍战法检测。"""
    _register("000010.SZ", "*ST美丽")
    _seed_amv()
    called = []
    monkeypatch.setattr(bd, "_collect_triggers", lambda k, i: called.append(1) or [])
    monkeypatch.setattr(bd, "_collect_vetoes", lambda k, i: called.append(1) or ([], {}))

    confirm_buy("000010.SZ", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert called == []


# ==================== 大盘门槛在触发之前 ====================


def test_bear_regime_blocks_before_reading_klines(monkeypatch, temp_db):
    """空头区间时一根 K 线都不该读——这正是把门槛前移的意义。"""
    _register()
    _seed_amv("bear")
    touched = []
    monkeypatch.setattr(bd, "_collect_vetoes", lambda k, i: touched.append("veto") or ([], {}))
    monkeypatch.setattr(bd, "_collect_triggers", lambda k, i: touched.append("trigger") or [])
    monkeypatch.setattr(bd, "attach_mdc_fields", lambda k: touched.append("mdc"))

    d = confirm_buy("600000.SH", "20260807", klines=_make_klines(60))
    assert d.action == "NONE"
    assert d.detail["stopped_at"] == "market_gate"
    assert "空头区间" in d.vetoes[0]
    assert touched == [], "空头区间时不应做任何个股分析"
    # 不落库：这不是"某一天对这只票的判断"
    assert d.trade_date == ""
    assert save_buy_decisions([d]) == 0


def test_gate_does_not_add_score(monkeypatch, temp_db):
    """门槛通过后不再给分——它已履职，再叠一次就是重复计算。"""
    _install(monkeypatch, triggers=[_trigger("B1", 0.7)])
    kl = _make_klines(60)
    strong = confirm_buy("600000.SH", klines=kl, market={"market_dir": "LONG", "market_strength": 95})
    weak = confirm_buy("600000.SH", klines=kl, market={"market_dir": "NEUTRAL", "market_strength": 45})
    assert strong.score == weak.score
    assert "market" not in strong.detail["breakdown"]


def test_batch_short_circuits_in_bear_regime(temp_db):
    """批量扫描时门槛只判一次，不过关直接返回空表和原因。"""
    _seed_amv("bear")
    decisions, blocked = bd.confirm_buy_batch(["600000.SH", "600519.SH"], "20260807")
    assert decisions == []
    assert "空头区间" in blocked


# ==================== 触发层只留 B1 ====================


def test_only_b1_is_a_trigger(temp_db, monkeypatch):
    """B2/B3/SB1 与复合战法都不再触发买点。"""
    import modules.strategies.base_strategies as base
    import modules.strategies.compound_strategies as comp

    called = []
    for mod, names in ((base, ["detect_b2", "detect_b3", "detect_sb1"]),
                       (comp, ["detect_changan", "detect_nana", "detect_kengqi"])):
        for n in names:
            if hasattr(mod, n):
                monkeypatch.setattr(mod, n, lambda *a, **kw: called.append(1) or None)

    monkeypatch.setattr(base, "detect_b1", lambda *a, **kw: None)
    bd._collect_triggers(_make_klines(60), 59)
    assert called == [], "触发层不应再调用 B1 以外的战法"


def test_trigger_uses_b1_only_label(monkeypatch, temp_db):
    from modules.strategies.core import Action, Priority, StrategySignal, StrategyType
    import modules.strategies.base_strategies as base

    def _fake_b1(klines, index, kirin_context=None):
        if index != 59:
            return None
        return StrategySignal(
            ts_code="600000.SH",
            trade_date=klines[index].trade_date,
            strategy=StrategyType.B1,
            confidence=0.75,
            description="B1买点 J=-15.00",
            action=Action.BUY.value,
            priority=Priority.OPPORTUNITY,
        )

    monkeypatch.setattr(base, "detect_b1", _fake_b1)
    triggers = bd._collect_triggers(_make_klines(60), 59)
    assert len(triggers) == 1
    assert triggers[0]["strategy"] == "B1"
    assert triggers[0]["confidence"] == 0.75


def test_no_trigger_message_mentions_b1(monkeypatch, temp_db):
    _install(monkeypatch, triggers=[])
    d = confirm_buy("600000.SH", klines=_make_klines(60), market=NEUTRAL_MARKET)
    assert "B1" in d.detail["reason"]


# ==================== MDC 现算 ====================


def test_attach_mdc_fills_indicator_fields(temp_db):
    """全市场扫描不能依赖 indicator_cache（只覆盖票池 7 只票），必须现算。"""
    import random

    random.seed(7)
    kl = _make_klines(60)
    price = 10.0
    for k in kl:  # 造点波动，否则布林带宽为 0、DMI 无定义
        price *= 1 + random.uniform(-0.03, 0.03)
        k.open = k.close = round(price, 2)
        k.high = round(price * 1.02, 2)
        k.low = round(price * 0.98, 2)

    assert kl[-1].boll_lower is None and kl[-1].adx is None

    bd.attach_mdc_fields(kl)

    last = kl[-1]
    assert last.boll_lower is not None and last.boll_upper is not None
    assert last.boll_lower < last.boll_mid < last.boll_upper
    assert last.rsi6 is not None and 0 <= last.rsi6 <= 100
    assert last.adx is not None


def test_attach_mdc_is_safe_on_short_series(temp_db):
    """样本不足时保持 None，不能填 0——0 会被 B1 当成"有数据"参与比较。"""
    kl = _make_klines(15)
    bd.attach_mdc_fields(kl)
    assert kl[-1].boll_lower is None
    assert kl[-1].adx is None


# ==================== 全市场扫描 ====================


def test_scan_market_blocked_in_bear_regime(temp_db, monkeypatch):
    import modules.market_context as mc

    _seed_amv("bear")
    monkeypatch.setattr(mc, "compute_market_context", lambda d: {"market_dir": "LONG", "market_strength": 90})
    monkeypatch.setattr(bd, "_latest_trade_date", lambda: "20260807")

    res = bd.scan_market()
    # 大盘宽度再好也没用：总开关是活跃市值
    assert res["blocked"]
    assert res["scanned"] == 0
    assert res["decisions"] == []
    assert "空头区间" in bd.format_scan_result(res)


def test_scan_market_reports_empty_db(temp_db):
    res = bd.scan_market()
    assert res["blocked"] == "库内没有任何日线数据"


def test_scan_market_runs_full_funnel(temp_db, monkeypatch):
    """大盘放行时走完两阶段，并回填 pick_rank。"""
    import modules.market_context as mc
    import modules.universe as uni

    _seed_amv("bull")
    monkeypatch.setattr(mc, "compute_market_context", lambda d: {"market_dir": "LONG", "market_strength": 80})
    monkeypatch.setattr(bd, "_latest_trade_date", lambda: "20260807")
    monkeypatch.setattr(uni, "tradable_codes", lambda d=None: ["A.SZ", "B.SZ"])

    def _fake_confirm(code, date, **kw):
        return BuyDecision(
            ts_code=code,
            trade_date="20260807",
            name=code,
            action="BUY",
            score=80.0 if code == "A.SZ" else 70.0,
            base_strategy="B1",
            theme={"theme": "强主线", "kind": "theme", "strength": 85.0, "rank": 1},
        )

    monkeypatch.setattr(bd, "confirm_buy", _fake_confirm)

    res = bd.scan_market()
    assert res["blocked"] == ""
    assert res["scanned"] == 2
    assert [e["decision"].ts_code for e in res["selection"]["picks"]] == ["A.SZ", "B.SZ"]
    assert res["decisions"][0].pick_rank in (1, 2)
    text = bd.format_scan_result(res)
    assert "全市场扫描" in text and "强主线" in text


def test_save_only_actionable_skips_none(temp_db):
    ds = [
        BuyDecision(ts_code="A.SZ", trade_date="20260807", action="BUY", score=80),
        BuyDecision(ts_code="B.SZ", trade_date="20260807", action="WATCH", score=50),
        BuyDecision(ts_code="C.SZ", trade_date="20260807", action="NONE", score=0),
    ]
    assert bd.save_buy_decisions(ds, only_actionable=True) == 2
    assert bd.save_buy_decisions(ds) == 3
