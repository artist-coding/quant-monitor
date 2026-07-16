"""As-of-safe event study for the daily buy score.

This module intentionally isolates entry quality from the still-uncalibrated
sell score.  Each signal-day close is scored from its historical prefix, an
eligible setup is priced at the next exchange session's raw open, and its
future path is labelled at fixed stock-trading-bar horizons.  Those labels are
research outcomes, never inputs to the historical decision.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import math
from statistics import median

from ..indicators import DailyData
from .buy_points import (
    BuyPointAssessment,
    BuyPointStatus,
    CONFIRMATION_POLICY_VERSION,
    CONFIRMING_ENTRY_VARIANT_PRIORITY,
    RESEARCH_ENTRY_VARIANT_PRIORITY,
)
from .config import DailyPortfolioConfig
from .dates import normalize_trade_date
from .evidence_adapter import MarketSnapshot
from .execution import create_buy_order
from .execution_model import (
    ExecutionConfig,
    ExecutionStatus,
    TradeFill,
    apply_execution_slippage,
    calculate_execution_costs,
    execute_target_order,
)
from .models import OrderSide, PositionState, TradeAction
from .service import evaluate_daily_bar


MarketProvider = Callable[[str], MarketSnapshot]


@dataclass(frozen=True)
class BuyExecutionQuote:
    ts_code: str
    execution_date: str
    raw_open: float
    previous_close: float | None


IsStProvider = Callable[[BuyExecutionQuote], bool]
TradabilityProvider = Callable[[BuyExecutionQuote], bool]


class BuyEventStatus(str, Enum):
    NOT_A_SETUP = "NOT_A_SETUP"
    BLOCKED = "BLOCKED"
    CONFLICT = "CONFLICT"
    NO_NEXT_SESSION = "NO_NEXT_SESSION"
    NO_EXECUTION_BAR = "NO_EXECUTION_BAR"
    EXECUTION_REJECTED = "EXECUTION_REJECTED"
    EXECUTED_CANDIDATE = "EXECUTED_CANDIDATE"
    EXECUTED_SELECTED = "EXECUTED_SELECTED"


class SampleEvidenceStatus(str, Enum):
    INCONCLUSIVE = "INCONCLUSIVE"
    OVERLAPPING_DIAGNOSTIC = "OVERLAPPING_DIAGNOSTIC"
    OBSERVED_NON_POSITIVE = "OBSERVED_NON_POSITIVE"
    RESEARCH_CANDIDATE = "RESEARCH_CANDIDATE"


@dataclass(frozen=True)
class BuyBacktestConfig:
    horizons: tuple[int, ...] = (1, 3, 5, 10, 20)
    score_bin_edges: tuple[float, ...] = (0, 20, 40, 60, 75, 85, 101)
    standardized_equity: float = 1_000_000.0
    standardized_target_pct: float = 1.0
    independent_horizon: int = 20
    minimum_research_sample: int = 30

    def __post_init__(self) -> None:
        if not self.horizons or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in self.horizons
        ):
            raise ValueError("horizons must contain positive integers")
        if tuple(sorted(set(self.horizons))) != self.horizons:
            raise ValueError("horizons must be unique and strictly increasing")
        if len(self.score_bin_edges) < 2:
            raise ValueError("score_bin_edges require at least two values")
        if tuple(sorted(set(self.score_bin_edges))) != self.score_bin_edges:
            raise ValueError("score_bin_edges must be unique and strictly increasing")
        if self.score_bin_edges[0] > 0 or self.score_bin_edges[-1] <= 100:
            raise ValueError("score bins must cover the full 0..100 score range")
        if not math.isfinite(self.standardized_equity) or self.standardized_equity <= 0:
            raise ValueError("standardized_equity must be finite and positive")
        if not 0 < self.standardized_target_pct <= 1:
            raise ValueError("standardized_target_pct must be in (0, 1]")
        if self.independent_horizon not in self.horizons:
            raise ValueError("independent_horizon must be one of horizons")
        if (
            isinstance(self.minimum_research_sample, bool)
            or not isinstance(self.minimum_research_sample, int)
            or self.minimum_research_sample <= 0
        ):
            raise ValueError("minimum_research_sample must be a positive integer")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "horizons": list(self.horizons),
            "independent_horizon": self.independent_horizon,
            "minimum_research_sample": self.minimum_research_sample,
            "score_bin_edges": list(self.score_bin_edges),
            "standardized_equity": self.standardized_equity,
            "standardized_target_pct": self.standardized_target_pct,
        }

    @property
    def canonical_fingerprint(self) -> str:
        return _fingerprint(self.canonical_payload())


@dataclass(frozen=True)
class BuyForwardOutcome:
    horizon_bars: int
    complete: bool
    observed_bars: int
    terminal_date: str
    gross_return: float | None
    net_return: float | None
    fixed_horizon_r: float | None
    max_favorable_return: float | None
    max_adverse_return: float | None
    stop_touched: bool
    first_stop_touch_date: str


@dataclass(frozen=True)
class BuyBacktestEvent:
    signal_date: str
    buy_score: float
    sell_score: float
    assessment: BuyPointAssessment
    status: BuyEventStatus
    policy_selected: bool
    execution_date: str
    execution_reason: str
    fill: TradeFill | None
    outcomes: tuple[BuyForwardOutcome, ...]
    raw_buy_components: Mapping[str, float]
    buy_contributions: Mapping[str, float]
    risk_penalty_points: float
    matched_variants: tuple[str, ...]


@dataclass(frozen=True)
class BuyHorizonMetrics:
    horizon_bars: int
    independent_sample: bool
    eligible_event_count: int
    censored_count: int
    sample_count: int
    win_count: int
    loss_count: int
    flat_count: int
    win_rate: float | None
    mean_net_return: float | None
    median_net_return: float | None
    mean_mfe: float | None
    mean_mae: float | None
    stop_touch_rate: float | None
    expectancy_r: float | None
    profit_factor: float | None
    evidence_status: SampleEvidenceStatus


@dataclass(frozen=True)
class BuyScoreBucketMetrics:
    lower_inclusive: float
    upper_exclusive: float
    metrics: BuyHorizonMetrics


@dataclass(frozen=True)
class BuyVariantMetrics:
    variant: str
    matched_event_count: int
    confirmation_qualified_count: int
    selected_event_count: int
    executed_event_count: int
    complete_outcome_count: int
    primary_event_count: int
    metrics: BuyHorizonMetrics
    primary_metrics: BuyHorizonMetrics
    primary_independent_metrics: BuyHorizonMetrics


@dataclass(frozen=True)
class BuyBacktestResult:
    ts_code: str
    analysis_start: str
    analysis_end: str
    strategy_version: str
    parameter_version: str
    parameter_fingerprint: str
    execution_config_fingerprint: str
    research_config: Mapping[str, object]
    research_config_fingerprint: str
    confirmation_policy_version: str
    confirmation_policy_fingerprint: str
    feature_versions: tuple[str, ...]
    bar_data_fingerprint: str
    calendar_fingerprint: str
    market_context_fingerprint: str
    horizon_unit: str
    events: tuple[BuyBacktestEvent, ...]
    setup_candidate_metrics: tuple[BuyHorizonMetrics, ...]
    selected_metrics: tuple[BuyHorizonMetrics, ...]
    independent_metrics: BuyHorizonMetrics
    score_buckets: tuple[BuyScoreBucketMetrics, ...]
    variant_metrics: tuple[BuyVariantMetrics, ...]
    execution_assumptions: tuple[str, ...]


def _normalize_inputs(
    bars: Sequence[DailyData],
    trading_dates: Sequence[str],
) -> tuple[tuple[DailyData, ...], tuple[str, ...], dict[str, DailyData], str]:
    if not bars:
        raise ValueError("bars cannot be empty")
    ts_code = bars[0].ts_code
    if not ts_code or any(bar.ts_code != ts_code for bar in bars):
        raise ValueError("all bars must belong to one non-empty ts_code")
    normalized_bar_dates = tuple(normalize_trade_date(bar.trade_date) for bar in bars)
    if len(set(normalized_bar_dates)) != len(normalized_bar_dates):
        raise ValueError("bars contain duplicate trade dates")
    if normalized_bar_dates != tuple(sorted(normalized_bar_dates)):
        raise ValueError("bars must be strictly ascending by trade_date")
    calendar = tuple(normalize_trade_date(value) for value in trading_dates)
    if not calendar:
        raise ValueError("explicit exchange trading_dates are required")
    if len(set(calendar)) != len(calendar):
        raise ValueError("trading_dates contain duplicates")
    if calendar != tuple(sorted(calendar)):
        raise ValueError("trading_dates must be strictly ascending")
    return tuple(bars), calendar, dict(zip(normalized_bar_dates, bars)), ts_code


def _next_session(current: str, calendar: tuple[str, ...]) -> str:
    index = bisect_right(calendar, current)
    return calendar[index] if index < len(calendar) else ""


def _fingerprint(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bar_fingerprint(bars: Sequence[DailyData]) -> str:
    return _fingerprint(
        [
            {
                "amount": bar.amount,
                "close": bar.close,
                "high": bar.high,
                "low": bar.low,
                "open": bar.open,
                "pct_chg": bar.pct_chg,
                "prev_close": bar.prev_close,
                "trade_date": normalize_trade_date(bar.trade_date),
                "ts_code": bar.ts_code,
                "vol": bar.vol,
            }
            for bar in bars
        ]
    )


def _risk_amount_at_stop(
    fill: TradeFill,
    stop_loss: float,
    execution_config: ExecutionConfig,
) -> float:
    stop_fill = apply_execution_slippage(stop_loss, OrderSide.SELL, execution_config)
    stop_gross = stop_fill * fill.shares
    stop_costs = calculate_execution_costs(
        stop_gross, OrderSide.SELL, execution_config
    )
    entry_cash_out = fill.gross_amount + fill.costs.total
    stop_proceeds = stop_gross - stop_costs.total
    return max(0.0, entry_cash_out - stop_proceeds)


def _forward_outcomes(
    bars: tuple[DailyData, ...],
    entry_index: int,
    fill: TradeFill,
    horizons: tuple[int, ...],
    execution_config: ExecutionConfig,
) -> tuple[BuyForwardOutcome, ...]:
    result: list[BuyForwardOutcome] = []
    stop_loss = fill.stop_loss
    risk_amount = (
        _risk_amount_at_stop(fill, stop_loss, execution_config)
        if stop_loss is not None and stop_loss > 0
        else 0.0
    )
    entry_cash_out = fill.gross_amount + fill.costs.total

    for horizon in horizons:
        window = bars[entry_index : entry_index + horizon]
        complete = len(window) == horizon
        first_stop = next(
            (
                normalize_trade_date(bar.trade_date)
                for bar in window
                if stop_loss is not None and bar.low <= stop_loss
            ),
            "",
        )
        if not window:
            result.append(
                BuyForwardOutcome(
                    horizon_bars=horizon,
                    complete=False,
                    observed_bars=0,
                    terminal_date="",
                    gross_return=None,
                    net_return=None,
                    fixed_horizon_r=None,
                    max_favorable_return=None,
                    max_adverse_return=None,
                    stop_touched=False,
                    first_stop_touch_date="",
                )
            )
            continue

        max_favorable = max(0.0, max(bar.high for bar in window) / fill.fill_price - 1)
        max_adverse = min(0.0, min(bar.low for bar in window) / fill.fill_price - 1)
        terminal_date = normalize_trade_date(window[-1].trade_date)
        gross_return = window[-1].close / fill.fill_price - 1 if complete else None
        net_return = None
        fixed_horizon_r = None
        if complete:
            sell_fill = apply_execution_slippage(
                window[-1].close, OrderSide.SELL, execution_config
            )
            sell_gross = sell_fill * fill.shares
            sell_costs = calculate_execution_costs(
                sell_gross, OrderSide.SELL, execution_config
            )
            net_pnl = sell_gross - sell_costs.total - entry_cash_out
            net_return = net_pnl / entry_cash_out
            if risk_amount > 0:
                fixed_horizon_r = net_pnl / risk_amount
        result.append(
            BuyForwardOutcome(
                horizon_bars=horizon,
                complete=complete,
                observed_bars=len(window),
                terminal_date=terminal_date,
                gross_return=(round(gross_return, 10) if gross_return is not None else None),
                net_return=(round(net_return, 10) if net_return is not None else None),
                fixed_horizon_r=(
                    round(fixed_horizon_r, 10)
                    if fixed_horizon_r is not None
                    else None
                ),
                max_favorable_return=round(max_favorable, 10),
                max_adverse_return=round(max_adverse, 10),
                stop_touched=bool(first_stop),
                first_stop_touch_date=first_stop,
            )
        )
    return tuple(result)


def _metrics(
    events: Sequence[BuyBacktestEvent],
    horizon: int,
    minimum_sample: int,
    *,
    independent_sample: bool = False,
) -> BuyHorizonMetrics:
    all_horizon_outcomes = [
        outcome
        for event in events
        for outcome in event.outcomes
        if outcome.horizon_bars == horizon
    ]
    outcomes = [
        outcome
        for outcome in all_horizon_outcomes
        if outcome.complete and outcome.net_return is not None
    ]
    returns = [float(outcome.net_return) for outcome in outcomes]
    r_values = [
        float(outcome.fixed_horizon_r)
        for outcome in outcomes
        if outcome.fixed_horizon_r is not None
    ]
    wins = sum(value > 0 for value in returns)
    losses = sum(value < 0 for value in returns)
    flats = len(returns) - wins - losses
    gross_profit_r = sum(value for value in r_values if value > 0)
    gross_loss_r = -sum(value for value in r_values if value < 0)
    expectancy_r = sum(r_values) / len(r_values) if r_values else None
    if len(returns) < minimum_sample or expectancy_r is None:
        evidence_status = SampleEvidenceStatus.INCONCLUSIVE
    elif not independent_sample:
        evidence_status = SampleEvidenceStatus.OVERLAPPING_DIAGNOSTIC
    elif expectancy_r > 0:
        evidence_status = SampleEvidenceStatus.RESEARCH_CANDIDATE
    else:
        evidence_status = SampleEvidenceStatus.OBSERVED_NON_POSITIVE
    return BuyHorizonMetrics(
        horizon_bars=horizon,
        independent_sample=independent_sample,
        eligible_event_count=len(all_horizon_outcomes),
        censored_count=sum(not item.complete for item in all_horizon_outcomes),
        sample_count=len(returns),
        win_count=wins,
        loss_count=losses,
        flat_count=flats,
        win_rate=(wins / len(returns) if returns else None),
        mean_net_return=(sum(returns) / len(returns) if returns else None),
        median_net_return=(median(returns) if returns else None),
        mean_mfe=(
            sum(float(item.max_favorable_return) for item in outcomes) / len(outcomes)
            if outcomes
            else None
        ),
        mean_mae=(
            sum(float(item.max_adverse_return) for item in outcomes) / len(outcomes)
            if outcomes
            else None
        ),
        stop_touch_rate=(
            sum(item.stop_touched for item in outcomes) / len(outcomes)
            if outcomes
            else None
        ),
        expectancy_r=expectancy_r,
        profit_factor=(
            gross_profit_r / gross_loss_r if gross_loss_r > 0 else None
        ),
        evidence_status=evidence_status,
    )


def summarize_buy_horizon(
    events: Sequence[BuyBacktestEvent],
    horizon: int,
    *,
    minimum_sample: int = 30,
    independent_sample: bool = False,
) -> BuyHorizonMetrics:
    """Public deterministic summary for one fixed-horizon event sample."""

    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise ValueError("horizon must be a positive integer")
    if (
        isinstance(minimum_sample, bool)
        or not isinstance(minimum_sample, int)
        or minimum_sample <= 0
    ):
        raise ValueError("minimum_sample must be a positive integer")
    return _metrics(
        events,
        horizon,
        minimum_sample,
        independent_sample=independent_sample,
    )


def _non_overlapping_events(
    events: Sequence[BuyBacktestEvent],
    bars_by_date_index: Mapping[str, int],
    horizon: int,
) -> tuple[BuyBacktestEvent, ...]:
    selected: list[BuyBacktestEvent] = []
    next_eligible_index = -1
    for event in events:
        if not event.fill:
            continue
        entry_index = bars_by_date_index[event.fill.execution_date]
        if entry_index < next_eligible_index:
            continue
        selected.append(event)
        next_eligible_index = entry_index + horizon
    return tuple(selected)


def backtest_buy_points(
    bars: Sequence[DailyData],
    *,
    analysis_start: str,
    analysis_end: str,
    trading_dates: Sequence[str],
    market_provider: MarketProvider,
    score_config: DailyPortfolioConfig | None = None,
    execution_config: ExecutionConfig | None = None,
    backtest_config: BuyBacktestConfig | None = None,
    is_st_provider: IsStProvider | None = None,
    tradability_provider: TradabilityProvider | None = None,
) -> BuyBacktestResult:
    """Score each D prefix and label matched setups from the D+1 raw open."""

    ordered_bars, calendar, bars_by_date, ts_code = _normalize_inputs(
        bars, trading_dates
    )
    start = normalize_trade_date(analysis_start)
    end = normalize_trade_date(analysis_end)
    if start > end:
        raise ValueError("analysis_start cannot be after analysis_end")
    config = score_config or DailyPortfolioConfig()
    execution = execution_config or ExecutionConfig()
    research = backtest_config or BuyBacktestConfig()
    bar_indices = {
        normalize_trade_date(bar.trade_date): index
        for index, bar in enumerate(ordered_bars)
    }
    signal_dates = tuple(
        normalize_trade_date(bar.trade_date)
        for bar in ordered_bars
        if start <= normalize_trade_date(bar.trade_date) <= end
    )
    if not signal_dates:
        raise ValueError("analysis range contains no stock bars")
    missing_calendar_dates = sorted(set(signal_dates).difference(calendar))
    if missing_calendar_dates:
        raise ValueError(
            f"trading_dates do not contain signal dates: {missing_calendar_dates}"
        )

    events: list[BuyBacktestEvent] = []
    used_market_contexts: list[dict[str, object]] = []
    used_feature_versions: set[str] = set()
    for signal_date in signal_dates:
        signal_index = bar_indices[signal_date]
        prefix = ordered_bars[: signal_index + 1]
        market = market_provider(signal_date)
        if not isinstance(market, MarketSnapshot):
            raise TypeError("market_provider must return MarketSnapshot")
        used_market_contexts.append(
            {
                "score": market.score,
                "source_hash": market.source_hash,
                "trade_date": market.trade_date,
                "version": market.version,
            }
        )
        evaluation = evaluate_daily_bar(
            ts_code,
            signal_date,
            prefix,
            PositionState(ts_code=ts_code),
            market,
            max_position_pct=1.0,
            config=config,
        )
        assessment = evaluation.buy_point
        used_feature_versions.add(evaluation.features.feature_version)
        common = {
            "signal_date": signal_date,
            "buy_score": evaluation.score.buy_score,
            "sell_score": evaluation.score.sell_score,
            "assessment": assessment,
            "policy_selected": (
                evaluation.score.desired_action == TradeAction.OPEN
            ),
            "raw_buy_components": (
                evaluation.adapted_evidence.score_evidence.buy.as_mapping()
            ),
            "buy_contributions": dict(evaluation.score.buy_contributions),
            "risk_penalty_points": (
                evaluation.adapted_evidence.score_evidence.risk_penalty_points
            ),
            "matched_variants": assessment.matched_variants,
        }
        if assessment.status == BuyPointStatus.BLOCKED:
            events.append(
                BuyBacktestEvent(
                    **common,
                    status=BuyEventStatus.BLOCKED,
                    execution_date="",
                    execution_reason=";".join(assessment.blocking_reasons),
                    fill=None,
                    outcomes=(),
                )
            )
            continue
        if not assessment.setup_matched:
            events.append(
                BuyBacktestEvent(
                    **common,
                    status=BuyEventStatus.NOT_A_SETUP,
                    execution_date="",
                    execution_reason="NO_CONFIRMED_ENTRY_VARIANT",
                    fill=None,
                    outcomes=(),
                )
            )
            continue
        if assessment.status == BuyPointStatus.CONFLICT:
            events.append(
                BuyBacktestEvent(
                    **common,
                    status=BuyEventStatus.CONFLICT,
                    execution_date="",
                    execution_reason="BUY_SELL_SCORE_CONFLICT",
                    fill=None,
                    outcomes=(),
                )
            )
            continue

        execution_date = _next_session(signal_date, calendar)
        following_date = (
            _next_session(execution_date, calendar)
            if execution_date and execution.t1_enabled
            else ""
        )
        if not execution_date or (execution.t1_enabled and not following_date):
            events.append(
                BuyBacktestEvent(
                    **common,
                    status=BuyEventStatus.NO_NEXT_SESSION,
                    execution_date=execution_date,
                    execution_reason=(
                        "calendar lacks D+1 or its T+1 following session"
                    ),
                    fill=None,
                    outcomes=(),
                )
            )
            continue
        execution_bar = bars_by_date.get(execution_date)
        if execution_bar is None:
            events.append(
                BuyBacktestEvent(
                    **common,
                    status=BuyEventStatus.NO_EXECUTION_BAR,
                    execution_date=execution_date,
                    execution_reason="stock has no D+1 execution bar",
                    fill=None,
                    outcomes=(),
                )
            )
            continue

        # Candidate events below the production threshold are deliberately
        # priced with a standardized hypothetical order so score bins can be
        # compared without changing the actual production policy.
        virtual_score = replace(
            evaluation.score,
            desired_action=TradeAction.OPEN,
            target_position_pct=research.standardized_target_pct,
            position_score=research.standardized_target_pct * 100,
            current_position_pct=0.0,
            vetoes=(),
            hard_exit_reasons=(),
        )
        order = create_buy_order(virtual_score, execution_date)
        previous_close = (
            execution_bar.prev_close
            if execution_bar.prev_close > 0
            else ordered_bars[signal_index].close
        )
        quote = BuyExecutionQuote(
            ts_code=ts_code,
            execution_date=execution_date,
            raw_open=execution_bar.open,
            previous_close=previous_close,
        )
        executed = execute_target_order(
            order,
            execution_bar,
            PositionState(ts_code=ts_code),
            cash=research.standardized_equity,
            equity=research.standardized_equity,
            previous_close=previous_close,
            config=execution,
            is_st=is_st_provider(quote) if is_st_provider else False,
            tradable_at_execution=(
                tradability_provider(quote) if tradability_provider else True
            ),
            next_trading_date=following_date,
        )
        if executed.status not in (ExecutionStatus.FILLED, ExecutionStatus.PARTIAL):
            events.append(
                BuyBacktestEvent(
                    **common,
                    status=BuyEventStatus.EXECUTION_REJECTED,
                    execution_date=execution_date,
                    execution_reason=executed.reason,
                    fill=None,
                    outcomes=(),
                )
            )
            continue
        if executed.fill is None:
            raise RuntimeError("filled buy execution must contain a fill")
        entry_index = bar_indices[execution_date]
        outcomes = _forward_outcomes(
            ordered_bars,
            entry_index,
            executed.fill,
            research.horizons,
            execution,
        )
        events.append(
            BuyBacktestEvent(
                **common,
                status=(
                    BuyEventStatus.EXECUTED_SELECTED
                    if common["policy_selected"]
                    else BuyEventStatus.EXECUTED_CANDIDATE
                ),
                execution_date=execution_date,
                execution_reason=executed.reason,
                fill=executed.fill,
                outcomes=outcomes,
            )
        )

    executed_events = tuple(event for event in events if event.fill is not None)
    selected_events = tuple(
        event
        for event in executed_events
        if event.status == BuyEventStatus.EXECUTED_SELECTED
    )
    setup_candidate_metrics = tuple(
        _metrics(executed_events, horizon, research.minimum_research_sample)
        for horizon in research.horizons
    )
    selected_metrics = tuple(
        _metrics(selected_events, horizon, research.minimum_research_sample)
        for horizon in research.horizons
    )
    independent = _non_overlapping_events(
        selected_events, bar_indices, research.independent_horizon
    )
    independent_metrics = _metrics(
        independent,
        research.independent_horizon,
        research.minimum_research_sample,
        independent_sample=True,
    )

    score_buckets: list[BuyScoreBucketMetrics] = []
    for lower, upper in zip(
        research.score_bin_edges[:-1], research.score_bin_edges[1:]
    ):
        bucket_events = tuple(
            event for event in executed_events if lower <= event.buy_score < upper
        )
        score_buckets.append(
            BuyScoreBucketMetrics(
                lower_inclusive=lower,
                upper_exclusive=upper,
                metrics=_metrics(
                    bucket_events,
                    research.independent_horizon,
                    research.minimum_research_sample,
                ),
            )
        )

    variants = sorted({variant for event in events for variant in event.matched_variants})
    variant_metric_items: list[BuyVariantMetrics] = []
    for variant in variants:
        matched_events = tuple(
            event for event in events if variant in event.matched_variants
        )
        executed_variant_events = tuple(
            event for event in matched_events if event.fill is not None
        )
        primary_events = tuple(
            event
            for event in matched_events
            if event.assessment.primary_variant == variant
        )
        primary_executed_events = tuple(
            event for event in primary_events if event.fill is not None
        )
        primary_independent = _non_overlapping_events(
            primary_executed_events,
            bar_indices,
            research.independent_horizon,
        )
        complete_count = sum(
            outcome.complete
            for event in executed_variant_events
            for outcome in event.outcomes
            if outcome.horizon_bars == research.independent_horizon
        )
        variant_metric_items.append(
            BuyVariantMetrics(
                variant=variant,
                matched_event_count=len(matched_events),
                confirmation_qualified_count=sum(
                    variant in event.assessment.confirming_variants
                    for event in matched_events
                ),
                selected_event_count=sum(
                    event.policy_selected for event in matched_events
                ),
                executed_event_count=len(executed_variant_events),
                complete_outcome_count=complete_count,
                primary_event_count=len(primary_events),
                metrics=_metrics(
                    executed_variant_events,
                    research.independent_horizon,
                    research.minimum_research_sample,
                ),
                primary_metrics=_metrics(
                    primary_executed_events,
                    research.independent_horizon,
                    research.minimum_research_sample,
                ),
                primary_independent_metrics=_metrics(
                    primary_independent,
                    research.independent_horizon,
                    research.minimum_research_sample,
                    independent_sample=True,
                ),
            )
        )
    variant_metrics = tuple(variant_metric_items)
    assumptions: list[str] = []
    if is_st_provider is None:
        assumptions.append("NO_POINT_IN_TIME_ST_PROVIDER_ASSUMED_NOT_ST")
    if tradability_provider is None:
        assumptions.append("NO_INTRADAY_TRADABILITY_PROVIDER_ASSUMED_OPEN_TRADABLE")
    assumptions.append("FORWARD_HORIZONS_COUNT_STOCK_TRADING_BARS")
    assumptions.append("FIXED_HORIZON_EXIT_IS_A_RESEARCH_LABEL_NOT_A_SELL_SIGNAL")
    assumptions.append("STOP_TOUCH_IS_A_PATH_LABEL_NOT_AN_EXECUTED_INTRADAY_STOP")
    assumptions.append("SINGLE_DAILYDATA_SERIES_DUAL_PRICE_BINDING_NOT_YET_VERIFIED")
    assumptions.append("PRICE_LIMIT_RULES_ARE_CODE_INFERRED_NOT_DATE_VERSIONED")

    return BuyBacktestResult(
        ts_code=ts_code,
        analysis_start=start,
        analysis_end=end,
        strategy_version=config.strategy_version,
        parameter_version=config.parameter_version,
        parameter_fingerprint=config.fingerprint(),
        execution_config_fingerprint=execution.canonical_fingerprint,
        research_config=research.canonical_payload(),
        research_config_fingerprint=research.canonical_fingerprint,
        confirmation_policy_version=CONFIRMATION_POLICY_VERSION,
        confirmation_policy_fingerprint=_fingerprint(
            {
                "confirmation_policy_version": CONFIRMATION_POLICY_VERSION,
                "confirming_variants": list(CONFIRMING_ENTRY_VARIANT_PRIORITY),
                "research_variants": list(RESEARCH_ENTRY_VARIANT_PRIORITY),
            }
        ),
        feature_versions=tuple(sorted(used_feature_versions)),
        bar_data_fingerprint=_bar_fingerprint(ordered_bars),
        calendar_fingerprint=_fingerprint(list(calendar)),
        market_context_fingerprint=_fingerprint(used_market_contexts),
        horizon_unit="STOCK_TRADING_BAR_FROM_D_PLUS_1_INCLUSIVE",
        events=tuple(events),
        setup_candidate_metrics=setup_candidate_metrics,
        selected_metrics=selected_metrics,
        independent_metrics=independent_metrics,
        score_buckets=tuple(score_buckets),
        variant_metrics=variant_metrics,
        execution_assumptions=tuple(assumptions),
    )


__all__ = [
    "BuyBacktestConfig",
    "BuyBacktestEvent",
    "BuyBacktestResult",
    "BuyEventStatus",
    "BuyExecutionQuote",
    "BuyForwardOutcome",
    "BuyHorizonMetrics",
    "BuyScoreBucketMetrics",
    "BuyVariantMetrics",
    "SampleEvidenceStatus",
    "backtest_buy_points",
    "summarize_buy_horizon",
]
