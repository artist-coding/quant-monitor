"""Dual-price and corporate-action provenance contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib

import pytest

from modules.daily_portfolio import (
    CorporateAction,
    CorporateActionLedgerStatus,
    CorporateActionType,
    DailyPriceFrame,
    DividendWithholdingModel,
    DualPriceSeries,
    FractionalSharePolicy,
    PriceBasis,
    PriceDataContractError,
    ScoreWeights,
    build_dual_price_series,
)
from modules.indicators import DailyData


TS_CODE = "600519.SH"


def _bar(
    trade_date: str,
    *,
    scale: float = 1.0,
    open_price: float = 100.0,
    close: float = 101.0,
    prev_close: float = 100.0,
) -> DailyData:
    return DailyData(
        ts_code=TS_CODE,
        trade_date=trade_date,
        open=open_price * scale,
        high=102.0 * scale,
        low=99.0 * scale,
        close=close * scale,
        vol=1_000_000,
        amount=100_000_000,
        pct_chg=(close / prev_close - 1) * 100,
        prev_close=prev_close * scale,
    )


def _frame(
    trade_date: str,
    *,
    basis: PriceBasis = PriceBasis.HFQ_POINT_IN_TIME,
    signal_scale: float = 2.0,
    ratio: float | None = None,
    raw_prev_close: float = 100.0,
) -> DailyPriceFrame:
    day = datetime.strptime(trade_date, "%Y%m%d")
    known_at = (day - timedelta(days=1)).strftime("%Y-%m-%dT18:00:00+08:00")
    resolved_ratio = 1 / signal_scale if ratio is None else ratio
    return DailyPriceFrame(
        signal_bar=_bar(
            trade_date, scale=signal_scale, prev_close=raw_prev_close
        ),
        execution_bar=_bar(trade_date, prev_close=raw_prev_close),
        signal_basis=basis,
        raw_per_signal_unit=resolved_ratio,
        adjustment_factor_known_at=known_at,
        adjustment_factor_source_sha256=hashlib.sha256(
            f"{trade_date}:{resolved_ratio}".encode()
        ).hexdigest(),
    )


def _series(
    *,
    basis: PriceBasis = PriceBasis.HFQ_POINT_IN_TIME,
    point_in_time_safe: bool = True,
):
    return build_dual_price_series(
        (
            _frame("20260709", basis=basis),
            _frame("20260710", basis=basis, raw_prev_close=101),
        ),
        point_in_time_safe=point_in_time_safe,
        ledger_status=CorporateActionLedgerStatus.EMPTY_INTERVAL_ATTESTED,
        signal_source="tushare-adj-factor-snapshot",
        execution_source="tushare-pro-daily-raw",
        adjustment_source="tushare-adj-factor",
        corporate_action_source="tushare-corporate-actions",
    )


def test_calibration_ready_series_is_content_addressed_and_stable() -> None:
    first = _series()
    second = _series()

    first.assert_calibration_ready()
    assert first == second
    assert first.manifest.fingerprint() == second.manifest.fingerprint()
    assert len(first.manifest.fingerprint()) == 64
    assert first.signal_bars[0].close == 202.0
    assert first.execution_bars[0].close == 101.0
    assert first.frames[0].signal_price_to_raw(200.0) == pytest.approx(100.0)

    # The public adapter is a fresh copy; neither it nor the frozen canonical
    # bar can mutate the content behind the manifest fingerprint.
    leaked = first.signal_bars[0]
    leaked.close = 999
    assert first.frames[0].signal_bar.close == 202.0
    with pytest.raises(Exception):
        first.frames[0].signal_bar.close = 999
    first.assert_calibration_ready()


@pytest.mark.parametrize(
    ("basis", "point_in_time_safe", "reason"),
    [
        (PriceBasis.QFQ, True, "not point-in-time HFQ"),
        (PriceBasis.HFQ_POINT_IN_TIME, False, "not attested"),
    ],
)
def test_unsafe_adjustment_provenance_fails_closed(
    basis: PriceBasis, point_in_time_safe: bool, reason: str
) -> None:
    series = _series(basis=basis, point_in_time_safe=point_in_time_safe)

    with pytest.raises(PriceDataContractError, match=reason):
        series.assert_calibration_ready()


def test_frame_rejects_mismatched_date_stock_and_intraday_scale() -> None:
    with pytest.raises(PriceDataContractError, match="same trade_date"):
        DailyPriceFrame(
            signal_bar=_bar("20260709", scale=2),
            execution_bar=_bar("20260710"),
            signal_basis=PriceBasis.HFQ_POINT_IN_TIME,
            raw_per_signal_unit=0.5,
            adjustment_factor_known_at="2026-07-08T18:00:00+08:00",
            adjustment_factor_source_sha256="a" * 64,
        )

    other = replace(_bar("20260709"), ts_code="000001.SZ")
    with pytest.raises(PriceDataContractError, match="same non-empty ts_code"):
        DailyPriceFrame(
            signal_bar=_bar("20260709", scale=2),
            execution_bar=other,
            signal_basis=PriceBasis.HFQ_POINT_IN_TIME,
            raw_per_signal_unit=0.5,
            adjustment_factor_known_at="2026-07-08T18:00:00+08:00",
            adjustment_factor_source_sha256="a" * 64,
        )

    distorted = replace(_bar("20260709"), high=120.0)
    with pytest.raises(PriceDataContractError, match="high raw/signal ratio"):
        DailyPriceFrame(
            signal_bar=_bar("20260709", scale=2),
            execution_bar=distorted,
            signal_basis=PriceBasis.HFQ_POINT_IN_TIME,
            raw_per_signal_unit=0.5,
            adjustment_factor_known_at="2026-07-08T18:00:00+08:00",
            adjustment_factor_source_sha256="a" * 64,
        )


def test_series_rejects_content_tampering_and_false_empty_ledger() -> None:
    series = _series()
    bad_manifest = replace(
        series.manifest,
        execution_content_sha256="0" * 64,
    )
    with pytest.raises(PriceDataContractError, match="does not match manifest"):
        DualPriceSeries(
            manifest=bad_manifest,
            frames=series.frames,
            corporate_actions=series.corporate_actions,
        )

    action = CorporateAction(
        action_id="fixture-action-1",
        ts_code=TS_CODE,
        action_type=CorporateActionType.CASH_AND_SHARES,
        record_date="20260709",
        ex_date="20260710",
        share_credit_date="20260710",
        share_sellable_date="20260713",
        cash_payment_date="20260714",
        share_multiplier=1.1,
        cash_dividend_gross_per_pre_action_share=0.5,
        withholding_model_version=(
            DividendWithholdingModel.CN_A_SHARE_HOLDING_PERIOD_V1
        ),
        fractional_share_policy=FractionalSharePolicy.REJECT_NON_INTEGER,
        action_known_at="2026-07-09T18:00:00+08:00",
        source_content_sha256="c" * 64,
        source_ref="fixture-action-1",
    )
    with pytest.raises(PriceDataContractError, match="EMPTY_INTERVAL_ATTESTED"):
        DualPriceSeries(
            manifest=series.manifest,
            frames=series.frames,
            corporate_actions=(action,),
        )


def test_complete_corporate_action_ledger_is_hashed() -> None:
    action = CorporateAction(
        action_id="fixture-action-1",
        ts_code=TS_CODE,
        action_type=CorporateActionType.CASH_AND_SHARES,
        record_date="20260709",
        ex_date="20260710",
        share_credit_date="20260710",
        share_sellable_date="20260713",
        cash_payment_date="20260714",
        share_multiplier=1.1,
        cash_dividend_gross_per_pre_action_share=0.5,
        withholding_model_version=(
            DividendWithholdingModel.CN_A_SHARE_HOLDING_PERIOD_V1
        ),
        fractional_share_policy=FractionalSharePolicy.REJECT_NON_INTEGER,
        action_known_at="2026-07-09T18:00:00+08:00",
        source_content_sha256="c" * 64,
        source_ref="fixture-action-1",
    )
    expected_reference = (101 - 0.5) / 1.1
    next_ratio = 0.5 * expected_reference / 101
    series = build_dual_price_series(
        (
            _frame("20260709"),
            _frame(
                "20260710",
                signal_scale=1 / next_ratio,
                raw_prev_close=expected_reference,
            ),
        ),
        corporate_actions=(action,),
        point_in_time_safe=True,
        ledger_status=CorporateActionLedgerStatus.COMPLETE,
        signal_source="signal",
        execution_source="raw",
        adjustment_source="adjustment",
        corporate_action_source="corporate-actions",
    )

    series.assert_calibration_ready()
    assert series.corporate_actions == (action,)
    assert series.manifest.corporate_actions_sha256 != "0" * 64


def test_adjustment_factor_is_hashed_and_must_be_known_before_open() -> None:
    first = build_dual_price_series(
        (_frame("20260709"),),
        point_in_time_safe=True,
        ledger_status=CorporateActionLedgerStatus.EMPTY_INTERVAL_ATTESTED,
        signal_source="signal",
        execution_source="raw",
        adjustment_source="factor",
        corporate_action_source="actions",
    )
    second = build_dual_price_series(
        (_frame("20260709", ratio=0.5004),),
        point_in_time_safe=True,
        ledger_status=CorporateActionLedgerStatus.EMPTY_INTERVAL_ATTESTED,
        signal_source="signal",
        execution_source="raw",
        adjustment_source="factor",
        corporate_action_source="actions",
    )
    assert first.manifest.fingerprint() != second.manifest.fingerprint()

    with pytest.raises(PriceDataContractError, match="session open"):
        DailyPriceFrame(
            signal_bar=_bar("20260709", scale=2),
            execution_bar=_bar("20260709"),
            signal_basis=PriceBasis.HFQ_POINT_IN_TIME,
            raw_per_signal_unit=0.5,
            adjustment_factor_known_at="2026-07-09T15:00:00+08:00",
            adjustment_factor_source_sha256="a" * 64,
        )


def test_factor_change_requires_a_matching_corporate_action() -> None:
    with pytest.raises(PriceDataContractError, match="change dates"):
        build_dual_price_series(
            (
                _frame("20260709"),
                _frame("20260710", signal_scale=2.5, raw_prev_close=101),
            ),
            point_in_time_safe=True,
            ledger_status=CorporateActionLedgerStatus.EMPTY_INTERVAL_ATTESTED,
            signal_source="signal",
            execution_source="raw",
            adjustment_source="factor",
            corporate_action_source="actions",
        )


def test_bar_semantics_and_cross_domain_units_are_strict() -> None:
    invalid_signal = replace(_bar("20260709", scale=2), high=180, low=220)
    with pytest.raises(PriceDataContractError, match="high must"):
        _ = DailyPriceFrame(
            signal_bar=invalid_signal,
            execution_bar=_bar("20260709"),
            signal_basis=PriceBasis.HFQ_POINT_IN_TIME,
            raw_per_signal_unit=0.5,
            adjustment_factor_known_at="2026-07-08T18:00:00+08:00",
            adjustment_factor_source_sha256="a" * 64,
        )

    different_volume = replace(_bar("20260709"), vol=900_000)
    with pytest.raises(PriceDataContractError, match="same volume units"):
        DailyPriceFrame(
            signal_bar=_bar("20260709", scale=2),
            execution_bar=different_volume,
            signal_basis=PriceBasis.HFQ_POINT_IN_TIME,
            raw_per_signal_unit=0.5,
            adjustment_factor_known_at="2026-07-08T18:00:00+08:00",
            adjustment_factor_source_sha256="a" * 64,
        )


def test_unsupported_corporate_action_type_is_rejected() -> None:
    with pytest.raises(PriceDataContractError, match="supported"):
        CorporateAction(
            action_id="rights-1",
            ts_code=TS_CODE,
            action_type="RIGHTS_SUBSCRIPTION",  # type: ignore[arg-type]
            record_date="20260709",
            ex_date="20260710",
            share_credit_date="20260710",
            share_sellable_date="20260713",
            cash_payment_date="20260710",
            share_multiplier=1.2,
            cash_dividend_gross_per_pre_action_share=0,
            withholding_model_version=DividendWithholdingModel.NONE,
            fractional_share_policy=FractionalSharePolicy.REJECT_NON_INTEGER,
            action_known_at="2026-07-09T18:00:00+08:00",
            source_content_sha256="d" * 64,
            source_ref="rights-source",
        )


def test_execution_prev_close_and_pct_change_must_be_consistent() -> None:
    bad_raw = replace(_bar("20260709"), prev_close=1, pct_chg=-999)
    with pytest.raises(PriceDataContractError, match="execution pct_chg"):
        DailyPriceFrame(
            signal_bar=_bar("20260709", scale=2),
            execution_bar=bad_raw,
            signal_basis=PriceBasis.HFQ_POINT_IN_TIME,
            raw_per_signal_unit=0.5,
            adjustment_factor_known_at="2026-07-08T18:00:00+08:00",
            adjustment_factor_source_sha256="a" * 64,
        )


def test_factor_magnitude_must_match_corporate_action_economics() -> None:
    action = CorporateAction(
        action_id="tiny-dividend",
        ts_code=TS_CODE,
        action_type=CorporateActionType.CASH_DIVIDEND,
        record_date="20260709",
        ex_date="20260710",
        share_credit_date="20260710",
        share_sellable_date="20260710",
        cash_payment_date="20260714",
        share_multiplier=1,
        cash_dividend_gross_per_pre_action_share=0.01,
        withholding_model_version=(
            DividendWithholdingModel.CN_A_SHARE_HOLDING_PERIOD_V1
        ),
        fractional_share_policy=FractionalSharePolicy.REJECT_NON_INTEGER,
        action_known_at="2026-07-09T18:00:00+08:00",
        source_content_sha256="e" * 64,
        source_ref="tiny-dividend-source",
    )
    with pytest.raises(PriceDataContractError, match="magnitude"):
        build_dual_price_series(
            (
                _frame("20260709"),
                _frame(
                    "20260710",
                    signal_scale=10_000,
                    raw_prev_close=100.99,
                ),
            ),
            corporate_actions=(action,),
            point_in_time_safe=True,
            ledger_status=CorporateActionLedgerStatus.COMPLETE,
            signal_source="signal",
            execution_source="raw",
            adjustment_source="factor",
            corporate_action_source="actions",
        )


def test_score_weights_reject_nonfinite_values_before_calibration() -> None:
    invalid = dict(ScoreWeights().buy)
    invalid["entry_structure"] = float("nan")
    with pytest.raises(ValueError, match="finite numbers"):
        ScoreWeights(buy=invalid)
