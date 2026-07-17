"""Lifecycle-state priority and replay integration tests."""

from modules.daily_portfolio.execution import create_buy_order, create_sell_order
from modules.daily_portfolio.holding_state_machine import resolve_lifecycle_after_day
from modules.daily_portfolio.models import (
    DailyStockScore,
    ExecutionMode,
    LifecycleState,
    PositionState,
    TradeAction,
)


def _score(action: TradeAction, target: float) -> DailyStockScore:
    return DailyStockScore(
        ts_code="000001.SZ",
        signal_date="20260710",
        buy_score=80,
        sell_score=20,
        position_score=75,
        current_position_pct=0.10 if action in (TradeAction.REDUCE, TradeAction.EXIT) else 0,
        target_position_pct=target,
        desired_action=action,
        stop_loss=9,
    )


def _held() -> PositionState:
    return PositionState(
        ts_code="000001.SZ",
        lifecycle_state=LifecycleState.HOLDING,
        shares=1000,
        available_shares=1000,
        avg_cost=10,
        current_position_pct=0.10,
    )


def test_pending_buy_and_sell_states_are_explicit() -> None:
    buy = create_buy_order(_score(TradeAction.OPEN, 0.10), "20260713")
    flat, flat_transition = resolve_lifecycle_after_day(
        PositionState(ts_code="000001.SZ"),
        [buy],
        trade_date="20260710",
    )
    assert flat.lifecycle_state == LifecycleState.PENDING_BUY
    assert flat_transition.reason == "pending_buy"

    sell = create_sell_order(
        _score(TradeAction.EXIT, 0),
        ExecutionMode.NEXT_OPEN_STRICT,
        "20260713",
    )
    held, held_transition = resolve_lifecycle_after_day(_held(), [sell], trade_date="20260710")
    assert held.lifecycle_state == LifecycleState.PENDING_SELL
    assert held_transition.reason == "pending_sell"


def test_blocked_sell_has_priority_over_pending_state() -> None:
    sell = create_sell_order(
        _score(TradeAction.EXIT, 0),
        ExecutionMode.NEXT_OPEN_STRICT,
        "20260713",
    )
    position, transition = resolve_lifecycle_after_day(
        _held(),
        [sell],
        trade_date="20260710",
        sell_blocked=True,
    )
    assert position.lifecycle_state == LifecycleState.LOCKED
    assert transition.reason == "sell_blocked"


def test_fill_states_settle_to_holding_on_a_later_quiet_day() -> None:
    building, _ = resolve_lifecycle_after_day(_held(), [], trade_date="20260710", had_buy_fill=True)
    assert building.lifecycle_state == LifecycleState.BUILDING

    holding, transition = resolve_lifecycle_after_day(building, [], trade_date="20260713")
    assert holding.lifecycle_state == LifecycleState.HOLDING
    assert transition.from_state == LifecycleState.BUILDING
