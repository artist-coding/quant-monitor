"""Deterministic aggregation of normalized daily evidence.

This module deliberately does not fetch bars or call strategy detectors.  It is
the single arithmetic contract shared by historical replay, daily scoring and
future watch-pool processing.  Upstream adapters must turn legacy indicators
into explicit 0..100 component scores before calling it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields

from .config import ScoreWeights


def _validate_component(name: str, value: float) -> None:
    if not 0 <= value <= 100:
        raise ValueError(f"{name} must be between 0 and 100, got {value}")


@dataclass(frozen=True)
class BuyComponents:
    entry_structure: float
    trend: float
    volume: float
    pattern_quality: float
    stage: float
    market: float
    resonance: float

    def __post_init__(self) -> None:
        for item in fields(self):
            _validate_component(item.name, getattr(self, item.name))

    def as_mapping(self) -> dict[str, float]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(frozen=True)
class SellComponents:
    stop: float
    exit_signal: float
    trend_break: float
    distribution: float
    market_risk: float
    position_heat: float
    # The design reserves this evidence dimension, but v0.1 gives it zero
    # default weight until its calibration has been validated independently.
    profit_protection: float = 0.0

    def __post_init__(self) -> None:
        for item in fields(self):
            _validate_component(item.name, getattr(self, item.name))

    def as_mapping(self) -> dict[str, float]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(frozen=True)
class ScoreEvidence:
    buy: BuyComponents
    sell: SellComponents
    risk_penalty_points: float = 0.0
    hard_vetoes: tuple[str, ...] = ()
    hard_exit_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_component("risk_penalty_points", self.risk_penalty_points)


@dataclass(frozen=True)
class AggregatedScores:
    buy_score: float
    sell_score: float
    buy_contributions: dict[str, float]
    sell_contributions: dict[str, float]
    hard_vetoes: tuple[str, ...]
    hard_exit_reasons: tuple[str, ...]


def _weighted_contributions(components: Mapping[str, float], weights: Mapping[str, float]) -> dict[str, float]:
    missing = set(weights) - set(components)
    unexpected = set(components) - set(weights)
    if missing or unexpected:
        raise ValueError(
            f"score component/weight contract mismatch: missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )
    return {name: round(weights[name] * components[name] / 100.0, 6) for name in weights}


def aggregate_scores(
    evidence: ScoreEvidence,
    weights: ScoreWeights | None = None,
) -> AggregatedScores:
    """Aggregate normalized evidence without any state or execution decisions."""

    configured_weights = weights or ScoreWeights()
    buy_contributions = _weighted_contributions(evidence.buy.as_mapping(), configured_weights.buy)
    sell_contributions = _weighted_contributions(evidence.sell.as_mapping(), configured_weights.sell)

    buy_contributions["risk_penalty"] = -evidence.risk_penalty_points
    buy_score = max(0.0, min(100.0, sum(buy_contributions.values())))
    sell_score = max(0.0, min(100.0, sum(sell_contributions.values())))

    if evidence.hard_vetoes:
        buy_score = 0.0
    if evidence.hard_exit_reasons:
        sell_score = 100.0

    return AggregatedScores(
        buy_score=round(buy_score, 4),
        sell_score=round(sell_score, 4),
        buy_contributions=buy_contributions,
        sell_contributions=sell_contributions,
        hard_vetoes=evidence.hard_vetoes,
        hard_exit_reasons=evidence.hard_exit_reasons,
    )
