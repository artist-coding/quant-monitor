"""核心指标与策略日期映射的回归测试。"""

import pytest

import modules.strategies as strategies
from modules.indicators import DailyData, calculate_zg_white, precompute_macd_sequence
from modules.strategies.core import _dict_to_daily, _get_macd_dif, _populate_macd_cache
from tests.conftest import generate_uptrend_klines


def _make_klines(prices: list[float]) -> list[DailyData]:
    klines = []
    for index, close in enumerate(prices):
        previous = prices[index - 1] if index else close
        klines.append(
            DailyData(
                ts_code="000001.SZ",
                trade_date=f"2026{index + 1:04d}",
                open=previous,
                high=max(previous, close) * 1.01,
                low=min(previous, close) * 0.99,
                close=close,
                vol=10_000 + index * 100,
                amount=close * (10_000 + index * 100),
                pct_chg=(close / previous - 1) * 100 if index else 0.0,
                prev_close=previous,
            )
        )
    return klines


def _double_ema_reference(values: list[float], period: int) -> float:
    alpha = 2 / (period + 1)
    ema1 = values[0]
    ema2 = ema1
    for value in values[1:]:
        ema1 = value * alpha + ema1 * (1 - alpha)
        ema2 = ema1 * alpha + ema2 * (1 - alpha)
    return ema2


def test_zg_white_is_ema_of_the_first_ema_sequence():
    prices = [100, 112, 95, 118, 91, 125, 98, 132, 102, 138, 107, 144]
    klines = _make_klines(prices)

    expected = round(_double_ema_reference(prices, 10), 2)

    assert calculate_zg_white(klines) == expected
    assert calculate_zg_white(klines[:9]) == 0


def test_macd_cache_has_one_value_for_each_matching_kline():
    prices = [100 + index * 0.6 + (index % 7 - 3) ** 2 * 0.25 for index in range(70)]
    klines = _make_klines(prices)
    expected_dif, expected_dea, expected_hist = precompute_macd_sequence(klines)

    _populate_macd_cache(klines)

    assert [k.macd_dif for k in klines] == pytest.approx(expected_dif)
    assert [k.macd_dea for k in klines] == pytest.approx(expected_dea)
    assert [k.macd_hist for k in klines] == pytest.approx(expected_hist)
    assert klines[24].macd_dif == 0.0
    assert klines[25].macd_dif == pytest.approx(expected_dif[25])
    assert klines[-1].macd_dif == pytest.approx(expected_dif[-1])


def test_lazy_macd_lookup_returns_the_requested_dates_dif():
    prices = [100 + index * 0.4 + (index % 5) * 0.7 for index in range(60)]
    klines = _make_klines(prices)
    requested_index = 45
    expected_dif, _, _ = precompute_macd_sequence(klines[: requested_index + 1])

    result = _get_macd_dif(klines, requested_index)

    assert result == pytest.approx(expected_dif[requested_index])
    assert klines[25].macd_dif == pytest.approx(expected_dif[25])
    assert klines[requested_index].trade_date == "20260046"


def test_strategy_precompute_keeps_macd_attached_to_the_same_trade_date(monkeypatch):
    rows = generate_uptrend_klines(n=80, ts_code="000001.SZ", daily_pct=0.7)
    expected_dif, _, _ = precompute_macd_sequence(_dict_to_daily(rows))
    captured: dict[int, tuple[str, float | None]] = {}

    def inspect_s2(klines, index, dif_list=None):
        captured[index] = (klines[index].trade_date, klines[index].macd_dif)
        return None

    monkeypatch.setattr(strategies, "get_kline_data", lambda ts_code, days: rows)
    monkeypatch.setattr(strategies, "detect_s2", inspect_s2)

    strategies.detect_all_strategies("000001.SZ", days=80)

    for index in (25, 40, 79):
        assert captured[index][0] == rows[index]["trade_date"]
        assert captured[index][1] == pytest.approx(expected_dif[index])
