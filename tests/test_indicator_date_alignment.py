"""Regression tests for date alignment between bars and warmed-up indicators."""

from modules.indicators import DailyData
from modules.indicators.price_patterns.complex_patterns import detect_divergence


def _bar(index: int, close: float) -> DailyData:
    return DailyData(
        ts_code="000001.SZ",
        trade_date=f"2026{index + 1:04d}",
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        vol=10_000,
        amount=close * 10_000,
        pct_chg=0.0,
        prev_close=close,
    )


def test_divergence_maps_truncated_dif_to_matching_trade_dates() -> None:
    closes = [100.0] * 100
    closes[45] = 110.0
    closes[-1] = 109.0
    klines = [_bar(index, close) for index, close in enumerate(closes)]

    # A 75-item DIF sequence corresponds to K-line indexes 25..99.  The prior
    # high at K-line index 45 therefore maps to DIF index 20, not index 45.
    dif = [1.0] * 75
    dif[20] = 10.0
    dif[-1] = 5.0

    result = detect_divergence(klines, dif)

    assert result["is_top_divergence"] is True
