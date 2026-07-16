"""模拟器 simple 模式与指标函数之间的字段契约测试。"""

from unittest.mock import MagicMock, patch

import pytest

from modules.screener import StockScore
from modules.simulator import SignalVerdict, SimulationConfig
from modules.simulator.signal_filter import _extract_signals, evaluate_stock


def _stock_score() -> StockScore:
    return StockScore(
        ts_code="600519.SH",
        name="贵州茅台",
        score=80,
        b1_score=70,
        trend_score=70,
        volume_score=70,
        risk_score=80,
        reasons=["B1信号"],
        warnings=[],
    )


@pytest.mark.parametrize(
    ("scenario", "expected_signal"),
    [
        ("攻击日", "量比攻击"),
        ("超级攻击", "量比攻击"),
        ("单向拉升", "量比攻击"),
        ("出货日", "量比恶劣"),
        ("弱势日", "量比恶劣"),
    ],
)
def test_extract_signals_reads_volume_ratio_scenario(scenario, expected_signal):
    with (
        patch(
            "modules.simulator.signal_filter.detect_volume_ratio_strategy",
            return_value={"scenario": scenario},
        ),
        patch(
            "modules.simulator.signal_filter.detect_bull_rope",
            return_value={"status": ""},
        ),
        patch(
            "modules.simulator.signal_filter.calculate_sandglass_score",
            return_value={"score": 0, "is_perfect": False},
        ),
    ):
        signals = _extract_signals(_stock_score(), [MagicMock() for _ in range(120)])

    assert expected_signal in signals


@pytest.mark.parametrize(
    ("status", "expected_signal"),
    [
        ("牵牛", "牛绳金叉"),
        ("金叉", "牛绳金叉"),
        ("牛绳断", "牛绳断"),
        ("死叉", "牛绳断"),
    ],
)
def test_extract_signals_reads_bull_rope_status(status, expected_signal):
    with (
        patch(
            "modules.simulator.signal_filter.detect_volume_ratio_strategy",
            return_value={"scenario": "正常震荡"},
        ),
        patch(
            "modules.simulator.signal_filter.detect_bull_rope",
            return_value={"status": status},
        ),
        patch(
            "modules.simulator.signal_filter.calculate_sandglass_score",
            return_value={"score": 0, "is_perfect": False},
        ),
    ):
        signals = _extract_signals(_stock_score(), [MagicMock() for _ in range(120)])

    assert expected_signal in signals


@pytest.mark.parametrize(
    ("scenario", "rope_status", "expected_verdict"),
    [
        ("攻击日", "牵牛", SignalVerdict.PASS),
        ("出货日", "牵牛", SignalVerdict.BAD_STAGE),
        ("正常震荡", "死叉", SignalVerdict.BAD_STAGE),
    ],
)
def test_simple_mode_applies_extracted_contract_signals(
    scenario, rope_status, expected_verdict
):
    klines = [MagicMock() for _ in range(120)]
    config = SimulationConfig(strategy_mode="simple")

    with (
        patch(
            "modules.simulator.signal_filter.analyze_stock",
            return_value=_stock_score(),
        ),
        patch(
            "modules.simulator.signal_filter.detect_volume_ratio_strategy",
            return_value={"scenario": scenario},
        ),
        patch(
            "modules.simulator.signal_filter.detect_bull_rope",
            return_value={"status": rope_status},
        ),
        patch(
            "modules.simulator.signal_filter.calculate_sandglass_score",
            return_value={"score": 0, "is_perfect": False},
        ),
    ):
        result = evaluate_stock(
            "600519.SH",
            "20260710",
            klines=klines,
            datasource=MagicMock(),
            config=config,
        )

    assert result.verdict == expected_verdict


def test_extract_signals_ignores_bull_rope_default_when_history_is_short():
    with (
        patch(
            "modules.simulator.signal_filter.detect_volume_ratio_strategy",
            return_value={"scenario": "正常震荡"},
        ),
        patch(
            "modules.simulator.signal_filter.detect_bull_rope",
            return_value={"status": "牛绳断"},
        ) as bull_rope,
        patch(
            "modules.simulator.signal_filter.calculate_sandglass_score",
            return_value={"score": 0, "is_perfect": False},
        ),
    ):
        signals = _extract_signals(
            _stock_score(), [MagicMock() for _ in range(119)]
        )

    bull_rope.assert_not_called()
    assert "牛绳断" not in signals
