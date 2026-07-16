"""Tests for the explicit buy-point confirmation gate."""

from modules.daily_portfolio.buy_points import BuyPointStatus, assess_buy_point
from modules.daily_portfolio.evidence_adapter import _resonance_score
from modules.daily_portfolio.models import PositionState, TradeAction
from modules.daily_portfolio.position_policy import (
    PositionPolicyInput,
    evaluate_position_policy,
)
from modules.daily_portfolio.score_engine import AggregatedScores
from modules.daily_portfolio.strategy_features import (
    DailyStrategyFeatures,
    VariantEvidence,
)


TS_CODE = "000001.SZ"
SIGNAL_DATE = "20260710"


def _features(
    variants: dict[str, VariantEvidence],
    *,
    hard_vetoes: tuple[str, ...] = (),
) -> DailyStrategyFeatures:
    return DailyStrategyFeatures(
        ts_code=TS_CODE,
        signal_date=SIGNAL_DATE,
        bars_end_date=SIGNAL_DATE,
        feature_version="test-features",
        required_bars=120,
        missing_fields=(),
        continuous_features={},
        boolean_features={},
        sequence_anchors={},
        variant_evidence=variants,
        eligibility_gates={"has_required_history": True},
        hard_vetoes=hard_vetoes,
    )


def _scores(
    buy: float = 80,
    sell: float = 10,
    *,
    hard_vetoes: tuple[str, ...] = (),
    hard_exit_reasons: tuple[str, ...] = (),
) -> AggregatedScores:
    return AggregatedScores(
        buy_score=buy,
        sell_score=sell,
        buy_contributions={},
        sell_contributions={},
        hard_vetoes=hard_vetoes,
        hard_exit_reasons=hard_exit_reasons,
    )


def test_high_context_score_without_a_matched_entry_variant_is_not_confirmed() -> None:
    result = assess_buy_point(
        _features(
            {
                "b1.loose_3of4": VariantEvidence(
                    "b1.loose_3of4", matched=False, strength=50
                )
            }
        ),
        _scores(buy=92),
        reference_close=10,
        planned_stop_loss=9,
    )

    assert result.status == BuyPointStatus.NO_SETUP
    assert result.confirmed is False
    assert result.setup_matched is False
    assert result.blocking_reasons == ("NO_CONFIRMED_ENTRY_VARIANT",)


def test_matched_variant_score_and_valid_stop_confirm_the_buy_point() -> None:
    result = assess_buy_point(
        _features(
            {
                "b1.quality_confirmed": VariantEvidence(
                    "b1.quality_confirmed", matched=True, strength=100
                ),
                "b3.consensus_continuation": VariantEvidence(
                    "b3.consensus_continuation", matched=True, strength=75
                ),
            }
        ),
        _scores(buy=84),
        reference_close=10,
        planned_stop_loss=9,
    )

    assert result.status == BuyPointStatus.CONFIRMED
    assert result.confirmed is True
    assert result.primary_variant == "b1.quality_confirmed"
    assert result.matched_variants == (
        "b1.quality_confirmed",
        "b3.consensus_continuation",
    )
    assert result.estimated_risk_pct == 0.1
    assert result.confirmation_setup_matched is True
    assert result.primary_confirming_variant == "b1.quality_confirmed"
    assert result.execution_timing == "NEXT_TRADING_DAY_OPEN"
    assert result.rule_qualification == "UNVALIDATED_RESEARCH_RULE"


def test_research_only_loose_b1_and_b3_cannot_authorize_an_entry() -> None:
    result = assess_buy_point(
        _features(
            {
                "b1.loose_3of4": VariantEvidence(
                    "b1.loose_3of4", matched=True, strength=100
                ),
                "b3.consensus_continuation": VariantEvidence(
                    "b3.consensus_continuation", matched=True, strength=100
                ),
            }
        ),
        _scores(buy=95),
        reference_close=10,
        planned_stop_loss=9,
    )

    assert result.status == BuyPointStatus.CANDIDATE
    assert result.setup_matched is True
    assert result.confirmation_setup_matched is False
    assert result.confirming_variants == ()
    assert result.blocking_reasons == (
        "RESEARCH_VARIANT_NOT_CONFIRMATION_QUALIFIED",
    )


def test_sell_evidence_and_unconfirmed_false_break_do_not_raise_buy_resonance() -> None:
    features = _features(
        {
            "s1.ugly_hat": VariantEvidence("s1.ugly_hat", True, 90),
            "s2.anchor_divergence": VariantEvidence(
                "s2.anchor_divergence", True, 85
            ),
            "sb1.false_break": VariantEvidence("sb1.false_break", True, 100),
        }
    )

    score, families = _resonance_score(features)

    assert score == 0
    assert families == ()


def test_only_whitelisted_matched_entry_families_contribute_to_resonance() -> None:
    features = _features(
        {
            "b1.strict_oversold": VariantEvidence(
                "b1.strict_oversold", True, 100
            ),
            "b1.quality_confirmed": VariantEvidence(
                "b1.quality_confirmed", True, 100
            ),
            "b2.knowledge_5bar": VariantEvidence(
                "b2.knowledge_5bar", True, 100
            ),
        }
    )

    score, families = _resonance_score(features)

    assert score == 50
    assert families == ("b1", "b2")


def test_invalid_signal_day_stop_blocks_an_otherwise_confirmed_setup() -> None:
    result = assess_buy_point(
        _features(
            {
                "b2.knowledge_5bar": VariantEvidence(
                    "b2.knowledge_5bar", True, 100
                )
            }
        ),
        _scores(buy=90),
        reference_close=10,
        planned_stop_loss=10,
    )

    assert result.status == BuyPointStatus.BLOCKED
    assert result.blocking_reasons == ("INVALID_SIGNAL_DAY_STOP",)


def test_hard_exit_blocks_buy_point_even_when_entry_evidence_is_strong() -> None:
    result = assess_buy_point(
        _features(
            {
                "b1.quality_confirmed": VariantEvidence(
                    "b1.quality_confirmed", True, 100
                )
            }
        ),
        _scores(buy=95, sell=100, hard_exit_reasons=("EXIT_STOP_LOSS",)),
        reference_close=10,
        planned_stop_loss=9,
    )

    assert result.status == BuyPointStatus.BLOCKED
    assert result.blocking_reasons == ("EXIT_STOP_LOSS",)


def test_position_policy_cannot_open_on_score_alone_when_entry_is_unconfirmed() -> None:
    decision = evaluate_position_policy(
        PositionState(ts_code=TS_CODE),
        PositionPolicyInput(
            signal_date=SIGNAL_DATE,
            buy_score=95,
            sell_score=10,
            stop_loss=9,
            entry_confirmed=False,
            entry_confirmation_reasons=("NO_CONFIRMED_ENTRY_VARIANT",),
        ),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.WATCH
    assert decision.decision_code == "entry_not_confirmed"
    assert "NO_CONFIRMED_ENTRY_VARIANT" in decision.daily_score.reasons
