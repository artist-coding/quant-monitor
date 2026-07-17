"""Tests for the isolated buy-score event study."""

from datetime import date, timedelta

import pytest

from modules.daily_portfolio.buy_backtest import (
    BuyBacktestConfig,
    BuyBacktestEvent,
    BuyEventStatus,
    BuyForwardOutcome,
    SampleEvidenceStatus,
    backtest_buy_points,
    summarize_buy_horizon,
)
from modules.daily_portfolio.buy_points import BuyPointAssessment, BuyPointStatus
from modules.daily_portfolio.evidence_adapter import MarketSnapshot
from modules.daily_portfolio.execution_model import ExecutionConfig
from modules.indicators import DailyData


TS_CODE = "000001.SZ"


def _bars(count: int = 150, *, signal_index: int = 129) -> list[DailyData]:
    result: list[DailyData] = []
    close = 10.0
    for index in range(count):
        previous = close
        pct = -0.015 if index in (signal_index - 1, signal_index) else 0.003
        close = previous * (1 + pct)
        volume = 1_000_000 + index * 1_000
        if index == signal_index:
            volume = 400_000
        result.append(
            DailyData(
                ts_code=TS_CODE,
                trade_date=(date(2025, 1, 1) + timedelta(days=index)).strftime("%Y%m%d"),
                open=previous,
                high=max(previous, close) * 1.005,
                low=min(previous, close) * 0.995,
                close=close,
                vol=volume,
                amount=volume * close,
                pct_chg=pct * 100,
                prev_close=previous,
            )
        )
    return result


def _market(trade_date: str) -> MarketSnapshot:
    return MarketSnapshot(trade_date, 100, source_hash="explicit-test-market")


def test_real_score_closes_on_d_and_executes_selected_buy_at_d_plus_1_open() -> None:
    bars = _bars()
    signal_date = bars[129].trade_date
    result = backtest_buy_points(
        bars,
        analysis_start=signal_date,
        analysis_end=signal_date,
        trading_dates=[bar.trade_date for bar in bars],
        market_provider=_market,
        execution_config=ExecutionConfig(apply_price_limits=False),
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert event.status == BuyEventStatus.EXECUTED_SELECTED
    assert event.assessment.status == BuyPointStatus.CONFIRMED
    assert event.fill is not None
    assert event.fill.signal_date == signal_date
    assert event.fill.execution_date == bars[130].trade_date
    assert event.fill.raw_price == bars[130].open
    assert event.fill.lookahead_flag is False
    assert set(event.raw_buy_components) == {
        "entry_structure",
        "trend",
        "volume",
        "pattern_quality",
        "stage",
        "market",
        "resonance",
    }
    assert "risk_penalty" in event.buy_contributions
    assert event.outcomes[-1].horizon_bars == 20
    assert event.outcomes[-1].complete is True
    assert event.outcomes[-1].terminal_date == bars[149].trade_date
    assert event.outcomes[-1].net_return is not None
    assert result.setup_candidate_metrics[-1].sample_count == 1
    assert result.selected_metrics[-1].sample_count == 1
    assert result.selected_metrics[-1].evidence_status == SampleEvidenceStatus.INCONCLUSIVE
    assert "b1.loose_3of4" in {item.variant for item in result.variant_metrics}
    assert result.bar_data_fingerprint
    assert result.calendar_fingerprint
    assert result.market_context_fingerprint
    assert result.research_config_fingerprint
    assert result.confirmation_policy_version == "buy-confirmation-policy-v0.2"
    assert result.confirmation_policy_fingerprint
    assert result.feature_versions == ("daily-strategy-features-v0.1",)


def test_high_context_day_without_matched_setup_is_recorded_but_not_executed() -> None:
    bars = _bars()
    signal_date = bars[127].trade_date
    result = backtest_buy_points(
        bars,
        analysis_start=signal_date,
        analysis_end=signal_date,
        trading_dates=[bar.trade_date for bar in bars],
        market_provider=_market,
        execution_config=ExecutionConfig(apply_price_limits=False),
    )

    event = result.events[0]
    assert event.status == BuyEventStatus.NOT_A_SETUP
    assert event.fill is None
    assert event.outcomes == ()


def test_exchange_d_plus_1_without_a_stock_bar_is_not_silently_skipped() -> None:
    bars = _bars()
    signal_date = bars[129].trade_date
    full_calendar = [bar.trade_date for bar in bars]
    bars_without_d_plus_1 = bars[:130] + bars[131:]

    result = backtest_buy_points(
        bars_without_d_plus_1,
        analysis_start=signal_date,
        analysis_end=signal_date,
        trading_dates=full_calendar,
        market_provider=_market,
        execution_config=ExecutionConfig(apply_price_limits=False),
    )

    assert result.events[0].status == BuyEventStatus.NO_EXECUTION_BAR
    assert result.events[0].execution_date == full_calendar[130]
    assert result.events[0].fill is None


def test_t1_disabled_does_not_require_a_calendar_session_after_d_plus_1() -> None:
    bars = _bars(count=131)
    signal_date = bars[129].trade_date
    result = backtest_buy_points(
        bars,
        analysis_start=signal_date,
        analysis_end=signal_date,
        trading_dates=[bar.trade_date for bar in bars],
        market_provider=_market,
        execution_config=ExecutionConfig(
            apply_price_limits=False,
            t1_enabled=False,
        ),
        backtest_config=BuyBacktestConfig(
            horizons=(1,),
            independent_horizon=1,
        ),
    )

    assert result.events[0].status == BuyEventStatus.EXECUTED_SELECTED
    assert result.events[0].outcomes[0].complete is True


def _assessment(signal_date: str) -> BuyPointAssessment:
    return BuyPointAssessment(
        ts_code=TS_CODE,
        signal_date=signal_date,
        status=BuyPointStatus.CONFIRMED,
        confirmed=True,
        setup_matched=True,
        confirmation_setup_matched=True,
        buy_score=80,
        sell_score=10,
        confirmation_threshold=75,
        reference_close=10,
        planned_stop_loss=9,
        estimated_risk_pct=0.1,
        primary_variant="b1.quality_confirmed",
        matched_variants=("b1.quality_confirmed",),
        confirming_variants=("b1.quality_confirmed",),
        variant_strengths={"b1.quality_confirmed": 100},
        primary_confirming_variant="b1.quality_confirmed",
        blocking_reasons=(),
    )


def _metric_event(index: int, r_multiple: float) -> BuyBacktestEvent:
    signal_date = f"202601{index + 1:02d}"
    return BuyBacktestEvent(
        signal_date=signal_date,
        buy_score=80,
        sell_score=10,
        assessment=_assessment(signal_date),
        status=BuyEventStatus.EXECUTED_SELECTED,
        policy_selected=True,
        execution_date=signal_date,
        execution_reason="",
        fill=None,
        outcomes=(
            BuyForwardOutcome(
                horizon_bars=10,
                complete=True,
                observed_bars=10,
                terminal_date=signal_date,
                gross_return=r_multiple / 10,
                net_return=r_multiple / 10,
                fixed_horizon_r=r_multiple,
                max_favorable_return=max(0, r_multiple / 10),
                max_adverse_return=min(0, r_multiple / 10),
                stop_touched=r_multiple < 0,
                first_stop_touch_date=signal_date if r_multiple < 0 else "",
            ),
        ),
        raw_buy_components={},
        buy_contributions={},
        risk_penalty_points=0,
        matched_variants=("b1.quality_confirmed",),
    )


def test_three_losses_and_two_large_wins_are_positive_expectancy() -> None:
    events = tuple(_metric_event(index, r_multiple) for index, r_multiple in enumerate((-1, -1, -1, 3, 3)))

    metrics = summarize_buy_horizon(events, 10, minimum_sample=5, independent_sample=True)

    assert metrics.sample_count == 5
    assert metrics.win_rate == pytest.approx(0.4)
    assert metrics.expectancy_r == pytest.approx(0.6)
    assert metrics.profit_factor == pytest.approx(2.0)
    assert metrics.evidence_status == SampleEvidenceStatus.RESEARCH_CANDIDATE


def test_enough_observations_with_non_positive_expectancy_are_not_candidates() -> None:
    events = tuple(_metric_event(index, r_multiple) for index, r_multiple in enumerate((-1, -1, -1, 1, 1)))

    metrics = summarize_buy_horizon(events, 10, minimum_sample=5, independent_sample=True)

    assert metrics.expectancy_r == pytest.approx(-0.2)
    assert metrics.evidence_status == SampleEvidenceStatus.OBSERVED_NON_POSITIVE


def test_overlapping_daily_events_never_upgrade_to_research_candidate() -> None:
    events = tuple(_metric_event(index, 1) for index in range(5))

    metrics = summarize_buy_horizon(events, 10, minimum_sample=5)

    assert metrics.independent_sample is False
    assert metrics.evidence_status == SampleEvidenceStatus.OVERLAPPING_DIAGNOSTIC


def test_research_configuration_changes_experiment_fingerprint() -> None:
    baseline = BuyBacktestConfig()
    changed = BuyBacktestConfig(standardized_equity=2_000_000)

    assert baseline.canonical_payload()["standardized_equity"] == 1_000_000
    assert baseline.canonical_fingerprint != changed.canonical_fingerprint


def test_future_window_shortage_is_censored_not_filled_with_zero_return() -> None:
    bars = _bars(count=135, signal_index=129)
    signal_date = bars[129].trade_date
    result = backtest_buy_points(
        bars,
        analysis_start=signal_date,
        analysis_end=signal_date,
        trading_dates=[bar.trade_date for bar in bars],
        market_provider=_market,
        execution_config=ExecutionConfig(apply_price_limits=False),
    )

    event = result.events[0]
    horizon_20 = next(item for item in event.outcomes if item.horizon_bars == 20)
    assert horizon_20.complete is False
    assert horizon_20.observed_bars == 5
    assert horizon_20.net_return is None
    assert result.selected_metrics[-1].sample_count == 0
    assert result.selected_metrics[-1].eligible_event_count == 1
    assert result.selected_metrics[-1].censored_count == 1
