"""Timing, price and A-share constraint tests for daily order execution."""

from dataclasses import replace

import pytest

from modules.daily_portfolio import (
    DailyStockScore,
    ExecutionMode,
    LifecycleState,
    PositionState,
    PriceType,
    TradeAction,
    create_buy_order,
    create_sell_order,
)
from modules.daily_portfolio.execution_model import (
    ExecutionConfig,
    ExecutionStatus,
    execute_target_order,
)
from modules.indicators import DailyData


def _score(action: TradeAction, target: float) -> DailyStockScore:
    return DailyStockScore(
        ts_code="000001.SZ",
        signal_date="20260710",
        buy_score=80,
        sell_score=20,
        position_score=75,
        current_position_pct=0,
        target_position_pct=target,
        desired_action=action,
        stop_loss=9.0,
    )


def _bar(
    date: str,
    *,
    open_price: float,
    high: float = 99,
    low: float = 1,
    close: float = 50,
    vol: float = 123,
) -> DailyData:
    return DailyData(
        ts_code="000001.SZ",
        trade_date=date,
        open=open_price,
        high=high,
        low=low,
        close=close,
        vol=vol,
        amount=vol * close,
        pct_chg=0,
        prev_close=10,
    )


def _flat() -> PositionState:
    return PositionState(ts_code="000001.SZ")


def _held(*, available: int = 1000, can_sell_date: str = "") -> PositionState:
    return PositionState(
        ts_code="000001.SZ",
        lifecycle_state=LifecycleState.HOLDING,
        shares=1000,
        available_shares=available,
        avg_cost=10,
        current_position_pct=0.10,
        can_sell_date=can_sell_date,
    )


def test_next_open_fill_ignores_execution_day_high_low_close_and_volume() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.10), "20260713")
    config = ExecutionConfig(apply_price_limits=False)
    first = execute_target_order(
        order,
        _bar("20260713", open_price=10, high=11, low=9, close=10.5, vol=1_000),
        _flat(),
        cash=1_000_000,
        equity=1_000_000,
        previous_close=9.8,
        config=config,
        next_trading_date="20260714",
    )
    second = execute_target_order(
        order,
        _bar("20260713", open_price=10, high=90, low=0.1, close=80, vol=99_000_000),
        _flat(),
        cash=1_000_000,
        equity=1_000_000,
        previous_close=9.8,
        config=config,
        next_trading_date="20260714",
    )

    assert first.fill is not None and second.fill is not None
    assert first.fill.price_type == PriceType.NEXT_OPEN
    assert first.fill.fill_price == second.fill.fill_price
    assert first.fill.shares == second.fill.shares
    assert first.cash == second.cash


def test_strict_sell_uses_next_day_open() -> None:
    order = create_sell_order(
        _score(TradeAction.EXIT, 0),
        ExecutionMode.NEXT_OPEN_STRICT,
        "20260713",
    )
    result = execute_target_order(
        order,
        _bar("20260713", open_price=8, close=12),
        _held(),
        cash=0,
        equity=8_000,
        previous_close=10,
        config=ExecutionConfig(apply_price_limits=False, sell_slippage_rate=0),
    )

    assert result.fill is not None
    assert result.fill.raw_price == 8
    assert result.fill.execution_date == "20260713"
    assert result.fill.lookahead_flag is False


def test_same_close_research_sell_is_explicit_and_uses_close() -> None:
    order = create_sell_order(
        _score(TradeAction.EXIT, 0), ExecutionMode.SAME_CLOSE_RESEARCH
    )
    result = execute_target_order(
        order,
        _bar("20260710", open_price=8, close=12),
        _held(),
        cash=0,
        equity=12_000,
        previous_close=10,
        config=ExecutionConfig(apply_price_limits=False, sell_slippage_rate=0),
    )

    assert result.fill is not None
    assert result.fill.raw_price == 12
    assert result.fill.execution_date == result.fill.signal_date == "20260710"
    assert result.fill.lookahead_flag is True


def test_order_cannot_execute_on_signal_day_when_planned_for_next_open() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.10), "20260713")
    result = execute_target_order(
        order,
        _bar("20260710", open_price=10),
        _flat(),
        cash=1_000_000,
        equity=1_000_000,
        previous_close=9.8,
    )
    assert result.status == ExecutionStatus.NOT_DUE
    assert result.fill is None


def test_open_limit_blocks_buy_using_only_open_and_previous_close() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.10), "20260713")
    result = execute_target_order(
        order,
        _bar("20260713", open_price=11),
        _flat(),
        cash=1_000_000,
        equity=1_000_000,
        previous_close=10,
    )
    assert result.status == ExecutionStatus.BLOCKED
    assert "涨停" in result.reason


def test_price_limit_uses_half_up_tick_rounding_not_bankers_rounding() -> None:
    score = replace(_score(TradeAction.OPEN, 0.10), stop_loss=1.0)
    order = create_buy_order(score, "20260713")
    below_limit = execute_target_order(
        order,
        _bar("20260713", open_price=1.26),
        _flat(),
        cash=100_000,
        equity=100_000,
        previous_close=1.15,
        config=ExecutionConfig(buy_slippage_rate=0),
        next_trading_date="20260714",
    )
    at_limit = execute_target_order(
        order,
        _bar("20260713", open_price=1.27),
        _flat(),
        cash=100_000,
        equity=100_000,
        previous_close=1.15,
        config=ExecutionConfig(buy_slippage_rate=0),
        next_trading_date="20260714",
    )

    assert below_limit.fill is not None
    assert below_limit.fill.raw_price == 1.26
    assert at_limit.status == ExecutionStatus.BLOCKED
    assert "1.27" in at_limit.reason


def test_missing_previous_close_fails_closed_when_limits_are_enabled() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.10), "20260713")
    result = execute_target_order(
        order,
        _bar("20260713", open_price=10),
        _flat(),
        cash=100_000,
        equity=100_000,
        previous_close=None,
        next_trading_date="20260714",
    )
    assert result.status == ExecutionStatus.BLOCKED
    assert "前收盘" in result.reason


def test_slippage_fill_respects_price_tick_and_inferred_limit_bound() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.10), "20260713")
    result = execute_target_order(
        order,
        _bar("20260713", open_price=10.99),
        _flat(),
        cash=100_000,
        equity=100_000,
        previous_close=10,
        config=ExecutionConfig(buy_slippage_rate=0.001),
        next_trading_date="20260714",
    )
    assert result.fill is not None
    assert result.fill.fill_price == 11.0
    assert result.fill.price_limit_model_version
    assert result.fill.cost_model_version


def test_t1_blocks_same_day_sell_when_no_shares_are_available() -> None:
    order = create_sell_order(
        _score(TradeAction.EXIT, 0), ExecutionMode.SAME_CLOSE_RESEARCH
    )
    result = execute_target_order(
        order,
        _bar("20260710", open_price=10, close=10),
        _held(available=0, can_sell_date="20260713"),
        cash=0,
        equity=10_000,
        previous_close=10,
        config=ExecutionConfig(apply_price_limits=False),
    )
    assert result.status == ExecutionStatus.BLOCKED
    assert "T+1" in result.reason


def test_buy_quantity_is_recomputed_from_actual_next_open() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.10), "20260713")
    config = ExecutionConfig(
        apply_price_limits=False,
        buy_slippage_rate=0,
        risk_per_trade_pct=1,
    )
    low_open = execute_target_order(
        order,
        _bar("20260713", open_price=10),
        _flat(),
        cash=1_000_000,
        equity=1_000_000,
        previous_close=10,
        config=config,
        next_trading_date="20260714",
    )
    high_open = execute_target_order(
        order,
        _bar("20260713", open_price=20),
        _flat(),
        cash=1_000_000,
        equity=1_000_000,
        previous_close=10,
        config=config,
        next_trading_date="20260714",
    )

    assert low_open.fill is not None and high_open.fill is not None
    assert low_open.fill.shares == 10_000
    assert high_open.fill.shares == 5_000


def test_buy_quantity_is_capped_by_risk_budget_and_stop_distance() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.50), "20260713")
    result = execute_target_order(
        order,
        _bar("20260713", open_price=10),
        _flat(),
        cash=100_000,
        equity=100_000,
        previous_close=10,
        config=ExecutionConfig(
            apply_price_limits=False,
            buy_slippage_rate=0,
            risk_per_trade_pct=0.01,
        ),
        next_trading_date="20260714",
    )

    assert result.fill is not None
    assert result.fill.shares == 1_000
    assert result.status == ExecutionStatus.PARTIAL
    assert result.fill.requested_shares == 5_000
    assert result.fill.unfilled_shares == 4_000
    assert result.fill.order_id == order.order_id
    assert result.fill.stop_loss == 9.0
    assert result.fill.risk_per_share == 1.0


def test_buy_without_a_stop_fails_closed() -> None:
    score = replace(_score(TradeAction.OPEN, 0.10), stop_loss=None)
    order = create_buy_order(score, "20260713")
    result = execute_target_order(
        order,
        _bar("20260713", open_price=10),
        _flat(),
        cash=100_000,
        equity=100_000,
        previous_close=10,
        config=ExecutionConfig(apply_price_limits=False),
        next_trading_date="20260714",
    )

    assert result.status == ExecutionStatus.BLOCKED
    assert result.fill is None
    assert "止损" in result.reason


def test_t1_buy_requires_a_known_next_trading_date() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.10), "20260713")
    with pytest.raises(ValueError, match="next_trading_date"):
        execute_target_order(
            order,
            _bar("20260713", open_price=10),
            _flat(),
            cash=1_000_000,
            equity=1_000_000,
            previous_close=10,
            config=ExecutionConfig(apply_price_limits=False),
        )


def test_t1_sell_date_must_be_after_the_buy_execution_date() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.10), "20260713")
    with pytest.raises(ValueError, match="must be after"):
        execute_target_order(
            order,
            _bar("20260713", open_price=10),
            _flat(),
            cash=1_000_000,
            equity=1_000_000,
            previous_close=10,
            config=ExecutionConfig(apply_price_limits=False),
            next_trading_date="20260713",
        )


def test_wrong_symbol_is_not_due() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.10), "20260713")
    bar = replace(_bar("20260713", open_price=10), ts_code="600000.SH")
    result = execute_target_order(
        order,
        bar,
        _flat(),
        cash=1_000_000,
        equity=1_000_000,
        previous_close=10,
    )
    assert result.status == ExecutionStatus.NOT_DUE


def test_fill_id_includes_the_canonical_execution_config() -> None:
    order = create_sell_order(
        _score(TradeAction.EXIT, 0),
        ExecutionMode.NEXT_OPEN_STRICT,
        "20260713",
    )
    base_config = ExecutionConfig(
        apply_price_limits=False,
        sell_slippage_rate=0,
        commission_rate=0,
        minimum_commission=0,
        stamp_duty_rate=0,
        transfer_fee_rate=0,
        cost_model_version="test-cost-v1",
    )
    changed_config = replace(
        base_config,
        commission_rate=0.001,
        cost_model_version="test-cost-v2",
    )

    base = execute_target_order(
        order,
        _bar("20260713", open_price=10, close=10),
        _held(),
        cash=0,
        equity=10_000,
        previous_close=10,
        config=base_config,
    )
    changed = execute_target_order(
        order,
        _bar("20260713", open_price=10, close=10),
        _held(),
        cash=0,
        equity=10_000,
        previous_close=10,
        config=changed_config,
    )

    assert base.fill is not None and changed.fill is not None
    assert base.fill.fill_price == changed.fill.fill_price
    assert base.fill.shares == changed.fill.shares
    assert base.fill.execution_config_fingerprint == base_config.canonical_fingerprint
    assert (
        changed.fill.execution_config_fingerprint
        == changed_config.canonical_fingerprint
    )
    assert base.fill.execution_config_fingerprint != changed.fill.execution_config_fingerprint
    assert base.fill.fill_id != changed.fill.fill_id


@pytest.mark.parametrize(
    "field",
    [
        "buy_slippage_rate",
        "sell_slippage_rate",
        "commission_rate",
        "minimum_commission",
        "stamp_duty_rate",
        "transfer_fee_rate",
        "cash_utilization_limit",
        "risk_per_trade_pct",
        "price_tick",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_execution_config_rejects_non_finite_numbers(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=r"finite|in \(0, 1\]"):
        ExecutionConfig(**{field: value})


@pytest.mark.parametrize("value", [1.5, float("nan"), float("inf"), True])
def test_execution_config_requires_an_integer_lot_size(value) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ExecutionConfig(lot_size=value)
