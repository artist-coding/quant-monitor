"""End-to-end tests for the single-stock daily scoring service."""

from datetime import date, timedelta

import pytest

from modules.daily_portfolio import (
    DailyPortfolioConfig,
    LifecycleState,
    MarketSnapshot,
    PositionState,
    TradeAction,
)
from modules.daily_portfolio.service import evaluate_daily_bar, score_daily_bar
from modules.indicators import DailyData


def _bars(count: int = 125) -> list[DailyData]:
    bars = []
    close = 10.0
    for index in range(count):
        previous = close
        close *= 1 + 0.001 + ((index % 5) - 2) * 0.0005
        volume = 1_000_000 + (index % 8) * 20_000
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


def test_daily_score_is_deterministic_and_versioned() -> None:
    bars = _bars()
    position = PositionState(ts_code="000001.SZ")
    market = MarketSnapshot(bars[-1].trade_date, 55, source_hash="same-input")
    config = DailyPortfolioConfig(
        strategy_version="test-strategy",
        parameter_version="test-parameters",
    )

    first = score_daily_bar(
        "000001.SZ",
        bars[-1].trade_date,
        bars,
        position,
        market,
        max_position_pct=0.10,
        config=config,
    )
    second = score_daily_bar(
        "000001.SZ",
        bars[-1].trade_date,
        list(bars),
        position,
        market,
        max_position_pct=0.10,
        config=config,
    )

    assert first == second
    assert first.last_bar_date == bars[-1].trade_date
    assert first.strategy_version == "test-strategy"
    assert first.parameter_version == "test-parameters"
    assert first.parameter_fingerprint == config.fingerprint()
    assert first.stop_loss == min(bar.low for bar in bars[-20:])


def test_hard_stop_flows_through_scoring_policy_to_exit() -> None:
    bars = _bars()
    position = PositionState(
        ts_code="000001.SZ",
        lifecycle_state=LifecycleState.HOLDING,
        shares=1000,
        available_shares=1000,
        avg_cost=10,
        current_position_pct=0.10,
        stop_loss=bars[-1].close * 1.01,
    )
    result = evaluate_daily_bar(
        "000001.SZ",
        bars[-1].trade_date,
        bars,
        position,
        MarketSnapshot(bars[-1].trade_date, 50),
        max_position_pct=0.10,
    )

    assert result.score.sell_score == 100
    assert result.score.desired_action == TradeAction.EXIT
    assert result.score.hard_exit_reasons == ("EXIT_STOP_LOSS",)


def test_held_position_without_persisted_stop_fails_closed() -> None:
    bars = _bars()
    position = PositionState(
        ts_code="000001.SZ",
        lifecycle_state=LifecycleState.HOLDING,
        shares=1000,
        available_shares=1000,
        avg_cost=10,
        current_position_pct=0.10,
    )

    with pytest.raises(ValueError, match="persisted stop_loss"):
        evaluate_daily_bar(
            "000001.SZ",
            bars[-1].trade_date,
            bars,
            position,
            MarketSnapshot(bars[-1].trade_date, 50),
            max_position_pct=0.10,
        )


def test_service_rejects_a_future_bar() -> None:
    bars = _bars()
    with pytest.raises(ValueError, match="future bar"):
        score_daily_bar(
            "000001.SZ",
            bars[-2].trade_date,
            bars,
            PositionState(ts_code="000001.SZ"),
            MarketSnapshot(bars[-2].trade_date, 50),
            max_position_pct=0.10,
        )


def test_stale_last_bar_produces_a_blocked_non_buy_snapshot() -> None:
    bars = _bars()
    next_date = (date(2026, 1, 1) + timedelta(days=len(bars))).strftime("%Y%m%d")
    result = score_daily_bar(
        "000001.SZ",
        next_date,
        bars,
        PositionState(ts_code="000001.SZ"),
        MarketSnapshot(next_date, 50),
        max_position_pct=0.10,
    )

    assert result.desired_action == TradeAction.BLOCK
    assert result.target_position_pct == 0
    assert "STALE_BAR" in result.vetoes
