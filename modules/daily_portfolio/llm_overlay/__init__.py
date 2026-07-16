"""Pure-local LLM overlay contracts and deterministic guardrails."""

from .canonical import canonical_json, canonical_sha256
from .exceptions import OverlayValidationError
from .evidence_builder import build_evidence_snapshot
from .guardrails import (
    apply_guardrails,
    evaluate_overlay_payload,
    quant_only_fallback,
)
from .models import (
    BuyDisposition,
    DecisionMode,
    EvidenceItem,
    EvidenceKind,
    EvidenceSnapshot,
    FallbackReason,
    GuardrailDecision,
    GuardrailEvent,
    OverlayProposal,
    QuantAgreement,
)

__all__ = [
    "BuyDisposition",
    "DecisionMode",
    "EvidenceItem",
    "EvidenceKind",
    "EvidenceSnapshot",
    "FallbackReason",
    "GuardrailDecision",
    "GuardrailEvent",
    "OverlayProposal",
    "OverlayValidationError",
    "QuantAgreement",
    "apply_guardrails",
    "build_evidence_snapshot",
    "canonical_json",
    "canonical_sha256",
    "evaluate_overlay_payload",
    "quant_only_fallback",
]
