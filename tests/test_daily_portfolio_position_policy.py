"""Deterministic tests for the daily position-policy layer."""

from __future__ import annotations

import pytest

from modules.daily_portfolio.config import ScoreThresholds
from modules.daily_portfolio.models import LifecycleState, PositionState, TradeAction
from modules.daily_portfolio.position_policy import (
    PositionPolicyInput,
    evaluate_position_policy,
    score_to_ladder_ratio,
)


def _position(position_pct: float = 0.0) -> PositionState:
    shares = 100 if position_pct > 0 else 0
    return PositionState(
        ts_code="000001.SZ",
        lifecycle_state=(LifecycleState.HOLDING if shares else LifecycleState.FLAT),
        shares=shares,
        available_shares=shares,
        avg_cost=10.0 if shares else 0.0,
        current_position_pct=position_pct,
    )


def _signals(
    buy_score: float,
    sell_score: float,
    **kwargs,
) -> PositionPolicyInput:
    kwargs.setdefault("entry_confirmed", True)
    return PositionPolicyInput(
        signal_date="20260710",
        buy_score=buy_score,
        sell_score=sell_score,
        **kwargs,
    )


def test_policy_input_fails_closed_without_explicit_entry_confirmation() -> None:
    decision = evaluate_position_policy(
        _position(),
        PositionPolicyInput(
            signal_date="20260710",
            buy_score=95,
            sell_score=10,
        ),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.WATCH
    assert decision.decision_code == "entry_not_confirmed"


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, 0.0),
        (19.999, 0.0),
        (20, 0.25),
        (39.999, 0.25),
        (40, 0.50),
        (60, 0.75),
        (79.999, 0.75),
        (80, 1.0),
        (100, 1.0),
    ],
)
def test_position_score_maps_to_documented_ladder(score: float, expected: float) -> None:
    assert score_to_ladder_ratio(score) == expected


def test_flat_high_buy_opens_at_scaled_target_position() -> None:
    decision = evaluate_position_policy(
        _position(),
        _signals(75, 20),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.OPEN
    assert decision.target_ladder_ratio == 0.75
    assert decision.daily_score.target_position_pct == pytest.approx(0.075)
    assert decision.daily_score.position_score == 75
    assert decision.decision_code == "open"


def test_flat_low_buy_only_watches() -> None:
    decision = evaluate_position_policy(_position(), _signals(74, 20), max_position_pct=0.10)

    assert decision.daily_score.desired_action == TradeAction.WATCH
    assert decision.target_ladder_ratio == 0.0


def test_flat_conflicting_scores_do_not_open() -> None:
    decision = evaluate_position_policy(_position(), _signals(90, 75), max_position_pct=0.10)

    assert decision.has_score_conflict is True
    assert decision.daily_score.desired_action == TradeAction.WATCH
    assert decision.target_ladder_ratio == 0.0
    assert decision.decision_code == "score_conflict"


def test_held_conflict_cannot_add_when_sell_score_is_below_reduce_line() -> None:
    thresholds = ScoreThresholds(
        open_buy_score=75,
        add_buy_score=82,
        reduce_sell_score=90,
        exit_sell_score=95,
        conflict_score=70,
    )
    decision = evaluate_position_policy(
        _position(0.05),
        _signals(90, 75),
        max_position_pct=0.10,
        thresholds=thresholds,
    )

    assert decision.has_score_conflict is True
    assert decision.daily_score.desired_action == TradeAction.HOLD
    assert decision.target_ladder_ratio == 0.50


def test_held_conflict_reduces_when_sell_score_reaches_reduce_line() -> None:
    decision = evaluate_position_policy(
        _position(0.05),
        _signals(90, 75),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.REDUCE
    assert decision.target_ladder_ratio == 0.25
    assert decision.decision_code == "sell_reduce"


def test_hard_exit_overrides_strong_buy_score() -> None:
    decision = evaluate_position_policy(
        _position(0.075),
        _signals(100, 0, hard_exit_reasons=("stop_loss",)),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.EXIT
    assert decision.target_ladder_ratio == 0.0
    assert decision.daily_score.vetoes == ()
    assert decision.daily_score.hard_exit_reasons == ("stop_loss",)
    assert decision.decision_code == "hard_exit"


def test_hard_exit_without_a_position_is_blocked() -> None:
    decision = evaluate_position_policy(
        _position(),
        _signals(100, 0, hard_exit_reasons=("invalid_price",)),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.BLOCK
    assert decision.target_ladder_ratio == 0.0


def test_hard_veto_blocks_opening() -> None:
    decision = evaluate_position_policy(
        _position(),
        _signals(100, 0, hard_vetoes=("suspended",)),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.BLOCK
    assert decision.decision_code == "hard_veto"


def test_hard_veto_prevents_add_but_does_not_force_an_exit() -> None:
    decision = evaluate_position_policy(
        _position(0.05),
        _signals(100, 0, hard_vetoes=("entry_quality",)),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.HOLD
    assert decision.target_ladder_ratio == 0.50


def test_hard_veto_does_not_hide_required_sell_exit() -> None:
    decision = evaluate_position_policy(
        _position(0.05),
        _signals(100, 90, hard_vetoes=("entry_quality",)),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.EXIT
    assert decision.decision_code == "sell_exit"


def test_sell_score_reduces_exactly_one_ladder_step() -> None:
    decision = evaluate_position_policy(
        _position(0.075),
        _signals(20, 70),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.REDUCE
    assert decision.current_ladder_ratio == 0.75
    assert decision.target_ladder_ratio == 0.50
    assert decision.daily_score.target_position_pct == pytest.approx(0.05)


def test_reducing_from_first_positive_rung_becomes_exit() -> None:
    decision = evaluate_position_policy(
        _position(0.025),
        _signals(20, 70),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.EXIT
    assert decision.target_ladder_ratio == 0.0


def test_high_buy_score_adds_to_higher_target_rung() -> None:
    decision = evaluate_position_policy(
        _position(0.05),
        _signals(82, 20),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.ADD
    assert decision.target_ladder_ratio == 1.0
    assert decision.daily_score.target_position_pct == pytest.approx(0.10)


def test_buy_score_falling_does_not_reduce_without_sell_pressure() -> None:
    decision = evaluate_position_policy(
        _position(0.075),
        _signals(20, 20),
        max_position_pct=0.10,
    )

    assert decision.daily_score.desired_action == TradeAction.HOLD
    assert decision.target_ladder_ratio == 0.75


def test_policy_preserves_trace_fields_without_mutating_inputs() -> None:
    buy_contributions = {"trend": 20.0}
    sell_contributions = {"stop": 0.0}
    signals = _signals(
        75,
        20,
        stop_loss=9.5,
        reasons=("b1",),
        buy_contributions=buy_contributions,
        sell_contributions=sell_contributions,
        strategy_version="strategy-test",
        parameter_version="params-test",
    )

    first = evaluate_position_policy(_position(), signals, max_position_pct=0.10)
    second = evaluate_position_policy(_position(), signals, max_position_pct=0.10)

    assert first == second
    assert first.daily_score.reasons == ("b1", "position_policy:open")
    assert first.daily_score.buy_contributions == buy_contributions
    assert first.daily_score.sell_contributions == sell_contributions
    assert first.daily_score.strategy_version == "strategy-test"
    assert first.daily_score.parameter_version == "params-test"
    assert buy_contributions == {"trend": 20.0}
    assert sell_contributions == {"stop": 0.0}


@pytest.mark.parametrize("max_position_pct", [0, -0.01, 1.01])
def test_max_position_pct_is_required_in_actual_portfolio_domain(max_position_pct: float) -> None:
    with pytest.raises(ValueError, match="max_position_pct"):
        evaluate_position_policy(
            _position(),
            _signals(75, 20),
            max_position_pct=max_position_pct,
        )


def test_invalid_ladder_is_rejected() -> None:
    with pytest.raises(ValueError, match="position_ladder"):
        score_to_ladder_ratio(80, (0.0, 0.5, 0.5, 1.0))


def test_minimum_position_delta_suppresses_tiny_open_adjustment() -> None:
    decision = evaluate_position_policy(
        _position(),
        _signals(75, 20),
        max_position_pct=0.02,
        minimum_position_delta_pct=0.02,
    )

    assert decision.daily_score.desired_action == TradeAction.WATCH
    assert decision.daily_score.target_position_pct == 0
    assert decision.decision_code == "minimum_position_delta"


def test_minimum_position_delta_never_suppresses_risk_reduction() -> None:
    decision = evaluate_position_policy(
        _position(0.05),
        _signals(20, 75),
        max_position_pct=0.05,
        minimum_position_delta_pct=0.02,
    )
    assert decision.daily_score.desired_action == TradeAction.REDUCE
    assert decision.daily_score.target_position_pct == pytest.approx(0.0375)


def test_position_above_hard_max_is_forced_to_reduce() -> None:
    decision = evaluate_position_policy(
        _position(0.20),
        _signals(0, 0),
        max_position_pct=0.10,
    )
    assert decision.daily_score.desired_action == TradeAction.REDUCE
    assert decision.daily_score.target_position_pct == pytest.approx(0.10)
    assert decision.decision_code == "position_limit_reduce"
