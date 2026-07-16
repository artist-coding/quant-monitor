"""单股日线回放器的时间、执行与前视边界测试。"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from modules.daily_portfolio.execution import create_buy_order, create_sell_order
from modules.daily_portfolio.execution_model import ExecutionConfig
from modules.daily_portfolio.models import (
    DailyStockScore,
    ExecutionMode,
    LifecycleState,
    OrderSide,
    PositionState,
    PriceType,
    TradeAction,
)
from modules.daily_portfolio.replay import replay_daily_stock, replay_exit_mode_pair
from modules.indicators import DailyData


TS_CODE = "000001.SZ"


def _bar(
    trade_date: str,
    *,
    open_price: float = 10.0,
    close: float = 10.0,
    previous_close: float = 10.0,
) -> DailyData:
    return DailyData(
        ts_code=TS_CODE,
        trade_date=trade_date,
        open=open_price,
        high=max(open_price, close) + 0.2,
        low=min(open_price, close) - 0.2,
        close=close,
        vol=1_000_000,
        amount=close * 1_000_000,
        pct_chg=(close / previous_close - 1) * 100,
        prev_close=previous_close,
    )


def _score(
    trade_date: str,
    position: PositionState,
    action: TradeAction,
    target: float,
) -> DailyStockScore:
    return DailyStockScore(
        ts_code=TS_CODE,
        signal_date=trade_date,
        buy_score=90 if action in (TradeAction.OPEN, TradeAction.ADD) else 10,
        sell_score=90 if action in (TradeAction.REDUCE, TradeAction.EXIT) else 10,
        position_score=70,
        current_position_pct=position.current_position_pct,
        target_position_pct=target,
        desired_action=action,
        stop_loss=9.0,
    )


def _provider(
    actions: Mapping[str, tuple[TradeAction, float]],
    calls: list[tuple[tuple[str, ...], object]] | None = None,
):
    def provide(prefix, position, market):
        dates = tuple(bar.trade_date.replace("-", "") for bar in prefix)
        if calls is not None:
            calls.append((dates, market))
        action, target = actions.get(
            dates[-1], (TradeAction.HOLD, position.current_position_pct)
        )
        return _score(dates[-1], position, action, target)

    return provide


def _execution_config(*, apply_price_limits: bool = False) -> ExecutionConfig:
    return ExecutionConfig(
        buy_slippage_rate=0,
        sell_slippage_rate=0,
        commission_rate=0,
        minimum_commission=0,
        stamp_duty_rate=0,
        transfer_fee_rate=0,
        cash_utilization_limit=1,
        apply_price_limits=apply_price_limits,
    )


def _replay(bars, **kwargs):
    kwargs.setdefault(
        "trading_dates",
        tuple(bar.trade_date.replace("-", "") for bar in bars),
    )
    return replay_daily_stock(bars, **kwargs)


def _holding() -> PositionState:
    return PositionState(
        ts_code=TS_CODE,
        lifecycle_state=LifecycleState.HOLDING,
        shares=1_000,
        available_shares=1_000,
        avg_cost=9.0,
        current_position_pct=0.10,
    )


def test_buy_signal_only_fills_at_next_trading_day_open() -> None:
    bars = [
        _bar("20260710", open_price=10.0, close=10.4),
        _bar("20260713", open_price=11.0, close=11.5, previous_close=10.4),
    ]
    calls: list[tuple[tuple[str, ...], object]] = []
    result = _replay(
        bars,
        score_provider=_provider(
            {"20260710": (TradeAction.OPEN, 0.25)}, calls
        ),
        market_provider=lambda trade_date: {"trade_date": trade_date},
        execution_config=_execution_config(),
        following_trading_date="20260714",
    )

    assert result.daily_records[0].fills == ()
    assert result.daily_records[0].pending_orders[0].planned_execution_date == "20260713"
    assert len(result.fills) == 1
    assert result.fills[0].side == OrderSide.BUY
    assert result.fills[0].signal_date == "20260710"
    assert result.fills[0].execution_date == "20260713"
    assert result.fills[0].raw_price == 11.0
    assert result.fills[0].price_type == PriceType.NEXT_OPEN
    assert calls[0] == (("20260710",), {"trade_date": "20260710"})


def test_open_executes_due_strict_sells_before_due_buys() -> None:
    source_position = _holding()
    sell_score = _score("20260710", source_position, TradeAction.EXIT, 0.0)
    buy_score = _score("20260710", source_position, TradeAction.ADD, 0.20)
    due_sell = create_sell_order(
        sell_score, ExecutionMode.NEXT_OPEN_STRICT, "20260713"
    )
    due_buy = create_buy_order(buy_score, "20260713")

    result = _replay(
        [_bar("20260713", open_price=10.0, close=10.0)],
        score_provider=_provider({}),
        initial_position=source_position,
        initial_cash=90_000,
        initial_pending_orders=(due_buy, due_sell),
        execution_config=_execution_config(),
        following_trading_date="20260714",
    )

    assert [fill.side for fill in result.fills] == [OrderSide.SELL, OrderSide.BUY]


def test_same_close_research_exit_is_same_day_and_marked_lookahead() -> None:
    result = _replay(
        [_bar("20260710", open_price=10.0, close=10.8)],
        score_provider=_provider({"20260710": (TradeAction.EXIT, 0.0)}),
        initial_position=_holding(),
        initial_cash=90_000,
        exit_mode=ExecutionMode.SAME_CLOSE_RESEARCH,
        execution_config=_execution_config(),
    )

    assert len(result.fills) == 1
    fill = result.fills[0]
    assert fill.execution_date == "20260710"
    assert fill.raw_price == 10.8
    assert fill.price_type == PriceType.SAME_CLOSE_RESEARCH
    assert fill.lookahead_flag is True
    assert result.final_position.shares == 0


def test_strict_exit_fills_at_next_open_without_lookahead() -> None:
    result = _replay(
        [
            _bar("20260710", close=10.2),
            _bar("20260713", open_price=9.6, close=9.8, previous_close=10.2),
        ],
        score_provider=_provider({"20260710": (TradeAction.EXIT, 0.0)}),
        initial_position=_holding(),
        initial_cash=90_000,
        exit_mode=ExecutionMode.NEXT_OPEN_STRICT,
        execution_config=_execution_config(),
    )

    assert len(result.fills) == 1
    fill = result.fills[0]
    assert fill.signal_date == "20260710"
    assert fill.execution_date == "20260713"
    assert fill.raw_price == 9.6
    assert fill.price_type == PriceType.NEXT_OPEN
    assert fill.lookahead_flag is False


def test_last_day_following_date_keeps_order_pending_and_never_fills_it() -> None:
    result = _replay(
        [_bar("20260710", close=10.2)],
        score_provider=_provider({"20260710": (TradeAction.OPEN, 0.25)}),
        following_trading_date="20260713",
        execution_config=_execution_config(),
    )

    assert result.fills == ()
    assert len(result.daily_records) == 1
    assert len(result.pending_orders) == 1
    assert result.pending_orders[0].planned_execution_date == "20260713"
    assert result.pending_orders[0].price_type == PriceType.NEXT_OPEN


def test_blocked_strict_sell_rolls_but_blocked_buy_does_not() -> None:
    strict_result = _replay(
        [
            _bar("20260710", close=10.0),
            _bar("20260713", open_price=9.0, close=9.2, previous_close=10.0),
            _bar("20260714", open_price=9.5, close=9.6, previous_close=9.2),
        ],
        score_provider=_provider({"20260710": (TradeAction.EXIT, 0.0)}),
        initial_position=_holding(),
        initial_cash=90_000,
        execution_config=_execution_config(apply_price_limits=True),
    )

    carried = [item for item in strict_result.rejections if item.carried_forward]
    assert len(carried) == 1
    assert "跌停" in carried[0].reason
    assert carried[0].rescheduled_to == "20260714"
    assert strict_result.fills[0].execution_date == "20260714"

    buy_result = _replay(
        [
            _bar("20260710", close=10.0),
            _bar("20260714", open_price=9.5, close=9.6, previous_close=10.0),
        ],
        trading_dates=("20260710", "20260713", "20260714"),
        score_provider=_provider({"20260710": (TradeAction.OPEN, 0.25)}),
        execution_config=_execution_config(),
    )

    assert buy_result.daily_records[1].bar is None
    assert "买单拒绝且不顺延" in buy_result.rejections[0].reason
    assert buy_result.fills == ()
    assert buy_result.pending_orders == ()


def test_score_prefix_is_invariant_when_future_bars_are_appended() -> None:
    bars = [
        _bar("20260710", close=10.2),
        _bar("20260713", open_price=10.5, close=10.6, previous_close=10.2),
        _bar("20260714", open_price=10.7, close=15.0, previous_close=10.6),
    ]
    actions = {"20260710": (TradeAction.OPEN, 0.25)}
    full_calls: list[tuple[tuple[str, ...], object]] = []
    prefix_calls: list[tuple[tuple[str, ...], object]] = []

    full = _replay(
        bars,
        score_provider=_provider(actions, full_calls),
        market_provider=lambda trade_date: trade_date,
        execution_config=_execution_config(),
        following_trading_date="20260715",
    )
    prefix = _replay(
        bars[:2],
        score_provider=_provider(actions, prefix_calls),
        market_provider=lambda trade_date: trade_date,
        following_trading_date="20260714",
        execution_config=_execution_config(),
    )

    assert full_calls[:2] == prefix_calls
    assert tuple(full.daily_records[:2]) == prefix.daily_records
    assert tuple(fill for fill in full.fills if fill.execution_date <= "20260713") == prefix.fills


def test_risk_sell_order_prevents_a_new_buy_order() -> None:
    position = _holding()
    strict_sell = create_sell_order(
        _score("20260709", position, TradeAction.EXIT, 0.0),
        ExecutionMode.NEXT_OPEN_STRICT,
        "20260714",
    )
    result = _replay(
        [_bar("20260710", close=10.0)],
        score_provider=_provider({"20260710": (TradeAction.ADD, 0.20)}),
        initial_position=position,
        initial_pending_orders=(strict_sell,),
        following_trading_date="20260713",
        execution_config=_execution_config(),
    )

    assert [order.side for order in result.pending_orders] == [OrderSide.SELL]
    assert any("禁止生成" in item.reason for item in result.rejections)


def test_sell_signal_cancels_a_future_buy_order() -> None:
    future_buy = create_buy_order(
        _score("20260709", _holding(), TradeAction.ADD, 0.20), "20260714"
    )
    result = _replay(
        [_bar("20260710", close=10.0)],
        score_provider=_provider({"20260710": (TradeAction.EXIT, 0.0)}),
        initial_position=_holding(),
        initial_pending_orders=(future_buy,),
        following_trading_date="20260713",
        execution_config=_execution_config(),
    )

    assert all(order.side != OrderSide.BUY for order in result.pending_orders)
    assert any("取消" in item.reason for item in result.rejections)


def test_replay_rejects_unsorted_bars_instead_of_silently_sorting() -> None:
    with pytest.raises(ValueError, match="strictly ascending"):
        _replay(
            [_bar("20260713"), _bar("20260710")],
            score_provider=_provider({}),
            execution_config=_execution_config(),
        )


def test_last_day_due_buy_requires_real_following_calendar_for_t1() -> None:
    pending_buy = create_buy_order(
        _score("20260709", PositionState(ts_code=TS_CODE), TradeAction.OPEN, 0.20),
        "20260710",
    )
    result = _replay(
        [_bar("20260710")],
        score_provider=_provider({}),
        initial_pending_orders=(pending_buy,),
        execution_config=_execution_config(),
    )

    assert result.fills == ()
    assert any("下一交易日历" in item.reason for item in result.rejections)


def test_replay_requires_an_explicit_exchange_calendar() -> None:
    with pytest.raises(ValueError, match="explicit exchange"):
        replay_daily_stock(
            [_bar("20260710")],
            score_provider=_provider({}),
            execution_config=_execution_config(),
        )


def test_stronger_exit_replaces_a_pending_reduce_order() -> None:
    position = _holding()
    pending_reduce = create_sell_order(
        _score("20260709", position, TradeAction.REDUCE, 0.05),
        ExecutionMode.NEXT_OPEN_STRICT,
        "20260714",
    )
    result = _replay(
        [_bar("20260710")],
        score_provider=_provider({"20260710": (TradeAction.EXIT, 0.0)}),
        initial_position=position,
        initial_pending_orders=(pending_reduce,),
        following_trading_date="20260713",
        execution_config=_execution_config(),
    )

    assert len(result.pending_orders) == 1
    assert result.pending_orders[0].target_position_pct == 0
    assert result.pending_orders[0].signal_date == "20260710"
    assert result.pending_orders[0].planned_execution_date == "20260713"
    assert any("替换" in item.reason for item in result.rejections)


def test_next_open_tradability_callback_cannot_see_full_day_fields() -> None:
    pending_buy = create_buy_order(
        _score(
            "20260709",
            PositionState(ts_code=TS_CODE),
            TradeAction.OPEN,
            0.20,
        ),
        "20260710",
    )
    seen = []

    def tradable(quote, order):
        seen.append((quote, order))
        assert quote.execution_price == 10.0
        assert quote.price_type == PriceType.NEXT_OPEN
        assert not hasattr(quote, "close")
        assert not hasattr(quote, "vol")
        return True

    result = _replay(
        [_bar("20260710", open_price=10.0, close=99.0)],
        score_provider=_provider({}),
        initial_pending_orders=(pending_buy,),
        following_trading_date="20260713",
        tradability_provider=tradable,
        execution_config=_execution_config(),
    )

    assert len(seen) == 1
    assert len(result.fills) == 1


def test_paired_replay_runs_both_exit_modes_on_the_same_inputs() -> None:
    bars = [
        _bar("20260710", close=10.0),
        _bar("20260713", open_price=8.0, close=8.5, previous_close=10.0),
    ]
    paired = replay_exit_mode_pair(
        bars,
        score_provider_factory=lambda: _provider(
            {"20260710": (TradeAction.EXIT, 0.0)}
        ),
        initial_position=_holding(),
        initial_cash=90_000,
        trading_dates=("20260710", "20260713"),
        following_trading_date="20260714",
        execution_config=_execution_config(),
    )

    assert paired.same_close_research.fills[0].lookahead_flag is True
    assert paired.next_open_strict.fills[0].lookahead_flag is False
    assert paired.same_close_research.fills[0].raw_price == 10.0
    assert paired.next_open_strict.fills[0].raw_price == 8.0
    assert paired.final_equity_difference > 0


def test_carried_partial_sell_uses_child_order_and_unique_fill_ids() -> None:
    position = PositionState(
        ts_code=TS_CODE,
        lifecycle_state=LifecycleState.HOLDING,
        shares=1_000,
        available_shares=500,
        avg_cost=9,
        current_position_pct=0.10,
        can_sell_date="20260714",
    )
    root_order = create_sell_order(
        _score("20260710", position, TradeAction.EXIT, 0),
        ExecutionMode.NEXT_OPEN_STRICT,
        "20260713",
    )
    result = _replay(
        [
            _bar("20260713", open_price=10, close=10),
            _bar("20260714", open_price=10, close=10),
        ],
        score_provider=_provider({}),
        initial_position=position,
        initial_pending_orders=(root_order,),
        following_trading_date="20260715",
        execution_config=_execution_config(),
    )

    assert len(result.fills) == 2
    assert result.fills[0].order_id != result.fills[1].order_id
    assert result.fills[0].fill_id != result.fills[1].fill_id
    assert result.fills[0].root_order_id == root_order.order_id
    assert result.fills[1].root_order_id == root_order.order_id


def test_t1_blocks_same_day_close_sale_then_unlocks_on_next_trade_date() -> None:
    result = _replay(
        [
            _bar("20260710", close=10.0),
            _bar("20260713", open_price=10.0, close=10.2, previous_close=10.0),
            _bar("20260714", open_price=10.1, close=10.3, previous_close=10.2),
        ],
        score_provider=_provider(
            {
                "20260710": (TradeAction.OPEN, 0.25),
                "20260713": (TradeAction.EXIT, 0.0),
                "20260714": (TradeAction.EXIT, 0.0),
            }
        ),
        exit_mode=ExecutionMode.SAME_CLOSE_RESEARCH,
        execution_config=_execution_config(),
    )

    assert [(fill.side, fill.execution_date) for fill in result.fills] == [
        (OrderSide.BUY, "20260713"),
        (OrderSide.SELL, "20260714"),
    ]
    assert any(
        rejection.trade_date == "20260713"
        and "T+1" in rejection.reason
        and not rejection.carried_forward
        for rejection in result.rejections
    )
    assert result.final_position.shares == 0


def test_blocked_same_close_hard_exit_latches_until_next_open_fill() -> None:
    bars = [
        _bar("20260710", close=10.0),
        _bar("20260713", open_price=10.0, close=10.1, previous_close=10.0),
        _bar("20260714", open_price=9.8, close=9.9, previous_close=10.1),
    ]
    initial = PositionState(
        ts_code=TS_CODE,
        lifecycle_state=LifecycleState.HOLDING,
        shares=1_000,
        available_shares=0,
        avg_cost=9.0,
        current_position_pct=0.10,
        can_sell_date="20260713",
    )

    def score_provider(prefix, position, market):
        trade_date = prefix[-1].trade_date.replace("-", "")
        if trade_date == "20260710":
            return DailyStockScore(
                ts_code=TS_CODE,
                signal_date=trade_date,
                buy_score=10,
                sell_score=100,
                position_score=0,
                current_position_pct=position.current_position_pct,
                target_position_pct=0,
                desired_action=TradeAction.EXIT,
                stop_loss=9.0,
                hard_exit_reasons=("EXIT_STOP_LOSS",),
            )
        if trade_date == "20260713":
            return _score(trade_date, position, TradeAction.ADD, 0.20)
        return _score(
            trade_date,
            position,
            TradeAction.HOLD,
            position.current_position_pct,
        )

    def tradable(quote, order):
        return not (
            quote.trade_date == "20260713"
            and quote.price_type == PriceType.NEXT_OPEN
            and order.side == OrderSide.SELL
        )

    result = _replay(
        bars,
        score_provider=score_provider,
        initial_position=initial,
        initial_cash=90_000,
        exit_mode=ExecutionMode.SAME_CLOSE_RESEARCH,
        following_trading_date="20260715",
        tradability_provider=tradable,
        execution_config=_execution_config(),
    )

    first_day = result.daily_records[0]
    original_research = next(
        rejection
        for rejection in first_day.rejections
        if rejection.order is not None
        and rejection.order.price_type == PriceType.SAME_CLOSE_RESEARCH
    )
    latched = next(
        rejection
        for rejection in first_day.rejections
        if rejection.carried_forward
        and rejection.order is not None
        and rejection.order.price_type == PriceType.NEXT_OPEN
    )
    assert original_research.carried_forward is False
    assert latched.order is not None and original_research.order is not None
    assert latched.order.supersedes_order_id == original_research.order.order_id
    assert first_day.position_at_close.lifecycle_state == LifecycleState.LOCKED
    assert first_day.pending_orders == (latched.order,)

    second_day = result.daily_records[1]
    assert second_day.position_at_close.lifecycle_state == LifecycleState.LOCKED
    assert any("禁止生成" in rejection.reason for rejection in second_day.rejections)
    assert len(second_day.pending_orders) == 1
    assert second_day.pending_orders[0].planned_execution_date == "20260714"

    assert len(result.fills) == 1
    assert result.fills[0].execution_date == "20260714"
    assert result.fills[0].price_type == PriceType.NEXT_OPEN
    assert result.fills[0].lookahead_flag is False
    assert result.final_position.shares == 0
    assert result.final_position.lifecycle_state == LifecycleState.EXITED


def test_overdue_tail_sell_gets_a_child_order_id_when_replay_resumes() -> None:
    position = _holding()
    root_order = create_sell_order(
        _score("20260709", position, TradeAction.EXIT, 0),
        ExecutionMode.NEXT_OPEN_STRICT,
        "20260710",
    )
    first = _replay(
        [_bar("20260710")],
        score_provider=_provider({}),
        initial_position=position,
        initial_cash=90_000,
        initial_pending_orders=(root_order,),
        tradability_provider=lambda quote, order: False,
        execution_config=_execution_config(),
    )
    assert len(first.pending_orders) == 1

    resumed = _replay(
        [_bar("20260713")],
        score_provider=_provider({}),
        initial_position=first.final_position,
        initial_cash=first.final_cash,
        initial_pending_orders=first.pending_orders,
        following_trading_date="20260714",
        execution_config=_execution_config(),
    )

    assert len(resumed.fills) == 1
    assert resumed.fills[0].order_id != root_order.order_id
    assert resumed.fills[0].root_order_id == root_order.root_order_id
    assert resumed.fills[0].supersedes_order_id == first.pending_orders[0].order_id


def test_blocked_tail_hard_exit_requires_a_following_exchange_date() -> None:
    position = PositionState(
        ts_code=TS_CODE,
        lifecycle_state=LifecycleState.HOLDING,
        shares=1_000,
        available_shares=0,
        avg_cost=9.0,
        current_position_pct=0.10,
        can_sell_date="20260713",
    )

    def hard_exit_provider(prefix, current, market):
        return DailyStockScore(
            ts_code=TS_CODE,
            signal_date="20260710",
            buy_score=0,
            sell_score=100,
            position_score=0,
            current_position_pct=current.current_position_pct,
            target_position_pct=0,
            desired_action=TradeAction.EXIT,
            stop_loss=9.0,
            hard_exit_reasons=("EXIT_STOP_LOSS",),
        )

    with pytest.raises(ValueError, match="next exchange trading date"):
        _replay(
            [_bar("20260710")],
            score_provider=hard_exit_provider,
            initial_position=position,
            exit_mode=ExecutionMode.SAME_CLOSE_RESEARCH,
            execution_config=_execution_config(),
        )
