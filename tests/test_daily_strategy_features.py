"""Contract tests for as-of-safe named strategy evidence."""

from datetime import date, timedelta

from modules.daily_portfolio.strategy_features import (
    VariantEvidence,
    _latest_anchor,
    build_strategy_features,
)
from modules.indicators import DailyData


def _bars(count: int = 130) -> list[DailyData]:
    result = []
    close = 10.0
    for index in range(count):
        trade_date = (date(2026, 1, 1) + timedelta(days=index)).strftime("%Y%m%d")
        previous = close
        close = previous * (1 + ((index % 7) - 3) * 0.001)
        result.append(
            DailyData(
                ts_code="000001.SZ",
                trade_date=trade_date,
                open=previous,
                high=max(previous, close) * 1.01,
                low=min(previous, close) * 0.99,
                close=close,
                vol=1_000_000 + index * 1_000,
                amount=close * (1_000_000 + index * 1_000),
                pct_chg=0,
                prev_close=previous,
            )
        )
    return result


def test_strategy_snapshot_has_named_non_overloaded_variants() -> None:
    bars = _bars()
    result = build_strategy_features(bars, bars[-1].trade_date)

    expected = {
        "b1.loose_3of4",
        "b1.strict_oversold",
        "b1.quality_confirmed",
        "b2.knowledge_5bar",
        "b2.legacy_5_14bar",
        "b3.pullback_reentry",
        "b3.consensus_continuation",
        "super_b1.washout",
        "sb1.false_break",
        "sb1.reclaim_confirmation",
        "s1.ugly_hat",
        "s2.anchor_divergence",
        "s3.failed_rebound",
    }
    assert expected <= set(result.variant_evidence)
    assert result.hard_vetoes == ()
    assert result.bars_end_date == bars[-1].trade_date


def test_anchor_search_never_uses_the_current_bar_as_prior_signal() -> None:
    histories = [
        {"b1.quality_confirmed": VariantEvidence("b1.quality_confirmed", False, 0)},
        {"b1.quality_confirmed": VariantEvidence("b1.quality_confirmed", True, 100)},
    ]

    assert _latest_anchor(histories, 1, "b1.quality_confirmed", 1, 5) is None


def test_latest_anchor_preserves_age_and_variant() -> None:
    histories = [
        {"b1.quality_confirmed": VariantEvidence("b1.quality_confirmed", True, 100)},
        {"b1.quality_confirmed": VariantEvidence("b1.quality_confirmed", False, 0)},
        {"b1.quality_confirmed": VariantEvidence("b1.quality_confirmed", False, 0)},
    ]
    found = _latest_anchor(histories, 2, "b1.quality_confirmed", 1, 5)
    assert found is not None
    assert found[0] == 0
    assert found[1] == 2
    assert found[2] == "b1.quality_confirmed"


def test_strategy_features_are_prefix_invariant() -> None:
    bars = _bars(131)
    prefix = build_strategy_features(bars[:130], bars[129].trade_date)
    repeated = build_strategy_features(list(bars[:130]), bars[129].trade_date)

    assert prefix == repeated
    # A caller must not pass the future bar to an earlier as-of calculation.
    try:
        build_strategy_features(bars, bars[129].trade_date)
    except ValueError as exc:
        assert "future bar" in str(exc)
    else:
        raise AssertionError("future data should have violated the as-of contract")


def test_short_history_is_explicitly_missing_not_silently_neutral() -> None:
    bars = _bars(20)
    result = build_strategy_features(bars, bars[-1].trade_date)

    assert "INSUFFICIENT_HISTORY" in result.hard_vetoes
    assert "required_history" in result.missing_fields
    assert result.boolean_features["white_above_yellow"] is None


def test_lagged_last_bar_is_an_explicit_entry_veto() -> None:
    bars = _bars(120)
    next_date = (date(2026, 1, 1) + timedelta(days=120)).strftime("%Y%m%d")
    result = build_strategy_features(bars, next_date)

    assert result.signal_date == next_date
    assert result.bars_end_date == bars[-1].trade_date
    assert "STALE_BAR" in result.hard_vetoes
