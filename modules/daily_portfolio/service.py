"""Public orchestration API for one stock's confirmed daily score."""

from __future__ import annotations

from dataclasses import dataclass

from ..indicators import DailyData
from .config import DailyPortfolioConfig
from .buy_points import BuyPointAssessment, assess_buy_point
from .evidence_adapter import (
    EvidenceAdapterResult,
    MarketSnapshot,
    build_score_evidence,
)
from .models import DailyStockScore, PositionState
from .position_policy import (
    PositionPolicyDecision,
    PositionPolicyInput,
    evaluate_position_policy,
)
from .score_engine import AggregatedScores, aggregate_scores
from .strategy_features import DailyStrategyFeatures, build_strategy_features


@dataclass(frozen=True)
class DailyScoreEvaluation:
    score: DailyStockScore
    position_snapshot: PositionState
    market_context: MarketSnapshot
    features: DailyStrategyFeatures
    adapted_evidence: EvidenceAdapterResult
    aggregated_scores: AggregatedScores
    buy_point: BuyPointAssessment
    policy: PositionPolicyDecision


def evaluate_daily_bar(
    ts_code: str,
    as_of_date: str,
    bars: list[DailyData] | tuple[DailyData, ...],
    position: PositionState,
    market_context: MarketSnapshot,
    *,
    max_position_pct: float,
    config: DailyPortfolioConfig | None = None,
) -> DailyScoreEvaluation:
    """Run the single source of truth for daily quant scoring and policy."""

    resolved_config = config or DailyPortfolioConfig()
    if position.ts_code != ts_code:
        raise ValueError("position must match ts_code")
    if position.shares > 0 and position.stop_loss is None:
        raise ValueError("a held position requires an explicit persisted stop_loss before scoring")
    if not bars or any(bar.ts_code != ts_code for bar in bars):
        raise ValueError("all bars must match ts_code")

    features = build_strategy_features(bars, as_of_date)
    adapted = build_score_evidence(
        bars,
        features,
        position,
        market_context,
        max_position_pct=max_position_pct,
    )
    aggregated = aggregate_scores(adapted.score_evidence, resolved_config.score_weights)
    stop_loss = position.stop_loss
    if stop_loss is None:  # Flat position: initialize the next entry's stop.
        lookback = min(len(bars), resolved_config.entry_stop_lookback_bars)
        stop_loss = min(bar.low for bar in bars[-lookback:])
    buy_point = assess_buy_point(
        features,
        aggregated,
        reference_close=bars[-1].close,
        planned_stop_loss=stop_loss,
        thresholds=resolved_config.thresholds,
        for_add=position.shares > 0,
    )
    policy_input = PositionPolicyInput(
        signal_date=as_of_date,
        last_bar_date=features.bars_end_date,
        buy_score=aggregated.buy_score,
        sell_score=aggregated.sell_score,
        stop_loss=stop_loss,
        reasons=adapted.reasons,
        hard_vetoes=aggregated.hard_vetoes,
        hard_exit_reasons=aggregated.hard_exit_reasons,
        entry_confirmed=buy_point.confirmed,
        entry_confirmation_reasons=buy_point.blocking_reasons,
        buy_contributions=aggregated.buy_contributions,
        sell_contributions=aggregated.sell_contributions,
        strategy_version=resolved_config.strategy_version,
        parameter_version=resolved_config.parameter_version,
        parameter_fingerprint=resolved_config.fingerprint(),
    )
    policy = evaluate_position_policy(
        position,
        policy_input,
        max_position_pct=max_position_pct,
        thresholds=resolved_config.thresholds,
        position_ladder=resolved_config.position_ladder,
        minimum_position_delta_pct=resolved_config.minimum_position_delta_pct,
    )
    return DailyScoreEvaluation(
        score=policy.daily_score,
        position_snapshot=position,
        market_context=market_context,
        features=features,
        adapted_evidence=adapted,
        aggregated_scores=aggregated,
        buy_point=buy_point,
        policy=policy,
    )


def score_daily_bar(
    ts_code: str,
    as_of_date: str,
    bars: list[DailyData] | tuple[DailyData, ...],
    position: PositionState,
    market_context: MarketSnapshot,
    *,
    max_position_pct: float,
    config: DailyPortfolioConfig | None = None,
) -> DailyStockScore:
    """Return only the stable score contract used by replay and daily jobs."""

    return evaluate_daily_bar(
        ts_code,
        as_of_date,
        bars,
        position,
        market_context,
        max_position_pct=max_position_pct,
        config=config,
    ).score
