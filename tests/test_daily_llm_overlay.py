"""Contracts and truth table for the pure-local daily LLM overlay."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from modules.daily_portfolio.llm_overlay import (
    BuyDisposition,
    DecisionMode,
    EvidenceItem,
    EvidenceSnapshot,
    FallbackReason,
    GuardrailDecision,
    GuardrailEvent,
    OverlayProposal,
    OverlayValidationError,
    apply_guardrails,
    evaluate_overlay_payload,
)
from modules.daily_portfolio.models import TradeAction


def _evidence_payload(
    *,
    ref_id: str = "indicator:kdj",
    observed_date: str = "20260710",
    value=None,
) -> dict:
    return {
        "ref_id": ref_id,
        "kind": "INDICATOR",
        "observed_date": observed_date,
        "name": "KDJ oversold structure",
        "value": {"j": -12.5, "flags": ["oversold"]} if value is None else value,
    }


def _snapshot_payload(
    *,
    quant_action: str = "OPEN",
    current_step: int = 0,
    quant_target_step: int = 1,
    allowed_actions: list[str] | None = None,
    hard_vetoes: list[str] | None = None,
    hard_exit_reasons: list[str] | None = None,
    evidence: list[dict] | None = None,
) -> dict:
    resolved_allowed_actions = allowed_actions or list(
        dict.fromkeys([quant_action, "WATCH", "BLOCK"])
    )
    return {
        "schema_version": "quant-evidence-v1",
        "as_of_date": "20260710",
        "last_bar_date": "20260710",
        "market_context_date": "20260710",
        "ts_code": "000001.SZ",
        "strategy_version": "daily-holding-v0.1",
        "parameter_version": "initial-v0.1",
        "parameter_fingerprint": "fixture-fingerprint",
        "quant_action": quant_action,
        "current_step": current_step,
        "quant_target_step": quant_target_step,
        "max_step": 4,
        "allowed_actions": resolved_allowed_actions,
        "evidence": evidence or [_evidence_payload()],
        "hard_vetoes": hard_vetoes or [],
        "hard_exit_reasons": hard_exit_reasons or [],
        "max_adjustment": 1,
    }


def _proposal_payload(
    *,
    adjustment: int = 0,
    disposition: str = "UNCHANGED",
    evidence_refs: list[str] | None = None,
) -> dict:
    return {
        "schema_version": "overlay-decision-v1",
        "position_step_adjustment": adjustment,
        "buy_disposition": disposition,
        "confidence": 74,
        "agreement": "AGREE",
        "reasons": ["confirmed by cited daily evidence"],
        "counter_arguments": [],
        "evidence_refs": evidence_refs or ["indicator:kdj"],
        "invalidation_conditions": ["structure breaks"],
        "risk_flags": [],
        "requires_human_review": True,
    }


def test_canonical_hash_is_stable_for_equivalent_json_key_order() -> None:
    first = EvidenceSnapshot.from_dict(
        _snapshot_payload(
            evidence=[
                _evidence_payload(value={"b": 2, "a": {"y": 2, "x": 1}})
            ]
        )
    )
    second = EvidenceSnapshot.from_dict(
        _snapshot_payload(
            evidence=[
                _evidence_payload(value={"a": {"x": 1, "y": 2}, "b": 2})
            ]
        )
    )

    assert first.canonical_json() == second.canonical_json()
    assert first.sha256() == second.sha256()
    assert len(first.sha256()) == 64


def test_models_and_nested_evidence_values_are_frozen() -> None:
    snapshot = EvidenceSnapshot.from_dict(_snapshot_payload())
    with pytest.raises(FrozenInstanceError):
        snapshot.current_step = 2
    with pytest.raises(TypeError):
        snapshot.evidence[0].value["j"] = 0


@pytest.mark.parametrize(
    ("builder", "payload"),
    [
        (
            EvidenceItem.from_dict,
            {**_evidence_payload(), "invented": True},
        ),
        (
            EvidenceSnapshot.from_dict,
            {**_snapshot_payload(), "raw_prompt": "not part of the contract"},
        ),
    ],
)
def test_from_dict_rejects_extra_fields(builder, payload) -> None:
    with pytest.raises(OverlayValidationError, match="unexpected fields"):
        builder(payload)


def test_proposal_cannot_emit_final_action_or_target() -> None:
    snapshot = EvidenceSnapshot.from_dict(_snapshot_payload())
    payload = {
        **_proposal_payload(),
        "action": "OPEN",
        "target_position_step": 4,
    }
    with pytest.raises(OverlayValidationError, match="unexpected fields"):
        OverlayProposal.from_dict(payload, snapshot=snapshot)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({**_evidence_payload(), "kind": "TECHNICAL"}, "invalid value"),
        (
            _evidence_payload(observed_date="20260711"),
            "future evidence",
        ),
    ],
)
def test_schema_and_as_of_semantics_reject_bad_evidence(payload, message) -> None:
    if message == "invalid value":
        with pytest.raises(OverlayValidationError, match=message):
            EvidenceItem.from_dict(payload)
        return
    snapshot_payload = _snapshot_payload(evidence=[payload])
    with pytest.raises(OverlayValidationError, match=message):
        EvidenceSnapshot.from_dict(snapshot_payload)


def test_proposal_rejects_unknown_evidence_reference() -> None:
    snapshot = EvidenceSnapshot.from_dict(_snapshot_payload())
    with pytest.raises(OverlayValidationError, match="unknown evidence_ref"):
        OverlayProposal.from_dict(
            _proposal_payload(evidence_refs=["news:future-rumour"]),
            snapshot=snapshot,
        )


@pytest.mark.parametrize("adjustment", [-2, 2, True, 0.5])
def test_proposal_rejects_more_than_one_step_or_non_integer(adjustment) -> None:
    snapshot = EvidenceSnapshot.from_dict(_snapshot_payload())
    with pytest.raises(OverlayValidationError, match="position_step_adjustment"):
        OverlayProposal.from_dict(
            _proposal_payload(adjustment=adjustment), snapshot=snapshot
        )


@pytest.mark.parametrize("adjustment", [-1, 0, 1])
def test_hard_exit_truth_table_always_forces_zero(adjustment) -> None:
    snapshot = EvidenceSnapshot.from_dict(
        _snapshot_payload(
            quant_action="EXIT",
            current_step=2,
            quant_target_step=0,
            allowed_actions=["ADD", "HOLD", "REDUCE", "EXIT"],
            hard_exit_reasons=["hard_stop"],
        )
    )
    proposal = OverlayProposal.from_dict(
        _proposal_payload(adjustment=adjustment), snapshot=snapshot
    )

    decision = apply_guardrails(snapshot, proposal)

    assert decision.mode == DecisionMode.QUANT_LLM_OVERLAY
    assert decision.final_target_step == 0
    assert decision.final_action == TradeAction.EXIT
    assert GuardrailEvent.HARD_EXIT_FORCED_ZERO in decision.guardrail_events


@pytest.mark.parametrize("adjustment", [-1, 0, 1])
def test_hard_veto_truth_table_blocks_any_increase(adjustment) -> None:
    snapshot = EvidenceSnapshot.from_dict(
        _snapshot_payload(
            quant_action="BLOCK",
            quant_target_step=0,
            hard_vetoes=["data_quality_veto"],
            allowed_actions=["OPEN", "WATCH", "BLOCK"],
        )
    )
    proposal = OverlayProposal.from_dict(
        _proposal_payload(adjustment=adjustment), snapshot=snapshot
    )

    decision = apply_guardrails(snapshot, proposal)

    assert decision.final_target_step == snapshot.current_step == 0
    assert decision.final_action == TradeAction.BLOCK
    if adjustment > 0:
        assert GuardrailEvent.HARD_VETO_BLOCKED_INCREASE in decision.guardrail_events


def test_hard_veto_on_an_existing_position_resolves_add_to_hold() -> None:
    snapshot = EvidenceSnapshot.from_dict(
        _snapshot_payload(
            quant_action="HOLD",
            current_step=2,
            quant_target_step=2,
            allowed_actions=["ADD", "HOLD", "REDUCE", "EXIT"],
            hard_vetoes=["entry_structure_veto"],
        )
    )
    proposal = OverlayProposal.from_dict(
        _proposal_payload(adjustment=1), snapshot=snapshot
    )

    decision = apply_guardrails(snapshot, proposal)

    assert decision.final_target_step == 2
    assert decision.final_action == TradeAction.HOLD
    assert GuardrailEvent.HARD_VETO_BLOCKED_INCREASE in decision.guardrail_events


@pytest.mark.parametrize(
    ("quant_action", "quant_target_step"),
    [("REDUCE", 2), ("EXIT", 0)],
)
def test_quant_sell_truth_table_cannot_be_upgraded(
    quant_action, quant_target_step
) -> None:
    snapshot = EvidenceSnapshot.from_dict(
        _snapshot_payload(
            quant_action=quant_action,
            current_step=3,
            quant_target_step=quant_target_step,
            allowed_actions=["REDUCE", "EXIT", "HOLD"],
        )
    )
    proposal = OverlayProposal.from_dict(
        _proposal_payload(adjustment=1), snapshot=snapshot
    )

    decision = apply_guardrails(snapshot, proposal)

    assert decision.final_target_step == quant_target_step
    assert decision.final_action == TradeAction(quant_action)
    assert GuardrailEvent.QUANT_SELL_BLOCKED_UPGRADE in decision.guardrail_events


def test_buy_disposition_can_downgrade_but_not_invent_a_final_action() -> None:
    snapshot = EvidenceSnapshot.from_dict(_snapshot_payload())
    proposal = OverlayProposal.from_dict(
        _proposal_payload(disposition="WATCH"), snapshot=snapshot
    )

    decision = apply_guardrails(snapshot, proposal)

    assert decision.final_target_step == 0
    assert decision.final_action == TradeAction.WATCH
    assert decision.buy_disposition == BuyDisposition.WATCH
    assert decision.requested_position_step_adjustment == 0


def test_human_review_requirement_survives_guardrails_and_serialization() -> None:
    snapshot = EvidenceSnapshot.from_dict(
        _snapshot_payload(
            quant_action="WATCH",
            current_step=0,
            quant_target_step=0,
            allowed_actions=["WATCH", "OPEN", "BLOCK"],
        )
    )
    proposal = OverlayProposal.from_dict(
        _proposal_payload(adjustment=1), snapshot=snapshot
    )

    decision = apply_guardrails(snapshot, proposal)

    assert decision.final_action == TradeAction.OPEN
    assert decision.requires_human_review is True
    assert GuardrailDecision.from_dict(decision.to_dict()) == decision


def test_position_ceiling_clamp_is_recorded() -> None:
    snapshot = EvidenceSnapshot.from_dict(
        _snapshot_payload(
            quant_action="HOLD",
            current_step=4,
            quant_target_step=4,
            allowed_actions=["HOLD", "ADD"],
        )
    )
    proposal = OverlayProposal.from_dict(
        _proposal_payload(adjustment=1), snapshot=snapshot
    )

    decision = apply_guardrails(snapshot, proposal)

    assert decision.final_target_step == 4
    assert decision.final_action == TradeAction.HOLD
    assert GuardrailEvent.POSITION_CEILING_CLAMPED in decision.guardrail_events


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (None, FallbackReason.PROPOSAL_UNAVAILABLE),
        (
            {**_proposal_payload(), "position_step_adjustment": 7},
            FallbackReason.PROPOSAL_VALIDATION_ERROR,
        ),
    ],
)
def test_failure_falls_back_to_exact_quant_only(payload, reason) -> None:
    snapshot = EvidenceSnapshot.from_dict(
        _snapshot_payload(
            quant_action="ADD",
            current_step=1,
            quant_target_step=2,
            allowed_actions=["ADD", "HOLD", "REDUCE"],
        )
    )

    decision = evaluate_overlay_payload(snapshot, payload)

    assert decision.mode == DecisionMode.QUANT_ONLY
    assert decision.final_action == snapshot.quant_action
    assert decision.final_target_step == snapshot.quant_target_step
    assert decision.requested_position_step_adjustment is None
    assert decision.buy_disposition is None
    assert decision.guardrail_events == ()
    assert decision.fallback_reason == reason
    assert GuardrailDecision.from_dict(decision.to_dict()) == decision
