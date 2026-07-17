"""Build an immutable LLM evidence snapshot from the deterministic quant run."""

from __future__ import annotations

from collections.abc import Sequence

from ..evidence_adapter import MarketSnapshot
from ..models import PositionState, TradeAction
from ..service import DailyScoreEvaluation
from .exceptions import OverlayValidationError
from .models import EvidenceItem, EvidenceKind, EvidenceSnapshot


def _step_index(value: float, ladder: tuple[float, ...], *, field: str) -> int:
    matches = [index for index, rung in enumerate(ladder) if abs(rung - value) <= 1e-9]
    if len(matches) != 1:
        raise OverlayValidationError(f"{field} is not an exact position-ladder rung")
    return matches[0]


def _allowed_actions(
    action: TradeAction,
    current_step: int,
    hard_vetoes: tuple[str, ...],
    hard_exit_reasons: tuple[str, ...],
) -> tuple[TradeAction, ...]:
    allowed: tuple[TradeAction, ...]
    if hard_exit_reasons:
        allowed = (TradeAction.EXIT,) if current_step > 0 else (TradeAction.BLOCK,)
    elif hard_vetoes and current_step == 0:
        allowed = (TradeAction.BLOCK, TradeAction.WATCH)
    elif current_step == 0:
        allowed = (TradeAction.OPEN, TradeAction.WATCH, TradeAction.BLOCK)
    else:
        allowed = (
            TradeAction.ADD,
            TradeAction.HOLD,
            TradeAction.REDUCE,
            TradeAction.EXIT,
        )
    if action not in allowed:
        raise OverlayValidationError("quant action is outside hard-risk allowed actions")
    return allowed


def build_evidence_snapshot(
    evaluation: DailyScoreEvaluation,
    *,
    position_ladder: Sequence[float] = (0.0, 0.25, 0.50, 0.75, 1.0),
) -> EvidenceSnapshot:
    """Convert one completed quant evaluation; never fetch or recompute facts."""

    if not isinstance(evaluation, DailyScoreEvaluation):
        raise OverlayValidationError("evaluation must be a DailyScoreEvaluation")

    score = evaluation.score
    position = evaluation.position_snapshot
    market = evaluation.market_context
    if not isinstance(position, PositionState) or not isinstance(market, MarketSnapshot):
        raise OverlayValidationError("evaluation contains invalid bound snapshots")
    if position.ts_code != score.ts_code:
        raise OverlayValidationError("position and evaluation stock codes differ")
    if score.last_bar_date != score.signal_date:
        raise OverlayValidationError("LLM overlay requires a current confirmed bar")
    if market.trade_date != score.signal_date:
        raise OverlayValidationError("market snapshot date must equal signal date")

    ladder = tuple(position_ladder)
    if not ladder or ladder[0] != 0 or ladder[-1] != 1:
        raise OverlayValidationError("position_ladder must start at 0 and end at 1")
    if tuple(sorted(set(ladder))) != ladder:
        raise OverlayValidationError("position_ladder must be strictly increasing")
    # A position can drift or be imported above its configured hard maximum.
    # Keep targets bounded by ``max_step``, but represent the current exposure
    # with one synthetic overflow step.  This preserves the semantic relation
    # ``REDUCE: target_step < current_step`` instead of disguising a mandatory
    # risk reduction as HOLD at the top rung.
    if position.current_position_pct > evaluation.policy.max_position_pct + 1e-9:
        current_step = len(ladder)
    else:
        current_step = _step_index(
            evaluation.policy.current_ladder_ratio,
            ladder,
            field="current_ladder_ratio",
        )
    target_step = _step_index(
        evaluation.policy.target_ladder_ratio,
        ladder,
        field="target_ladder_ratio",
    )

    items: list[EvidenceItem] = [
        EvidenceItem(
            ref_id="quant:buy_score",
            kind=EvidenceKind.QUANT_SCORE,
            observed_date=score.signal_date,
            name="buy_score",
            value={
                "score": score.buy_score,
                "contributions": score.buy_contributions,
            },
        ),
        EvidenceItem(
            ref_id="quant:sell_score",
            kind=EvidenceKind.QUANT_SCORE,
            observed_date=score.signal_date,
            name="sell_score",
            value={
                "score": score.sell_score,
                "contributions": score.sell_contributions,
            },
        ),
        EvidenceItem(
            ref_id="position:current",
            kind=EvidenceKind.POSITION,
            observed_date=score.signal_date,
            name="current_position",
            value={
                "lifecycle_state": position.lifecycle_state.value,
                "shares": position.shares,
                "available_shares": position.available_shares,
                "current_position_pct": position.current_position_pct,
                "stop_loss": position.stop_loss,
                "can_sell_date": position.can_sell_date,
            },
        ),
        EvidenceItem(
            ref_id="market:context",
            kind=EvidenceKind.MARKET_CONTEXT,
            observed_date=market.trade_date,
            name="market_context",
            value={
                "score": market.score,
                "version": market.version,
                "source_hash": market.source_hash,
            },
        ),
    ]
    for name, variant in sorted(evaluation.features.variant_evidence.items()):
        if not variant.matched:
            continue
        items.append(
            EvidenceItem(
                ref_id=f"strategy:{name}",
                kind=EvidenceKind.STRATEGY_SIGNAL,
                observed_date=score.signal_date,
                name=name,
                value={
                    "strength": variant.strength,
                    "anchor": (
                        {
                            "anchor_date": variant.anchor.anchor_date,
                            "age_bars": variant.anchor.age_bars,
                            "anchor_variant": variant.anchor.anchor_variant,
                        }
                        if variant.anchor
                        else None
                    ),
                    "details": variant.details,
                },
            )
        )
    for index, reason in enumerate(score.vetoes):
        items.append(
            EvidenceItem(
                ref_id=f"risk:veto:{index}",
                kind=EvidenceKind.RISK_CONSTRAINT,
                observed_date=score.signal_date,
                name="hard_veto",
                value=reason,
            )
        )
    for index, reason in enumerate(score.hard_exit_reasons):
        items.append(
            EvidenceItem(
                ref_id=f"risk:exit:{index}",
                kind=EvidenceKind.RISK_CONSTRAINT,
                observed_date=score.signal_date,
                name="hard_exit",
                value=reason,
            )
        )

    return EvidenceSnapshot(
        schema_version="quant-evidence-v1",
        as_of_date=score.signal_date,
        last_bar_date=score.last_bar_date,
        market_context_date=market.trade_date,
        ts_code=score.ts_code,
        strategy_version=score.strategy_version,
        parameter_version=score.parameter_version,
        parameter_fingerprint=score.parameter_fingerprint,
        quant_action=score.desired_action,
        current_step=current_step,
        quant_target_step=target_step,
        max_step=len(ladder) - 1,
        allowed_actions=_allowed_actions(
            score.desired_action,
            current_step,
            score.vetoes,
            score.hard_exit_reasons,
        ),
        evidence=tuple(items),
        hard_vetoes=score.vetoes,
        hard_exit_reasons=score.hard_exit_reasons,
        max_adjustment=1,
    )
