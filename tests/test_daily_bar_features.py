"""Tests for canonical and prefix-stable daily-bar enrichment."""

from dataclasses import replace

import pytest

from modules.daily_portfolio.bar_features import enrich_daily_bars
from modules.indicators import DailyData


def _bar(date: str, open_: float, close: float, vol: float) -> DailyData:
    return DailyData(
        ts_code="000001.SZ",
        trade_date=date,
        open=open_,
        high=max(open_, close) * 1.01,
        low=min(open_, close) * 0.99,
        close=close,
        vol=vol,
        amount=close * vol,
        pct_chg=999,
        macd_dif=999,
    )


def test_enrichment_derives_legacy_flags_without_mutating_inputs() -> None:
    bars = [
        _bar("20260708", 10, 10, 1_000),
        _bar("20260709", 10.2, 10.1, 2_000),
        _bar("20260710", 10.0, 9.9, 900),
    ]

    enriched = enrich_daily_bars(bars, "20260710")

    assert enriched[1].is_rise is True
    assert enriched[1].is_beidou is True
    assert enriched[1].is_jiayin is True
    assert enriched[2].is_suoliang is True
    assert enriched[2].is_yinxian is True
    assert enriched[2].macd_dif is None
    assert bars[1].is_beidou is False
    assert bars[1].pct_chg == 999


def test_enrichment_is_prefix_invariant() -> None:
    bars = [
        _bar("20260708", 10, 10, 1_000),
        _bar("20260709", 10, 10.5, 2_000),
        _bar("20260710", 10.5, 10.2, 800),
        _bar("20260713", 30, 50, 99_000_000),
    ]

    prefix = enrich_daily_bars(bars[:3], "20260710")
    full = enrich_daily_bars(bars, "20260713")

    assert prefix == full[:3]


def test_enrichment_rejects_future_data_instead_of_silently_truncating() -> None:
    bars = [_bar("20260710", 10, 10, 1_000), _bar("20260713", 10, 11, 2_000)]
    with pytest.raises(ValueError, match="future bar"):
        enrich_daily_bars(bars, "20260710")


def test_enrichment_rejects_non_positive_prices() -> None:
    bad = replace(_bar("20260710", 10, 10, 1_000), low=0)
    with pytest.raises(ValueError, match="positive"):
        enrich_daily_bars([bad], "20260710")
