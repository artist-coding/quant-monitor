"""Deterministic guardrails for adjustment-only LLM proposals."""

from __future__ import annotations

from typing import Any
from collections.abc import Mapping

from ..models import TradeAction
from .exceptions import OverlayValidationError
from .models import (
    BuyDisposition,
    DecisionMode,
    EvidenceSnapshot,
    FallbackReason,
    GuardrailDecision,
    GuardrailEvent,
    OverlayProposal,
)


def _append_once(events: list[GuardrailEvent], event: GuardrailEvent) -> None:
    if event not in events:
        events.append(event)


def _action_for_target(
    snapshot: EvidenceSnapshot,
    target_step: int,
    *,
    buy_disposition: BuyDisposition,
) -> TradeAction:
    """Derive the sole final action from current and guarded target steps."""

    if target_step > snapshot.current_step:
        return TradeAction.OPEN if snapshot.current_step == 0 else TradeAction.ADD
    if target_step < snapshot.current_step:
        return TradeAction.EXIT if target_step == 0 else TradeAction.REDUCE
    if snapshot.current_step > 0:
        return TradeAction.HOLD
    if snapshot.hard_vetoes or buy_disposition == BuyDisposition.BLOCK:
        return TradeAction.BLOCK
    if buy_disposition == BuyDisposition.WATCH:
        return TradeAction.WATCH
    if snapshot.quant_action in (TradeAction.WATCH, TradeAction.BLOCK):
        return snapshot.quant_action
    return TradeAction.WATCH


def quant_only_fallback(
    snapshot: EvidenceSnapshot,
    reason: FallbackReason,
) -> GuardrailDecision:
    """Return the quant action and target byte-for-byte in semantic terms."""

    if not isinstance(reason, FallbackReason):
        raise OverlayValidationError("reason must be a FallbackReason")
    return GuardrailDecision(
        schema_version="guardrail-decision-v1",
        mode=DecisionMode.QUANT_ONLY,
        current_step=snapshot.current_step,
        quant_action=snapshot.quant_action,
        quant_target_step=snapshot.quant_target_step,
        final_action=snapshot.quant_action,
        final_target_step=snapshot.quant_target_step,
        max_step=snapshot.max_step,
        requested_position_step_adjustment=None,
        buy_disposition=None,
        requires_human_review=False,
        fallback_reason=reason,
    )


def apply_guardrails(
    snapshot: EvidenceSnapshot,
    proposal: OverlayProposal,
) -> GuardrailDecision:
    """Apply hard-risk precedence and derive a single final action.

    Precedence is intentionally local and deterministic:

    1. the proposal is limited to one rung and clamped to valid step bounds;
    2. WATCH/BLOCK may cancel a quant OPEN/ADD;
    3. quant REDUCE/EXIT may not be upgraded;
    4. hard vetoes may never increase exposure;
    5. hard exits always force the final target to zero.
    """

    if not isinstance(snapshot, EvidenceSnapshot):
        raise OverlayValidationError("snapshot must be an EvidenceSnapshot")
    if not isinstance(proposal, OverlayProposal):
        raise OverlayValidationError("proposal must be an OverlayProposal")
    proposal.validate_against(snapshot)

    events: list[GuardrailEvent] = []
    target_step = snapshot.quant_target_step + proposal.position_step_adjustment
    if target_step < 0:
        target_step = 0
        _append_once(events, GuardrailEvent.POSITION_FLOOR_CLAMPED)
    if target_step > snapshot.max_step:
        target_step = snapshot.max_step
        _append_once(events, GuardrailEvent.POSITION_CEILING_CLAMPED)

    if proposal.buy_disposition != BuyDisposition.UNCHANGED:
        # validate_against has already restricted this path to quant OPEN/ADD.
        target_step = snapshot.current_step
        event = (
            GuardrailEvent.BUY_DOWNGRADED_TO_WATCH
            if proposal.buy_disposition == BuyDisposition.WATCH
            else GuardrailEvent.BUY_DOWNGRADED_TO_BLOCK
        )
        _append_once(events, event)

    if snapshot.quant_action in (TradeAction.REDUCE, TradeAction.EXIT) and target_step > snapshot.quant_target_step:
        target_step = snapshot.quant_target_step
        _append_once(events, GuardrailEvent.QUANT_SELL_BLOCKED_UPGRADE)

    if snapshot.hard_vetoes and target_step > snapshot.current_step:
        target_step = snapshot.current_step
        _append_once(events, GuardrailEvent.HARD_VETO_BLOCKED_INCREASE)

    if snapshot.hard_exit_reasons:
        target_step = 0
        _append_once(events, GuardrailEvent.HARD_EXIT_FORCED_ZERO)

    final_action = _action_for_target(
        snapshot,
        target_step,
        buy_disposition=proposal.buy_disposition,
    )
    if final_action not in snapshot.allowed_actions:
        # A malformed allowed-action set must never be silently expanded by
        # the overlay.  The safe operational response is the exact baseline.
        return quant_only_fallback(snapshot, FallbackReason.PROPOSAL_VALIDATION_ERROR)

    return GuardrailDecision(
        schema_version="guardrail-decision-v1",
        mode=DecisionMode.QUANT_LLM_OVERLAY,
        current_step=snapshot.current_step,
        quant_action=snapshot.quant_action,
        quant_target_step=snapshot.quant_target_step,
        final_action=final_action,
        final_target_step=target_step,
        max_step=snapshot.max_step,
        requested_position_step_adjustment=proposal.position_step_adjustment,
        buy_disposition=proposal.buy_disposition,
        requires_human_review=proposal.requires_human_review,
        guardrail_events=tuple(events),
    )


def evaluate_overlay_payload(
    snapshot: EvidenceSnapshot,
    payload: Mapping[str, Any] | None,
) -> GuardrailDecision:
    """Parse a structured payload or fall back exactly to ``QUANT_ONLY``.

    This boundary deliberately accepts only an already-decoded JSON object.
    It does not strip Markdown fences, repair JSON, call a provider, or infer
    missing fields.  Those behaviours would make historical replay ambiguous.
    """

    if payload is None:
        return quant_only_fallback(snapshot, FallbackReason.PROPOSAL_UNAVAILABLE)
    try:
        proposal = OverlayProposal.from_dict(payload, snapshot=snapshot)
        return apply_guardrails(snapshot, proposal)
    except (OverlayValidationError, TypeError, ValueError):
        return quant_only_fallback(snapshot, FallbackReason.PROPOSAL_VALIDATION_ERROR)
