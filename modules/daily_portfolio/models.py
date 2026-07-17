"""日线持仓系统的领域对象。

这些对象只描述评分、持仓状态和待执行订单，不包含具体战法实现。
把交易时间语义集中在这里，可以避免回测、每日任务和前端各自解释。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .dates import normalize_trade_date


class TradeAction(str, Enum):
    OPEN = "OPEN"
    ADD = "ADD"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    WATCH = "WATCH"
    BLOCK = "BLOCK"


class LifecycleState(str, Enum):
    FLAT = "FLAT"
    READY = "READY"
    PENDING_BUY = "PENDING_BUY"
    BUILDING = "BUILDING"
    HOLDING = "HOLDING"
    PENDING_SELL = "PENDING_SELL"
    REDUCING = "REDUCING"
    LOCKED = "LOCKED"
    EXITED = "EXITED"


class ExecutionMode(str, Enum):
    """卖出执行口径。

    SAME_CLOSE_RESEARCH 使用完整 D 日K线后仍按 D 日收盘成交，带有
    同K线前视偏差，只允许用于研究对照。
    """

    SAME_CLOSE_RESEARCH = "SAME_CLOSE_RESEARCH"
    NEXT_OPEN_STRICT = "NEXT_OPEN_STRICT"


class PriceType(str, Enum):
    NEXT_OPEN = "NEXT_OPEN"
    SAME_CLOSE_RESEARCH = "SAME_CLOSE_RESEARCH"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


def _validate_score(name: str, value: float) -> None:
    if not 0 <= value <= 100:
        raise ValueError(f"{name} must be between 0 and 100, got {value}")


def _validate_pct(name: str, value: float) -> None:
    if not 0 <= value <= 1:
        raise ValueError(f"{name} must be between 0 and 1, got {value}")


@dataclass(frozen=True)
class PositionState:
    ts_code: str
    lifecycle_state: LifecycleState = LifecycleState.FLAT
    shares: int = 0
    available_shares: int = 0
    avg_cost: float = 0.0
    current_position_pct: float = 0.0
    stop_loss: float | None = None
    can_sell_date: str = ""

    def __post_init__(self) -> None:
        if not self.ts_code:
            raise ValueError("ts_code cannot be empty")
        if (
            isinstance(self.shares, bool)
            or isinstance(self.available_shares, bool)
            or not isinstance(self.shares, int)
            or not isinstance(self.available_shares, int)
        ):
            raise ValueError("share counts must be integers")
        if self.shares < 0 or self.available_shares < 0:
            raise ValueError("share counts cannot be negative")
        if self.available_shares > self.shares:
            raise ValueError("available_shares cannot exceed shares")
        if not math.isfinite(self.avg_cost) or self.avg_cost < 0:
            raise ValueError("avg_cost must be finite and non-negative")
        _validate_pct("current_position_pct", self.current_position_pct)
        if self.stop_loss is not None and (not math.isfinite(self.stop_loss) or self.stop_loss <= 0):
            raise ValueError("stop_loss must be finite and positive")
        if self.shares == 0:
            if self.available_shares != 0 or self.avg_cost != 0 or self.current_position_pct != 0:
                raise ValueError("an empty position cannot carry shares, cost, or exposure")
            if self.lifecycle_state not in (
                LifecycleState.FLAT,
                LifecycleState.READY,
                LifecycleState.PENDING_BUY,
                LifecycleState.EXITED,
            ):
                raise ValueError("empty position lifecycle state is inconsistent")
        else:
            if self.avg_cost <= 0:
                raise ValueError("a held position requires a positive avg_cost")
            if self.lifecycle_state not in (
                LifecycleState.PENDING_BUY,
                LifecycleState.BUILDING,
                LifecycleState.HOLDING,
                LifecycleState.PENDING_SELL,
                LifecycleState.REDUCING,
                LifecycleState.LOCKED,
            ):
                raise ValueError("held position lifecycle state is inconsistent")
        if self.can_sell_date:
            object.__setattr__(self, "can_sell_date", normalize_trade_date(self.can_sell_date))


@dataclass(frozen=True)
class DailyStockScore:
    ts_code: str
    signal_date: str
    buy_score: float
    sell_score: float
    position_score: float
    current_position_pct: float
    target_position_pct: float
    desired_action: TradeAction
    last_bar_date: str = ""
    stop_loss: float | None = None
    reasons: tuple[str, ...] = ()
    vetoes: tuple[str, ...] = ()
    hard_exit_reasons: tuple[str, ...] = ()
    buy_contributions: dict[str, float] = field(default_factory=dict)
    sell_contributions: dict[str, float] = field(default_factory=dict)
    strategy_version: str = "daily-holding-v0.2"
    parameter_version: str = "buy-confirmation-initial-v0.2"
    parameter_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.ts_code:
            raise ValueError("ts_code cannot be empty")
        object.__setattr__(self, "signal_date", normalize_trade_date(self.signal_date))
        last_bar_date = normalize_trade_date(self.last_bar_date) if self.last_bar_date else self.signal_date
        if last_bar_date > self.signal_date:
            raise ValueError("last_bar_date cannot be after signal_date")
        object.__setattr__(self, "last_bar_date", last_bar_date)
        _validate_score("buy_score", self.buy_score)
        _validate_score("sell_score", self.sell_score)
        _validate_score("position_score", self.position_score)
        _validate_pct("current_position_pct", self.current_position_pct)
        _validate_pct("target_position_pct", self.target_position_pct)
        if self.stop_loss is not None and self.stop_loss <= 0:
            raise ValueError("stop_loss must be positive when provided")
        if self.hard_exit_reasons:
            if self.target_position_pct != 0:
                raise ValueError("hard exit reasons require a zero target position")
            if self.desired_action in (TradeAction.OPEN, TradeAction.ADD):
                raise ValueError("hard exit reasons cannot create buy exposure")
            if self.current_position_pct > 0 and self.desired_action != TradeAction.EXIT:
                raise ValueError("a held position with hard exit reasons must EXIT")
        if self.vetoes and self.desired_action in (TradeAction.OPEN, TradeAction.ADD):
            raise ValueError("hard vetoes cannot create buy exposure")
        if self.desired_action == TradeAction.EXIT and self.target_position_pct != 0:
            raise ValueError("EXIT requires a zero target position")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "signal_date": self.signal_date,
            "buy_score": self.buy_score,
            "sell_score": self.sell_score,
            "position_score": self.position_score,
            "current_position_pct": self.current_position_pct,
            "target_position_pct": self.target_position_pct,
            "desired_action": self.desired_action.value,
            "last_bar_date": self.last_bar_date,
            "stop_loss": self.stop_loss,
            "reasons": list(self.reasons),
            "vetoes": list(self.vetoes),
            "hard_exit_reasons": list(self.hard_exit_reasons),
            "buy_contributions": dict(self.buy_contributions),
            "sell_contributions": dict(self.sell_contributions),
            "strategy_version": self.strategy_version,
            "parameter_version": self.parameter_version,
            "parameter_fingerprint": self.parameter_fingerprint,
        }


@dataclass(frozen=True)
class PendingOrder:
    ts_code: str
    signal_date: str
    planned_execution_date: str
    side: OrderSide
    action: TradeAction
    price_type: PriceType
    target_position_pct: float
    lookahead_flag: bool
    score: DailyStockScore
    order_id: str = ""
    root_order_id: str = ""
    supersedes_order_id: str = ""

    def __post_init__(self) -> None:
        if not self.ts_code:
            raise ValueError("ts_code cannot be empty")
        signal_date = normalize_trade_date(self.signal_date)
        execution_date = normalize_trade_date(self.planned_execution_date)
        object.__setattr__(self, "signal_date", signal_date)
        object.__setattr__(self, "planned_execution_date", execution_date)
        _validate_pct("target_position_pct", self.target_position_pct)
        if self.score.ts_code != self.ts_code or self.score.signal_date != signal_date:
            raise ValueError("pending order must match its source score")
        if self.action != self.score.desired_action:
            raise ValueError("pending order action must match its source score")
        if self.target_position_pct != self.score.target_position_pct:
            raise ValueError("pending order target must match its source score")
        if self.side == OrderSide.BUY and self.price_type != PriceType.NEXT_OPEN:
            raise ValueError("buy orders must execute at NEXT_OPEN")
        if self.side == OrderSide.BUY and self.action not in (
            TradeAction.OPEN,
            TradeAction.ADD,
        ):
            raise ValueError("buy orders require OPEN or ADD action")
        if self.side == OrderSide.SELL and self.action not in (
            TradeAction.REDUCE,
            TradeAction.EXIT,
        ):
            raise ValueError("sell orders require REDUCE or EXIT action")
        if self.price_type == PriceType.SAME_CLOSE_RESEARCH and not self.lookahead_flag:
            raise ValueError("same-close research orders must be marked lookahead")
        if self.price_type == PriceType.SAME_CLOSE_RESEARCH:
            if execution_date != signal_date:
                raise ValueError("same-close execution date must equal signal date")
        elif execution_date <= signal_date:
            raise ValueError("next-open execution date must be after signal date")
        if self.price_type == PriceType.NEXT_OPEN and self.lookahead_flag:
            raise ValueError("next-open orders cannot be marked lookahead")
        if not self.order_id:
            identity = {
                "ts_code": self.ts_code,
                "signal_date": signal_date,
                "planned_execution_date": execution_date,
                "side": self.side.value,
                "action": self.action.value,
                "price_type": self.price_type.value,
                "target_position_pct": self.target_position_pct,
                "strategy_version": self.score.strategy_version,
                "parameter_version": self.score.parameter_version,
                "parameter_fingerprint": self.score.parameter_fingerprint,
            }
            canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
            object.__setattr__(
                self,
                "order_id",
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            )
        if not self.root_order_id:
            object.__setattr__(self, "root_order_id", self.order_id)
        if self.supersedes_order_id == self.order_id:
            raise ValueError("an order cannot supersede itself")
