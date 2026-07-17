"""单只股票的无前视日线事件回放器。

事件顺序固定为：交易日开盘先处理严格卖单、再处理买单，随后在有完整
日线的交易日收盘调用评分器。评分器只能看到截至当天的 K 线前缀。
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, TypeAlias

from ..indicators import DailyData
from .dates import normalize_trade_date
from .execution import create_buy_order, create_sell_order
from .execution_model import (
    ExecutionConfig,
    ExecutionStatus,
    TradeFill,
    execute_target_order,
    unlock_t1,
)
from .holding_state_machine import LifecycleTransition, resolve_lifecycle_after_day
from .models import (
    DailyStockScore,
    ExecutionMode,
    OrderSide,
    PendingOrder,
    PositionState,
    PriceType,
    TradeAction,
)


ScoreProvider: TypeAlias = Callable[[tuple[DailyData, ...], PositionState, Any], DailyStockScore]
MarketProvider: TypeAlias = Callable[[str], Any]


@dataclass(frozen=True)
class ExecutionQuoteSnapshot:
    """Only quote fields knowable at the configured execution moment."""

    ts_code: str
    trade_date: str
    price_type: PriceType
    execution_price: float
    previous_close: float | None


TradabilityProvider: TypeAlias = Callable[[ExecutionQuoteSnapshot, PendingOrder], bool]
IsStProvider: TypeAlias = Callable[[ExecutionQuoteSnapshot], bool]


@dataclass(frozen=True)
class ReplayRejection:
    """一次未成交或无法安排执行日的记录。"""

    trade_date: str
    phase: str
    reason: str
    order: PendingOrder | None = None
    score: DailyStockScore | None = None
    carried_forward: bool = False
    rescheduled_to: str = ""


@dataclass(frozen=True)
class DailyReplayRecord:
    """一个市场交易日完成后的可审计快照。"""

    trade_date: str
    bar: DailyData | None
    position_at_open: PositionState
    cash_at_open: float
    score: DailyStockScore | None
    fills: tuple[TradeFill, ...]
    rejections: tuple[ReplayRejection, ...]
    position_at_close: PositionState
    cash_at_close: float
    pending_orders: tuple[PendingOrder, ...]
    lifecycle_transition: LifecycleTransition


@dataclass(frozen=True)
class DailyReplayResult:
    """完整回放结果。"""

    daily_records: tuple[DailyReplayRecord, ...]
    fills: tuple[TradeFill, ...]
    rejections: tuple[ReplayRejection, ...]
    final_position: PositionState
    final_cash: float
    final_equity: float
    pending_orders: tuple[PendingOrder, ...]
    calendar_source: str


@dataclass(frozen=True)
class PairedExitReplayResult:
    same_close_research: DailyReplayResult
    next_open_strict: DailyReplayResult
    final_equity_difference: float
    fill_count_difference: int


def _order_sort_key(order: PendingOrder) -> tuple[str, int, str]:
    side_priority = 0 if order.side == OrderSide.SELL else 1
    return order.planned_execution_date, side_priority, order.signal_date


def _normalize_calendar(
    bars: Sequence[DailyData], trading_dates: Sequence[str] | None
) -> tuple[tuple[str, ...], dict[str, DailyData], str]:
    if not bars:
        raise ValueError("bars cannot be empty")

    ts_code = bars[0].ts_code
    if not ts_code or any(bar.ts_code != ts_code for bar in bars):
        raise ValueError("all bars must belong to one non-empty ts_code")

    bars_by_date: dict[str, DailyData] = {}
    ordered_bar_dates: list[str] = []
    for bar in bars:
        trade_date = normalize_trade_date(bar.trade_date)
        if trade_date in bars_by_date:
            raise ValueError(f"duplicate bar date: {trade_date}")
        bars_by_date[trade_date] = bar
        ordered_bar_dates.append(trade_date)
    if ordered_bar_dates != sorted(ordered_bar_dates):
        raise ValueError("bars must be strictly ascending by trade_date")

    if trading_dates is None:
        raise ValueError(
            "explicit exchange trading_dates are required; stock bar dates "
            "cannot distinguish suspension from the next market session"
        )

    normalized = tuple(normalize_trade_date(value) for value in trading_dates)
    if len(set(normalized)) != len(normalized):
        raise ValueError("trading_dates contain duplicates")
    if normalized != tuple(sorted(normalized)):
        raise ValueError("trading_dates must be strictly ascending")
    missing = set(bars_by_date).difference(normalized)
    if missing:
        raise ValueError(f"trading_dates do not contain bar dates: {sorted(missing)}")
    calendar = normalized

    return calendar, bars_by_date, ts_code


def _next_trading_date(
    current_date: str,
    calendar: tuple[str, ...],
    following_trading_date: str,
) -> str:
    index = bisect_right(calendar, current_date)
    if index < len(calendar):
        return calendar[index]
    return following_trading_date


def _mark_position(position: PositionState, cash: float, mark_price: float) -> PositionState:
    if mark_price <= 0:
        return position
    equity = cash + position.shares * mark_price
    position_pct = position.shares * mark_price / equity if equity > 0 else 0.0
    return replace(
        position,
        current_position_pct=max(0.0, min(1.0, position_pct)),
    )


def _portfolio_equity(cash: float, position: PositionState, price: float) -> float:
    equity = cash + position.shares * price
    if equity <= 0:
        raise ValueError("portfolio equity must be positive when executing an order")
    return equity


def _previous_close(bar: DailyData, latest_close: float | None) -> float | None:
    if bar.prev_close > 0:
        return bar.prev_close
    return latest_close if latest_close is not None and latest_close > 0 else None


def _reschedule_strict_sell(order: PendingOrder, current_date: str, next_date: str) -> PendingOrder:
    attempted = (
        order if order.planned_execution_date == current_date else replace(order, planned_execution_date=current_date)
    )
    if next_date:
        return replace(
            attempted,
            planned_execution_date=next_date,
            order_id="",
            root_order_id=order.root_order_id,
            supersedes_order_id=order.order_id,
        )
    return attempted


def _has_pending_side(pending: Sequence[PendingOrder], side: OrderSide) -> bool:
    return any(order.side == side for order in pending)


def _latch_hard_exit_next_open(
    pending: list[PendingOrder],
    score: DailyStockScore,
    close_order: PendingOrder,
    next_date: str,
) -> tuple[list[PendingOrder], PendingOrder, tuple[PendingOrder, ...]]:
    """Persist an unfilled same-close hard exit as one sticky NEXT_OPEN order."""

    if not next_date:
        raise ValueError("a blocked same-close hard exit requires the next exchange trading date")

    candidate = create_sell_order(
        score,
        ExecutionMode.NEXT_OPEN_STRICT,
        next_date,
    )
    existing_sells = tuple(order for order in pending if order.side == OrderSide.SELL)
    if not existing_sells:
        candidate = replace(
            candidate,
            root_order_id=close_order.root_order_id,
            supersedes_order_id=close_order.order_id,
        )
        pending.append(candidate)
        return pending, candidate, ()

    most_defensive = min(
        existing_sells,
        key=lambda order: (
            order.target_position_pct,
            order.planned_execution_date,
        ),
    )
    candidate_is_stronger = candidate.target_position_pct < most_defensive.target_position_pct or (
        candidate.target_position_pct == most_defensive.target_position_pct
        and candidate.planned_execution_date < most_defensive.planned_execution_date
    )
    if not candidate_is_stronger:
        return pending, most_defensive, ()

    candidate = replace(
        candidate,
        root_order_id=most_defensive.root_order_id,
        supersedes_order_id=most_defensive.order_id,
    )
    retained = [order for order in pending if order.side != OrderSide.SELL]
    retained.append(candidate)
    return retained, candidate, existing_sells


def _quote_snapshot(
    bar: DailyData,
    order: PendingOrder,
    previous_close: float | None,
) -> ExecutionQuoteSnapshot:
    return ExecutionQuoteSnapshot(
        ts_code=bar.ts_code,
        trade_date=normalize_trade_date(bar.trade_date),
        price_type=order.price_type,
        execution_price=(bar.open if order.price_type == PriceType.NEXT_OPEN else bar.close),
        previous_close=previous_close,
    )


def _validate_score(score: DailyStockScore, *, ts_code: str, trade_date: str) -> None:
    if not isinstance(score, DailyStockScore):
        raise TypeError("score_provider must return DailyStockScore")
    if score.ts_code != ts_code:
        raise ValueError("score_provider returned a score for another stock")
    if score.signal_date != trade_date:
        raise ValueError("score signal_date must equal the current close date")
    if score.last_bar_date != trade_date:
        raise ValueError("score last_bar_date must equal the current close date")


def replay_daily_stock(
    bars: Sequence[DailyData],
    *,
    score_provider: ScoreProvider,
    market_provider: MarketProvider | None = None,
    initial_position: PositionState | None = None,
    initial_cash: float = 100_000.0,
    exit_mode: ExecutionMode = ExecutionMode.NEXT_OPEN_STRICT,
    execution_config: ExecutionConfig | None = None,
    trading_dates: Sequence[str] | None = None,
    following_trading_date: str = "",
    initial_pending_orders: Sequence[PendingOrder] = (),
    tradability_provider: TradabilityProvider | None = None,
    is_st_provider: IsStProvider | None = None,
) -> DailyReplayResult:
    """按显式交易日历回放一只股票。

    ``following_trading_date`` 只用于给最后一个收盘信号安排下一交易日，
    它不会被加入本次事件循环，因此该日订单只会保留为待单，绝不会在
    没有 K 线的情况下被成交。

    当 ``trading_dates`` 含有某个交易日但该日没有个股 K 线时，该日视为
    停牌或无报价：到期买单被拒绝且不顺延，严格卖单保留到下个交易日。
    """

    calendar, bars_by_date, ts_code = _normalize_calendar(bars, trading_dates)
    if initial_cash < 0:
        raise ValueError("initial_cash cannot be negative")

    following = normalize_trade_date(following_trading_date) if following_trading_date else ""
    if following and following <= calendar[-1]:
        raise ValueError("following_trading_date must be after the replay calendar")

    position = initial_position or PositionState(ts_code=ts_code)
    if position.ts_code != ts_code:
        raise ValueError("initial_position must match the replay stock")

    pending = list(initial_pending_orders)
    if any(order.ts_code != ts_code for order in pending):
        raise ValueError("initial_pending_orders must match the replay stock")
    if len({order.order_id for order in pending}) != len(pending):
        raise ValueError("initial_pending_orders contain duplicate order_id values")

    config = execution_config or ExecutionConfig()
    cash = float(initial_cash)
    prefix: list[DailyData] = []
    latest_close: float | None = None
    records: list[DailyReplayRecord] = []
    all_fills: list[TradeFill] = []
    all_rejections: list[ReplayRejection] = []

    for trade_date in calendar:
        bar = bars_by_date.get(trade_date)
        next_date = _next_trading_date(trade_date, calendar, following)
        position = unlock_t1(position, trade_date)
        if bar is not None:
            position = _mark_position(position, cash, bar.open)
        elif latest_close is not None:
            position = _mark_position(position, cash, latest_close)

        position_at_open = position
        cash_at_open = cash
        day_fills: list[TradeFill] = []
        day_rejections: list[ReplayRejection] = []

        due = sorted(
            (order for order in pending if order.planned_execution_date <= trade_date),
            key=_order_sort_key,
        )
        pending = [order for order in pending if order.planned_execution_date > trade_date]

        for original_order in due:
            if original_order.price_type != PriceType.NEXT_OPEN:
                rejection = ReplayRejection(
                    trade_date=trade_date,
                    phase="OPEN",
                    reason="过期的同收盘研究订单不能在后续开盘成交",
                    order=original_order,
                )
                day_rejections.append(rejection)
                continue

            is_strict_sell = original_order.side == OrderSide.SELL
            if original_order.planned_execution_date < trade_date and not is_strict_sell:
                rejection = ReplayRejection(
                    trade_date=trade_date,
                    phase="OPEN",
                    reason="买单已错过唯一计划执行日，不允许顺延",
                    order=original_order,
                )
                day_rejections.append(rejection)
                continue

            attempted_order = original_order
            if is_strict_sell and original_order.planned_execution_date < trade_date:
                attempted_order = replace(
                    original_order,
                    planned_execution_date=trade_date,
                    order_id="",
                    root_order_id=original_order.root_order_id,
                    supersedes_order_id=original_order.order_id,
                )

            if bar is None:
                if is_strict_sell:
                    rolled = _reschedule_strict_sell(attempted_order, trade_date, next_date)
                    pending.append(rolled)
                    rejection = ReplayRejection(
                        trade_date=trade_date,
                        phase="OPEN",
                        reason="当日无个股K线或有效报价，严格卖单顺延",
                        order=attempted_order,
                        carried_forward=True,
                        rescheduled_to=next_date,
                    )
                else:
                    rejection = ReplayRejection(
                        trade_date=trade_date,
                        phase="OPEN",
                        reason="当日无个股K线或有效报价，买单拒绝且不顺延",
                        order=attempted_order,
                    )
                day_rejections.append(rejection)
                continue

            equity = _portfolio_equity(cash, position, bar.open)
            if attempted_order.side == OrderSide.BUY and config.t1_enabled and not next_date:
                rejection = ReplayRejection(
                    trade_date=trade_date,
                    phase="OPEN",
                    reason="缺少下一交易日历，无法安全设置T+1可卖日期",
                    order=attempted_order,
                )
                day_rejections.append(rejection)
                continue
            previous_close = _previous_close(bar, latest_close)
            quote = _quote_snapshot(bar, attempted_order, previous_close)
            execution = execute_target_order(
                attempted_order,
                bar,
                position,
                cash=cash,
                equity=equity,
                previous_close=previous_close,
                config=config,
                is_st=is_st_provider(quote) if is_st_provider else False,
                tradable_at_execution=(tradability_provider(quote, attempted_order) if tradability_provider else True),
                next_trading_date=next_date,
            )
            position, cash = execution.position, execution.cash
            if execution.fill is not None:
                day_fills.append(execution.fill)

            should_roll = is_strict_sell and (
                execution.status == ExecutionStatus.PARTIAL
                or (execution.status == ExecutionStatus.BLOCKED and execution.reason != "目标仓位无需成交")
            )
            if should_roll:
                rolled = _reschedule_strict_sell(attempted_order, trade_date, next_date)
                pending.append(rolled)
                rejection = ReplayRejection(
                    trade_date=trade_date,
                    phase="OPEN",
                    reason=execution.reason or "严格卖单部分成交，余量顺延",
                    order=attempted_order,
                    carried_forward=True,
                    rescheduled_to=next_date,
                )
                day_rejections.append(rejection)
            elif execution.status in (
                ExecutionStatus.BLOCKED,
                ExecutionStatus.NOT_DUE,
            ):
                rejection = ReplayRejection(
                    trade_date=trade_date,
                    phase="OPEN",
                    reason=execution.reason,
                    order=attempted_order,
                )
                day_rejections.append(rejection)
            elif execution.status == ExecutionStatus.PARTIAL and not is_strict_sell:
                day_rejections.append(
                    ReplayRejection(
                        trade_date=trade_date,
                        phase="OPEN",
                        reason=(execution.reason or "买单部分成交，未成交余量取消且不顺延"),
                        order=attempted_order,
                    )
                )

        score: DailyStockScore | None = None
        if bar is not None:
            prefix.append(bar)
            position = _mark_position(position, cash, bar.close)
            market = market_provider(trade_date) if market_provider else None
            score = score_provider(tuple(prefix), position, market)
            _validate_score(score, ts_code=ts_code, trade_date=trade_date)

            if score.desired_action in (TradeAction.OPEN, TradeAction.ADD):
                if _has_pending_side(pending, OrderSide.SELL):
                    day_rejections.append(
                        ReplayRejection(
                            trade_date=trade_date,
                            phase="CLOSE",
                            reason="已有风险优先卖单，禁止生成新增仓位买单",
                            score=score,
                        )
                    )
                elif next_date and not _has_pending_side(pending, OrderSide.BUY):
                    pending.append(create_buy_order(score, next_date))
                elif not next_date:
                    day_rejections.append(
                        ReplayRejection(
                            trade_date=trade_date,
                            phase="CLOSE",
                            reason="没有已知的下一交易日，无法安排买单",
                            score=score,
                        )
                    )
            elif score.desired_action in (TradeAction.REDUCE, TradeAction.EXIT):
                cancelled_buys = [order for order in pending if order.side == OrderSide.BUY]
                pending = [order for order in pending if order.side != OrderSide.BUY]
                for cancelled in cancelled_buys:
                    day_rejections.append(
                        ReplayRejection(
                            trade_date=trade_date,
                            phase="CLOSE",
                            reason="卖出风险信号取消尚未执行的买单",
                            order=cancelled,
                            score=score,
                        )
                    )
                if exit_mode == ExecutionMode.SAME_CLOSE_RESEARCH:
                    close_order = create_sell_order(score, exit_mode)
                    previous_close = _previous_close(bar, latest_close)
                    quote = _quote_snapshot(bar, close_order, previous_close)
                    close_execution = execute_target_order(
                        close_order,
                        bar,
                        position,
                        cash=cash,
                        equity=_portfolio_equity(cash, position, bar.close),
                        previous_close=previous_close,
                        config=config,
                        is_st=is_st_provider(quote) if is_st_provider else False,
                        tradable_at_execution=(
                            tradability_provider(quote, close_order) if tradability_provider else True
                        ),
                        next_trading_date=next_date,
                    )
                    position, cash = close_execution.position, close_execution.cash
                    if close_execution.fill is not None:
                        day_fills.append(close_execution.fill)
                    is_hard_exit = bool(score.hard_exit_reasons)
                    if close_execution.status in (
                        ExecutionStatus.BLOCKED,
                        ExecutionStatus.NOT_DUE,
                    ):
                        day_rejections.append(
                            ReplayRejection(
                                trade_date=trade_date,
                                phase="CLOSE",
                                reason=close_execution.reason,
                                order=close_order,
                            )
                        )
                    elif close_execution.status == ExecutionStatus.PARTIAL:
                        day_rejections.append(
                            ReplayRejection(
                                trade_date=trade_date,
                                phase="CLOSE",
                                reason=(
                                    "同收盘硬退出仅部分成交，剩余风险仓位待锁存"
                                    if is_hard_exit
                                    else "同收盘研究卖单仅部分成交，未成交部分不顺延"
                                ),
                                order=close_order,
                            )
                        )
                    hard_exit_unfinished = (
                        is_hard_exit
                        and position.shares > 0
                        and close_execution.status
                        in (
                            ExecutionStatus.BLOCKED,
                            ExecutionStatus.NOT_DUE,
                            ExecutionStatus.PARTIAL,
                        )
                    )
                    if hard_exit_unfinished:
                        pending, latched_order, replaced_sells = _latch_hard_exit_next_open(
                            pending,
                            score,
                            close_order,
                            next_date,
                        )
                        for replaced_sell in replaced_sells:
                            day_rejections.append(
                                ReplayRejection(
                                    trade_date=trade_date,
                                    phase="CLOSE",
                                    reason="同收盘硬退出升级并替换原待卖单",
                                    order=replaced_sell,
                                    score=score,
                                )
                            )
                        day_rejections.append(
                            ReplayRejection(
                                trade_date=trade_date,
                                phase="CLOSE",
                                reason="同收盘硬退出未完成，锁存为NEXT_OPEN风险卖单",
                                order=latched_order,
                                score=score,
                                carried_forward=True,
                                rescheduled_to=latched_order.planned_execution_date,
                            )
                        )
                    elif is_hard_exit and position.shares == 0:
                        stale_sells = [order for order in pending if order.side == OrderSide.SELL]
                        pending = [order for order in pending if order.side != OrderSide.SELL]
                        for stale_sell in stale_sells:
                            day_rejections.append(
                                ReplayRejection(
                                    trade_date=trade_date,
                                    phase="CLOSE",
                                    reason="同收盘硬退出已完成，取消残留待卖单",
                                    order=stale_sell,
                                    score=score,
                                )
                            )
                elif next_date:
                    candidate = create_sell_order(score, exit_mode, next_date)
                    existing_sells = [order for order in pending if order.side == OrderSide.SELL]
                    if not existing_sells:
                        pending.append(candidate)
                    else:
                        most_defensive = min(
                            existing_sells,
                            key=lambda order: order.target_position_pct,
                        )
                        should_replace = candidate.target_position_pct < most_defensive.target_position_pct or bool(
                            score.hard_exit_reasons
                        )
                        if should_replace:
                            candidate = replace(
                                candidate,
                                root_order_id=most_defensive.root_order_id,
                                supersedes_order_id=most_defensive.order_id,
                            )
                            pending = [order for order in pending if order.side != OrderSide.SELL]
                            pending.append(candidate)
                            for replaced in existing_sells:
                                day_rejections.append(
                                    ReplayRejection(
                                        trade_date=trade_date,
                                        phase="CLOSE",
                                        reason="更强卖出风险替换原待卖单",
                                        order=replaced,
                                        score=score,
                                    )
                                )
                elif not next_date:
                    day_rejections.append(
                        ReplayRejection(
                            trade_date=trade_date,
                            phase="CLOSE",
                            reason="没有已知的下一交易日，无法安排严格卖单",
                            score=score,
                        )
                    )

            position = _mark_position(position, cash, bar.close)
            latest_close = bar.close

        sell_blocked = any(
            rejection.order is not None
            and rejection.order.side == OrderSide.SELL
            and (
                rejection.carried_forward
                or "T+1" in rejection.reason
                or "无法卖出" in rejection.reason
                or "停牌" in rejection.reason
            )
            for rejection in day_rejections
        )
        had_buy_fill = any(fill.side == OrderSide.BUY for fill in day_fills)
        had_sell_fill = any(fill.side == OrderSide.SELL for fill in day_fills)
        position, lifecycle_transition = resolve_lifecycle_after_day(
            position,
            pending,
            trade_date=trade_date,
            from_state=position_at_open.lifecycle_state,
            sell_blocked=sell_blocked,
            had_buy_fill=had_buy_fill,
            had_sell_fill=had_sell_fill,
        )

        pending.sort(key=_order_sort_key)
        all_fills.extend(day_fills)
        all_rejections.extend(day_rejections)
        records.append(
            DailyReplayRecord(
                trade_date=trade_date,
                bar=bar,
                position_at_open=position_at_open,
                cash_at_open=cash_at_open,
                score=score,
                fills=tuple(day_fills),
                rejections=tuple(day_rejections),
                position_at_close=position,
                cash_at_close=cash,
                pending_orders=tuple(pending),
                lifecycle_transition=lifecycle_transition,
            )
        )

    return DailyReplayResult(
        daily_records=tuple(records),
        fills=tuple(all_fills),
        rejections=tuple(all_rejections),
        final_position=position,
        final_cash=cash,
        final_equity=round(cash + position.shares * (latest_close or position.avg_cost), 2),
        pending_orders=tuple(sorted(pending, key=_order_sort_key)),
        calendar_source="EXPLICIT_EXCHANGE_CALENDAR",
    )


def replay_exit_mode_pair(
    bars: Sequence[DailyData],
    *,
    score_provider_factory: Callable[[], ScoreProvider],
    **replay_kwargs: Any,
) -> PairedExitReplayResult:
    """Run identical inputs through both required exit-price assumptions."""

    if "exit_mode" in replay_kwargs:
        raise ValueError("exit_mode is controlled by replay_exit_mode_pair")
    research = replay_daily_stock(
        bars,
        score_provider=score_provider_factory(),
        exit_mode=ExecutionMode.SAME_CLOSE_RESEARCH,
        **replay_kwargs,
    )
    strict = replay_daily_stock(
        bars,
        score_provider=score_provider_factory(),
        exit_mode=ExecutionMode.NEXT_OPEN_STRICT,
        **replay_kwargs,
    )
    return PairedExitReplayResult(
        same_close_research=research,
        next_open_strict=strict,
        final_equity_difference=round(research.final_equity - strict.final_equity, 2),
        fill_count_difference=len(research.fills) - len(strict.fills),
    )
