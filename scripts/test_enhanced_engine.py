#!/usr/bin/env python3
"""
增强版少妇战法测试

对比基础版和增强版的胜率
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.loop_engine import ShaofuLoopEngine, LoopConfig
from modules.loop_engine_enhanced import EnhancedShaofuLoopEngine, EnhancedLoopConfig
from modules.backtest_six_step import backtest_shaofu_single, ShaofuBacktestResult
from modules.indicators import get_kline_data


def test_single_stock(ts_code: str, days: int = 500):
    """测试单只股票的基础版 vs 增强版"""
    print(f"\n{'=' * 80}")
    print(f"测试股票: {ts_code}")
    print(f"回测周期: {days} 天")
    print(f"{'=' * 80}\n")

    # 获取 K 线数据
    klines = get_kline_data(ts_code, days)
    if not klines or len(klines) < 50:
        print(f"❌ 数据不足")
        return

    # 1. 基础版
    print("【基础版少妇战法】")
    base_engine = ShaofuLoopEngine(LoopConfig())
    base_trades = base_engine.run_stock(klines, ts_code=ts_code)

    if base_trades:
        base_result = ShaofuBacktestResult(ts_code=ts_code, trades=base_trades)
        from modules.backtest_six_step import _calc_metrics
        _calc_metrics(base_result)

        print(f"  交易次数: {base_result.total_trades}")
        print(f"  胜率: {base_result.win_rate:.1%}")
        print(f"  盈亏比: {base_result.profit_factor:.2f}")
        print(f"  累计收益: {base_result.total_return:+.2%}")
        print(f"  夏普比率: {base_result.sharpe_ratio:.2f}")
    else:
        print(f"  无交易")
        base_result = None

    # 2. 增强版（只使用 B1）
    print("\n【增强版 - 仅 B1】")
    enhanced_config_1 = EnhancedLoopConfig(
        enable_b2=False,
        enable_changan=False,
        enable_nana=False,
        enable_pinghang=False,
        min_signals=1,
    )
    enhanced_engine_1 = EnhancedShaofuLoopEngine(enhanced_config_1)
    enhanced_trades_1 = enhanced_engine_1.run_stock(klines, ts_code=ts_code)

    if enhanced_trades_1:
        enhanced_result_1 = ShaofuBacktestResult(ts_code=ts_code, trades=enhanced_trades_1)
        _calc_metrics(enhanced_result_1)

        print(f"  交易次数: {enhanced_result_1.total_trades}")
        print(f"  胜率: {enhanced_result_1.win_rate:.1%}")
        print(f"  盈亏比: {enhanced_result_1.profit_factor:.2f}")
        print(f"  累计收益: {enhanced_result_1.total_return:+.2%}")
    else:
        print(f"  无交易")

    # 3. 增强版（B1 + B2，需要 2 个信号）
    print("\n【增强版 - B1 + B2（2信号）】")
    enhanced_config_2 = EnhancedLoopConfig(
        enable_b2=True,
        enable_changan=False,
        enable_nana=False,
        enable_pinghang=False,
        min_signals=2,
    )
    enhanced_engine_2 = EnhancedShaofuLoopEngine(enhanced_config_2)
    enhanced_trades_2 = enhanced_engine_2.run_stock(klines, ts_code=ts_code)

    if enhanced_trades_2:
        enhanced_result_2 = ShaofuBacktestResult(ts_code=ts_code, trades=enhanced_trades_2)
        _calc_metrics(enhanced_result_2)

        print(f"  交易次数: {enhanced_result_2.total_trades}")
        print(f"  胜率: {enhanced_result_2.win_rate:.1%}")
        print(f"  盈亏比: {enhanced_result_2.profit_factor:.2f}")
        print(f"  累计收益: {enhanced_result_2.total_return:+.2%}")

        # 显示触发的策略
        from modules.loop_engine_enhanced import EnhancedLoopTrade
        for trade in enhanced_trades_2[:3]:
            if hasattr(trade, "triggered_strategies") and trade.triggered_strategies:
                print(f"    {trade.entry_date}: {', '.join(trade.triggered_strategies)}")
    else:
        print(f"  无交易")

    # 4. 增强版（所有策略，需要 2 个信号）
    print("\n【增强版 - 所有策略（2信号）】")
    enhanced_config_3 = EnhancedLoopConfig(
        enable_b2=True,
        enable_changan=True,
        enable_nana=True,
        enable_pinghang=True,
        min_signals=2,
    )
    enhanced_engine_3 = EnhancedShaofuLoopEngine(enhanced_config_3)
    enhanced_trades_3 = enhanced_engine_3.run_stock(klines, ts_code=ts_code)

    if enhanced_trades_3:
        enhanced_result_3 = ShaofuBacktestResult(ts_code=ts_code, trades=enhanced_trades_3)
        _calc_metrics(enhanced_result_3)

        print(f"  交易次数: {enhanced_result_3.total_trades}")
        print(f"  胜率: {enhanced_result_3.win_rate:.1%}")
        print(f"  盈亏比: {enhanced_result_3.profit_factor:.2f}")
        print(f"  累计收益: {enhanced_result_3.total_return:+.2%}")

        # 显示触发的策略
        for trade in enhanced_trades_3[:3]:
            if hasattr(trade, "triggered_strategies") and trade.triggered_strategies:
                print(f"    {trade.entry_date}: {', '.join(trade.triggered_strategies)}")
    else:
        print(f"  无交易")

    # 5. 对比总结
    print(f"\n{'=' * 80}")
    print("【对比总结】")
    print(f"{'=' * 80}")

    if base_result and enhanced_trades_3:
        base_wr = base_result.win_rate
        enhanced_wr = enhanced_result_3.win_rate
        improvement = (enhanced_wr - base_wr) / base_wr if base_wr > 0 else 0

        print(f"基础版胜率: {base_wr:.1%}")
        print(f"增强版胜率: {enhanced_wr:.1%}")
        print(f"胜率提升: {improvement:+.1%}")

        if enhanced_wr > base_wr:
            print(f"✅ 增强版胜率提升!")
        else:
            print(f"❌ 增强版胜率未提升")

    print()


def test_multiple_stocks(ts_codes: list[str], days: int = 500):
    """测试多只股票"""
    print(f"\n{'=' * 80}")
    print(f"批量测试: {len(ts_codes)} 只股票")
    print(f"{'=' * 80}\n")

    base_wins = []
    enhanced_wins = []

    for i, ts_code in enumerate(ts_codes, 1):
        print(f"[{i}/{len(ts_codes)}] {ts_code}...", end=" ", flush=True)

        try:
            klines = get_kline_data(ts_code, days)
            if not klines or len(klines) < 50:
                print("❌ 数据不足")
                continue

            # 基础版
            base_engine = ShaofuLoopEngine(LoopConfig())
            base_trades = base_engine.run_stock(klines, ts_code=ts_code)

            # 增强版（所有策略）
            enhanced_config = EnhancedLoopConfig(
                enable_b2=True,
                enable_changan=True,
                enable_nana=True,
                enable_pinghang=True,
                min_signals=2,
            )
            enhanced_engine = EnhancedShaofuLoopEngine(enhanced_config)
            enhanced_trades = enhanced_engine.run_stock(klines, ts_code=ts_code)

            if base_trades and enhanced_trades:
                base_result = ShaofuBacktestResult(ts_code=ts_code, trades=base_trades)
                enhanced_result = ShaofuBacktestResult(ts_code=ts_code, trades=enhanced_trades)

                from modules.backtest_six_step import _calc_metrics
                _calc_metrics(base_result)
                _calc_metrics(enhanced_result)

                base_wins.append(base_result.win_rate)
                enhanced_wins.append(enhanced_result.win_rate)

                improvement = (enhanced_result.win_rate - base_result.win_rate) / base_result.win_rate if base_result.win_rate > 0 else 0

                print(
                    f"基础{base_result.win_rate:.0%} → 增强{enhanced_result.win_rate:.0%} "
                    f"({improvement:+.0%})"
                )
            else:
                print("无交易")

        except Exception as e:
            print(f"⚠️  错误: {e}")

    # 汇总
    if base_wins:
        avg_base = sum(base_wins) / len(base_wins)
        avg_enhanced = sum(enhanced_wins) / len(enhanced_wins)
        avg_improvement = (avg_enhanced - avg_base) / avg_base if avg_base > 0 else 0

        print(f"\n{'=' * 80}")
        print("【汇总】")
        print(f"{'=' * 80}")
        print(f"平均基础胜率: {avg_base:.1%}")
        print(f"平均增强胜率: {avg_enhanced:.1%}")
        print(f"平均提升: {avg_improvement:+.1%}")

        if avg_enhanced > avg_base:
            print(f"✅ 增强版整体胜率提升!")
        else:
            print(f"❌ 增强版整体胜率未提升")


if __name__ == "__main__":
    # 默认测试中国平安
    test_stocks = ["601318.SH"]

    if len(sys.argv) > 1:
        test_stocks = sys.argv[1:]

    if len(test_stocks) == 1:
        test_single_stock(test_stocks[0], days=500)
    else:
        test_multiple_stocks(test_stocks, days=500)
