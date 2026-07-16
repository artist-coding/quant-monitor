"""日线持仓系统最基础的时间与执行契约测试。"""

from __future__ import annotations

import pytest

from modules.daily_portfolio import (
    AsOfContractError,
    DailyPortfolioConfig,
    DailyStockScore,
    ExecutionMode,
    LifecycleState,
    PositionState,
    PriceType,
    TradeAction,
    create_buy_order,
    create_sell_order,
    validate_bars_as_of,
)
from modules.indicators import DailyData


def _bar(date: str, close: float = 10.0) -> DailyData:
    return DailyData(
        ts_code="000001.SZ",
        trade_date=date,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.2,
        close=close,
        vol=1_000_000,
        amount=close * 1_000_000,
        pct_chg=0.0,
        prev_close=close,
    )


def _score(action: TradeAction, target: float) -> DailyStockScore:
    return DailyStockScore(
        ts_code="000001.SZ",
        signal_date="20260710",
        buy_score=80,
        sell_score=20,
        position_score=75,
        current_position_pct=0.0,
        target_position_pct=target,
        desired_action=action,
    )


def test_as_of_contract_accepts_confirmed_history() -> None:
    bars = [_bar("20260709"), _bar("2026-07-10")]
    assert validate_bars_as_of(bars, "20260710") == tuple(bars)


def test_as_of_contract_rejects_future_bar() -> None:
    bars = [_bar("20260710"), _bar("20260713")]
    with pytest.raises(AsOfContractError, match="future bar"):
        validate_bars_as_of(bars, "20260710")


def test_as_of_contract_rejects_impossible_calendar_date() -> None:
    with pytest.raises(AsOfContractError, match="unsupported trade date"):
        validate_bars_as_of([_bar("20260230")], "20260710")


@pytest.mark.parametrize(
    "bars,error",
    [
        ([_bar("20260710"), _bar("20260709")], "ordered"),
        ([_bar("20260710"), _bar("20260710")], "duplicate"),
    ],
)
def test_as_of_contract_rejects_bad_sequence(bars, error) -> None:
    with pytest.raises(AsOfContractError, match=error):
        validate_bars_as_of(bars, "20260710")


def test_buy_signal_creates_next_open_order() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.15), "20260713")
    assert order.signal_date == "20260710"
    assert order.planned_execution_date == "20260713"
    assert order.price_type == PriceType.NEXT_OPEN
    assert order.lookahead_flag is False
    assert len(order.order_id) == 64
    assert order.root_order_id == order.order_id


def test_order_identity_is_deterministic_for_retry_idempotency() -> None:
    first = create_buy_order(_score(TradeAction.OPEN, 0.15), "20260713")
    second = create_buy_order(_score(TradeAction.OPEN, 0.15), "2026-07-13")
    different = create_buy_order(_score(TradeAction.OPEN, 0.20), "20260713")

    assert first.order_id == second.order_id
    assert first.order_id != different.order_id


def test_next_open_date_comparison_normalizes_mixed_formats() -> None:
    order = create_buy_order(_score(TradeAction.OPEN, 0.15), "2026-07-13")
    assert order.signal_date == "20260710"
    assert order.planned_execution_date == "20260713"


def test_next_open_rejects_same_date_in_a_different_format() -> None:
    with pytest.raises(ValueError, match="must be after"):
        create_buy_order(_score(TradeAction.OPEN, 0.15), "2026-07-10")


def test_score_records_last_bar_date_separately_from_signal_date() -> None:
    score = _score(TradeAction.WATCH, 0)
    assert score.last_bar_date == "20260710"
    assert score.as_dict()["hard_exit_reasons"] == []

    lagged = DailyStockScore(
        ts_code="000001.SZ",
        signal_date="20260710",
        last_bar_date="2026-07-09",
        buy_score=0,
        sell_score=0,
        position_score=0,
        current_position_pct=0,
        target_position_pct=0,
        desired_action=TradeAction.WATCH,
    )
    assert lagged.last_bar_date == "20260709"


@pytest.mark.parametrize(
    "kwargs,match",
    [
        (
            {
                "desired_action": TradeAction.OPEN,
                "target_position_pct": 0.10,
                "hard_exit_reasons": ("STOP",),
            },
            "hard exit",
        ),
        (
            {
                "desired_action": TradeAction.ADD,
                "target_position_pct": 0.10,
                "vetoes": ("FUTURE_BAR",),
            },
            "hard veto",
        ),
    ],
)
def test_score_cannot_encode_buy_exposure_against_hard_risk(kwargs, match) -> None:
    with pytest.raises(ValueError, match=match):
        DailyStockScore(
            ts_code="000001.SZ",
            signal_date="20260710",
            buy_score=100,
            sell_score=100,
            position_score=100,
            current_position_pct=0,
            **kwargs,
        )


@pytest.mark.parametrize(
    "action",
    [TradeAction.HOLD, TradeAction.WATCH, TradeAction.REDUCE, TradeAction.BLOCK],
)
def test_held_hard_exit_cannot_be_downgraded_to_a_non_exit_action(action) -> None:
    with pytest.raises(ValueError, match="must EXIT"):
        DailyStockScore(
            ts_code="000001.SZ",
            signal_date="20260710",
            buy_score=0,
            sell_score=100,
            position_score=0,
            current_position_pct=0.10,
            target_position_pct=0,
            desired_action=action,
            hard_exit_reasons=("STOP",),
        )


def test_same_close_sell_is_explicitly_marked_lookahead() -> None:
    order = create_sell_order(
        _score(TradeAction.EXIT, 0.0),
        ExecutionMode.SAME_CLOSE_RESEARCH,
    )
    assert order.planned_execution_date == "20260710"
    assert order.price_type == PriceType.SAME_CLOSE_RESEARCH
    assert order.lookahead_flag is True


def test_strict_sell_executes_next_open() -> None:
    order = create_sell_order(
        _score(TradeAction.REDUCE, 0.05),
        ExecutionMode.NEXT_OPEN_STRICT,
        "20260713",
    )
    assert order.planned_execution_date == "20260713"
    assert order.price_type == PriceType.NEXT_OPEN
    assert order.lookahead_flag is False


def test_sell_order_rejects_an_untyped_execution_mode() -> None:
    with pytest.raises(ValueError, match="ExecutionMode"):
        create_sell_order(_score(TradeAction.EXIT, 0), "NEXT_OPEN_STRICT")


def test_default_weight_and_threshold_config_is_valid() -> None:
    config = DailyPortfolioConfig()
    assert sum(config.score_weights.buy.values()) == 100
    assert sum(config.score_weights.sell.values()) == 100
    assert config.thresholds.add_buy_score >= config.thresholds.open_buy_score


def test_weight_maps_are_immutable_and_config_has_stable_fingerprint() -> None:
    first = DailyPortfolioConfig()
    second = DailyPortfolioConfig()
    assert first.fingerprint() == second.fingerprint()
    assert len(first.fingerprint()) == 64
    with pytest.raises(TypeError):
        first.score_weights.buy["trend"] = 999


def test_position_state_rejects_inconsistent_lifecycle_and_noninteger_shares() -> None:
    with pytest.raises(ValueError, match="lifecycle"):
        PositionState(
            ts_code="000001.SZ",
            lifecycle_state=LifecycleState.FLAT,
            shares=100,
            available_shares=100,
            avg_cost=10,
            current_position_pct=0.10,
        )
    with pytest.raises(ValueError, match="integers"):
        PositionState(ts_code="000001.SZ", shares=100.5)
