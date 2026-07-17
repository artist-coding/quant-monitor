"""Deterministic lifecycle transitions for one stock after each market day."""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Sequence

from .dates import normalize_trade_date
from .models import LifecycleState, OrderSide, PendingOrder, PositionState


@dataclass(frozen=True)
class LifecycleTransition:
    trade_date: str
    from_state: LifecycleState
    to_state: LifecycleState
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_date", normalize_trade_date(self.trade_date))


def resolve_lifecycle_after_day(
    position: PositionState,
    pending_orders: Sequence[PendingOrder],
    *,
    trade_date: str,
    from_state: LifecycleState | None = None,
    sell_blocked: bool = False,
    had_buy_fill: bool = False,
    had_sell_fill: bool = False,
) -> tuple[PositionState, LifecycleTransition]:
    """Resolve exactly one close-state using risk-first transition priority."""

    if any(order.ts_code != position.ts_code for order in pending_orders):
        raise ValueError("pending orders must match the position stock")
    origin = from_state or position.lifecycle_state
    has_pending_sell = any(order.side == OrderSide.SELL for order in pending_orders)
    has_pending_buy = any(order.side == OrderSide.BUY for order in pending_orders)

    if sell_blocked and position.shares > 0:
        target = LifecycleState.LOCKED
        reason = "sell_blocked"
    elif has_pending_sell:
        target = LifecycleState.PENDING_SELL
        reason = "pending_sell"
    elif has_pending_buy:
        target = LifecycleState.PENDING_BUY
        reason = "pending_buy"
    elif position.shares == 0:
        if position.lifecycle_state == LifecycleState.EXITED or had_sell_fill:
            target = LifecycleState.EXITED
            reason = "position_exited"
        else:
            target = LifecycleState.FLAT
            reason = "no_position"
    elif had_buy_fill:
        target = LifecycleState.BUILDING
        reason = "buy_filled"
    elif had_sell_fill:
        target = LifecycleState.REDUCING
        reason = "sell_filled"
    else:
        target = LifecycleState.HOLDING
        reason = "position_aligned"

    updated = replace(position, lifecycle_state=target)
    return updated, LifecycleTransition(
        trade_date=trade_date,
        from_state=origin,
        to_state=target,
        reason=reason,
    )
