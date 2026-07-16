"""Deterministic confirmation contract for one daily buy point.

The continuous ``buy_score`` answers how attractive the current evidence is.
It is not, by itself, permission to create exposure.  A confirmed buy point
also needs an explicitly matched entry variant, a valid frozen stop and no
buy/sell score conflict.  Keeping those concepts separate lets research score
every day without silently turning a high contextual score into an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

from .config import ScoreThresholds
from .dates import normalize_trade_date
from .score_engine import AggregatedScores
from .strategy_features import DailyStrategyFeatures


# Only these variants are allowed to confirm or positively resonate with an
# entry.  In particular, SB1 false-break evidence and S1/S2/S3 exit evidence
# must never increase entry eligibility.
RESEARCH_ENTRY_VARIANT_PRIORITY: tuple[str, ...] = (
    "b1.quality_confirmed",
    "b2.knowledge_5bar",
    "sb1.reclaim_confirmation",
    "super_b1.washout",
    "b1.strict_oversold",
    "b3.pullback_reentry",
    "b2.legacy_5_14bar",
    "b3.consensus_continuation",
    "b1.loose_3of4",
)
ENTRY_VARIANT_PRIORITY = RESEARCH_ENTRY_VARIANT_PRIORITY
ENTRY_VARIANT_NAMES = frozenset(ENTRY_VARIANT_PRIORITY)
CONFIRMING_ENTRY_VARIANT_PRIORITY: tuple[str, ...] = (
    "b1.quality_confirmed",
    "b2.knowledge_5bar",
    "sb1.reclaim_confirmation",
    "super_b1.washout",
)
CONFIRMING_ENTRY_VARIANT_NAMES = frozenset(CONFIRMING_ENTRY_VARIANT_PRIORITY)
CONFIRMATION_POLICY_VERSION = "buy-confirmation-policy-v0.2"


class BuyPointStatus(str, Enum):
    BLOCKED = "BLOCKED"
    NO_SETUP = "NO_SETUP"
    CANDIDATE = "CANDIDATE"
    CONFLICT = "CONFLICT"
    CONFIRMED = "CONFIRMED"


@dataclass(frozen=True)
class BuyPointAssessment:
    """Auditable conclusion made at the signal-day close."""

    ts_code: str
    signal_date: str
    status: BuyPointStatus
    confirmed: bool
    setup_matched: bool
    confirmation_setup_matched: bool
    buy_score: float
    sell_score: float
    confirmation_threshold: float
    reference_close: float
    planned_stop_loss: float | None
    estimated_risk_pct: float | None
    primary_variant: str
    matched_variants: tuple[str, ...]
    confirming_variants: tuple[str, ...]
    variant_strengths: dict[str, float]
    primary_confirming_variant: str
    blocking_reasons: tuple[str, ...]
    execution_timing: str = "NEXT_TRADING_DAY_OPEN"
    rule_qualification: str = "UNVALIDATED_RESEARCH_RULE"
    confirmation_policy_version: str = CONFIRMATION_POLICY_VERSION

    def __post_init__(self) -> None:
        if not self.ts_code:
            raise ValueError("ts_code cannot be empty")
        object.__setattr__(self, "signal_date", normalize_trade_date(self.signal_date))
        if not 0 <= self.buy_score <= 100 or not 0 <= self.sell_score <= 100:
            raise ValueError("buy and sell scores must be between 0 and 100")
        if not 0 <= self.confirmation_threshold <= 100:
            raise ValueError("confirmation_threshold must be between 0 and 100")
        if not math.isfinite(self.reference_close) or self.reference_close <= 0:
            raise ValueError("reference_close must be finite and positive")
        if self.planned_stop_loss is not None and (
            not math.isfinite(self.planned_stop_loss) or self.planned_stop_loss <= 0
        ):
            raise ValueError("planned_stop_loss must be finite and positive")
        if self.estimated_risk_pct is not None and (
            not math.isfinite(self.estimated_risk_pct)
            or not 0 < self.estimated_risk_pct < 1
        ):
            raise ValueError("estimated_risk_pct must be in (0, 1)")
        if self.confirmed != (self.status == BuyPointStatus.CONFIRMED):
            raise ValueError("confirmed must agree with status")
        if not self.rule_qualification:
            raise ValueError("rule_qualification cannot be empty")
        if self.setup_matched != bool(self.matched_variants):
            raise ValueError("setup_matched must agree with matched_variants")
        if self.confirmation_setup_matched != bool(self.confirming_variants):
            raise ValueError(
                "confirmation_setup_matched must agree with confirming_variants"
            )
        if self.primary_variant and self.primary_variant not in self.matched_variants:
            raise ValueError("primary_variant must be one of matched_variants")
        if (
            self.primary_confirming_variant
            and self.primary_confirming_variant not in self.confirming_variants
        ):
            raise ValueError(
                "primary_confirming_variant must be one of confirming_variants"
            )
        if not set(self.confirming_variants).issubset(self.matched_variants):
            raise ValueError("confirming_variants must be matched research variants")
        if set(self.variant_strengths) != set(self.matched_variants):
            raise ValueError("variant_strengths must describe every matched variant")
        if not self.confirmation_policy_version:
            raise ValueError("confirmation_policy_version cannot be empty")


def matched_entry_variants(features: DailyStrategyFeatures) -> tuple[str, ...]:
    """Return matched entry variants in stable research priority order."""

    return tuple(
        name
        for name in ENTRY_VARIANT_PRIORITY
        if (evidence := features.variant_evidence.get(name)) is not None
        and evidence.matched
    )


def matched_confirming_entry_variants(
    features: DailyStrategyFeatures,
) -> tuple[str, ...]:
    """Return only variants currently allowed to authorize an entry."""

    return tuple(
        name
        for name in CONFIRMING_ENTRY_VARIANT_PRIORITY
        if (evidence := features.variant_evidence.get(name)) is not None
        and evidence.matched
    )


def assess_buy_point(
    features: DailyStrategyFeatures,
    scores: AggregatedScores,
    *,
    reference_close: float,
    planned_stop_loss: float | None,
    thresholds: ScoreThresholds | None = None,
    for_add: bool = False,
) -> BuyPointAssessment:
    """Separate a continuous score from a confirmed, executable-later setup."""

    resolved_thresholds = thresholds or ScoreThresholds()
    threshold = (
        resolved_thresholds.add_buy_score
        if for_add
        else resolved_thresholds.open_buy_score
    )
    matched = matched_entry_variants(features)
    confirming = matched_confirming_entry_variants(features)
    strengths = {
        name: float(features.variant_evidence[name].strength or 0.0)
        for name in matched
    }
    primary = matched[0] if matched else ""
    primary_confirming = confirming[0] if confirming else ""
    reasons: list[str] = []

    if scores.hard_exit_reasons:
        status = BuyPointStatus.BLOCKED
        reasons.extend(scores.hard_exit_reasons)
    elif scores.hard_vetoes:
        status = BuyPointStatus.BLOCKED
        reasons.extend(scores.hard_vetoes)
    elif planned_stop_loss is None or planned_stop_loss >= reference_close:
        status = BuyPointStatus.BLOCKED
        reasons.append("INVALID_SIGNAL_DAY_STOP")
    elif not matched:
        status = BuyPointStatus.NO_SETUP
        reasons.append("NO_CONFIRMED_ENTRY_VARIANT")
    elif (
        scores.buy_score >= resolved_thresholds.conflict_score
        and scores.sell_score >= resolved_thresholds.conflict_score
    ):
        status = BuyPointStatus.CONFLICT
        reasons.append("BUY_SELL_SCORE_CONFLICT")
    elif not confirming:
        status = BuyPointStatus.CANDIDATE
        reasons.append("RESEARCH_VARIANT_NOT_CONFIRMATION_QUALIFIED")
    elif scores.buy_score < threshold:
        status = BuyPointStatus.CANDIDATE
        reasons.append("BUY_SCORE_BELOW_CONFIRMATION_THRESHOLD")
    else:
        status = BuyPointStatus.CONFIRMED

    risk_pct = None
    if planned_stop_loss is not None and 0 < planned_stop_loss < reference_close:
        risk_pct = (reference_close - planned_stop_loss) / reference_close

    return BuyPointAssessment(
        ts_code=features.ts_code,
        signal_date=features.signal_date,
        status=status,
        confirmed=status == BuyPointStatus.CONFIRMED,
        setup_matched=bool(matched),
        confirmation_setup_matched=bool(confirming),
        buy_score=scores.buy_score,
        sell_score=scores.sell_score,
        confirmation_threshold=threshold,
        reference_close=reference_close,
        planned_stop_loss=planned_stop_loss,
        estimated_risk_pct=round(risk_pct, 8) if risk_pct is not None else None,
        primary_variant=primary,
        matched_variants=matched,
        confirming_variants=confirming,
        variant_strengths=strengths,
        primary_confirming_variant=primary_confirming,
        blocking_reasons=tuple(reasons),
    )


__all__ = [
    "BuyPointAssessment",
    "BuyPointStatus",
    "CONFIRMATION_POLICY_VERSION",
    "CONFIRMING_ENTRY_VARIANT_NAMES",
    "CONFIRMING_ENTRY_VARIANT_PRIORITY",
    "ENTRY_VARIANT_NAMES",
    "ENTRY_VARIANT_PRIORITY",
    "RESEARCH_ENTRY_VARIANT_PRIORITY",
    "assess_buy_point",
    "matched_entry_variants",
    "matched_confirming_entry_variants",
]
