"""Inventory lots are the execution truth behind aggregate PositionState."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
import hashlib

import pytest

from modules.daily_portfolio import (
    DailyPriceFrame,
    InventoryContractError,
    LedgerProvenance,
    LifecycleState,
    PositionLedger,
    PositionState,
    PriceBasis,
    PriceLevel,
    add_buy_lot,
    consume_sellable_fifo,
    ledger_from_position_state,
)
from modules.indicators import DailyData


TS_CODE = "600519.SH"
MANIFEST = "a" * 64


def _bar(trade_date: str, *, scale: float) -> DailyData:
    return DailyData(
        ts_code=TS_CODE,
        trade_date=trade_date,
        open=100 * scale,
        high=102 * scale,
        low=99 * scale,
        close=101 * scale,
        vol=1_000_000,
        amount=100_000_000,
        pct_chg=1,
        prev_close=100 * scale,
    )


def _frame(trade_date: str = "20260710") -> DailyPriceFrame:
    return DailyPriceFrame(
        signal_bar=_bar(trade_date, scale=2),
        execution_bar=_bar(trade_date, scale=1),
        signal_basis=PriceBasis.HFQ_POINT_IN_TIME,
        raw_per_signal_unit=0.5,
        adjustment_factor_known_at="2026-07-09T18:00:00+08:00",
        adjustment_factor_source_sha256=hashlib.sha256(b"factor").hexdigest(),
    )


def _position(*, available: int, can_sell_date: str) -> PositionState:
    return PositionState(
        ts_code=TS_CODE,
        lifecycle_state=LifecycleState.HOLDING,
        shares=1000,
        available_shares=available,
        avg_cost=100,
        current_position_pct=0.25,
        stop_loss=90,
        can_sell_date=can_sell_date,
    )


def test_legacy_position_migrates_to_separate_available_and_locked_lots() -> None:
    ledger = ledger_from_position_state(
        _position(available=500, can_sell_date="20260712"),
        as_of_date="20260710",
        acquired_date="20260105",
        source_id="broker-import-1",
        price_manifest_fingerprint=MANIFEST,
    )

    assert ledger.shares == 1000
    assert ledger.available_shares("20260710") == 500
    assert ledger.available_shares("20260712") == 1000
    assert ledger.next_unlock_date("20260710") == "20260712"
    assert ledger.total_cost_basis_cash == Decimal("100000")
    assert all(lot.provenance == LedgerProvenance.ESTIMATED for lot in ledger.lots)

    before = ledger.snapshot(
        as_of_date="20260710",
        raw_mark_price=110,
        portfolio_equity=200_000,
    )
    after = ledger.snapshot(
        as_of_date="20260712",
        raw_mark_price=110,
        portfolio_equity=200_000,
    )
    assert before.lifecycle_state == LifecycleState.LOCKED
    assert before.available_shares == 500
    assert before.can_sell_date == "20260712"
    assert after.lifecycle_state == LifecycleState.HOLDING
    assert after.available_shares == 1000
    assert after.can_sell_date == ""


def test_consecutive_buys_keep_independent_t1_unlock_dates() -> None:
    ledger = PositionLedger(ts_code=TS_CODE)
    ledger = add_buy_lot(
        ledger,
        lot_id="buy-d1",
        execution_date="20260710",
        sellable_from="20260713",
        shares=100,
        cost_basis_cash="1000",
        source_id="fill-d1",
    )
    ledger = add_buy_lot(
        ledger,
        lot_id="buy-d2",
        execution_date="20260713",
        sellable_from="20260714",
        shares=200,
        cost_basis_cash="2400",
        source_id="fill-d2",
    )

    assert ledger.available_shares("20260710") == 0
    assert ledger.available_shares("20260713") == 100
    assert ledger.available_shares("20260714") == 300
    assert ledger.next_unlock_date("20260710") == "20260713"
    assert ledger.next_unlock_date("20260713") == "20260714"


def test_fifo_sell_consumes_only_sellable_lots_and_preserves_cost() -> None:
    ledger = PositionLedger(ts_code=TS_CODE)
    ledger = add_buy_lot(
        ledger,
        lot_id="older",
        execution_date="20260709",
        sellable_from="20260710",
        shares=100,
        cost_basis_cash="1000",
        source_id="fill-1",
    )
    ledger = add_buy_lot(
        ledger,
        lot_id="newer",
        execution_date="20260710",
        sellable_from="20260713",
        shares=100,
        cost_basis_cash="1200",
        source_id="fill-2",
    )

    with pytest.raises(InventoryContractError, match="sellable"):
        consume_sellable_fifo(ledger, as_of_date="20260710", shares=101)

    result = consume_sellable_fifo(ledger, as_of_date="20260710", shares=50)
    assert result.total_cost_basis_cash == Decimal("500")
    assert result.consumptions[0].lot_id == "older"
    assert result.consumptions[0].shares == 50
    assert result.ledger.shares == 150
    assert result.ledger.total_cost_basis_cash == Decimal("1700")
    assert result.ledger.available_shares("20260710") == 50
    assert result.ledger.available_shares("20260713") == 150


def test_price_level_converts_with_execution_day_factor_and_manifest() -> None:
    level = PriceLevel(
        value=Decimal("180"),
        basis=PriceBasis.HFQ_POINT_IN_TIME,
        observed_date="20260709",
        price_manifest_fingerprint=MANIFEST,
    )
    assert level.to_raw(
        _frame(), price_manifest_fingerprint=MANIFEST
    ) == Decimal("90.0")
    with pytest.raises(InventoryContractError, match="fingerprint"):
        level.to_raw(_frame(), price_manifest_fingerprint="b" * 64)


def test_ledger_is_frozen_hashed_and_rejects_ambiguous_legacy_lock() -> None:
    ledger = PositionLedger(ts_code=TS_CODE)
    first = ledger.fingerprint()
    assert first == PositionLedger(ts_code=TS_CODE).fingerprint()
    with pytest.raises(FrozenInstanceError):
        ledger.ts_code = "000001.SZ"

    ambiguous = PositionState(
        ts_code=TS_CODE,
        lifecycle_state=LifecycleState.LOCKED,
        shares=100,
        available_shares=0,
        avg_cost=100,
        current_position_pct=0.1,
        stop_loss=90,
    )
    with pytest.raises(InventoryContractError, match="can_sell_date"):
        ledger_from_position_state(
            ambiguous,
            as_of_date="20260710",
            acquired_date="20260709",
            source_id="ambiguous",
            price_manifest_fingerprint=MANIFEST,
        )
