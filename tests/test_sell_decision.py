"""sell_decision.py 逐步放飞阶梯测试（纯函数部分，不碰数据库）"""

from modules.sell_decision import (
    PositionState,
    evaluate_sell,
    format_sell_decision,
)
from modules.strategies import StrategyType


def _sig(strategy):
    """最小信号替身：evaluate_sell 只读 strategy 属性"""
    return {"strategy": strategy}


class TestLadderRungs:
    def test_no_signal_no_profit_holds(self):
        pos = PositionState(entry_price=100.0)
        d = evaluate_sell(pos, 105.0, [])
        assert d.action == "HOLD"
        assert d.sell_fraction == 0

    def test_profit_20_releases_one_third(self):
        pos = PositionState(entry_price=100.0)
        d = evaluate_sell(pos, 121.0, [])
        assert d.action == "REDUCE"
        assert abs(d.sell_fraction - 1 / 3) < 1e-3
        assert d.state.took_profit is True
        assert d.profit_pct == 0.21

    def test_profit_20_boundary_inclusive(self):
        """恰好 +20%（收盘=成本×1.2）应触发"""
        pos = PositionState(entry_price=100.0)
        d = evaluate_sell(pos, 120.0, [])
        assert d.action == "REDUCE"

    def test_profit_rung_fires_only_once(self):
        pos = PositionState(entry_price=100.0, took_profit=True)
        d = evaluate_sell(pos, 130.0, [])
        assert d.action == "HOLD"
        assert d.sell_fraction == 0

    def test_s1_sells_half(self):
        pos = PositionState(entry_price=100.0)
        d = evaluate_sell(pos, 110.0, [_sig(StrategyType.S1)])
        assert d.action == "REDUCE"
        assert abs(d.sell_fraction - 0.5) < 1e-9
        assert d.state.s1_done is True

    def test_s1_fires_only_once(self):
        pos = PositionState(entry_price=100.0, s1_done=True)
        d = evaluate_sell(pos, 110.0, [_sig(StrategyType.S1)])
        assert d.action == "HOLD"

    def test_s2_sells_half_of_remaining(self):
        """S1 已执行过之后出现 S2：再砍现有仓位的一半"""
        pos = PositionState(entry_price=100.0, s1_done=True)
        d = evaluate_sell(pos, 110.0, [_sig(StrategyType.S2)])
        assert d.action == "REDUCE"
        assert abs(d.sell_fraction - 0.5) < 1e-9
        assert d.state.s2_done is True

    def test_s1_s2_same_day_compound(self):
        """同日 S1+S2 首现：1/2 后再 1/2，共 3/4"""
        pos = PositionState(entry_price=100.0)
        d = evaluate_sell(pos, 110.0, [_sig(StrategyType.S1), _sig(StrategyType.S2)])
        assert d.action == "REDUCE"
        assert abs(d.sell_fraction - 0.75) < 1e-9

    def test_profit_and_s1_same_day_compound(self):
        """同日 +20% 与 S1：先 1/3 落袋，剩余再砍半 → 共 2/3"""
        pos = PositionState(entry_price=100.0)
        d = evaluate_sell(pos, 125.0, [_sig(StrategyType.S1)])
        assert abs(d.sell_fraction - (1 - (2 / 3) * 0.5)) < 1e-3

    def test_s3_exits_everything(self):
        pos = PositionState(entry_price=100.0)
        d = evaluate_sell(pos, 125.0, [_sig(StrategyType.S1), _sig(StrategyType.S3)])
        assert d.action == "EXIT"
        assert d.sell_fraction == 1.0
        assert d.triggered[0]["rung"] == "S3"

    def test_unknown_entry_price_skips_profit_rung(self):
        pos = PositionState(entry_price=0.0)
        d = evaluate_sell(pos, 125.0, [_sig(StrategyType.S1)])
        assert abs(d.sell_fraction - 0.5) < 1e-9
        assert d.profit_pct is None
        assert any("成本价未知" in n for n in d.notes)

    def test_input_position_not_mutated(self):
        pos = PositionState(entry_price=100.0)
        evaluate_sell(pos, 125.0, [_sig(StrategyType.S1)])
        assert pos.took_profit is False
        assert pos.s1_done is False


class TestSignalShapes:
    """信号入参兼容 StrategySignal / dict / 裸字符串"""

    def test_string_signals(self):
        d = evaluate_sell(PositionState(entry_price=100.0), 100.0, ["S1"])
        assert abs(d.sell_fraction - 0.5) < 1e-9

    def test_strategy_signal_objects(self):
        from modules.strategies import StrategySignal

        sig = StrategySignal(
            ts_code="600519.SH",
            trade_date="20260810",
            strategy=StrategyType.S2,
            confidence=0.8,
            description="",
            action="SELL",
        )
        d = evaluate_sell(PositionState(entry_price=100.0), 100.0, [sig])
        assert abs(d.sell_fraction - 0.5) < 1e-9

    def test_non_exit_signals_ignored(self):
        """B1/观察类信号不触发任何梯级"""
        d = evaluate_sell(
            PositionState(entry_price=100.0),
            110.0,
            [_sig(StrategyType.B1), _sig(StrategyType.WATCH), _sig(StrategyType.PAIFA)],
        )
        assert d.action == "HOLD"


class TestSequentialLadder:
    def test_two_day_sequence_state_carries(self):
        """第1天 S1 砍半，第2天 S2 再砍半——状态在两次评估间传递"""
        pos = PositionState(entry_price=100.0)

        day1 = evaluate_sell(pos, 110.0, [_sig(StrategyType.S1)])
        assert abs(day1.sell_fraction - 0.5) < 1e-9

        day2 = evaluate_sell(day1.state, 108.0, [_sig(StrategyType.S1), _sig(StrategyType.S2)])
        # S1 已执行过不重复触发，只有 S2 砍现有仓位的一半
        assert abs(day2.sell_fraction - 0.5) < 1e-9
        assert [t["rung"] for t in day2.triggered] == ["S2"]

    def test_full_ladder_cumulative(self):
        """+20% → S1 → S2 → S3 全走完：累计留仓 2/3 × 1/2 × 1/2 → 最后清零"""
        pos = PositionState(entry_price=100.0)
        remaining = 1.0

        d1 = evaluate_sell(pos, 121.0, [])
        remaining *= 1 - d1.sell_fraction
        d2 = evaluate_sell(d1.state, 125.0, [_sig(StrategyType.S1)])
        remaining *= 1 - d2.sell_fraction
        d3 = evaluate_sell(d2.state, 122.0, [_sig(StrategyType.S2)])
        remaining *= 1 - d3.sell_fraction
        assert abs(remaining - (2 / 3) * 0.5 * 0.5) < 1e-3

        d4 = evaluate_sell(d3.state, 118.0, [_sig(StrategyType.S3)])
        assert d4.action == "EXIT"
        assert remaining * (1 - d4.sell_fraction) == 0


class TestFormatting:
    def test_format_contains_action_and_rungs(self):
        d = evaluate_sell(PositionState(entry_price=100.0), 121.0, [_sig(StrategyType.S1)])
        text = format_sell_decision(d)
        assert "[减仓]" in text
        assert "+20%" in text or "落袋" in text
        assert "S1" in text

    def test_format_hold(self):
        d = evaluate_sell(PositionState(entry_price=100.0), 100.0, [])
        text = format_sell_decision(d)
        assert "[持有]" in text
