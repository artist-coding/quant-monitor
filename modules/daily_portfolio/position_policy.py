"""Pure position-policy rules for the daily portfolio system.

The configured ``position_ladder`` is expressed as a ratio of a stock's
maximum allowed position, while :class:`PositionState` and
:class:`DailyStockScore` use actual portfolio percentages.  Callers must pass
``max_position_pct`` so the two domains are never mixed implicitly.

This module deliberately has no database, market-data, clock, or LLM
dependency.  Given the same position, scores, and configuration it always
returns the same decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .config import ScoreThresholds
from .models import DailyStockScore, PositionState, TradeAction


DEFAULT_POSITION_LADDER: tuple[float, ...] = (0.0, 0.25, 0.50, 0.75, 1.0)
_EPSILON = 1e-9


@dataclass(frozen=True)
class PositionPolicyInput:
    """Quant scores and hard rules already known at the signal-day close."""

    signal_date: str
    buy_score: float
    sell_score: float
    last_bar_date: str = ""
    stop_loss: float | None = None
    reasons: tuple[str, ...] = ()
    hard_vetoes: tuple[str, ...] = ()
    hard_exit_reasons: tuple[str, ...] = ()
    entry_confirmed: bool = False
    entry_confirmation_reasons: tuple[str, ...] = ()
    buy_contributions: Mapping[str, float] = field(default_factory=dict)
    sell_contributions: Mapping[str, float] = field(default_factory=dict)
    strategy_version: str = "daily-holding-v0.2"
    parameter_version: str = "buy-confirmation-initial-v0.2"
    parameter_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.signal_date:
            raise ValueError("signal_date cannot be empty")
        for name, value in (("buy_score", self.buy_score), ("sell_score", self.sell_score)):
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100, got {value}")
        if self.stop_loss is not None and self.stop_loss <= 0:
            raise ValueError("stop_loss must be positive when provided")


@dataclass(frozen=True)
class PositionPolicyDecision:
    """A score ready for execution plus its explicit ladder interpretation."""

    daily_score: DailyStockScore
    current_ladder_ratio: float
    target_ladder_ratio: float
    max_position_pct: float
    decision_code: str
    has_score_conflict: bool


def _validate_position_ladder(position_ladder: Sequence[float]) -> tuple[float, ...]:
    ladder = tuple(position_ladder)
    if not ladder or ladder[0] != 0.0 or ladder[-1] != 1.0:
        raise ValueError("position_ladder must start at 0 and end at 1")
    if tuple(sorted(set(ladder))) != ladder:
        raise ValueError("position_ladder must be unique and strictly increasing")
    return ladder


def score_to_ladder_ratio(
    position_score: float,
    position_ladder: Sequence[float] = DEFAULT_POSITION_LADDER,
) -> float:
    """Map a 0-100 strength score to an evenly partitioned ladder level.

    With the default five-level ladder this implements the design contract:
    0-19 -> 0%, 20-39 -> 25%, 40-59 -> 50%, 60-79 -> 75%,
    and 80-100 -> 100% of the stock's maximum allowed position.
    """

    if not 0 <= position_score <= 100:
        raise ValueError(f"position_score must be between 0 and 100, got {position_score}")
    ladder = _validate_position_ladder(position_ladder)
    if position_score == 100:
        return ladder[-1]
    bucket_width = 100.0 / len(ladder)
    return ladder[int(position_score / bucket_width)]


def _has_position(position: PositionState) -> bool:
    return position.shares > 0 or position.current_position_pct > _EPSILON


def _nearest_ladder_index(value: float, ladder: tuple[float, ...]) -> int:
    # Ties intentionally choose the lower/conservative rung because ``min``
    # retains the first item in the ordered ladder.
    return min(range(len(ladder)), key=lambda index: abs(ladder[index] - value))


def _current_ladder_index(
    position: PositionState,
    max_position_pct: float,
    ladder: tuple[float, ...],
) -> int:
    if not _has_position(position):
        return 0
    current_ratio = position.current_position_pct / max_position_pct
    nearest = _nearest_ladder_index(current_ratio, ladder)
    # A real holding must not be represented as the zero rung merely because
    # price drift or rounding made its percentage very small.
    return max(1, nearest)


def _action_from_ladder_change(
    *,
    has_position: bool,
    current_index: int,
    target_index: int,
    blocked: bool = False,
) -> TradeAction:
    if blocked:
        return TradeAction.BLOCK
    if not has_position:
        return TradeAction.OPEN if target_index > 0 else TradeAction.WATCH
    if target_index == 0:
        return TradeAction.EXIT
    if target_index > current_index:
        return TradeAction.ADD
    if target_index < current_index:
        return TradeAction.REDUCE
    return TradeAction.HOLD


def _merge_unique(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


def evaluate_position_policy(
    position: PositionState,
    signals: PositionPolicyInput,
    *,
    max_position_pct: float,
    thresholds: ScoreThresholds | None = None,
    position_ladder: Sequence[float] = DEFAULT_POSITION_LADDER,
    minimum_position_delta_pct: float = 0.0,
) -> PositionPolicyDecision:
    """Resolve independent buy/sell scores into one deterministic action.

    Priority is hard exit, sell exit/reduction, entry veto, score conflict,
    and finally buy/open/add.  A hard veto blocks new exposure but never hides
    an exit or reduction that is already required by the sell score.

    ``max_position_pct`` is the actual portfolio allocation allowed for this
    stock (for example ``0.10`` for 10%).  ``position_ladder`` remains a ratio
    of that maximum.  The returned ``DailyStockScore.target_position_pct`` is
    therefore ``target_ladder_ratio * max_position_pct``.
    """

    if not 0 < max_position_pct <= 1:
        raise ValueError("max_position_pct must be greater than 0 and at most 1")
    if not 0 <= minimum_position_delta_pct <= 1:
        raise ValueError("minimum_position_delta_pct must be between 0 and 1")
    ladder = _validate_position_ladder(position_ladder)
    resolved_thresholds = thresholds or ScoreThresholds()
    has_position = _has_position(position)
    current_index = _current_ladder_index(position, max_position_pct, ladder)
    buy_target_ratio = score_to_ladder_ratio(signals.buy_score, ladder)
    buy_target_index = ladder.index(buy_target_ratio)
    has_conflict = (
        signals.buy_score >= resolved_thresholds.conflict_score
        and signals.sell_score >= resolved_thresholds.conflict_score
    )

    blocked = False
    forced_action: TradeAction | None = None
    if signals.hard_exit_reasons:
        target_index = 0
        blocked = not has_position
        decision_code = "hard_exit"
    elif has_position and signals.sell_score >= resolved_thresholds.exit_sell_score:
        target_index = 0
        decision_code = "sell_exit"
    elif has_position and signals.sell_score >= resolved_thresholds.reduce_sell_score:
        target_index = max(0, current_index - 1)
        decision_code = "sell_reduce"
    elif position.current_position_pct > max_position_pct + _EPSILON:
        target_index = len(ladder) - 1
        forced_action = TradeAction.REDUCE
        decision_code = "position_limit_reduce"
    elif signals.hard_vetoes:
        target_index = current_index if has_position else 0
        blocked = not has_position
        decision_code = "hard_veto"
    elif has_conflict:
        target_index = current_index if has_position else 0
        decision_code = "score_conflict"
    elif not signals.entry_confirmed:
        target_index = current_index if has_position else 0
        decision_code = "entry_not_confirmed"
    elif not has_position:
        if signals.buy_score >= resolved_thresholds.open_buy_score:
            target_index = max(1, buy_target_index)
            decision_code = "open"
        else:
            target_index = 0
            decision_code = "watch"
    elif signals.buy_score >= resolved_thresholds.add_buy_score and buy_target_index > current_index:
        target_index = buy_target_index
        decision_code = "add"
    else:
        target_index = current_index
        decision_code = "hold"

    action = _action_from_ladder_change(
        has_position=has_position,
        current_index=current_index,
        target_index=target_index,
        blocked=blocked,
    )
    if forced_action is not None:
        action = forced_action
    target_ratio = ladder[target_index]
    target_position_pct = target_ratio * max_position_pct
    if (
        action in (TradeAction.OPEN, TradeAction.ADD)
        and abs(target_position_pct - position.current_position_pct)
        < minimum_position_delta_pct
    ):
        target_index = current_index if has_position else 0
        target_ratio = ladder[target_index]
        target_position_pct = target_ratio * max_position_pct
        action = TradeAction.HOLD if has_position else TradeAction.WATCH
        decision_code = "minimum_position_delta"
    if action == TradeAction.HOLD:
        target_position_pct = position.current_position_pct
    policy_reason = f"position_policy:{decision_code}"
    daily_score = DailyStockScore(
        ts_code=position.ts_code,
        signal_date=signals.signal_date,
        buy_score=signals.buy_score,
        sell_score=signals.sell_score,
        position_score=target_ratio * 100.0,
        current_position_pct=position.current_position_pct,
        target_position_pct=target_position_pct,
        desired_action=action,
        last_bar_date=signals.last_bar_date,
        stop_loss=signals.stop_loss,
        reasons=_merge_unique(
            signals.reasons,
            signals.entry_confirmation_reasons,
            (policy_reason,),
        ),
        vetoes=signals.hard_vetoes,
        hard_exit_reasons=signals.hard_exit_reasons,
        buy_contributions=dict(signals.buy_contributions),
        sell_contributions=dict(signals.sell_contributions),
        strategy_version=signals.strategy_version,
        parameter_version=signals.parameter_version,
        parameter_fingerprint=signals.parameter_fingerprint,
    )
    return PositionPolicyDecision(
        daily_score=daily_score,
        current_ladder_ratio=ladder[current_index],
        target_ladder_ratio=target_ratio,
        max_position_pct=max_position_pct,
        decision_code=decision_code,
        has_score_conflict=has_conflict,
    )
