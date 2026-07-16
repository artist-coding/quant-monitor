"""Immutable inventory lots for T+1, cost basis, and corporate entitlements.

``PositionState`` remains the compact scoring/UI projection.  This ledger is
the execution truth: every acquisition has its own sellable date and remaining
cost basis, so consecutive buys and future corporate-action credits cannot
accidentally unlock one another.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import math
from typing import Any, Sequence

from .dates import normalize_trade_date
from .models import LifecycleState, PositionState
from .price_data import DailyPriceFrame, PriceBasis, PriceDataContractError


class InventoryContractError(ValueError):
    """Raised when inventory state or a lot transition is inconsistent."""


class LotSourceKind(str, Enum):
    BUY_FILL = "BUY_FILL"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    OPENING_BALANCE = "OPENING_BALANCE"


class LedgerProvenance(str, Enum):
    EXACT = "EXACT"
    ESTIMATED = "ESTIMATED"


class EntitlementStatus(str, Enum):
    PENDING = "PENDING"
    SETTLED = "SETTLED"
    CANCELLED = "CANCELLED"


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InventoryContractError(f"{field} must be a non-empty string")
    return value.strip()


def _shares(value: Any, *, field: str, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InventoryContractError(f"{field} must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise InventoryContractError(f"{field} must be {qualifier}")
    return value


def _money(value: Any, *, field: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise InventoryContractError(f"{field} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InventoryContractError(f"{field} must be a finite decimal") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        qualifier = "positive" if positive else "non-negative"
        raise InventoryContractError(f"{field} must be finite and {qualifier}")
    return result


def _canonical_hash(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PriceLevel:
    """A stop/price level with an explicit domain and dataset identity."""

    value: Decimal
    basis: PriceBasis
    observed_date: str
    price_manifest_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _money(self.value, field="value", positive=True))
        if not isinstance(self.basis, PriceBasis):
            raise InventoryContractError("basis must be a PriceBasis")
        object.__setattr__(
            self, "observed_date", normalize_trade_date(self.observed_date)
        )
        fingerprint = _text(
            self.price_manifest_fingerprint, field="price_manifest_fingerprint"
        )
        if len(fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in fingerprint
        ):
            raise InventoryContractError(
                "price_manifest_fingerprint must be a lowercase SHA-256"
            )
        object.__setattr__(self, "price_manifest_fingerprint", fingerprint)

    def to_raw(
        self,
        frame: DailyPriceFrame,
        *,
        price_manifest_fingerprint: str,
    ) -> Decimal:
        if price_manifest_fingerprint != self.price_manifest_fingerprint:
            raise InventoryContractError("price manifest fingerprint mismatch")
        if frame.trade_date < self.observed_date:
            raise InventoryContractError("cannot convert a price level using a past frame")
        if self.basis == PriceBasis.RAW:
            return self.value
        if self.basis != frame.signal_basis:
            raise InventoryContractError("price level basis differs from signal frame")
        try:
            converted = frame.signal_price_to_raw(float(self.value))
        except PriceDataContractError as exc:
            raise InventoryContractError(str(exc)) from exc
        return Decimal(str(converted))

    def as_dict(self) -> dict[str, str]:
        return {
            "value": str(self.value),
            "basis": self.basis.value,
            "observed_date": self.observed_date,
            "price_manifest_fingerprint": self.price_manifest_fingerprint,
        }


@dataclass(frozen=True)
class InventoryLot:
    lot_id: str
    ts_code: str
    acquired_date: str
    holding_period_start_date: str
    shares: int
    sellable_from: str
    remaining_cost_basis_cash: Decimal
    source_kind: LotSourceKind
    source_id: str
    provenance: LedgerProvenance
    parent_lot_id: str = ""

    def __post_init__(self) -> None:
        for field in ("lot_id", "ts_code", "source_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field=field))
        acquired = normalize_trade_date(self.acquired_date)
        holding_start = normalize_trade_date(self.holding_period_start_date)
        sellable = normalize_trade_date(self.sellable_from)
        if holding_start > acquired:
            raise InventoryContractError(
                "holding_period_start_date cannot follow acquired_date"
            )
        if sellable < acquired:
            raise InventoryContractError("sellable_from cannot precede acquired_date")
        object.__setattr__(self, "acquired_date", acquired)
        object.__setattr__(self, "holding_period_start_date", holding_start)
        object.__setattr__(self, "sellable_from", sellable)
        object.__setattr__(self, "shares", _shares(self.shares, field="shares"))
        object.__setattr__(
            self,
            "remaining_cost_basis_cash",
            _money(
                self.remaining_cost_basis_cash,
                field="remaining_cost_basis_cash",
                positive=True,
            ),
        )
        if not isinstance(self.source_kind, LotSourceKind):
            raise InventoryContractError("source_kind must be a LotSourceKind")
        if not isinstance(self.provenance, LedgerProvenance):
            raise InventoryContractError("provenance must be LedgerProvenance")
        if self.parent_lot_id:
            object.__setattr__(
                self,
                "parent_lot_id",
                _text(self.parent_lot_id, field="parent_lot_id"),
            )
            if self.parent_lot_id == self.lot_id:
                raise InventoryContractError("a lot cannot be its own parent")

    def is_sellable(self, as_of_date: str) -> bool:
        return self.sellable_from <= normalize_trade_date(as_of_date)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "ts_code": self.ts_code,
            "acquired_date": self.acquired_date,
            "holding_period_start_date": self.holding_period_start_date,
            "shares": self.shares,
            "sellable_from": self.sellable_from,
            "remaining_cost_basis_cash": str(self.remaining_cost_basis_cash),
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "provenance": self.provenance.value,
            "parent_lot_id": self.parent_lot_id,
        }


@dataclass(frozen=True)
class CashReceivable:
    entitlement_id: str
    action_id: str
    recognized_date: str
    payment_date: str
    gross_amount: Decimal
    estimated_net_amount: Decimal
    withholding_model_version: str
    status: EntitlementStatus = EntitlementStatus.PENDING

    def __post_init__(self) -> None:
        for field in (
            "entitlement_id",
            "action_id",
            "withholding_model_version",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field=field))
        recognized = normalize_trade_date(self.recognized_date)
        payment = normalize_trade_date(self.payment_date)
        if payment < recognized:
            raise InventoryContractError("payment_date cannot precede recognized_date")
        object.__setattr__(self, "recognized_date", recognized)
        object.__setattr__(self, "payment_date", payment)
        gross = _money(self.gross_amount, field="gross_amount", positive=True)
        net = _money(
            self.estimated_net_amount,
            field="estimated_net_amount",
            positive=True,
        )
        if net > gross:
            raise InventoryContractError("estimated net cash cannot exceed gross cash")
        object.__setattr__(self, "gross_amount", gross)
        object.__setattr__(self, "estimated_net_amount", net)
        if not isinstance(self.status, EntitlementStatus):
            raise InventoryContractError("status must be an EntitlementStatus")

    def as_dict(self) -> dict[str, Any]:
        return {
            "entitlement_id": self.entitlement_id,
            "action_id": self.action_id,
            "recognized_date": self.recognized_date,
            "payment_date": self.payment_date,
            "gross_amount": str(self.gross_amount),
            "estimated_net_amount": str(self.estimated_net_amount),
            "withholding_model_version": self.withholding_model_version,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class ShareEntitlement:
    entitlement_id: str
    action_id: str
    parent_lot_id: str
    units: Decimal
    credit_date: str
    sellable_from: str
    status: EntitlementStatus = EntitlementStatus.PENDING

    def __post_init__(self) -> None:
        for field in ("entitlement_id", "action_id", "parent_lot_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field=field))
        units = _money(self.units, field="units", positive=True)
        object.__setattr__(self, "units", units)
        credit = normalize_trade_date(self.credit_date)
        sellable = normalize_trade_date(self.sellable_from)
        if sellable < credit:
            raise InventoryContractError("sellable_from cannot precede credit_date")
        object.__setattr__(self, "credit_date", credit)
        object.__setattr__(self, "sellable_from", sellable)
        if not isinstance(self.status, EntitlementStatus):
            raise InventoryContractError("status must be an EntitlementStatus")

    def as_dict(self) -> dict[str, Any]:
        return {
            "entitlement_id": self.entitlement_id,
            "action_id": self.action_id,
            "parent_lot_id": self.parent_lot_id,
            "units": str(self.units),
            "credit_date": self.credit_date,
            "sellable_from": self.sellable_from,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class PositionLedger:
    ts_code: str
    lots: tuple[InventoryLot, ...] = ()
    cash_receivables: tuple[CashReceivable, ...] = ()
    share_entitlements: tuple[ShareEntitlement, ...] = ()
    applied_action_ids: tuple[str, ...] = ()
    global_stop: PriceLevel | None = None
    lot_selection_policy: str = "FIFO-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_code", _text(self.ts_code, field="ts_code"))
        lots = tuple(self.lots)
        receivables = tuple(self.cash_receivables)
        entitlements = tuple(self.share_entitlements)
        if any(not isinstance(lot, InventoryLot) for lot in lots):
            raise InventoryContractError("lots must contain InventoryLot values")
        if any(lot.ts_code != self.ts_code for lot in lots):
            raise InventoryContractError("lot stock code differs from ledger")
        if len({lot.lot_id for lot in lots}) != len(lots):
            raise InventoryContractError("lot_id values must be unique")
        ordered_lots = tuple(
            sorted(lots, key=lambda lot: (lot.acquired_date, lot.lot_id))
        )
        if lots != ordered_lots:
            raise InventoryContractError("lots must be ordered by acquisition and id")
        if any(not isinstance(item, CashReceivable) for item in receivables):
            raise InventoryContractError(
                "cash_receivables must contain CashReceivable values"
            )
        if any(not isinstance(item, ShareEntitlement) for item in entitlements):
            raise InventoryContractError(
                "share_entitlements must contain ShareEntitlement values"
            )
        entitlement_ids = [item.entitlement_id for item in (*receivables, *entitlements)]
        if len(set(entitlement_ids)) != len(entitlement_ids):
            raise InventoryContractError("entitlement_id values must be unique")
        actions = tuple(self.applied_action_ids)
        if any(not isinstance(item, str) or not item for item in actions):
            raise InventoryContractError("applied_action_ids must be non-empty strings")
        if len(set(actions)) != len(actions) or actions != tuple(sorted(actions)):
            raise InventoryContractError(
                "applied_action_ids must be unique and sorted"
            )
        if self.global_stop is not None and not isinstance(self.global_stop, PriceLevel):
            raise InventoryContractError("global_stop must be a PriceLevel or None")
        object.__setattr__(
            self,
            "lot_selection_policy",
            _text(self.lot_selection_policy, field="lot_selection_policy"),
        )
        if self.lot_selection_policy != "FIFO-v1":
            raise InventoryContractError("v1 only supports FIFO-v1 lot selection")
        object.__setattr__(self, "lots", lots)
        object.__setattr__(self, "cash_receivables", receivables)
        object.__setattr__(self, "share_entitlements", entitlements)
        object.__setattr__(self, "applied_action_ids", actions)

    @property
    def shares(self) -> int:
        return sum(lot.shares for lot in self.lots)

    @property
    def total_cost_basis_cash(self) -> Decimal:
        return sum(
            (lot.remaining_cost_basis_cash for lot in self.lots), Decimal("0")
        )

    @property
    def avg_cost(self) -> Decimal:
        return (
            self.total_cost_basis_cash / self.shares
            if self.shares
            else Decimal("0")
        )

    def available_shares(self, as_of_date: str) -> int:
        date = normalize_trade_date(as_of_date)
        return sum(lot.shares for lot in self.lots if lot.sellable_from <= date)

    def next_unlock_date(self, as_of_date: str) -> str:
        date = normalize_trade_date(as_of_date)
        future = sorted(
            {lot.sellable_from for lot in self.lots if lot.sellable_from > date}
        )
        return future[0] if future else ""

    def snapshot(
        self,
        *,
        as_of_date: str,
        raw_mark_price: float,
        portfolio_equity: float,
        raw_stop_price: float | None = None,
    ) -> PositionState:
        date = normalize_trade_date(as_of_date)
        if not math.isfinite(raw_mark_price) or raw_mark_price <= 0:
            raise InventoryContractError("raw_mark_price must be finite and positive")
        if not math.isfinite(portfolio_equity) or portfolio_equity <= 0:
            raise InventoryContractError("portfolio_equity must be finite and positive")
        shares = self.shares
        if not shares:
            return PositionState(ts_code=self.ts_code)
        available = self.available_shares(date)
        lifecycle = (
            LifecycleState.HOLDING
            if available == shares
            else LifecycleState.LOCKED
        )
        stop = raw_stop_price
        if stop is None and self.global_stop is not None:
            if self.global_stop.basis != PriceBasis.RAW:
                raise InventoryContractError(
                    "raw_stop_price is required for an adjusted global stop"
                )
            stop = float(self.global_stop.value)
        return PositionState(
            ts_code=self.ts_code,
            lifecycle_state=lifecycle,
            shares=shares,
            available_shares=available,
            avg_cost=float(self.avg_cost),
            current_position_pct=min(
                1.0, shares * raw_mark_price / portfolio_equity
            ),
            stop_loss=stop,
            can_sell_date=self.next_unlock_date(date),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "position-ledger-v1",
            "ts_code": self.ts_code,
            "lots": [lot.as_dict() for lot in self.lots],
            "cash_receivables": [item.as_dict() for item in self.cash_receivables],
            "share_entitlements": [
                item.as_dict() for item in self.share_entitlements
            ],
            "applied_action_ids": list(self.applied_action_ids),
            "global_stop": self.global_stop.as_dict() if self.global_stop else None,
            "lot_selection_policy": self.lot_selection_policy,
        }

    def fingerprint(self) -> str:
        return _canonical_hash(self.as_dict())


@dataclass(frozen=True)
class LotConsumption:
    lot_id: str
    shares: int
    cost_basis_cash: Decimal


@dataclass(frozen=True)
class SellLotResult:
    ledger: PositionLedger
    consumptions: tuple[LotConsumption, ...]
    total_cost_basis_cash: Decimal


def add_buy_lot(
    ledger: PositionLedger,
    *,
    lot_id: str,
    execution_date: str,
    sellable_from: str,
    shares: int,
    cost_basis_cash: Decimal | float | str,
    source_id: str,
    provenance: LedgerProvenance = LedgerProvenance.EXACT,
) -> PositionLedger:
    if not isinstance(ledger, PositionLedger):
        raise InventoryContractError("ledger must be a PositionLedger")
    lot = InventoryLot(
        lot_id=lot_id,
        ts_code=ledger.ts_code,
        acquired_date=execution_date,
        holding_period_start_date=execution_date,
        shares=shares,
        sellable_from=sellable_from,
        remaining_cost_basis_cash=_money(
            cost_basis_cash, field="cost_basis_cash", positive=True
        ),
        source_kind=LotSourceKind.BUY_FILL,
        source_id=source_id,
        provenance=provenance,
    )
    if any(existing.lot_id == lot.lot_id for existing in ledger.lots):
        raise InventoryContractError("buy lot_id already exists")
    lots = tuple(sorted((*ledger.lots, lot), key=lambda item: (item.acquired_date, item.lot_id)))
    return replace(ledger, lots=lots)


def consume_sellable_fifo(
    ledger: PositionLedger,
    *,
    as_of_date: str,
    shares: int,
) -> SellLotResult:
    requested = _shares(shares, field="shares")
    date = normalize_trade_date(as_of_date)
    if requested > ledger.available_shares(date):
        raise InventoryContractError("sell shares exceed FIFO sellable inventory")

    remaining = requested
    updated: list[InventoryLot] = []
    consumptions: list[LotConsumption] = []
    for lot in ledger.lots:
        if remaining == 0 or not lot.is_sellable(date):
            updated.append(lot)
            continue
        taken = min(remaining, lot.shares)
        if taken == lot.shares:
            consumed_cost = lot.remaining_cost_basis_cash
        else:
            consumed_cost = (
                lot.remaining_cost_basis_cash * Decimal(taken) / Decimal(lot.shares)
            )
        consumptions.append(
            LotConsumption(
                lot_id=lot.lot_id,
                shares=taken,
                cost_basis_cash=consumed_cost,
            )
        )
        remaining -= taken
        left = lot.shares - taken
        if left:
            updated.append(
                replace(
                    lot,
                    shares=left,
                    remaining_cost_basis_cash=(
                        lot.remaining_cost_basis_cash - consumed_cost
                    ),
                )
            )
    if remaining:
        raise InventoryContractError("internal FIFO consumption mismatch")
    new_ledger = replace(
        ledger,
        lots=tuple(updated),
        global_stop=ledger.global_stop if updated else None,
    )
    total = sum(
        (item.cost_basis_cash for item in consumptions), Decimal("0")
    )
    return SellLotResult(
        ledger=new_ledger,
        consumptions=tuple(consumptions),
        total_cost_basis_cash=total,
    )


def ledger_from_position_state(
    position: PositionState,
    *,
    as_of_date: str,
    acquired_date: str,
    source_id: str,
    price_manifest_fingerprint: str,
) -> PositionLedger:
    """Migrate an aggregate v1 position into explicitly estimated lots."""

    if not isinstance(position, PositionState):
        raise InventoryContractError("position must be a PositionState")
    date = normalize_trade_date(as_of_date)
    acquired = normalize_trade_date(acquired_date)
    if acquired > date:
        raise InventoryContractError("acquired_date cannot follow as_of_date")
    if position.shares == 0:
        return PositionLedger(ts_code=position.ts_code)
    locked = position.shares - position.available_shares
    if locked and not position.can_sell_date:
        raise InventoryContractError(
            "locked aggregate shares require an explicit can_sell_date"
        )
    if not locked and position.can_sell_date:
        raise InventoryContractError(
            "fully available aggregate shares cannot carry can_sell_date"
        )
    total_cost = Decimal(str(position.avg_cost)) * Decimal(position.shares)
    lots: list[InventoryLot] = []
    for suffix, shares, sellable in (
        ("available", position.available_shares, date),
        ("locked", locked, position.can_sell_date),
    ):
        if not shares:
            continue
        cost = total_cost * Decimal(shares) / Decimal(position.shares)
        lots.append(
            InventoryLot(
                lot_id=f"legacy:{source_id}:{suffix}",
                ts_code=position.ts_code,
                acquired_date=acquired,
                holding_period_start_date=acquired,
                shares=shares,
                sellable_from=sellable,
                remaining_cost_basis_cash=cost,
                source_kind=LotSourceKind.OPENING_BALANCE,
                source_id=source_id,
                provenance=LedgerProvenance.ESTIMATED,
            )
        )
    lots.sort(key=lambda item: (item.acquired_date, item.lot_id))
    stop = (
        PriceLevel(
            value=Decimal(str(position.stop_loss)),
            basis=PriceBasis.RAW,
            observed_date=date,
            price_manifest_fingerprint=price_manifest_fingerprint,
        )
        if position.stop_loss is not None
        else None
    )
    return PositionLedger(ts_code=position.ts_code, lots=tuple(lots), global_stop=stop)


__all__ = [
    "CashReceivable",
    "EntitlementStatus",
    "InventoryContractError",
    "InventoryLot",
    "LedgerProvenance",
    "LotConsumption",
    "LotSourceKind",
    "PositionLedger",
    "PriceLevel",
    "SellLotResult",
    "ShareEntitlement",
    "add_buy_lot",
    "consume_sellable_fifo",
    "ledger_from_position_state",
]
