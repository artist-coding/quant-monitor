"""Tests for deterministic daily score aggregation."""

import pytest

from modules.daily_portfolio.score_engine import (
    BuyComponents,
    ScoreEvidence,
    SellComponents,
    aggregate_scores,
)


def _buy(value: float) -> BuyComponents:
    return BuyComponents(
        entry_structure=value,
        trend=value,
        volume=value,
        pattern_quality=value,
        stage=value,
        market=value,
        resonance=value,
    )


def _sell(value: float) -> SellComponents:
    return SellComponents(
        stop=value,
        exit_signal=value,
        trend_break=value,
        distribution=value,
        market_risk=value,
        position_heat=value,
        profit_protection=value,
    )


def test_uniform_components_preserve_normalized_score() -> None:
    result = aggregate_scores(ScoreEvidence(buy=_buy(80), sell=_sell(30)))

    assert result.buy_score == 80
    assert result.sell_score == 30
    assert sum(result.buy_contributions.values()) == pytest.approx(80)
    assert sum(result.sell_contributions.values()) == pytest.approx(30)


def test_risk_penalty_is_auditable_negative_contribution() -> None:
    result = aggregate_scores(ScoreEvidence(buy=_buy(80), sell=_sell(20), risk_penalty_points=17))

    assert result.buy_score == 63
    assert result.buy_contributions["risk_penalty"] == -17


def test_hard_veto_and_hard_exit_override_soft_scores() -> None:
    result = aggregate_scores(
        ScoreEvidence(
            buy=_buy(95),
            sell=_sell(5),
            hard_vetoes=("停牌",),
            hard_exit_reasons=("硬止损",),
        )
    )

    assert result.buy_score == 0
    assert result.sell_score == 100


@pytest.mark.parametrize("value", [-0.01, 100.01])
def test_component_values_must_be_normalized(value: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        _buy(value)
