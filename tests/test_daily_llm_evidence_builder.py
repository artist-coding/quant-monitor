"""Integration tests from deterministic daily scoring to frozen LLM evidence."""

from datetime import date, timedelta

import pytest

from modules.daily_portfolio import LifecycleState, MarketSnapshot, PositionState
from modules.daily_portfolio.llm_overlay import (
    DecisionMode,
    OverlayValidationError,
    evaluate_overlay_payload,
    build_evidence_snapshot,
)
from modules.daily_portfolio.service import evaluate_daily_bar
from modules.indicators import DailyData


def _bars(count: int = 125) -> list[DailyData]:
    bars = []
    close = 10.0
    for index in range(count):
        previous = close
        close *= 1.001
        volume = 1_000_000 + index * 1_000
        bars.append(
            DailyData(
                ts_code="000001.SZ",
                trade_date=(date(2026, 1, 1) + timedelta(days=index)).strftime("%Y%m%d"),
                open=previous,
                high=close * 1.01,
                low=previous * 0.99,
                close=close,
                vol=volume,
                amount=volume * close,
                pct_chg=(close / previous - 1) * 100,
                prev_close=previous,
            )
        )
    return bars


def _evaluation(position: PositionState, market_score: float = 50):
    bars = _bars()
    market = MarketSnapshot(bars[-1].trade_date, market_score, source_hash="fixture")
    result = evaluate_daily_bar(
        "000001.SZ",
        bars[-1].trade_date,
        bars,
        position,
        market,
        max_position_pct=0.10,
    )
    return bars, market, result


def test_builder_is_stable_and_preserves_versions_and_unique_refs() -> None:
    position = PositionState(ts_code="000001.SZ")
    _, market, evaluation = _evaluation(position)
    first = build_evidence_snapshot(evaluation)
    second = build_evidence_snapshot(evaluation)

    assert first == second
    assert first.sha256() == second.sha256()
    assert first.parameter_fingerprint == evaluation.score.parameter_fingerprint
    refs = [item.ref_id for item in first.evidence]
    assert len(refs) == len(set(refs))
    assert "quant:buy_score" in refs
    assert "market:context" in refs


def test_builder_rejects_stale_last_bar_for_llm_even_if_quant_is_blocked() -> None:
    position = PositionState(ts_code="000001.SZ")
    bars, market, evaluation = _evaluation(position)
    stale_market = MarketSnapshot(
        (date(2026, 1, 1) + timedelta(days=len(bars))).strftime("%Y%m%d"),
        market.score,
    )
    with pytest.raises(OverlayValidationError, match="market snapshot"):
        from dataclasses import replace

        build_evidence_snapshot(replace(evaluation, market_context=stale_market))


def test_hard_exit_builds_a_zero_target_exit_snapshot() -> None:
    bars = _bars()
    position = PositionState(
        ts_code="000001.SZ",
        lifecycle_state=LifecycleState.HOLDING,
        shares=1000,
        available_shares=1000,
        avg_cost=10,
        current_position_pct=0.10,
        stop_loss=bars[-1].close * 1.01,
    )
    market = MarketSnapshot(bars[-1].trade_date, 50)
    evaluation = evaluate_daily_bar(
        "000001.SZ",
        bars[-1].trade_date,
        bars,
        position,
        market,
        max_position_pct=0.10,
    )
    snapshot = build_evidence_snapshot(evaluation)

    assert snapshot.quant_action.value == "EXIT"
    assert snapshot.quant_target_step == 0
    assert snapshot.hard_exit_reasons == ("EXIT_STOP_LOSS",)
    assert any(item.ref_id.startswith("risk:exit:") for item in snapshot.evidence)


def test_over_limit_position_uses_non_targetable_overflow_step() -> None:
    position = PositionState(
        ts_code="000001.SZ",
        lifecycle_state=LifecycleState.HOLDING,
        shares=2000,
        available_shares=2000,
        avg_cost=10,
        current_position_pct=0.20,
        stop_loss=9,
    )
    _, _, evaluation = _evaluation(position)

    assert evaluation.policy.decision_code == "position_limit_reduce"
    snapshot = build_evidence_snapshot(evaluation)

    assert snapshot.current_step == snapshot.max_step + 1
    assert snapshot.quant_target_step == snapshot.max_step
    assert snapshot.quant_action.value == "REDUCE"

    # An unavailable model must still preserve the mandatory quant reduction.
    decision = evaluate_overlay_payload(snapshot, None)
    assert decision.mode == DecisionMode.QUANT_ONLY
    assert decision.final_action.value == "REDUCE"
    assert decision.final_target_step == snapshot.max_step
