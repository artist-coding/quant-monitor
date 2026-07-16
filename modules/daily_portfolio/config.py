"""日线评分与执行的版本化初始配置。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from .models import ExecutionMode


@dataclass(frozen=True)
class ScoreWeights:
    buy: Mapping[str, float] = field(
        default_factory=lambda: {
            "entry_structure": 25.0,
            "trend": 20.0,
            "volume": 15.0,
            "pattern_quality": 15.0,
            "stage": 10.0,
            "market": 10.0,
            "resonance": 5.0,
        }
    )
    sell: Mapping[str, float] = field(
        default_factory=lambda: {
            "stop": 30.0,
            "exit_signal": 25.0,
            "trend_break": 20.0,
            "distribution": 10.0,
            "market_risk": 5.0,
            "position_heat": 10.0,
            "profit_protection": 0.0,
        }
    )

    def __post_init__(self) -> None:
        for name, weights in (("buy", self.buy), ("sell", self.sell)):
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in weights.values()
            ):
                raise ValueError(f"{name} weights must be finite numbers")
            if any(value < 0 for value in weights.values()):
                raise ValueError(f"{name} weights cannot be negative")
            total = sum(weights.values())
            if abs(total - 100.0) > 1e-9:
                raise ValueError(f"{name} weights must total 100, got {total}")
        object.__setattr__(self, "buy", MappingProxyType(dict(self.buy)))
        object.__setattr__(self, "sell", MappingProxyType(dict(self.sell)))


@dataclass(frozen=True)
class ScoreThresholds:
    open_buy_score: float = 75.0
    add_buy_score: float = 82.0
    reduce_sell_score: float = 70.0
    exit_sell_score: float = 85.0
    conflict_score: float = 70.0

    def __post_init__(self) -> None:
        values = (
            self.open_buy_score,
            self.add_buy_score,
            self.reduce_sell_score,
            self.exit_sell_score,
            self.conflict_score,
        )
        if any(not 0 <= value <= 100 for value in values):
            raise ValueError("score thresholds must be between 0 and 100")
        if self.add_buy_score < self.open_buy_score:
            raise ValueError("add_buy_score cannot be lower than open_buy_score")
        if self.exit_sell_score < self.reduce_sell_score:
            raise ValueError("exit_sell_score cannot be lower than reduce_sell_score")


@dataclass(frozen=True)
class DailyPortfolioConfig:
    strategy_version: str = "daily-holding-v0.2"
    parameter_version: str = "buy-confirmation-initial-v0.2"
    score_weights: ScoreWeights = field(default_factory=ScoreWeights)
    thresholds: ScoreThresholds = field(default_factory=ScoreThresholds)
    primary_exit_mode: ExecutionMode = ExecutionMode.SAME_CLOSE_RESEARCH
    validation_exit_mode: ExecutionMode = ExecutionMode.NEXT_OPEN_STRICT
    position_ladder: tuple[float, ...] = (0.0, 0.25, 0.50, 0.75, 1.0)
    entry_stop_lookback_bars: int = 20
    minimum_position_delta_pct: float = 0.02

    def __post_init__(self) -> None:
        if not self.strategy_version or not self.parameter_version:
            raise ValueError("strategy and parameter versions cannot be empty")
        if not self.position_ladder or self.position_ladder[0] != 0 or self.position_ladder[-1] != 1:
            raise ValueError("position_ladder must start at 0 and end at 1")
        if tuple(sorted(set(self.position_ladder))) != self.position_ladder:
            raise ValueError("position_ladder must be unique and strictly increasing")
        if not 0 <= self.minimum_position_delta_pct <= 1:
            raise ValueError("minimum_position_delta_pct must be between 0 and 1")
        if self.entry_stop_lookback_bars < 2:
            raise ValueError("entry_stop_lookback_bars must be at least 2")

    def fingerprint(self) -> str:
        """Return a stable hash of every currently effective quant parameter."""

        payload = {
            "strategy_version": self.strategy_version,
            "parameter_version": self.parameter_version,
            "score_weights": {
                "buy": dict(self.score_weights.buy),
                "sell": dict(self.score_weights.sell),
            },
            "thresholds": {
                "open_buy_score": self.thresholds.open_buy_score,
                "add_buy_score": self.thresholds.add_buy_score,
                "reduce_sell_score": self.thresholds.reduce_sell_score,
                "exit_sell_score": self.thresholds.exit_sell_score,
                "conflict_score": self.thresholds.conflict_score,
            },
            "primary_exit_mode": self.primary_exit_mode.value,
            "validation_exit_mode": self.validation_exit_mode.value,
            "position_ladder": list(self.position_ladder),
            "entry_stop_lookback_bars": self.entry_stop_lookback_bars,
            "minimum_position_delta_pct": self.minimum_position_delta_pct,
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
