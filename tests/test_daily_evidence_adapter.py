"""Tests for pure strategy-feature to normalized-score mapping."""

from datetime import date, timedelta

import pytest

from modules.daily_portfolio.evidence_adapter import MarketSnapshot, build_score_evidence
from modules.daily_portfolio.models import LifecycleState, PositionState
from modules.daily_portfolio.score_engine import aggregate_scores
from modules.daily_portfolio.strategy_features import build_strategy_features
from modules.indicators import DailyData


def _bars(count: int = 125) -> list[DailyData]:
    bars = []
    close = 10.0
    for index in range(count):
        previous = close
        close = previous * (1 + 0.002 + ((index % 5) - 2) * 0.0005)
        volume = 1_000_000 + (index % 10) * 10_000
        bars.append(
            DailyData(
                ts_code="000001.SZ",
                trade_date=(date(2026, 1, 1) + timedelta(days=index)).strftime("%Y%m%d"),
                open=previous,
                high=max(previous, close) * 1.01,
                low=min(previous, close) * 0.99,
                close=close,
                vol=volume,
                amount=volume * close,
                pct_chg=(close / previous - 1) * 100,
                prev_close=previous,
            )
        )
    return bars


def test_evidence_adapter_populates_every_configured_component() -> None:
    bars = _bars()
    features = build_strategy_features(bars, bars[-1].trade_date)
    result = build_score_evidence(
        bars,
        features,
        PositionState(ts_code="000001.SZ"),
        MarketSnapshot(bars[-1].trade_date, 60, source_hash="fixture"),
        max_position_pct=0.10,
    )
    scores = aggregate_scores(result.score_evidence)

    assert 0 <= scores.buy_score <= 100
    assert 0 <= scores.sell_score <= 100
    assert set(result.score_evidence.buy.as_mapping()) == {
        "entry_structure",
        "trend",
        "volume",
        "pattern_quality",
        "stage",
        "market",
        "resonance",
    }
    assert result.diagnostics["market_source_hash"] == "fixture"


def test_closing_below_stop_is_a_hard_exit() -> None:
    bars = _bars()
    features = build_strategy_features(bars, bars[-1].trade_date)
    position = PositionState(
        ts_code="000001.SZ",
        lifecycle_state=LifecycleState.HOLDING,
        shares=1000,
        available_shares=1000,
        avg_cost=10,
        current_position_pct=0.10,
        stop_loss=bars[-1].close * 1.01,
    )
    result = build_score_evidence(
        bars,
        features,
        position,
        MarketSnapshot(bars[-1].trade_date, 50),
        max_position_pct=0.10,
    )
    aggregated = aggregate_scores(result.score_evidence)

    assert result.score_evidence.sell.stop == 100
    assert result.score_evidence.hard_exit_reasons == ("EXIT_STOP_LOSS",)
    assert aggregated.sell_score == 100


def test_nonmatching_market_snapshot_date_is_rejected() -> None:
    bars = _bars()
    features = build_strategy_features(bars, bars[-1].trade_date)
    with pytest.raises(ValueError, match="must equal"):
        build_score_evidence(
            bars,
            features,
            PositionState(ts_code="000001.SZ"),
            MarketSnapshot("20270101", 50),
            max_position_pct=0.10,
        )


def test_short_history_remains_a_hard_entry_veto_after_mapping() -> None:
    bars = _bars(30)
    features = build_strategy_features(bars, bars[-1].trade_date)
    result = build_score_evidence(
        bars,
        features,
        PositionState(ts_code="000001.SZ"),
        MarketSnapshot(bars[-1].trade_date, 50),
        max_position_pct=0.10,
    )
    aggregated = aggregate_scores(result.score_evidence)

    assert "INSUFFICIENT_HISTORY" in result.score_evidence.hard_vetoes
    assert aggregated.buy_score == 0
