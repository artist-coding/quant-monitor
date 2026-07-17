"""MACD 顾问战法的状态、结构信号、否决与优先级测试。"""

from __future__ import annotations

from modules.indicators import DailyData
from modules.strategies import (
    Action,
    MacdStrategyConfig,
    MacdUpstreamSignal,
    Priority,
    StrategySignal,
    StrategyType,
    apply_macd_advisor,
    classify_four_state,
    detect_confirmed_divergence,
    detect_cross_failure,
    evaluate_macd_strategy,
    macd_result_to_signal,
)
from modules.strategies import _post_process_signals


def _bars(closes: list[float], highs: list[float] | None = None, amounts: list[float] | None = None) -> list[DailyData]:
    highs = highs or [close + 0.5 for close in closes]
    amounts = amounts or [100.0] * len(closes)
    result: list[DailyData] = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        result.append(
            DailyData(
                ts_code="000001.SZ",
                trade_date=f"202601{index + 1:02d}",
                open=previous,
                high=highs[index],
                low=min(close, previous) - 0.5,
                close=close,
                vol=amounts[index],
                amount=amounts[index],
                pct_chg=(close / previous - 1) * 100 if previous else 0.0,
                prev_close=previous,
            )
        )
    return result


def _config(**overrides) -> MacdStrategyConfig:
    values = {
        "warmup_bars": 2,
        "pivot_left": 2,
        "pivot_right": 2,
        "price_tolerance_atr": 0.0,
        "price_tolerance_pct": 0.0,
        "dif_tolerance_std": 0.0,
    }
    values.update(overrides)
    return MacdStrategyConfig(**values)


def test_four_state_distinguishes_major_trend_from_histogram() -> None:
    assert classify_four_state(1.0, -0.2) == ("BULL", "BULL_PULLBACK")
    assert classify_four_state(-1.0, 0.2) == ("BEAR", "BEAR_REBOUND")


def test_bear_rebound_has_no_trend_long_qualification() -> None:
    bars = _bars([10, 10.1, 10.2, 10.3, 10.4])
    dif = [-0.5, -0.45, -0.4, -0.35, -0.3]
    dea = [-0.6, -0.55, -0.5, -0.45, -0.4]
    hist = [2 * (left - right) for left, right in zip(dif, dea)]
    result = evaluate_macd_strategy(bars, config=_config(), macd_values=(dif, dea, hist))

    assert result["regime"]["phase"] == "BEAR_REBOUND"
    assert result["decision"]["trend_eligible_long"] is False
    assert "BEAR_REBOUND_ONLY" in result["decision"]["warning_codes"]


def test_divergence_appears_only_on_pivot_confirmation_day() -> None:
    highs = [9, 10, 11, 15, 12, 11, 10, 12, 13, 16, 14, 13]
    closes = [value - 1 for value in highs]
    dif = [0.1] * len(closes)
    dif[3] = 1.0
    dif[9] = 0.4
    hist = [0.1] * len(closes)
    hist[3] = 0.8
    hist[9] = 0.2
    bars = _bars(closes, highs)

    before = detect_confirmed_divergence(bars[:11], dif[:11], hist[:11], _config())
    confirmed = detect_confirmed_divergence(bars, dif, hist, _config())

    assert before["top_dif"] is False
    assert confirmed["top_dif"] is True
    assert confirmed["top_confirmation_index"] == 11
    assert confirmed["confirmed_today"] is True


def test_continuous_divergence_increments_count() -> None:
    highs = [9, 10, 11, 15, 12, 11, 10, 12, 13, 16, 14, 13, 12, 13, 14, 17, 15, 14]
    closes = [value - 1 for value in highs]
    dif = [0.1] * len(closes)
    hist = [0.05] * len(closes)
    for index, dif_value, hist_value in ((3, 1.2, 0.9), (9, 0.8, 0.6), (15, 0.4, 0.3)):
        dif[index] = dif_value
        hist[index] = hist_value

    result = detect_confirmed_divergence(_bars(closes, highs), dif, hist, _config())

    assert result["top_dif"] is True
    assert result["top_divergence_count"] == 2
    assert result["divergence_count"] == 2


def test_near_cross_turns_down_is_gold_cross_failure() -> None:
    result = detect_cross_failure(
        dif_values=[-1.0, -0.1, -0.2],
        dea_values=[0.0, 0.0, 0.0],
        closes=[10.0, 10.0, 10.0],
        config=_config(near_cross_ratio=0.35),
    )
    assert result["gold_cross_failure"] is True
    assert result["gold_pattern"] == "A"


def test_near_cross_turns_up_is_dead_cross_failure() -> None:
    result = detect_cross_failure(
        dif_values=[1.0, 0.1, 0.2],
        dea_values=[0.0, 0.0, 0.0],
        closes=[10.0, 10.0, 10.0],
        config=_config(near_cross_ratio=0.35),
    )
    assert result["dead_cross_failure"] is True
    assert result["dead_pattern"] == "A"


def test_ordinary_gold_cross_never_becomes_standalone_buy() -> None:
    bars = _bars([10, 10.1, 10.2, 10.3, 10.4])
    dif = [-0.3, -0.2, -0.1, 0.1, 0.2]
    dea = [0.0] * len(dif)
    hist = [2 * value for value in dif]
    result = evaluate_macd_strategy(bars, config=_config(), macd_values=(dif, dea, hist))
    signal = macd_result_to_signal(result)

    assert result["decision"]["entry_ready"] is False
    assert signal is not None
    assert signal.action != Action.BUY.value


def test_initial_synchronized_rise_is_only_impulse_candidate() -> None:
    closes = [100.0] * 20 + [101.0, 102.0, 103.0, 104.0, 105.0]
    amounts = [100.0] * 20 + [180.0] * 5
    bars = _bars(closes, amounts=amounts)
    dif = [0.05] * 20 + [0.1, 0.2, 0.3, 0.4, 0.5]
    dea = [0.0] * len(dif)
    hist = [0.02] * 20 + [0.05, 0.1, 0.15, 0.2, 0.25]
    result = evaluate_macd_strategy(bars, config=_config(), macd_values=(dif, dea, hist))

    assert result["impulse"]["state"] == "IMPULSE_CANDIDATE"
    assert result["impulse"]["pullback_confirmed"] is False


def test_accumulation_confirmation_requires_pullback_and_restart_data() -> None:
    closes = [100.0] * 20 + [102, 104, 106, 108, 110, 109, 107, 107.2, 107.5, 108]
    amounts = [100.0] * 20 + [200] * 5 + [80] * 4 + [120]
    bars = _bars(closes, amounts=amounts)
    dif = [0.05] * 20 + [0.1, 0.2, 0.3, 0.4, 0.5, 0.48, 0.42, 0.43, 0.45, 0.5]
    dea = [0.0] * len(dif)
    hist = [0.02] * 20 + [0.05, 0.1, 0.15, 0.2, 0.25, 0.1, -0.08, -0.06, -0.04, 0.04]

    before = evaluate_macd_strategy(bars[:27], config=_config(), macd_values=(dif[:27], dea[:27], hist[:27]))
    confirmed = evaluate_macd_strategy(bars, config=_config(), macd_values=(dif, dea, hist))

    assert before["impulse"]["state"] != "ACCUMULATION_CONFIRMED"
    assert confirmed["impulse"]["state"] == "ACCUMULATION_CONFIRMED"
    assert confirmed["impulse"]["restart_confirmed"] is True


def test_bear_regime_hard_veto_downgrades_b1_to_watch() -> None:
    bars = _bars([10, 9.9, 9.8, 9.7, 9.6])
    dif = [-0.1, -0.2, -0.3, -0.4, -0.5]
    dea = [0.0] * len(dif)
    hist = [2 * value for value in dif]
    result = evaluate_macd_strategy(
        bars,
        upstream_signal=MacdUpstreamSignal(exists=True, is_trend_long=True, is_b1=True),
        config=_config(),
        macd_values=(dif, dea, hist),
    )
    b1 = StrategySignal(
        ts_code="000001.SZ",
        trade_date=bars[-1].trade_date,
        strategy=StrategyType.B1,
        action=Action.BUY.value,
        confidence=0.9,
        description="B1",
        priority=Priority.OPPORTUNITY,
    )

    apply_macd_advisor([b1], result)

    assert result["decision"]["hard_veto"] is True
    assert b1.action == Action.WATCH.value
    assert b1.details["macd_advisor"]["hard_veto"] is True


def test_insufficient_warmup_does_not_leave_upstream_buy_executable() -> None:
    bars = _bars([10, 10.1, 10.2])
    result = evaluate_macd_strategy(
        bars,
        upstream_signal=MacdUpstreamSignal(exists=True, is_trend_long=True, is_b1=True),
        config=MacdStrategyConfig(warmup_bars=120),
        macd_values=([0.1, 0.2, 0.3], [0.0, 0.0, 0.0], [0.2, 0.4, 0.6]),
    )
    b1 = StrategySignal(
        ts_code="000001.SZ",
        trade_date=bars[-1].trade_date,
        strategy=StrategyType.B1,
        action=Action.BUY.value,
        confidence=0.9,
        description="B1",
    )

    apply_macd_advisor([b1], result)

    assert b1.action == Action.WATCH.value
    assert b1.details["macd_advisor"]["warning_codes"] == ["INSUFFICIENT_WARMUP"]


def test_normal_priority_is_b1_then_macd_then_other_strategies() -> None:
    def signal(strategy: StrategyType, confidence: float) -> StrategySignal:
        return StrategySignal(
            ts_code="000001.SZ",
            trade_date="20260101",
            strategy=strategy,
            action=Action.WATCH.value,
            confidence=confidence,
            description=strategy.value,
            priority=Priority.OPPORTUNITY,
        )

    ordered = _post_process_signals(
        [
            signal(StrategyType.B2, 1.0),
            signal(StrategyType.MACD, 0.99),
            signal(StrategyType.B1, 0.1),
        ]
    )

    assert [item.strategy for item in ordered] == [StrategyType.B1, StrategyType.MACD, StrategyType.B2]


def test_macd_screener_exposes_trend_qualification_not_buy_signal() -> None:
    from modules.screener.criteria import _criteria_macd_eligible
    from modules.screener.models import StockScore
    from tests.conftest import generate_uptrend_klines

    score = StockScore(ts_code="000001.SZ")
    matched = _criteria_macd_eligible(generate_uptrend_klines(n=150), score)

    assert matched is True
    assert any(reason.startswith("MACD趋势资格:") for reason in score.reasons)


def test_b1_screener_candidate_is_rejected_in_macd_bear_regime() -> None:
    from unittest.mock import patch

    from modules.screener.engine import _filter_stock
    from modules.screener.models import StockScore

    closes = [200.0 - index for index in range(150)]
    score = StockScore(ts_code="000001.SZ", b1_score=80)
    with (
        patch("modules.screener.engine._check_centipede", return_value=False),
        patch("modules.screener.engine._check_sandglass_min", return_value=False),
    ):
        matched = _filter_stock(("000001.SZ", _bars(closes), score), "b1")

    assert matched is False
    assert any(warning.startswith("MACD顾问否决:") for warning in score.warnings)


def test_simple_simulator_applies_macd_hard_veto() -> None:
    from unittest.mock import MagicMock, patch

    from modules.screener.models import StockScore
    from modules.simulator import SignalVerdict, SimulationConfig
    from modules.simulator.signal_filter import evaluate_stock

    closes = [200.0 - index for index in range(120)]
    score = StockScore(
        ts_code="000001.SZ",
        name="测试",
        score=90,
        b1_score=90,
        trend_score=50,
        volume_score=80,
        risk_score=80,
    )
    with (
        patch("modules.simulator.signal_filter.analyze_stock", return_value=score),
        patch("modules.simulator.signal_filter._extract_signals", return_value=["B1"]),
    ):
        result = evaluate_stock(
            "000001.SZ",
            "20261231",
            klines=_bars(closes),
            datasource=MagicMock(),
            config=SimulationConfig(strategy_mode="simple"),
        )

    assert result.verdict == SignalVerdict.BAD_STAGE
    assert "MACD硬否决" in result.signals
