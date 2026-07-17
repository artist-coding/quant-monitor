"""把日线评分转换成具有明确成交时点的待执行订单。"""

from __future__ import annotations

from .dates import require_later_trade_date
from .models import (
    DailyStockScore,
    ExecutionMode,
    OrderSide,
    PendingOrder,
    PriceType,
    TradeAction,
)


def create_buy_order(score: DailyStockScore, next_trading_date: str) -> PendingOrder:
    """D日买入信号只能生成D+1开盘待执行订单。"""

    if score.desired_action not in (TradeAction.OPEN, TradeAction.ADD):
        raise ValueError("buy order requires OPEN or ADD action")
    if score.vetoes or score.hard_exit_reasons:
        raise ValueError("buy order cannot override hard veto or hard exit reasons")
    execution_date = require_later_trade_date(next_trading_date, score.signal_date, label="next_trading_date")

    return PendingOrder(
        ts_code=score.ts_code,
        signal_date=score.signal_date,
        planned_execution_date=execution_date,
        side=OrderSide.BUY,
        action=score.desired_action,
        price_type=PriceType.NEXT_OPEN,
        target_position_pct=score.target_position_pct,
        lookahead_flag=False,
        score=score,
    )


def create_sell_order(
    score: DailyStockScore,
    mode: ExecutionMode,
    next_trading_date: str | None = None,
) -> PendingOrder:
    """为同一卖出信号生成研究或严格执行口径。"""

    if score.desired_action not in (TradeAction.REDUCE, TradeAction.EXIT):
        raise ValueError("sell order requires REDUCE or EXIT action")
    if not isinstance(mode, ExecutionMode):
        raise ValueError("mode must be an ExecutionMode")

    if mode == ExecutionMode.SAME_CLOSE_RESEARCH:
        execution_date = score.signal_date
        price_type = PriceType.SAME_CLOSE_RESEARCH
        lookahead = True
    else:
        if not next_trading_date:
            raise ValueError("strict exit requires a next trading date")
        execution_date = require_later_trade_date(next_trading_date, score.signal_date, label="next_trading_date")
        price_type = PriceType.NEXT_OPEN
        lookahead = False

    return PendingOrder(
        ts_code=score.ts_code,
        signal_date=score.signal_date,
        planned_execution_date=execution_date,
        side=OrderSide.SELL,
        action=score.desired_action,
        price_type=price_type,
        target_position_pct=score.target_position_pct,
        lookahead_flag=lookahead,
        score=score,
    )
