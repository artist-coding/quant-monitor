#!/usr/bin/env python3
"""MACD 趋势顾问：资格过滤、动能确认、结构风险与可审计否决。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..indicators import DailyData, precompute_macd_sequence

BarLike = DailyData | dict[str, Any]


@dataclass(frozen=True)
class MacdStrategyConfig:
    """MACD 战法第一版研究参数。"""

    fast: int = 12
    slow: int = 26
    signal: int = 9
    histogram_multiplier: int = 2
    warmup_bars: int = 120
    zero_epsilon: float = 0.0
    sync_window: int = 5
    pivot_left: int = 3
    pivot_right: int = 3
    max_pivot_distance: int = 60
    price_tolerance_atr: float = 0.5
    price_tolerance_pct: float = 0.005
    dif_tolerance_std: float = 0.25
    approach_bars: int = 2
    failure_window: int = 3
    near_cross_ratio: float = 0.35


@dataclass(frozen=True)
class MacdUpstreamSignal:
    """由 B1、砖形图、突破等上游模块提供的最小上下文。"""

    exists: bool = False
    is_trend_long: bool = False
    is_b1: bool = False
    key_support_broken: bool = False
    s1_present: bool = False


def _value(bar: BarLike, name: str, default: Any = 0.0) -> Any:
    if isinstance(bar, dict):
        return bar.get(name, default)
    return getattr(bar, name, default)


def _macd_input_bar(bar: BarLike) -> DailyData:
    if isinstance(bar, DailyData):
        return bar
    close = _number(_value(bar, "close"))
    return DailyData(
        ts_code=str(_value(bar, "ts_code", "")),
        trade_date=str(_value(bar, "trade_date", "")),
        open=_number(_value(bar, "open", close)),
        high=_number(_value(bar, "high", close)),
        low=_number(_value(bar, "low", close)),
        close=close,
        vol=_number(_value(bar, "vol")),
        amount=_number(_value(bar, "amount")),
        pct_chg=_number(_value(bar, "pct_chg")),
        prev_close=_number(_value(bar, "prev_close")),
    )


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _linear_slope(values: list[float]) -> float:
    """返回等间隔序列的最小二乘斜率。"""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    if denominator <= 0:
        return 0.0
    return sum((i - x_mean) * (value - y_mean) for i, value in enumerate(values)) / denominator


def classify_four_state(dif: float, bar: float, zero_epsilon: float = 0.0) -> tuple[str, str]:
    """先用 DIF 判大区间，再用柱体判局部阶段。"""
    if dif > zero_epsilon:
        return "BULL", "BULL_IMPULSE" if bar > 0 else "BULL_PULLBACK"
    if dif < -zero_epsilon:
        return "BEAR", "BEAR_REBOUND" if bar > 0 else "BEAR_IMPULSE"
    return "NEUTRAL", "NEUTRAL"


def detect_synchronization(
    klines: Sequence[BarLike],
    dif_values: list[float],
    bar_values: list[float],
    window: int = 5,
) -> dict[str, Any]:
    """计算价格、归一化 DIF 与归一化柱体的窗口同步关系。"""
    n = min(len(klines), len(dif_values), len(bar_values))
    if n < 2:
        return {
            "price_slope": 0.0,
            "dif_slope": 0.0,
            "bar_slope": 0.0,
            "bar_improvement": 0.0,
            "up_sync_components": 0,
            "down_sync_components": 0,
            "up_sync_strong": False,
            "down_sync_strong": False,
        }

    start = max(0, n - max(2, window))
    closes = [max(_number(_value(bar, "close")), 1e-12) for bar in klines[start:n]]
    ndif = [dif_values[i] / max(_number(_value(klines[i], "close")), 1e-12) for i in range(start, n)]
    nbar = [bar_values[i] / max(_number(_value(klines[i], "close")), 1e-12) for i in range(start, n)]
    price_slope = _linear_slope([math.log(value) for value in closes])
    dif_slope = _linear_slope(ndif)
    bar_slope = _linear_slope(nbar)
    slopes = (price_slope, dif_slope, bar_slope)
    up_components = sum(value > 0 for value in slopes)
    down_components = sum(value < 0 for value in slopes)

    return {
        "price_slope": price_slope,
        "dif_slope": dif_slope,
        "bar_slope": bar_slope,
        "bar_improvement": nbar[-1] - nbar[-2],
        "up_sync_components": up_components,
        "down_sync_components": down_components,
        "up_sync_strong": up_components == 3 and bar_values[n - 1] > 0,
        "down_sync_strong": down_components == 3 and bar_values[n - 1] < 0,
    }


def _confirmed_pivots(values: list[float], left: int, right: int, high: bool) -> list[int]:
    """只返回在当前时点已经由右侧 K 线确认的摆动点。"""
    pivots: list[int] = []
    if len(values) < left + right + 1:
        return pivots
    for index in range(left, len(values) - right):
        center = values[index]
        left_values = values[index - left : index]
        right_values = values[index + 1 : index + right + 1]
        if high:
            matched = center > max(left_values) and center >= max(right_values)
        else:
            matched = center < min(left_values) and center <= min(right_values)
        if matched:
            pivots.append(index)
    return pivots


def _atr_at(klines: Sequence[BarLike], index: int, period: int = 14) -> float:
    start = max(0, index - period + 1)
    true_ranges: list[float] = []
    for current in range(start, index + 1):
        high = _number(_value(klines[current], "high"))
        low = _number(_value(klines[current], "low"))
        if current == 0:
            previous_close = _number(_value(klines[current], "close"))
        else:
            previous_close = _number(_value(klines[current - 1], "close"))
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0


def _indicator_peak(values: list[float], index: int, high: bool) -> float:
    window = values[max(0, index - 1) : min(len(values), index + 2)]
    return (max if high else min)(window)


def _indicator_tolerance(values: list[float], closes: list[float], index: int, multiplier: float) -> float:
    del closes  # values 已在调用方按收盘价归一化
    window = values[max(0, index - 59) : index + 1]
    return multiplier * statistics.pstdev(window) if len(window) >= 2 else 0.0


def _divergence_streak(
    pivots: list[int],
    prices: list[float],
    indicator: list[float],
    closes: list[float],
    klines: Sequence[BarLike],
    config: MacdStrategyConfig,
    high: bool,
) -> tuple[bool, int, int | None]:
    """比较相邻确认摆动点并返回当前连续背离次数。"""
    streak = 0
    latest_confirmation: int | None = None
    latest_match = False
    for first, second in zip(pivots, pivots[1:]):
        if second - first > config.max_pivot_distance:
            streak = 0
            continue
        price_tolerance = max(
            config.price_tolerance_atr * _atr_at(klines, second),
            closes[second] * config.price_tolerance_pct,
        )
        indicator_tolerance = _indicator_tolerance(indicator, closes, second, config.dif_tolerance_std)
        first_indicator = _indicator_peak(indicator, first, high)
        second_indicator = _indicator_peak(indicator, second, high)
        if high:
            matched = (
                prices[second] > prices[first] + price_tolerance
                and second_indicator < first_indicator - indicator_tolerance
            )
        else:
            matched = (
                prices[second] < prices[first] - price_tolerance
                and second_indicator > first_indicator + indicator_tolerance
            )
        streak = streak + 1 if matched else 0
        latest_match = matched
        latest_confirmation = second + config.pivot_right if matched else None
    return latest_match, streak if latest_match else 0, latest_confirmation


def detect_confirmed_divergence(
    klines: Sequence[BarLike],
    dif_values: list[float],
    bar_values: list[float],
    config: MacdStrategyConfig | None = None,
) -> dict[str, Any]:
    """基于已确认摆动点识别顶底背离，不把信号回填到摆动发生日。"""
    config = config or MacdStrategyConfig()
    n = min(len(klines), len(dif_values), len(bar_values))
    empty = {
        "top_dif": False,
        "top_bar": False,
        "bottom_dif": False,
        "bottom_bar": False,
        "strong_top": False,
        "strong_bottom": False,
        "top_divergence_count": 0,
        "bottom_divergence_count": 0,
        "divergence_count": 0,
        "top_confirmation_index": None,
        "bottom_confirmation_index": None,
        "confirmed_today": False,
    }
    if n < config.pivot_left + config.pivot_right + 2:
        return empty

    highs = [_number(_value(bar, "high")) for bar in klines[:n]]
    lows = [_number(_value(bar, "low")) for bar in klines[:n]]
    closes = [max(_number(_value(bar, "close")), 1e-12) for bar in klines[:n]]
    ndif = [dif_values[i] / closes[i] for i in range(n)]
    nbar = [bar_values[i] / closes[i] for i in range(n)]
    high_pivots = _confirmed_pivots(highs, config.pivot_left, config.pivot_right, high=True)
    low_pivots = _confirmed_pivots(lows, config.pivot_left, config.pivot_right, high=False)

    top_dif, top_count, top_confirmation = _divergence_streak(
        high_pivots, highs, ndif, closes, klines, config, high=True
    )
    bottom_dif, bottom_count, bottom_confirmation = _divergence_streak(
        low_pivots, lows, ndif, closes, klines, config, high=False
    )
    top_bar, _, _ = _divergence_streak(high_pivots, highs, nbar, closes, klines, config, high=True)
    bottom_bar, _, _ = _divergence_streak(low_pivots, lows, nbar, closes, klines, config, high=False)

    return {
        "top_dif": top_dif,
        "top_bar": top_bar,
        "bottom_dif": bottom_dif,
        "bottom_bar": bottom_bar,
        "strong_top": top_dif and top_bar,
        "strong_bottom": bottom_dif and bottom_bar,
        "top_divergence_count": top_count,
        "bottom_divergence_count": bottom_count,
        "divergence_count": max(top_count, bottom_count),
        "top_confirmation_index": top_confirmation,
        "bottom_confirmation_index": bottom_confirmation,
        "confirmed_today": top_confirmation == n - 1 or bottom_confirmation == n - 1,
    }


def detect_cross_failure(
    dif_values: list[float],
    dea_values: list[float],
    closes: list[float] | None = None,
    bar_values: list[float] | None = None,
    config: MacdStrategyConfig | None = None,
) -> dict[str, Any]:
    """识别未交叉失败与短暂假交叉两类金叉空/死叉多。"""
    config = config or MacdStrategyConfig()
    n = min(len(dif_values), len(dea_values))
    empty = {
        "gold_cross": False,
        "dead_cross": False,
        "gold_cross_failure": False,
        "dead_cross_failure": False,
        "gold_pattern": None,
        "dead_pattern": None,
        "confidence": 0.0,
    }
    if n < max(3, config.approach_bars + 1):
        return empty

    closes = closes or [1.0] * n
    bars = bar_values or [(dif_values[i] - dea_values[i]) * config.histogram_multiplier for i in range(n)]
    gaps = [dif_values[i] - dea_values[i] for i in range(n)]
    normalized_gap = [gaps[i] / max(abs(closes[i]), 1e-12) for i in range(n)]
    recent_scale = sum(abs(value) for value in normalized_gap[max(0, n - 20) :]) / min(20, n)
    recent_scale = max(recent_scale, 1e-12)

    def near(index: int) -> bool:
        return abs(normalized_gap[index]) / recent_scale <= config.near_cross_ratio

    current = n - 1
    gold_cross = gaps[current] > 0 and gaps[current - 1] <= 0
    dead_cross = gaps[current] < 0 and gaps[current - 1] >= 0
    approach = gaps[current - config.approach_bars : current]
    gold_a = (
        len(approach) == config.approach_bars
        and all(value < 0 for value in approach)
        and all(approach[i + 1] > approach[i] for i in range(len(approach) - 1))
        and near(current - 1)
        and gaps[current] < gaps[current - 1] < 0
        and dif_values[current] < dif_values[current - 1]
    )
    dead_a = (
        len(approach) == config.approach_bars
        and all(value > 0 for value in approach)
        and all(approach[i + 1] < approach[i] for i in range(len(approach) - 1))
        and near(current - 1)
        and gaps[current] > gaps[current - 1] > 0
        and dif_values[current] > dif_values[current - 1]
    )

    recent_start = max(1, current - config.failure_window)
    recent_gold = any(gaps[index] > 0 and gaps[index - 1] <= 0 for index in range(recent_start, current))
    recent_dead = any(gaps[index] < 0 and gaps[index - 1] >= 0 for index in range(recent_start, current))
    green_expanding = current >= 1 and bars[current] < bars[current - 1] < 0
    red_expanding = current >= 1 and bars[current] > bars[current - 1] > 0
    gold_b = recent_gold and gaps[current] < 0 and (dif_values[current] < dif_values[current - 1] or green_expanding)
    dead_b = recent_dead and gaps[current] > 0 and (dif_values[current] > dif_values[current - 1] or red_expanding)
    gold_failure = gold_a or gold_b
    dead_failure = dead_a or dead_b

    return {
        "gold_cross": gold_cross,
        "dead_cross": dead_cross,
        "gold_cross_failure": gold_failure,
        "dead_cross_failure": dead_failure,
        "gold_pattern": "A" if gold_a else ("B" if gold_b else None),
        "dead_pattern": "A" if dead_a else ("B" if dead_b else None),
        "confidence": 0.9 if gold_b or dead_b else (0.75 if gold_a or dead_a else 0.0),
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _classify_impulse(
    klines: Sequence[BarLike],
    dif_values: list[float],
    bar_values: list[float],
    momentum: dict[str, Any],
    divergence: dict[str, Any],
    cross_failure: dict[str, Any],
) -> dict[str, Any]:
    """使用“推动—回调—再启动”状态机，避免首段上涨直接确认为建仓。"""
    n = len(klines)
    closes = [max(_number(_value(bar, "close")), 1e-12) for bar in klines]
    lows = [_number(_value(bar, "low"), closes[i]) for i, bar in enumerate(klines)]
    amounts = [_number(_value(bar, "amount")) or _number(_value(bar, "vol")) for bar in klines]
    ndif = [dif_values[i] / closes[i] for i in range(n)]
    nbar = [bar_values[i] / closes[i] for i in range(n)]
    amount_ratio = _mean(amounts[-5:]) / max(_mean(amounts[-20:]), 1e-12) if n >= 20 else 0.0

    recent_returns = [max(0.0, closes[i] / closes[i - 1] - 1) for i in range(max(1, n - 5), n)]
    total_positive = sum(recent_returns)
    concentration = max(recent_returns, default=0.0) / total_positive if total_positive > 0 else 1.0
    sync_days = sum(
        closes[i] > closes[i - 1] and ndif[i] > ndif[i - 1] and nbar[i] > nbar[i - 1] for i in range(max(1, n - 5), n)
    )
    candidate_now = (
        momentum["up_sync_components"] == 3
        and dif_values[-1] > 0
        and bar_values[-1] > 0
        and amount_ratio > 1.0
        and concentration <= 0.6
    )

    impulse_end: int | None = None
    for end in range(max(4, n - 40), max(4, n - 2)):
        transitions = range(end - 2, end + 1)
        if (
            all(closes[i] > closes[i - 1] and ndif[i] > ndif[i - 1] and nbar[i] > nbar[i - 1] for i in transitions)
            and dif_values[end] > 0
            and bar_values[end] > 0
        ):
            impulse_end = end

    pullback_valid = False
    pullback_volume_ok = False
    support_held = False
    restart = False
    if impulse_end is not None and n - impulse_end >= 3:
        pullback_slice = list(range(impulse_end + 1, n - 1))
        impulse_slice = list(range(max(0, impulse_end - 4), impulse_end + 1))
        pulled_back = bool(pullback_slice) and min(closes[i] for i in pullback_slice) < closes[impulse_end] * 0.995
        dif_held = bool(pullback_slice) and all(dif_values[i] > 0 for i in pullback_slice)
        pullback_volume_ok = _mean([amounts[i] for i in pullback_slice]) < _mean([amounts[i] for i in impulse_slice])
        support = min(lows[i] for i in impulse_slice)
        support_held = bool(pullback_slice) and min(lows[i] for i in pullback_slice) >= support * 0.98
        pullback_valid = pulled_back and dif_held and pullback_volume_ok and support_held
        restart = (
            closes[-1] > closes[-2]
            and dif_values[-1] > dif_values[-2]
            and (
                cross_failure["dead_cross_failure"]
                or (bar_values[-1] > 0 >= bar_values[-2])
                or bar_values[-1] > bar_values[-2]
            )
        )

    bullish_run = 1
    for index in range(n - 1, max(0, n - 20), -1):
        if closes[index] > closes[index - 1] and dif_values[index] >= dif_values[index - 1]:
            bullish_run += 1
        else:
            break

    score = 0
    if n >= 3 and all(value > 0 for value in dif_values[-3:]):
        score += 15
    if sync_days >= 3:
        score += 20
    if 5 <= bullish_run <= 20:
        score += 10
    if 1.2 <= amount_ratio <= 2.5:
        score += 10
    if concentration <= 0.5:
        score += 10
    if pullback_valid:
        score += 15
    if pullback_valid and restart:
        score += 10
    if divergence["top_dif"]:
        score -= 15
    if impulse_end is not None and (not pullback_volume_ok or not support_held):
        score -= 15
    if cross_failure["gold_cross_failure"]:
        score -= 15
    if dif_values[-1] < 0:
        score -= 20
    if concentration >= 0.6:
        score -= 20
    score = max(0, min(100, score))

    failed = impulse_end is not None and dif_values[-1] < 0 and not support_held
    one_wave_risk = (candidate_now or impulse_end is not None) and (
        concentration >= 0.6
        or divergence["top_dif"]
        or cross_failure["gold_cross_failure"]
        or (impulse_end is not None and not support_held)
    )
    if failed:
        state = "FAILED_IMPULSE"
    elif pullback_valid and restart and score >= 60:
        state = "ACCUMULATION_CONFIRMED"
    elif one_wave_risk:
        state = "ONE_WAVE_RISK"
    elif candidate_now or impulse_end is not None:
        state = "IMPULSE_CANDIDATE"
    else:
        state = "UNKNOWN"

    return {
        "state": state,
        "accumulation_score": score,
        "amount_ratio_5_20": amount_ratio,
        "single_day_gain_concentration": concentration,
        "sync_days_5": sync_days,
        "pullback_confirmed": pullback_valid,
        "restart_confirmed": pullback_valid and restart,
    }


def evaluate_macd_strategy(
    klines: Sequence[BarLike],
    upstream_signal: MacdUpstreamSignal | None = None,
    config: MacdStrategyConfig | None = None,
    macd_values: tuple[list[float], list[float], list[float]] | None = None,
) -> dict[str, Any]:
    """输出规格化、可解释且不会单独批准交易的 MACD 顾问结果。"""
    config = config or MacdStrategyConfig()
    upstream = upstream_signal or MacdUpstreamSignal()
    n = len(klines)
    if macd_values is None:
        daily_klines = [_macd_input_bar(item) for item in klines]
        dif_values, dea_values, bar_values = precompute_macd_sequence(
            daily_klines,
            config.fast,
            config.slow,
            config.signal,
        )
    else:
        dif_values, dea_values, bar_values = macd_values
    if not (len(dif_values) == len(dea_values) == len(bar_values) == n):
        raise ValueError("MACD 序列必须与 K 线按日期一一对齐")

    symbol = str(_value(klines[-1], "ts_code", "")) if klines else ""
    date = str(_value(klines[-1], "trade_date", "")) if klines else ""
    ready = n >= config.warmup_bars and n >= 2
    dif = dif_values[-1] if n else 0.0
    dea = dea_values[-1] if n else 0.0
    bar = bar_values[-1] if n else 0.0
    close = max(_number(_value(klines[-1], "close")), 1e-12) if n else 1.0
    major, phase = classify_four_state(dif, bar, config.zero_epsilon) if ready else ("NEUTRAL", "NEUTRAL")
    zero_cross_up = ready and dif > 0 and dif_values[-2] <= 0
    zero_cross_down = ready and dif < 0 and dif_values[-2] >= 0
    momentum = detect_synchronization(klines, dif_values, bar_values, config.sync_window)
    divergence = (
        detect_confirmed_divergence(klines, dif_values, bar_values, config)
        if ready
        else detect_confirmed_divergence([], [], [], config)
    )
    closes = [max(_number(_value(item, "close")), 1e-12) for item in klines]
    cross_failure = (
        detect_cross_failure(dif_values, dea_values, closes, bar_values, config)
        if ready
        else detect_cross_failure([], [], config=config)
    )
    impulse = (
        _classify_impulse(klines, dif_values, bar_values, momentum, divergence, cross_failure)
        if ready
        else {
            "state": "UNKNOWN",
            "accumulation_score": 0,
            "amount_ratio_5_20": 0.0,
            "single_day_gain_concentration": 0.0,
            "sync_days_5": 0,
            "pullback_confirmed": False,
            "restart_confirmed": False,
        }
    )

    trend_eligible_long = ready and dif > config.zero_epsilon
    green_expanding = ready and bar < bar_values[-2] < 0
    green_contracting = ready and bar_values[-2] < bar < 0
    qualification_veto = upstream.exists and upstream.is_trend_long and not trend_eligible_long
    divergence_veto = divergence["top_dif"] and cross_failure["gold_cross_failure"]
    support_veto = upstream.key_support_broken and dif < 0 and green_expanding
    hard_veto = ready and (qualification_veto or divergence_veto or support_veto)
    confirmation = ready and (
        phase == "BULL_IMPULSE"
        or (phase == "BULL_PULLBACK" and green_contracting)
        or (dif > 0 and cross_failure["dead_cross_failure"])
    )
    entry_ready = upstream.exists and trend_eligible_long and not hard_veto and not divergence["strong_top"]

    warning_codes: list[str] = []
    confirm_codes: list[str] = []
    if not ready:
        warning_codes.append("INSUFFICIENT_WARMUP")
    if ready and dif < 0:
        warning_codes.append("DIF_BELOW_ZERO")
    if phase == "BEAR_REBOUND":
        warning_codes.append("BEAR_REBOUND_ONLY")
    if green_expanding:
        warning_codes.append("GREEN_BAR_EXPANDING")
    if divergence["top_dif"]:
        warning_codes.append("TOP_DIF_DIVERGENCE")
    if divergence["strong_top"]:
        warning_codes.append("STRONG_TOP_DIVERGENCE")
    if cross_failure["gold_cross_failure"]:
        warning_codes.append("GOLD_CROSS_FAILURE")
    if impulse["state"] in ("ONE_WAVE_RISK", "FAILED_IMPULSE"):
        warning_codes.append(impulse["state"])
    if qualification_veto:
        warning_codes.append("TREND_LONG_QUALIFICATION_VETO")
    if support_veto:
        warning_codes.append("SUPPORT_BREAK_MACD_BEAR")
    if trend_eligible_long:
        confirm_codes.append("DIF_ABOVE_ZERO")
    if momentum["up_sync_strong"]:
        confirm_codes.append("UP_SYNC_STRONG")
    if phase == "BULL_PULLBACK" and green_contracting:
        confirm_codes.append("BULL_PULLBACK_CONTRACTING")
    if dif > 0 and cross_failure["dead_cross_failure"]:
        confirm_codes.append("DEAD_CROSS_FAILURE_BULL")
    if impulse["state"] == "ACCUMULATION_CONFIRMED":
        confirm_codes.append("ACCUMULATION_CONFIRMED")
    if upstream.is_b1 and entry_ready:
        confirm_codes.append("B1_TREND_QUALIFIED")

    return {
        "symbol": symbol,
        "date": date,
        "timeframe": "1d",
        "ready": ready,
        "priority": {"rank": 2, "label": "SECOND_AFTER_B1"},
        "macd": {
            "dif": dif,
            "dea": dea,
            "bar": bar,
            "ndif": dif / close,
            "nbar": bar / close,
        },
        "regime": {
            "major": major,
            "phase": phase,
            "zero_cross_up": zero_cross_up,
            "zero_cross_down": zero_cross_down,
        },
        "momentum": momentum,
        "divergence": divergence,
        "cross_failure": cross_failure,
        "impulse": impulse,
        "decision": {
            "trend_eligible_long": trend_eligible_long,
            "entry_ready": entry_ready,
            "confirmation": confirmation,
            "hard_veto": hard_veto,
            "warning_codes": warning_codes,
            "confirm_codes": confirm_codes,
        },
    }


def apply_macd_advisor(signals: list[Any], result: dict[str, Any]) -> list[Any]:
    """将硬否决落实到上游买入信号，同时保留原始信号供审计。"""
    if not result["ready"]:
        for signal in signals:
            if getattr(signal, "action", None) != "BUY":
                continue
            details = dict(getattr(signal, "details", {}) or {})
            details["macd_advisor"] = {
                "trend_eligible_long": False,
                "entry_ready": False,
                "hard_veto": False,
                "warning_codes": ["INSUFFICIENT_WARMUP"],
                "confirm_codes": [],
            }
            signal.details = details
            signal.action = "WATCH"
            signal.reason = f"{signal.reason or signal.description}；MACD 预热不足，暂不放行"
        return signals
    decision = result["decision"]
    for signal in signals:
        if getattr(signal, "action", None) != "BUY":
            continue
        details = dict(getattr(signal, "details", {}) or {})
        details["macd_advisor"] = {
            "trend_eligible_long": decision["trend_eligible_long"],
            "entry_ready": decision["entry_ready"],
            "hard_veto": decision["hard_veto"],
            "warning_codes": decision["warning_codes"],
            "confirm_codes": decision["confirm_codes"],
        }
        signal.details = details
        if decision["hard_veto"]:
            signal.action = "WATCH"
            signal.reason = (
                f"{signal.reason or signal.description}；MACD 顾问否决：{','.join(decision['warning_codes'])}"
            )
    return signals


def macd_result_to_signal(result: dict[str, Any]) -> Any | None:
    """把结构化顾问结果转换为通用 StrategySignal；MACD 永不单独输出 BUY。"""
    if not result["ready"]:
        return None
    from .core import Action, Priority, StrategySignal, StrategyType

    decision = result["decision"]
    phase = result["regime"]["phase"]
    if decision["hard_veto"]:
        action = Action.SELL.value
        priority = Priority.CRITICAL
        confidence = 0.95
        conclusion = "硬否决"
    elif decision["confirmation"]:
        action = Action.HOLD.value
        priority = Priority.OPPORTUNITY
        confidence = 0.85
        conclusion = "确认上游信号"
    else:
        action = Action.WATCH.value
        priority = Priority.OPPORTUNITY
        confidence = 0.75 if decision["trend_eligible_long"] else 0.8
        conclusion = "仅观察，不独立批准交易"

    return StrategySignal(
        ts_code=result["symbol"],
        trade_date=result["date"],
        strategy=StrategyType.MACD,
        action=action,
        confidence=confidence,
        description=f"MACD顾问 {phase}：{conclusion}",
        reason=f"warning={decision['warning_codes']} confirm={decision['confirm_codes']}",
        details=result,
        price=None,
        priority=priority,
    )


__all__ = [
    "MacdStrategyConfig",
    "MacdUpstreamSignal",
    "apply_macd_advisor",
    "classify_four_state",
    "detect_confirmed_divergence",
    "detect_cross_failure",
    "detect_synchronization",
    "evaluate_macd_strategy",
    "macd_result_to_signal",
]
