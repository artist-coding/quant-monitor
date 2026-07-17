"""Frozen data contracts for the deterministic daily LLM overlay.

The LLM is intentionally not allowed to emit a final trade action or target
position.  It may only propose a one-rung adjustment and a conservative
disposition for an existing quant buy decision.  ``guardrails.py`` owns the
only conversion from that proposal to an executable portfolio decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeVar
from collections.abc import Mapping

from ..dates import TradeDateError, normalize_trade_date
from ..models import TradeAction
from .canonical import canonical_json, canonical_sha256
from .exceptions import OverlayValidationError


class EvidenceKind(str, Enum):
    QUANT_SCORE = "QUANT_SCORE"
    INDICATOR = "INDICATOR"
    STRATEGY_SIGNAL = "STRATEGY_SIGNAL"
    POSITION = "POSITION"
    MARKET_CONTEXT = "MARKET_CONTEXT"
    RISK_CONSTRAINT = "RISK_CONSTRAINT"
    FUNDAMENTAL = "FUNDAMENTAL"
    NEWS = "NEWS"


class BuyDisposition(str, Enum):
    UNCHANGED = "UNCHANGED"
    WATCH = "WATCH"
    BLOCK = "BLOCK"


class QuantAgreement(str, Enum):
    AGREE = "AGREE"
    PARTIAL = "PARTIAL"
    DISAGREE = "DISAGREE"


class DecisionMode(str, Enum):
    QUANT_ONLY = "QUANT_ONLY"
    QUANT_LLM_OVERLAY = "QUANT_LLM_OVERLAY"


class GuardrailEvent(str, Enum):
    POSITION_FLOOR_CLAMPED = "POSITION_FLOOR_CLAMPED"
    POSITION_CEILING_CLAMPED = "POSITION_CEILING_CLAMPED"
    BUY_DOWNGRADED_TO_WATCH = "BUY_DOWNGRADED_TO_WATCH"
    BUY_DOWNGRADED_TO_BLOCK = "BUY_DOWNGRADED_TO_BLOCK"
    HARD_EXIT_FORCED_ZERO = "HARD_EXIT_FORCED_ZERO"
    HARD_VETO_BLOCKED_INCREASE = "HARD_VETO_BLOCKED_INCREASE"
    QUANT_SELL_BLOCKED_UPGRADE = "QUANT_SELL_BLOCKED_UPGRADE"


class FallbackReason(str, Enum):
    PROPOSAL_UNAVAILABLE = "PROPOSAL_UNAVAILABLE"
    PROPOSAL_VALIDATION_ERROR = "PROPOSAL_VALIDATION_ERROR"


EnumT = TypeVar("EnumT", bound=Enum)


def _strict_keys(data: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    if not isinstance(data, Mapping):
        raise OverlayValidationError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in data):
        raise OverlayValidationError(f"{label} object keys must be strings")
    actual = set(data)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected fields: {', '.join(extra)}")
        raise OverlayValidationError(f"{label} schema error ({'; '.join(details)})")


def _enum_value(enum_type: type[EnumT], value: Any, *, field: str) -> EnumT:
    if not isinstance(value, str):
        raise OverlayValidationError(f"{field} must be a string enum")
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise OverlayValidationError(f"{field} has invalid value {value!r}; allowed: {allowed}") from exc


def _strict_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OverlayValidationError(f"{field} must be an integer")
    return value


def _nonempty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OverlayValidationError(f"{field} must be a non-empty string")
    return value


def _string_tuple(
    value: Any,
    *,
    field: str,
    allow_empty: bool = True,
    unique: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise OverlayValidationError(f"{field} must be an array of strings")
    result = tuple(_nonempty_string(item, field=f"{field}[{index}]") for index, item in enumerate(value))
    if not allow_empty and not result:
        raise OverlayValidationError(f"{field} must not be empty")
    if unique and len(set(result)) != len(result):
        raise OverlayValidationError(f"{field} must not contain duplicates")
    return result


def _freeze_json(value: Any, *, field: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OverlayValidationError(f"{field} must not contain NaN or Infinity")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise OverlayValidationError(f"{field} object keys must be strings")
            frozen[key] = _freeze_json(item, field=f"{field}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field=f"{field}[{index}]") for index, item in enumerate(value))
    raise OverlayValidationError(f"{field} contains unsupported JSON value {type(value).__name__}")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _normalize_date(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise OverlayValidationError(f"{field} must be a trade-date string")
    try:
        return normalize_trade_date(value)
    except TradeDateError as exc:
        raise OverlayValidationError(f"{field}: {exc}") from exc


def _is_action_step_consistent(
    action: TradeAction,
    *,
    current_step: int,
    target_step: int,
) -> bool:
    return {
        TradeAction.OPEN: current_step == 0 and target_step > 0,
        TradeAction.ADD: current_step > 0 and target_step > current_step,
        TradeAction.HOLD: current_step > 0 and target_step == current_step,
        TradeAction.REDUCE: current_step > 0 and 0 < target_step < current_step,
        TradeAction.EXIT: current_step > 0 and target_step == 0,
        TradeAction.WATCH: current_step == 0 and target_step == 0,
        TradeAction.BLOCK: current_step == 0 and target_step == 0,
    }[action]


class _CanonicalContract:
    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    def content_sha256(self) -> str:
        return self.sha256()


@dataclass(frozen=True)
class EvidenceItem(_CanonicalContract):
    ref_id: str
    kind: EvidenceKind
    observed_date: str
    name: str
    value: Any

    _FIELDS = {"ref_id", "kind", "observed_date", "name", "value"}

    def __post_init__(self) -> None:
        _nonempty_string(self.ref_id, field="ref_id")
        if not isinstance(self.kind, EvidenceKind):
            raise OverlayValidationError("kind must be an EvidenceKind")
        object.__setattr__(
            self,
            "observed_date",
            _normalize_date(self.observed_date, field="observed_date"),
        )
        _nonempty_string(self.name, field="name")
        object.__setattr__(self, "value", _freeze_json(self.value, field="value"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvidenceItem:
        _strict_keys(data, cls._FIELDS, label="EvidenceItem")
        return cls(
            ref_id=data["ref_id"],
            kind=_enum_value(EvidenceKind, data["kind"], field="kind"),
            observed_date=data["observed_date"],
            name=data["name"],
            value=data["value"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "kind": self.kind.value,
            "observed_date": self.observed_date,
            "name": self.name,
            "value": _plain_json(self.value),
        }


@dataclass(frozen=True)
class EvidenceSnapshot(_CanonicalContract):
    schema_version: str
    as_of_date: str
    last_bar_date: str
    market_context_date: str
    ts_code: str
    strategy_version: str
    parameter_version: str
    parameter_fingerprint: str
    quant_action: TradeAction
    current_step: int
    quant_target_step: int
    max_step: int
    allowed_actions: tuple[TradeAction, ...]
    evidence: tuple[EvidenceItem, ...]
    hard_vetoes: tuple[str, ...] = ()
    hard_exit_reasons: tuple[str, ...] = ()
    max_adjustment: int = 1

    _FIELDS = {
        "schema_version",
        "as_of_date",
        "last_bar_date",
        "market_context_date",
        "ts_code",
        "strategy_version",
        "parameter_version",
        "parameter_fingerprint",
        "quant_action",
        "current_step",
        "quant_target_step",
        "max_step",
        "allowed_actions",
        "evidence",
        "hard_vetoes",
        "hard_exit_reasons",
        "max_adjustment",
    }

    def __post_init__(self) -> None:
        if self.schema_version != "quant-evidence-v1":
            raise OverlayValidationError("schema_version must be quant-evidence-v1")
        object.__setattr__(self, "as_of_date", _normalize_date(self.as_of_date, field="as_of_date"))
        object.__setattr__(
            self,
            "last_bar_date",
            _normalize_date(self.last_bar_date, field="last_bar_date"),
        )
        object.__setattr__(
            self,
            "market_context_date",
            _normalize_date(self.market_context_date, field="market_context_date"),
        )
        if self.last_bar_date != self.as_of_date:
            raise OverlayValidationError("last_bar_date must equal as_of_date")
        if self.market_context_date != self.as_of_date:
            raise OverlayValidationError("market_context_date must equal as_of_date")
        _nonempty_string(self.ts_code, field="ts_code")
        _nonempty_string(self.strategy_version, field="strategy_version")
        _nonempty_string(self.parameter_version, field="parameter_version")
        _nonempty_string(self.parameter_fingerprint, field="parameter_fingerprint")
        if not isinstance(self.quant_action, TradeAction):
            raise OverlayValidationError("quant_action must be a TradeAction")
        current_step = _strict_int(self.current_step, field="current_step")
        target_step = _strict_int(self.quant_target_step, field="quant_target_step")
        max_step = _strict_int(self.max_step, field="max_step")
        max_adjustment = _strict_int(self.max_adjustment, field="max_adjustment")
        if max_step < 1:
            raise OverlayValidationError("max_step must be at least 1")
        # ``max_step + 1`` is a reserved overflow sentinel for an actual
        # position above the configured hard maximum.  It can be a current
        # state, never a target state.
        if not 0 <= current_step <= max_step + 1:
            raise OverlayValidationError("current_step must be between 0 and max_step + 1")
        if not 0 <= target_step <= max_step:
            raise OverlayValidationError("quant_target_step must be between 0 and max_step")
        if max_adjustment != 1:
            raise OverlayValidationError("max_adjustment is frozen at 1")

        allowed_actions = tuple(self.allowed_actions)
        if not allowed_actions or any(not isinstance(action, TradeAction) for action in allowed_actions):
            raise OverlayValidationError("allowed_actions must contain one or more TradeAction values")
        if len(set(allowed_actions)) != len(allowed_actions):
            raise OverlayValidationError("allowed_actions must not contain duplicates")
        if self.quant_action not in allowed_actions:
            raise OverlayValidationError("quant_action must be in allowed_actions")
        object.__setattr__(self, "allowed_actions", allowed_actions)

        evidence = tuple(self.evidence)
        if any(not isinstance(item, EvidenceItem) for item in evidence):
            raise OverlayValidationError("evidence must contain EvidenceItem values")
        ref_ids = tuple(item.ref_id for item in evidence)
        if len(set(ref_ids)) != len(ref_ids):
            raise OverlayValidationError("evidence ref_id values must be unique")
        future_refs = [item.ref_id for item in evidence if item.observed_date > self.as_of_date]
        if future_refs:
            raise OverlayValidationError("future evidence is not allowed: " + ", ".join(future_refs))
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(
            self,
            "hard_vetoes",
            _string_tuple(self.hard_vetoes, field="hard_vetoes", unique=True),
        )
        object.__setattr__(
            self,
            "hard_exit_reasons",
            _string_tuple(self.hard_exit_reasons, field="hard_exit_reasons", unique=True),
        )
        self._validate_quant_action_steps()
        if self.hard_exit_reasons:
            expected_action = TradeAction.EXIT if current_step > 0 else TradeAction.BLOCK
            if self.quant_action != expected_action or target_step != 0:
                raise OverlayValidationError("hard_exit_reasons require a zero-target quant EXIT/BLOCK")
        if self.hard_vetoes:
            if target_step > current_step:
                raise OverlayValidationError("hard_vetoes cannot increase the quant target step")
            if current_step == 0 and self.quant_action != TradeAction.BLOCK:
                raise OverlayValidationError("a flat hard-veto snapshot must use quant BLOCK")
        if self.hard_vetoes and current_step == 0 and TradeAction.BLOCK not in allowed_actions:
            raise OverlayValidationError("allowed_actions must include BLOCK when hard_vetoes are present")
        if self.hard_exit_reasons and current_step > 0 and TradeAction.EXIT not in allowed_actions:
            raise OverlayValidationError("allowed_actions must include EXIT when hard_exit_reasons are present")

    def _validate_quant_action_steps(self) -> None:
        if not _is_action_step_consistent(
            self.quant_action,
            current_step=self.current_step,
            target_step=self.quant_target_step,
        ):
            raise OverlayValidationError("quant_action is inconsistent with current_step and quant_target_step")

    @property
    def evidence_ref_ids(self) -> frozenset[str]:
        return frozenset(item.ref_id for item in self.evidence)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvidenceSnapshot:
        _strict_keys(data, cls._FIELDS, label="EvidenceSnapshot")
        if not isinstance(data["allowed_actions"], (list, tuple)):
            raise OverlayValidationError("allowed_actions must be an array")
        if not isinstance(data["evidence"], (list, tuple)):
            raise OverlayValidationError("evidence must be an array")
        return cls(
            schema_version=data["schema_version"],
            as_of_date=data["as_of_date"],
            last_bar_date=data["last_bar_date"],
            market_context_date=data["market_context_date"],
            ts_code=data["ts_code"],
            strategy_version=data["strategy_version"],
            parameter_version=data["parameter_version"],
            parameter_fingerprint=data["parameter_fingerprint"],
            quant_action=_enum_value(TradeAction, data["quant_action"], field="quant_action"),
            current_step=_strict_int(data["current_step"], field="current_step"),
            quant_target_step=_strict_int(data["quant_target_step"], field="quant_target_step"),
            max_step=_strict_int(data["max_step"], field="max_step"),
            allowed_actions=tuple(
                _enum_value(TradeAction, value, field=f"allowed_actions[{index}]")
                for index, value in enumerate(data["allowed_actions"])
            ),
            evidence=tuple(EvidenceItem.from_dict(item) for item in data["evidence"]),
            hard_vetoes=_string_tuple(data["hard_vetoes"], field="hard_vetoes"),
            hard_exit_reasons=_string_tuple(data["hard_exit_reasons"], field="hard_exit_reasons"),
            max_adjustment=_strict_int(data["max_adjustment"], field="max_adjustment"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "as_of_date": self.as_of_date,
            "last_bar_date": self.last_bar_date,
            "market_context_date": self.market_context_date,
            "ts_code": self.ts_code,
            "strategy_version": self.strategy_version,
            "parameter_version": self.parameter_version,
            "parameter_fingerprint": self.parameter_fingerprint,
            "quant_action": self.quant_action.value,
            "current_step": self.current_step,
            "quant_target_step": self.quant_target_step,
            "max_step": self.max_step,
            "allowed_actions": [action.value for action in self.allowed_actions],
            "evidence": [item.to_dict() for item in self.evidence],
            "hard_vetoes": list(self.hard_vetoes),
            "hard_exit_reasons": list(self.hard_exit_reasons),
            "max_adjustment": self.max_adjustment,
        }


@dataclass(frozen=True)
class OverlayProposal(_CanonicalContract):
    schema_version: str
    position_step_adjustment: int
    buy_disposition: BuyDisposition
    confidence: int
    agreement: QuantAgreement
    reasons: tuple[str, ...]
    counter_arguments: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    risk_flags: tuple[str, ...]
    requires_human_review: bool

    _FIELDS = {
        "schema_version",
        "position_step_adjustment",
        "buy_disposition",
        "confidence",
        "agreement",
        "reasons",
        "counter_arguments",
        "evidence_refs",
        "invalidation_conditions",
        "risk_flags",
        "requires_human_review",
    }

    def __post_init__(self) -> None:
        if self.schema_version != "overlay-decision-v1":
            raise OverlayValidationError("schema_version must be overlay-decision-v1")
        adjustment = _strict_int(self.position_step_adjustment, field="position_step_adjustment")
        if adjustment not in (-1, 0, 1):
            raise OverlayValidationError("position_step_adjustment must be one of -1, 0, 1")
        if not isinstance(self.buy_disposition, BuyDisposition):
            raise OverlayValidationError("buy_disposition must be a BuyDisposition")
        confidence = _strict_int(self.confidence, field="confidence")
        if not 0 <= confidence <= 100:
            raise OverlayValidationError("confidence must be between 0 and 100")
        if not isinstance(self.agreement, QuantAgreement):
            raise OverlayValidationError("agreement must be a QuantAgreement")
        object.__setattr__(
            self,
            "reasons",
            _string_tuple(self.reasons, field="reasons", allow_empty=False),
        )
        object.__setattr__(
            self,
            "counter_arguments",
            _string_tuple(self.counter_arguments, field="counter_arguments"),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _string_tuple(
                self.evidence_refs,
                field="evidence_refs",
                allow_empty=False,
                unique=True,
            ),
        )
        object.__setattr__(
            self,
            "invalidation_conditions",
            _string_tuple(self.invalidation_conditions, field="invalidation_conditions"),
        )
        object.__setattr__(
            self,
            "risk_flags",
            _string_tuple(self.risk_flags, field="risk_flags", unique=True),
        )
        if not isinstance(self.requires_human_review, bool):
            raise OverlayValidationError("requires_human_review must be a boolean")

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        snapshot: EvidenceSnapshot,
    ) -> OverlayProposal:
        _strict_keys(data, cls._FIELDS, label="OverlayProposal")
        proposal = cls(
            schema_version=data["schema_version"],
            position_step_adjustment=_strict_int(
                data["position_step_adjustment"],
                field="position_step_adjustment",
            ),
            buy_disposition=_enum_value(BuyDisposition, data["buy_disposition"], field="buy_disposition"),
            confidence=_strict_int(data["confidence"], field="confidence"),
            agreement=_enum_value(QuantAgreement, data["agreement"], field="agreement"),
            reasons=_string_tuple(data["reasons"], field="reasons"),
            counter_arguments=_string_tuple(data["counter_arguments"], field="counter_arguments"),
            evidence_refs=_string_tuple(data["evidence_refs"], field="evidence_refs"),
            invalidation_conditions=_string_tuple(
                data["invalidation_conditions"],
                field="invalidation_conditions",
            ),
            risk_flags=_string_tuple(data["risk_flags"], field="risk_flags"),
            requires_human_review=data["requires_human_review"],
        )
        proposal.validate_against(snapshot)
        return proposal

    def validate_against(self, snapshot: EvidenceSnapshot) -> None:
        if abs(self.position_step_adjustment) > snapshot.max_adjustment:
            raise OverlayValidationError("position_step_adjustment exceeds snapshot.max_adjustment")
        unknown = sorted(set(self.evidence_refs) - snapshot.evidence_ref_ids)
        if unknown:
            raise OverlayValidationError("unknown evidence_ref values: " + ", ".join(unknown))
        if self.buy_disposition != BuyDisposition.UNCHANGED:
            if snapshot.quant_action not in (TradeAction.OPEN, TradeAction.ADD):
                raise OverlayValidationError("buy_disposition WATCH/BLOCK requires quant OPEN or ADD")
            if self.position_step_adjustment > 0:
                raise OverlayValidationError("a downgraded buy cannot also request a positive adjustment")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "position_step_adjustment": self.position_step_adjustment,
            "buy_disposition": self.buy_disposition.value,
            "confidence": self.confidence,
            "agreement": self.agreement.value,
            "reasons": list(self.reasons),
            "counter_arguments": list(self.counter_arguments),
            "evidence_refs": list(self.evidence_refs),
            "invalidation_conditions": list(self.invalidation_conditions),
            "risk_flags": list(self.risk_flags),
            "requires_human_review": self.requires_human_review,
        }


@dataclass(frozen=True)
class GuardrailDecision(_CanonicalContract):
    schema_version: str
    mode: DecisionMode
    current_step: int
    quant_action: TradeAction
    quant_target_step: int
    final_action: TradeAction
    final_target_step: int
    max_step: int
    requested_position_step_adjustment: int | None
    buy_disposition: BuyDisposition | None
    requires_human_review: bool
    guardrail_events: tuple[GuardrailEvent, ...] = ()
    fallback_reason: FallbackReason | None = None

    _FIELDS = {
        "schema_version",
        "mode",
        "current_step",
        "quant_action",
        "quant_target_step",
        "final_action",
        "final_target_step",
        "max_step",
        "requested_position_step_adjustment",
        "buy_disposition",
        "requires_human_review",
        "guardrail_events",
        "fallback_reason",
    }

    def __post_init__(self) -> None:
        if self.schema_version != "guardrail-decision-v1":
            raise OverlayValidationError("schema_version must be guardrail-decision-v1")
        if not isinstance(self.mode, DecisionMode):
            raise OverlayValidationError("mode must be a DecisionMode")
        for name, action in (
            ("quant_action", self.quant_action),
            ("final_action", self.final_action),
        ):
            if not isinstance(action, TradeAction):
                raise OverlayValidationError(f"{name} must be a TradeAction")
        current = _strict_int(self.current_step, field="current_step")
        quant_target = _strict_int(self.quant_target_step, field="quant_target_step")
        final_target = _strict_int(self.final_target_step, field="final_target_step")
        max_step = _strict_int(self.max_step, field="max_step")
        # As in EvidenceSnapshot, max_step + 1 denotes an existing overflow
        # position.  Quant and final targets remain capped at max_step.
        if max_step < 1 or not 0 <= current <= max_step + 1:
            raise OverlayValidationError("decision step bounds are invalid")
        if not 0 <= quant_target <= max_step or not 0 <= final_target <= max_step:
            raise OverlayValidationError("decision target steps are out of bounds")
        if any(not isinstance(event, GuardrailEvent) for event in self.guardrail_events):
            raise OverlayValidationError("guardrail_events must contain GuardrailEvent values")
        object.__setattr__(self, "guardrail_events", tuple(self.guardrail_events))
        if len(set(self.guardrail_events)) != len(self.guardrail_events):
            raise OverlayValidationError("guardrail_events must not contain duplicates")
        if not _is_action_step_consistent(
            self.quant_action,
            current_step=current,
            target_step=quant_target,
        ):
            raise OverlayValidationError("quant_action is inconsistent with current_step and quant_target_step")
        if not _is_action_step_consistent(
            self.final_action,
            current_step=current,
            target_step=final_target,
        ):
            raise OverlayValidationError("final_action is inconsistent with current_step and final_target_step")

        if self.mode == DecisionMode.QUANT_ONLY:
            if self.final_action != self.quant_action or final_target != quant_target:
                raise OverlayValidationError("QUANT_ONLY must preserve the exact quant action and target")
            if self.requested_position_step_adjustment is not None or self.buy_disposition is not None:
                raise OverlayValidationError("QUANT_ONLY cannot contain an overlay proposal")
        else:
            requested = _strict_int(
                self.requested_position_step_adjustment,
                field="requested_position_step_adjustment",
            )
            if requested not in (-1, 0, 1):
                raise OverlayValidationError("requested_position_step_adjustment must be -1, 0, or 1")
            if not isinstance(self.buy_disposition, BuyDisposition):
                raise OverlayValidationError("QUANT_LLM_OVERLAY requires buy_disposition")
            if self.fallback_reason is not None:
                raise OverlayValidationError("QUANT_LLM_OVERLAY cannot contain fallback_reason")
        if self.fallback_reason is not None and not isinstance(self.fallback_reason, FallbackReason):
            raise OverlayValidationError("fallback_reason must be a FallbackReason or null")
        if not isinstance(self.requires_human_review, bool):
            raise OverlayValidationError("requires_human_review must be a boolean")
        if self.mode == DecisionMode.QUANT_ONLY and self.requires_human_review:
            raise OverlayValidationError("QUANT_ONLY cannot require review for a discarded proposal")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GuardrailDecision:
        _strict_keys(data, cls._FIELDS, label="GuardrailDecision")
        events = data["guardrail_events"]
        if not isinstance(events, (list, tuple)):
            raise OverlayValidationError("guardrail_events must be an array")
        disposition = data["buy_disposition"]
        fallback = data["fallback_reason"]
        requested = data["requested_position_step_adjustment"]
        return cls(
            schema_version=data["schema_version"],
            mode=_enum_value(DecisionMode, data["mode"], field="mode"),
            current_step=_strict_int(data["current_step"], field="current_step"),
            quant_action=_enum_value(TradeAction, data["quant_action"], field="quant_action"),
            quant_target_step=_strict_int(data["quant_target_step"], field="quant_target_step"),
            final_action=_enum_value(TradeAction, data["final_action"], field="final_action"),
            final_target_step=_strict_int(data["final_target_step"], field="final_target_step"),
            max_step=_strict_int(data["max_step"], field="max_step"),
            requested_position_step_adjustment=(
                None if requested is None else _strict_int(requested, field="requested_position_step_adjustment")
            ),
            buy_disposition=(
                None if disposition is None else _enum_value(BuyDisposition, disposition, field="buy_disposition")
            ),
            requires_human_review=data["requires_human_review"],
            guardrail_events=tuple(
                _enum_value(GuardrailEvent, value, field=f"guardrail_events[{index}]")
                for index, value in enumerate(events)
            ),
            fallback_reason=(
                None if fallback is None else _enum_value(FallbackReason, fallback, field="fallback_reason")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "current_step": self.current_step,
            "quant_action": self.quant_action.value,
            "quant_target_step": self.quant_target_step,
            "final_action": self.final_action.value,
            "final_target_step": self.final_target_step,
            "max_step": self.max_step,
            "requested_position_step_adjustment": self.requested_position_step_adjustment,
            "buy_disposition": (self.buy_disposition.value if self.buy_disposition else None),
            "requires_human_review": self.requires_human_review,
            "guardrail_events": [event.value for event in self.guardrail_events],
            "fallback_reason": (self.fallback_reason.value if self.fallback_reason else None),
        }
