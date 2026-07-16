"""Time-safe order execution for the daily portfolio engine.

NEXT_OPEN fills are intentionally a function of the execution day's open and
information confirmed before that open.  They never inspect that day's high,
low, close, volume or amount, which prevents the dynamic-slippage lookahead in
the legacy simulator from leaking into this engine.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from math import floor, isfinite

from ..indicators import DailyData
from .dates import normalize_trade_date, require_later_trade_date
from .models import (
    LifecycleState,
    OrderSide,
    PendingOrder,
    PositionState,
    PriceType,
)


class ExecutionStatus(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    NOT_DUE = "NOT_DUE"


@dataclass(frozen=True)
class ExecutionConfig:
    lot_size: int = 100
    buy_slippage_rate: float = 0.001
    sell_slippage_rate: float = 0.001
    commission_rate: float = 0.00025
    minimum_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    cash_utilization_limit: float = 0.95
    risk_per_trade_pct: float = 0.02
    require_valid_stop_for_buy: bool = True
    allow_st: bool = False
    apply_price_limits: bool = True
    require_previous_close_for_limits: bool = True
    t1_enabled: bool = True
    price_tick: float = 0.01
    price_limit_model_version: str = "legacy-code-inferred-research-v0.1"
    cost_model_version: str = "fixed-current-research-v0.1"

    def __post_init__(self) -> None:
        if (
            isinstance(self.lot_size, bool)
            or not isinstance(self.lot_size, int)
            or self.lot_size <= 0
        ):
            raise ValueError("lot_size must be a positive integer")
        rates = (
            self.buy_slippage_rate,
            self.sell_slippage_rate,
            self.commission_rate,
            self.minimum_commission,
            self.stamp_duty_rate,
            self.transfer_fee_rate,
        )
        if any(not isfinite(value) or value < 0 for value in rates):
            raise ValueError("execution rates and costs must be finite and non-negative")
        if (
            not isfinite(self.cash_utilization_limit)
            or not 0 < self.cash_utilization_limit <= 1
        ):
            raise ValueError("cash_utilization_limit must be in (0, 1]")
        if (
            not isfinite(self.risk_per_trade_pct)
            or not 0 < self.risk_per_trade_pct <= 1
        ):
            raise ValueError("risk_per_trade_pct must be in (0, 1]")
        if not isfinite(self.price_tick) or self.price_tick <= 0:
            raise ValueError("price_tick must be finite and positive")
        if not self.price_limit_model_version or not self.cost_model_version:
            raise ValueError("execution model versions cannot be empty")

    def canonical_payload(self) -> dict[str, object]:
        """Return every execution-affecting input in a stable representation."""

        return {
            "allow_st": self.allow_st,
            "apply_price_limits": self.apply_price_limits,
            "buy_slippage_rate": self.buy_slippage_rate,
            "cash_utilization_limit": self.cash_utilization_limit,
            "commission_rate": self.commission_rate,
            "cost_model_version": self.cost_model_version,
            "lot_size": self.lot_size,
            "minimum_commission": self.minimum_commission,
            "price_limit_model_version": self.price_limit_model_version,
            "price_tick": self.price_tick,
            "require_previous_close_for_limits": self.require_previous_close_for_limits,
            "require_valid_stop_for_buy": self.require_valid_stop_for_buy,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "sell_slippage_rate": self.sell_slippage_rate,
            "stamp_duty_rate": self.stamp_duty_rate,
            "t1_enabled": self.t1_enabled,
            "transfer_fee_rate": self.transfer_fee_rate,
        }

    @property
    def canonical_fingerprint(self) -> str:
        canonical = json.dumps(
            self.canonical_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionCosts:
    commission: float
    stamp_duty: float
    transfer_fee: float
    total: float


@dataclass(frozen=True)
class TradeFill:
    fill_id: str
    order_id: str
    root_order_id: str
    supersedes_order_id: str
    ts_code: str
    signal_date: str
    execution_date: str
    side: OrderSide
    action: str
    price_type: PriceType
    raw_price: float
    fill_price: float
    shares: int
    gross_amount: float
    costs: ExecutionCosts
    lookahead_flag: bool
    stop_loss: float | None = None
    risk_per_share: float = 0.0
    requested_shares: int = 0
    unfilled_shares: int = 0
    price_limit_model_version: str = ""
    cost_model_version: str = ""
    execution_config_fingerprint: str = ""


@dataclass(frozen=True)
class OrderExecutionResult:
    status: ExecutionStatus
    order: PendingOrder
    position: PositionState
    cash: float
    fill: TradeFill | None = None
    reason: str = ""


def unlock_t1(position: PositionState, trade_date: str) -> PositionState:
    """Make all currently held shares sellable on or after ``can_sell_date``."""

    current_date = normalize_trade_date(trade_date)
    if position.can_sell_date and current_date >= position.can_sell_date:
        return replace(position, available_shares=position.shares, can_sell_date="")
    return position


def _limit_rate(ts_code: str, is_st: bool) -> float:
    if is_st:
        return 0.05
    code = ts_code.split(".", 1)[0]
    if code.startswith(("300", "301", "688")):
        return 0.20
    return 0.10


def _round_price_to_tick(value: float, price_tick: float) -> float:
    amount = Decimal(str(value))
    tick = Decimal(str(price_tick))
    ticks = (amount / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(ticks * tick)


def _blocked_by_price_limit(
    order: PendingOrder,
    raw_price: float,
    previous_close: float | None,
    is_st: bool,
    price_tick: float,
) -> str:
    if previous_close is None or previous_close <= 0:
        return ""
    limit_rate = _limit_rate(order.ts_code, is_st)
    upper = _round_price_to_tick(previous_close * (1 + limit_rate), price_tick)
    lower = _round_price_to_tick(previous_close * (1 - limit_rate), price_tick)
    if order.side == OrderSide.BUY and raw_price >= upper:
        return f"开盘涨停，无法买入（{upper:.2f}）"
    if order.side == OrderSide.SELL and raw_price <= lower:
        timing = "开盘" if order.price_type == PriceType.NEXT_OPEN else "收盘"
        return f"{timing}跌停，无法卖出（{lower:.2f}）"
    return ""


def _costs(amount: float, side: OrderSide, config: ExecutionConfig) -> ExecutionCosts:
    commission = max(amount * config.commission_rate, config.minimum_commission)
    transfer_fee = amount * config.transfer_fee_rate
    stamp_duty = amount * config.stamp_duty_rate if side == OrderSide.SELL else 0.0
    commission = round(commission, 2)
    transfer_fee = round(transfer_fee, 2)
    stamp_duty = round(stamp_duty, 2)
    return ExecutionCosts(
        commission=commission,
        stamp_duty=stamp_duty,
        transfer_fee=transfer_fee,
        total=round(commission + transfer_fee + stamp_duty, 2),
    )


def _fill_price(raw_price: float, side: OrderSide, config: ExecutionConfig) -> float:
    if raw_price <= 0:
        raise ValueError("execution price must be positive")
    rate = (
        config.buy_slippage_rate
        if side == OrderSide.BUY
        else -config.sell_slippage_rate
    )
    adjusted = Decimal(str(raw_price)) * (Decimal("1") + Decimal(str(rate)))
    tick = Decimal(str(config.price_tick))
    ticks = (adjusted / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(ticks * tick)


def calculate_execution_costs(
    amount: float,
    side: OrderSide,
    config: ExecutionConfig | None = None,
) -> ExecutionCosts:
    """Public, versioned cost calculation shared by fills and research labels."""

    if not isfinite(amount) or amount <= 0:
        raise ValueError("amount must be finite and positive")
    return _costs(amount, side, config or ExecutionConfig())


def apply_execution_slippage(
    raw_price: float,
    side: OrderSide,
    config: ExecutionConfig | None = None,
) -> float:
    """Public execution-price transform used by fixed-horizon event studies."""

    return _fill_price(raw_price, side, config or ExecutionConfig())


def _target_shares(
    target_position_pct: float,
    equity: float,
    fill_price: float,
    lot_size: int,
) -> int:
    target_value = equity * target_position_pct
    return floor(target_value / fill_price / lot_size) * lot_size


def _affordable_buy_shares(
    requested: int,
    cash: float,
    fill_price: float,
    config: ExecutionConfig,
) -> int:
    available_cash = cash * config.cash_utilization_limit
    shares = requested - requested % config.lot_size
    while shares > 0:
        amount = fill_price * shares
        if amount + _costs(amount, OrderSide.BUY, config).total <= available_cash:
            return shares
        shares -= config.lot_size
    return 0


def _not_executed(
    status: ExecutionStatus,
    order: PendingOrder,
    position: PositionState,
    cash: float,
    reason: str,
) -> OrderExecutionResult:
    return OrderExecutionResult(
        status=status,
        order=order,
        position=position,
        cash=cash,
        reason=reason,
    )


def execute_target_order(
    order: PendingOrder,
    bar: DailyData,
    position: PositionState,
    *,
    cash: float,
    equity: float,
    previous_close: float | None,
    config: ExecutionConfig | None = None,
    is_st: bool = False,
    tradable_at_execution: bool = True,
    next_trading_date: str = "",
) -> OrderExecutionResult:
    """Execute an order toward its target portfolio percentage.

    Quantity is calculated only now, from the actual execution price.  The
    signal day therefore cannot pre-compute shares using its own close.
    """

    resolved_config = config or ExecutionConfig()
    execution_date = normalize_trade_date(bar.trade_date)

    if bar.ts_code != order.ts_code:
        return _not_executed(
            ExecutionStatus.NOT_DUE, order, position, cash, "K线股票与订单不匹配"
        )
    if execution_date != order.planned_execution_date:
        return _not_executed(
            ExecutionStatus.NOT_DUE, order, position, cash, "尚未到计划执行日"
        )
    position = unlock_t1(position, execution_date)
    if cash < 0 or equity <= 0:
        raise ValueError("cash cannot be negative and equity must be positive")
    if not tradable_at_execution:
        return _not_executed(
            ExecutionStatus.BLOCKED, order, position, cash, "停牌或无有效开收盘报价"
        )
    if order.side == OrderSide.BUY and is_st and not resolved_config.allow_st:
        return _not_executed(
            ExecutionStatus.BLOCKED, order, position, cash, "ST标的禁止新增仓位"
        )

    raw_price = bar.open if order.price_type == PriceType.NEXT_OPEN else bar.close
    if raw_price <= 0:
        return _not_executed(
            ExecutionStatus.BLOCKED, order, position, cash, "成交价格无效"
        )
    if resolved_config.apply_price_limits:
        if (
            resolved_config.require_previous_close_for_limits
            and (previous_close is None or previous_close <= 0)
        ):
            return _not_executed(
                ExecutionStatus.BLOCKED,
                order,
                position,
                cash,
                "缺少权威前收盘价，无法验证涨跌停约束",
            )
        limit_reason = _blocked_by_price_limit(
            order,
            raw_price,
            previous_close,
            is_st,
            resolved_config.price_tick,
        )
        if limit_reason:
            return _not_executed(
                ExecutionStatus.BLOCKED, order, position, cash, limit_reason
            )

    fill_price = _fill_price(raw_price, order.side, resolved_config)
    if resolved_config.apply_price_limits and previous_close is not None:
        limit_rate = _limit_rate(order.ts_code, is_st)
        upper = _round_price_to_tick(
            previous_close * (1 + limit_rate), resolved_config.price_tick
        )
        lower = _round_price_to_tick(
            previous_close * (1 - limit_rate), resolved_config.price_tick
        )
        fill_price = (
            min(fill_price, upper)
            if order.side == OrderSide.BUY
            else max(fill_price, lower)
        )
    desired_shares = _target_shares(
        order.target_position_pct, equity, fill_price, resolved_config.lot_size
    )
    requested_shares = (
        max(0, desired_shares - position.shares)
        if order.side == OrderSide.BUY
        else max(0, position.shares - desired_shares)
    )
    if requested_shares == 0:
        return _not_executed(
            ExecutionStatus.BLOCKED, order, position, cash, "目标仓位无需成交"
        )

    allocation_requested_shares = requested_shares
    status = ExecutionStatus.FILLED
    partial_reasons: list[str] = []
    stop_loss = order.score.stop_loss or position.stop_loss
    risk_per_share = 0.0
    if order.side == OrderSide.BUY:
        if resolved_config.t1_enabled and not next_trading_date:
            raise ValueError("next_trading_date is required for a T+1 buy")
        if stop_loss is None and resolved_config.require_valid_stop_for_buy:
            return _not_executed(
                ExecutionStatus.BLOCKED,
                order,
                position,
                cash,
                "买入订单缺少有效止损位",
            )
        if stop_loss is not None:
            risk_per_share = fill_price - stop_loss
            if risk_per_share <= 0:
                return _not_executed(
                    ExecutionStatus.BLOCKED,
                    order,
                    position,
                    cash,
                    "实际开盘成交价不高于止损位，风险仓位无效",
                )
            risk_budget = equity * resolved_config.risk_per_trade_pct
            existing_risk = max(0.0, position.avg_cost - stop_loss) * position.shares
            remaining_risk = max(0.0, risk_budget - existing_risk)
            max_risk_shares = (
                floor(
                    remaining_risk
                    / risk_per_share
                    / resolved_config.lot_size
                )
                * resolved_config.lot_size
            )
            requested_shares = min(requested_shares, max_risk_shares)
            if requested_shares < allocation_requested_shares:
                partial_reasons.append("风险预算限制")
            if requested_shares == 0:
                return _not_executed(
                    ExecutionStatus.BLOCKED,
                    order,
                    position,
                    cash,
                    "风险预算不足一手",
                )
        shares = _affordable_buy_shares(
            requested_shares, cash, fill_price, resolved_config
        )
        if shares == 0:
            return _not_executed(
                ExecutionStatus.BLOCKED, order, position, cash, "可用现金不足一手"
            )
        if shares < requested_shares:
            partial_reasons.append("可用现金限制")
    else:
        shares = min(requested_shares, position.available_shares)
        if shares == 0:
            return _not_executed(
                ExecutionStatus.BLOCKED, order, position, cash, "T+1或可卖股数不足"
            )
        if shares < requested_shares:
            partial_reasons.append("T+1或可卖股数限制")

    if shares < allocation_requested_shares:
        status = ExecutionStatus.PARTIAL

    gross_amount = round(fill_price * shares, 2)
    costs = _costs(gross_amount, order.side, resolved_config)
    old_cost_value = position.avg_cost * position.shares

    if order.side == OrderSide.BUY:
        new_shares = position.shares + shares
        new_cash = cash - gross_amount - costs.total
        avg_cost = (old_cost_value + gross_amount + costs.total) / new_shares
        available_shares = (
            position.available_shares
            if resolved_config.t1_enabled
            else new_shares
        )
        can_sell_date = (
            require_later_trade_date(
                next_trading_date,
                execution_date,
                label="next_trading_date",
            )
            if resolved_config.t1_enabled and next_trading_date
            else position.can_sell_date
        )
        lifecycle = LifecycleState.BUILDING
    else:
        new_shares = position.shares - shares
        new_cash = cash + gross_amount - costs.total
        avg_cost = position.avg_cost if new_shares else 0.0
        available_shares = max(0, position.available_shares - shares)
        can_sell_date = position.can_sell_date if new_shares else ""
        lifecycle = (
            LifecycleState.EXITED if new_shares == 0 else LifecycleState.REDUCING
        )

    updated_position = PositionState(
        ts_code=position.ts_code,
        lifecycle_state=lifecycle,
        shares=new_shares,
        available_shares=available_shares,
        avg_cost=round(avg_cost, 6),
        current_position_pct=min(1.0, new_shares * fill_price / equity),
        stop_loss=(
            None
            if new_shares == 0
            else order.score.stop_loss or position.stop_loss
        ),
        can_sell_date=can_sell_date,
    )
    execution_config_fingerprint = resolved_config.canonical_fingerprint
    fill_identity = "|".join(
        (
            order.order_id,
            execution_date,
            order.side.value,
            str(shares),
            str(fill_price),
            str(gross_amount),
            execution_config_fingerprint,
        )
    )
    fill = TradeFill(
        fill_id=hashlib.sha256(fill_identity.encode("utf-8")).hexdigest(),
        order_id=order.order_id,
        root_order_id=order.root_order_id,
        supersedes_order_id=order.supersedes_order_id,
        ts_code=order.ts_code,
        signal_date=order.signal_date,
        execution_date=execution_date,
        side=order.side,
        action=order.action.value,
        price_type=order.price_type,
        raw_price=raw_price,
        fill_price=fill_price,
        shares=shares,
        gross_amount=gross_amount,
        costs=costs,
        lookahead_flag=order.lookahead_flag,
        stop_loss=stop_loss,
        risk_per_share=round(risk_per_share, 6),
        requested_shares=allocation_requested_shares,
        unfilled_shares=allocation_requested_shares - shares,
        price_limit_model_version=resolved_config.price_limit_model_version,
        cost_model_version=resolved_config.cost_model_version,
        execution_config_fingerprint=execution_config_fingerprint,
    )
    return OrderExecutionResult(
        status=status,
        order=order,
        position=updated_position,
        cash=round(new_cash, 2),
        fill=fill,
        reason="；".join(partial_reasons),
    )
