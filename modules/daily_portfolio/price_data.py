"""Immutable dual-price contracts for point-in-time daily calibration.

Signals and fills intentionally live in different price domains:

* a point-in-time adjusted series is used for indicators and stop structure;
* raw exchange prices are used for fills, fees, price limits, and valuation.

Every adjustment factor must be explicitly supplied, known before that
session's open, content-addressed, and reconciled to the corporate-action
ledger.  No factor is inferred from an end-of-day close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
import hashlib
import json
import math
from typing import Any, Sequence

from ..indicators import DailyData
from .dates import normalize_trade_date


class PriceDataContractError(ValueError):
    """Raised when price provenance or dual-series alignment is unsafe."""


class PriceBasis(str, Enum):
    RAW = "RAW"
    QFQ = "QFQ"
    HFQ_POINT_IN_TIME = "HFQ_POINT_IN_TIME"


class CorporateActionLedgerStatus(str, Enum):
    COMPLETE = "COMPLETE"
    EMPTY_INTERVAL_ATTESTED = "EMPTY_INTERVAL_ATTESTED"
    MISSING = "MISSING"


class CorporateActionType(str, Enum):
    """Only deterministic v1 event families; rights/options are rejected."""

    CASH_DIVIDEND = "CASH_DIVIDEND"
    BONUS_SHARES = "BONUS_SHARES"
    SPLIT_OR_CONSOLIDATION = "SPLIT_OR_CONSOLIDATION"
    CASH_AND_SHARES = "CASH_AND_SHARES"


class FractionalSharePolicy(str, Enum):
    """v1 refuses to guess a cash-in-lieu price."""

    REJECT_NON_INTEGER = "REJECT_NON_INTEGER"


class DividendWithholdingModel(str, Enum):
    """Registered cash-dividend withholding semantics."""

    NONE = "NONE"
    CN_A_SHARE_HOLDING_PERIOD_V1 = "CN_A_SHARE_HOLDING_PERIOD_V1"


_CHINA_TZ = timezone(timedelta(hours=8))
_MARKET_OPEN = time(9, 30)


def _finite(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PriceDataContractError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise PriceDataContractError(f"{field} must be a finite number")
    return result


def _positive(value: Any, *, field: str) -> float:
    result = _finite(value, field=field)
    if result <= 0:
        raise PriceDataContractError(f"{field} must be positive")
    return result


def _nonnegative(value: Any, *, field: str) -> float:
    result = _finite(value, field=field)
    if result < 0:
        raise PriceDataContractError(f"{field} must be non-negative")
    return result


def _nonempty(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PriceDataContractError(f"{field} must be a non-empty string")
    return value.strip()


def _sha256(value: str, *, field: str) -> str:
    result = _nonempty(value, field=field)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise PriceDataContractError(f"{field} must be a lowercase SHA-256")
    return result


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _known_at(value: Any, *, trade_date: str) -> str:
    raw = _nonempty(value, field="adjustment_factor_known_at")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PriceDataContractError(
            "adjustment_factor_known_at must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PriceDataContractError(
            "adjustment_factor_known_at must include a timezone offset"
        )
    day = datetime.strptime(trade_date, "%Y%m%d").date()
    open_time = datetime.combine(day, _MARKET_OPEN, tzinfo=_CHINA_TZ)
    if parsed.astimezone(_CHINA_TZ) > open_time:
        raise PriceDataContractError(
            "adjustment factor must be known no later than the session open"
        )
    return parsed.isoformat()


@dataclass(frozen=True)
class FrozenDailyBar:
    """Canonical immutable base bar; enriched indicator fields are excluded."""

    ts_code: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    vol: float
    amount: float
    pct_chg: float
    prev_close: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_code", _nonempty(self.ts_code, field="ts_code"))
        object.__setattr__(self, "trade_date", normalize_trade_date(self.trade_date))
        for field in ("open", "high", "low", "close", "prev_close"):
            object.__setattr__(self, field, _positive(getattr(self, field), field=field))
        for field in ("vol", "amount"):
            object.__setattr__(
                self, field, _nonnegative(getattr(self, field), field=field)
            )
        object.__setattr__(self, "pct_chg", _finite(self.pct_chg, field="pct_chg"))
        if self.high < max(self.open, self.close):
            raise PriceDataContractError("high must be at least open and close")
        if self.low > min(self.open, self.close):
            raise PriceDataContractError("low must be at most open and close")
        if self.high < self.low:
            raise PriceDataContractError("high cannot be below low")

    @classmethod
    def from_daily_data(cls, bar: DailyData | "FrozenDailyBar") -> "FrozenDailyBar":
        if isinstance(bar, cls):
            return bar
        if not isinstance(bar, DailyData):
            raise PriceDataContractError("price bars must be DailyData values")
        return cls(
            ts_code=bar.ts_code,
            trade_date=bar.trade_date,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            vol=bar.vol,
            amount=bar.amount,
            pct_chg=bar.pct_chg,
            prev_close=bar.prev_close,
        )

    def to_daily_data(self) -> DailyData:
        """Return a fresh mutable adapter for legacy indicator functions."""

        return DailyData(**self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "trade_date": self.trade_date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "vol": self.vol,
            "amount": self.amount,
            "pct_chg": self.pct_chg,
            "prev_close": self.prev_close,
        }


@dataclass(frozen=True)
class CorporateAction:
    """Source-backed action metadata required by the future inventory ledger.

    The action is not applied by this module.  Record, credit, sellable, and
    payment dates are separate so a replay cannot spend dividends at ex-date
    or make bonus shares sellable before listing.  Rights subscriptions and
    optional events are deliberately outside the v1 enum and fail parsing.
    """

    action_id: str
    ts_code: str
    action_type: CorporateActionType
    record_date: str
    ex_date: str
    share_credit_date: str
    share_sellable_date: str
    cash_payment_date: str
    share_multiplier: float
    cash_dividend_gross_per_pre_action_share: float
    withholding_model_version: DividendWithholdingModel
    fractional_share_policy: FractionalSharePolicy
    action_known_at: str
    source_content_sha256: str
    source_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "action_id", _nonempty(self.action_id, field="action_id")
        )
        object.__setattr__(self, "ts_code", _nonempty(self.ts_code, field="ts_code"))
        if not isinstance(self.action_type, CorporateActionType):
            raise PriceDataContractError(
                "action_type must be a supported CorporateActionType"
            )
        for field in (
            "record_date",
            "ex_date",
            "share_credit_date",
            "share_sellable_date",
            "cash_payment_date",
        ):
            object.__setattr__(
                self, field, normalize_trade_date(getattr(self, field))
            )
        if self.record_date > self.ex_date:
            raise PriceDataContractError("record_date cannot follow ex_date")
        if self.share_credit_date < self.ex_date:
            raise PriceDataContractError("share_credit_date cannot precede ex_date")
        if self.share_sellable_date < self.share_credit_date:
            raise PriceDataContractError(
                "share_sellable_date cannot precede share_credit_date"
            )
        if self.cash_payment_date < self.ex_date:
            raise PriceDataContractError("cash_payment_date cannot precede ex_date")
        object.__setattr__(
            self,
            "share_multiplier",
            _positive(self.share_multiplier, field="share_multiplier"),
        )
        object.__setattr__(
            self,
            "cash_dividend_gross_per_pre_action_share",
            _nonnegative(
                self.cash_dividend_gross_per_pre_action_share,
                field="cash_dividend_gross_per_pre_action_share",
            ),
        )
        if not isinstance(self.withholding_model_version, DividendWithholdingModel):
            raise PriceDataContractError(
                "withholding_model_version must be a registered model"
            )
        if self.fractional_share_policy != FractionalSharePolicy.REJECT_NON_INTEGER:
            raise PriceDataContractError(
                "v1 only supports REJECT_NON_INTEGER fractional-share handling"
            )
        object.__setattr__(
            self,
            "action_known_at",
            _known_at(self.action_known_at, trade_date=self.ex_date),
        )
        object.__setattr__(
            self,
            "source_content_sha256",
            _sha256(self.source_content_sha256, field="source_content_sha256"),
        )
        object.__setattr__(
            self, "source_ref", _nonempty(self.source_ref, field="source_ref")
        )

        has_shares = not math.isclose(self.share_multiplier, 1.0, abs_tol=1e-12)
        has_cash = self.cash_dividend_gross_per_pre_action_share > 0
        expected = {
            (False, True): CorporateActionType.CASH_DIVIDEND,
            (True, False): (
                CorporateActionType.BONUS_SHARES
                if self.share_multiplier > 1
                else CorporateActionType.SPLIT_OR_CONSOLIDATION
            ),
            (True, True): CorporateActionType.CASH_AND_SHARES,
        }.get((has_shares, has_cash))
        if expected is None or self.action_type != expected:
            raise PriceDataContractError(
                "action_type is inconsistent with its share and cash effects"
            )
        if has_cash and (
            self.withholding_model_version
            != DividendWithholdingModel.CN_A_SHARE_HOLDING_PERIOD_V1
        ):
            raise PriceDataContractError(
                "cash dividends require the registered A-share withholding model"
            )
        if not has_cash and self.withholding_model_version != DividendWithholdingModel.NONE:
            raise PriceDataContractError(
                "a share-only action must use withholding model NONE"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "ts_code": self.ts_code,
            "action_type": self.action_type.value,
            "record_date": self.record_date,
            "ex_date": self.ex_date,
            "share_credit_date": self.share_credit_date,
            "share_sellable_date": self.share_sellable_date,
            "cash_payment_date": self.cash_payment_date,
            "share_multiplier": self.share_multiplier,
            "cash_dividend_gross_per_pre_action_share": (
                self.cash_dividend_gross_per_pre_action_share
            ),
            "withholding_model_version": self.withholding_model_version.value,
            "fractional_share_policy": self.fractional_share_policy.value,
            "action_known_at": self.action_known_at,
            "source_content_sha256": self.source_content_sha256,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class DailyPriceFrame:
    """One aligned session with an open-known adjustment conversion."""

    signal_bar: FrozenDailyBar | DailyData
    execution_bar: FrozenDailyBar | DailyData
    signal_basis: PriceBasis
    raw_per_signal_unit: float
    adjustment_factor_known_at: str
    adjustment_factor_source_sha256: str

    def __post_init__(self) -> None:
        signal = FrozenDailyBar.from_daily_data(self.signal_bar)
        execution = FrozenDailyBar.from_daily_data(self.execution_bar)
        object.__setattr__(self, "signal_bar", signal)
        object.__setattr__(self, "execution_bar", execution)
        if not isinstance(self.signal_basis, PriceBasis):
            raise PriceDataContractError("signal_basis must be a PriceBasis")
        if self.signal_basis == PriceBasis.RAW:
            raise PriceDataContractError(
                "signal_basis must be adjusted; execution_bar already owns RAW prices"
            )
        if signal.trade_date != execution.trade_date:
            raise PriceDataContractError(
                "signal and execution bars must have the same trade_date"
            )
        if signal.ts_code != execution.ts_code:
            raise PriceDataContractError(
                "signal and execution bars must have the same non-empty ts_code"
            )
        ratio = _positive(self.raw_per_signal_unit, field="raw_per_signal_unit")
        object.__setattr__(self, "raw_per_signal_unit", ratio)
        object.__setattr__(
            self,
            "adjustment_factor_known_at",
            _known_at(self.adjustment_factor_known_at, trade_date=signal.trade_date),
        )
        object.__setattr__(
            self,
            "adjustment_factor_source_sha256",
            _sha256(
                self.adjustment_factor_source_sha256,
                field="adjustment_factor_source_sha256",
            ),
        )

        for field in ("open", "high", "low", "close"):
            raw_price = getattr(execution, field)
            signal_price = getattr(signal, field)
            reconstructed = signal_price * ratio
            tolerance = max(0.02, abs(raw_price) * 0.001)
            if abs(reconstructed - raw_price) > tolerance:
                raise PriceDataContractError(
                    f"{field} raw/signal ratio is inconsistent within the session"
                )
        if not math.isclose(signal.vol, execution.vol, rel_tol=1e-9, abs_tol=1e-9):
            raise PriceDataContractError(
                "signal and execution bars must use the same volume units"
            )
        if not math.isclose(
            signal.amount, execution.amount, rel_tol=1e-9, abs_tol=0.01
        ):
            raise PriceDataContractError(
                "signal and execution bars must use the same amount units"
            )
        expected_pct = (signal.close / signal.prev_close - 1.0) * 100.0
        if not math.isclose(signal.pct_chg, expected_pct, abs_tol=0.1):
            raise PriceDataContractError(
                "signal pct_chg must be consistent with adjusted close/prev_close"
            )
        expected_execution_pct = (
            execution.close / execution.prev_close - 1.0
        ) * 100.0
        if not math.isclose(
            execution.pct_chg, expected_execution_pct, abs_tol=0.1
        ):
            raise PriceDataContractError(
                "execution pct_chg must be consistent with raw close/prev_close"
            )

    @property
    def ts_code(self) -> str:
        return self.signal_bar.ts_code

    @property
    def trade_date(self) -> str:
        return self.signal_bar.trade_date

    def signal_price_to_raw(self, price: float) -> float:
        return _positive(price, field="signal price") * self.raw_per_signal_unit

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "signal_basis": self.signal_basis.value,
            "raw_per_signal_unit": self.raw_per_signal_unit,
            "adjustment_factor_known_at": self.adjustment_factor_known_at,
            "adjustment_factor_source_sha256": (
                self.adjustment_factor_source_sha256
            ),
            "signal_bar": self.signal_bar.as_dict(),
            "execution_bar": self.execution_bar.as_dict(),
        }


@dataclass(frozen=True)
class PriceSeriesManifest:
    schema_version: str
    ts_code: str
    start_date: str
    end_date: str
    signal_basis: PriceBasis
    execution_basis: PriceBasis
    point_in_time_safe: bool
    corporate_action_ledger_status: CorporateActionLedgerStatus
    signal_source: str
    execution_source: str
    adjustment_source: str
    corporate_action_source: str
    signal_content_sha256: str
    execution_content_sha256: str
    adjustment_content_sha256: str
    corporate_actions_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != "dual-price-manifest-v1":
            raise PriceDataContractError(
                "schema_version must be dual-price-manifest-v1"
            )
        object.__setattr__(self, "ts_code", _nonempty(self.ts_code, field="ts_code"))
        start = normalize_trade_date(self.start_date)
        end = normalize_trade_date(self.end_date)
        if start > end:
            raise PriceDataContractError("manifest start_date cannot exceed end_date")
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)
        if not isinstance(self.signal_basis, PriceBasis):
            raise PriceDataContractError("signal_basis must be a PriceBasis")
        if self.signal_basis == PriceBasis.RAW:
            raise PriceDataContractError("manifest signal_basis cannot be RAW")
        if self.execution_basis != PriceBasis.RAW:
            raise PriceDataContractError("execution_basis must be RAW")
        if not isinstance(self.point_in_time_safe, bool):
            raise PriceDataContractError("point_in_time_safe must be boolean")
        if not isinstance(
            self.corporate_action_ledger_status, CorporateActionLedgerStatus
        ):
            raise PriceDataContractError(
                "corporate_action_ledger_status must be a ledger status"
            )
        for field in (
            "signal_source",
            "execution_source",
            "adjustment_source",
            "corporate_action_source",
        ):
            object.__setattr__(self, field, _nonempty(getattr(self, field), field=field))
        for field in (
            "signal_content_sha256",
            "execution_content_sha256",
            "adjustment_content_sha256",
            "corporate_actions_sha256",
        ):
            object.__setattr__(self, field, _sha256(getattr(self, field), field=field))

    def assert_calibration_ready(self) -> None:
        reasons: list[str] = []
        if self.signal_basis != PriceBasis.HFQ_POINT_IN_TIME:
            reasons.append("signal basis is not point-in-time HFQ")
        if not self.point_in_time_safe:
            reasons.append("source is not attested point-in-time safe")
        if self.corporate_action_ledger_status not in (
            CorporateActionLedgerStatus.COMPLETE,
            CorporateActionLedgerStatus.EMPTY_INTERVAL_ATTESTED,
        ):
            reasons.append("corporate-action ledger is missing")
        if reasons:
            raise PriceDataContractError(
                "price dataset is not calibration-ready: " + "; ".join(reasons)
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ts_code": self.ts_code,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "signal_basis": self.signal_basis.value,
            "execution_basis": self.execution_basis.value,
            "point_in_time_safe": self.point_in_time_safe,
            "corporate_action_ledger_status": (
                self.corporate_action_ledger_status.value
            ),
            "signal_source": self.signal_source,
            "execution_source": self.execution_source,
            "adjustment_source": self.adjustment_source,
            "corporate_action_source": self.corporate_action_source,
            "signal_content_sha256": self.signal_content_sha256,
            "execution_content_sha256": self.execution_content_sha256,
            "adjustment_content_sha256": self.adjustment_content_sha256,
            "corporate_actions_sha256": self.corporate_actions_sha256,
        }

    def fingerprint(self) -> str:
        return _sha256_json(self.as_dict())


@dataclass(frozen=True)
class DualPriceSeries:
    manifest: PriceSeriesManifest
    frames: tuple[DailyPriceFrame, ...]
    corporate_actions: tuple[CorporateAction, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, PriceSeriesManifest):
            raise PriceDataContractError("manifest must be a PriceSeriesManifest")
        frames = tuple(self.frames)
        if not frames or any(not isinstance(frame, DailyPriceFrame) for frame in frames):
            raise PriceDataContractError("frames must contain DailyPriceFrame values")
        dates = tuple(frame.trade_date for frame in frames)
        if len(set(dates)) != len(dates) or dates != tuple(sorted(dates)):
            raise PriceDataContractError(
                "dual-price frames must be unique and strictly ascending"
            )
        if any(frame.ts_code != self.manifest.ts_code for frame in frames):
            raise PriceDataContractError("frame stock code differs from manifest")
        if any(frame.signal_basis != self.manifest.signal_basis for frame in frames):
            raise PriceDataContractError("frame signal basis differs from manifest")
        if dates[0] != self.manifest.start_date or dates[-1] != self.manifest.end_date:
            raise PriceDataContractError(
                "manifest interval must equal the first and last frame dates"
            )

        actions = tuple(self.corporate_actions)
        if any(not isinstance(action, CorporateAction) for action in actions):
            raise PriceDataContractError(
                "corporate_actions must contain CorporateAction values"
            )
        action_ids = tuple(action.action_id for action in actions)
        if len(set(action_ids)) != len(action_ids):
            raise PriceDataContractError("corporate action_id values must be unique")
        if actions != tuple(sorted(actions, key=lambda item: (item.ex_date, item.action_id))):
            raise PriceDataContractError(
                "corporate actions must be ordered by ex_date and action_id"
            )
        if len({action.ex_date for action in actions}) != len(actions):
            raise PriceDataContractError(
                "v1 requires one normalized corporate action per ex_date"
            )
        if any(action.ts_code != self.manifest.ts_code for action in actions):
            raise PriceDataContractError(
                "corporate-action stock code differs from manifest"
            )
        if any(
            not self.manifest.start_date <= action.ex_date <= self.manifest.end_date
            for action in actions
        ):
            raise PriceDataContractError(
                "corporate action ex_date falls outside the manifest interval"
            )
        status = self.manifest.corporate_action_ledger_status
        if status == CorporateActionLedgerStatus.EMPTY_INTERVAL_ATTESTED and actions:
            raise PriceDataContractError(
                "an EMPTY_INTERVAL_ATTESTED ledger cannot contain actions"
            )
        if status == CorporateActionLedgerStatus.COMPLETE and not actions:
            raise PriceDataContractError(
                "a COMPLETE ledger must contain actions; use empty attestation"
            )

        change_dates = {
            frames[index].trade_date
            for index in range(1, len(frames))
            if not math.isclose(
                frames[index].raw_per_signal_unit,
                frames[index - 1].raw_per_signal_unit,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        }
        action_dates = {action.ex_date for action in actions}
        if change_dates != action_dates:
            raise PriceDataContractError(
                "adjustment-factor change dates must exactly match corporate-action ex-dates"
            )
        if actions and actions[0].ex_date == frames[0].trade_date:
            raise PriceDataContractError(
                "a first-frame corporate action lacks a prior raw reference price"
            )
        actions_by_date = {action.ex_date: action for action in actions}
        for index in range(1, len(frames)):
            previous = frames[index - 1]
            current = frames[index]
            action = actions_by_date.get(current.trade_date)
            expected_reference = previous.execution_bar.close
            if action is not None:
                expected_reference = (
                    previous.execution_bar.close
                    - action.cash_dividend_gross_per_pre_action_share
                ) / action.share_multiplier
                if expected_reference <= 0:
                    raise PriceDataContractError(
                        "corporate action produces a non-positive reference price"
                    )
            price_tolerance = max(0.02, abs(expected_reference) * 0.002)
            if abs(current.execution_bar.prev_close - expected_reference) > price_tolerance:
                raise PriceDataContractError(
                    "raw prev_close is inconsistent with the corporate action"
                )
            expected_ratio_change = expected_reference / previous.execution_bar.close
            observed_ratio_change = (
                current.raw_per_signal_unit / previous.raw_per_signal_unit
            )
            if not math.isclose(
                observed_ratio_change,
                expected_ratio_change,
                rel_tol=0.002,
                abs_tol=1e-9,
            ):
                raise PriceDataContractError(
                    "adjustment-factor magnitude is inconsistent with the corporate action"
                )
            signal_tolerance = max(
                0.02, abs(previous.signal_bar.close) * 0.002
            )
            if (
                abs(current.signal_bar.prev_close - previous.signal_bar.close)
                > signal_tolerance
            ):
                raise PriceDataContractError(
                    "adjusted prev_close is not continuous across sessions"
                )
        object.__setattr__(self, "frames", frames)
        object.__setattr__(self, "corporate_actions", actions)
        self._verify_hashes()

    def _verify_hashes(self) -> None:
        signal_hash = _sha256_json(
            [frame.signal_bar.as_dict() for frame in self.frames]
        )
        execution_hash = _sha256_json(
            [frame.execution_bar.as_dict() for frame in self.frames]
        )
        adjustment_hash = _sha256_json(
            [
                {
                    "trade_date": frame.trade_date,
                    "signal_basis": frame.signal_basis.value,
                    "raw_per_signal_unit": frame.raw_per_signal_unit,
                    "known_at": frame.adjustment_factor_known_at,
                    "source_sha256": frame.adjustment_factor_source_sha256,
                }
                for frame in self.frames
            ]
        )
        action_hash = _sha256_json(
            [action.as_dict() for action in self.corporate_actions]
        )
        expected = {
            "signal_content_sha256": signal_hash,
            "execution_content_sha256": execution_hash,
            "adjustment_content_sha256": adjustment_hash,
            "corporate_actions_sha256": action_hash,
        }
        for field, actual in expected.items():
            if getattr(self.manifest, field) != actual:
                raise PriceDataContractError(f"{field} does not match manifest")

    @property
    def signal_bars(self) -> tuple[DailyData, ...]:
        return tuple(frame.signal_bar.to_daily_data() for frame in self.frames)

    @property
    def execution_bars(self) -> tuple[DailyData, ...]:
        return tuple(frame.execution_bar.to_daily_data() for frame in self.frames)

    def assert_calibration_ready(self) -> None:
        self.manifest.assert_calibration_ready()
        self._verify_hashes()


def build_dual_price_series(
    frames: Sequence[DailyPriceFrame],
    *,
    corporate_actions: Sequence[CorporateAction] = (),
    point_in_time_safe: bool,
    ledger_status: CorporateActionLedgerStatus,
    signal_source: str,
    execution_source: str,
    adjustment_source: str,
    corporate_action_source: str,
) -> DualPriceSeries:
    resolved_frames = tuple(frames)
    if not resolved_frames:
        raise PriceDataContractError("frames cannot be empty")
    resolved_actions = tuple(corporate_actions)
    signal_hash = _sha256_json(
        [frame.signal_bar.as_dict() for frame in resolved_frames]
    )
    execution_hash = _sha256_json(
        [frame.execution_bar.as_dict() for frame in resolved_frames]
    )
    adjustment_hash = _sha256_json(
        [
            {
                "trade_date": frame.trade_date,
                "signal_basis": frame.signal_basis.value,
                "raw_per_signal_unit": frame.raw_per_signal_unit,
                "known_at": frame.adjustment_factor_known_at,
                "source_sha256": frame.adjustment_factor_source_sha256,
            }
            for frame in resolved_frames
        ]
    )
    action_hash = _sha256_json([action.as_dict() for action in resolved_actions])
    manifest = PriceSeriesManifest(
        schema_version="dual-price-manifest-v1",
        ts_code=resolved_frames[0].ts_code,
        start_date=resolved_frames[0].trade_date,
        end_date=resolved_frames[-1].trade_date,
        signal_basis=resolved_frames[0].signal_basis,
        execution_basis=PriceBasis.RAW,
        point_in_time_safe=point_in_time_safe,
        corporate_action_ledger_status=ledger_status,
        signal_source=signal_source,
        execution_source=execution_source,
        adjustment_source=adjustment_source,
        corporate_action_source=corporate_action_source,
        signal_content_sha256=signal_hash,
        execution_content_sha256=execution_hash,
        adjustment_content_sha256=adjustment_hash,
        corporate_actions_sha256=action_hash,
    )
    return DualPriceSeries(
        manifest=manifest,
        frames=resolved_frames,
        corporate_actions=resolved_actions,
    )


__all__ = [
    "CorporateAction",
    "CorporateActionLedgerStatus",
    "CorporateActionType",
    "DailyPriceFrame",
    "DividendWithholdingModel",
    "DualPriceSeries",
    "FractionalSharePolicy",
    "FrozenDailyBar",
    "PriceBasis",
    "PriceDataContractError",
    "PriceSeriesManifest",
    "build_dual_price_series",
]
