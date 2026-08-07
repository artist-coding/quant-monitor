#!/usr/bin/env python3
"""
少妇战法统计检验演示

展示如何使用统计检验验证策略有效性：
1. 夏普比率 t 检验
2. Bootstrap 置信区间
3. Monte Carlo 置换检验
4. 子周期分析

用法：
    python3 scripts/demo_validation.py 600519.SH
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.backtest_six_step import backtest_shaofu_with_validation, summary_with_validation
from modules.loop_engine import LoopConfig
from modules.statistics.criteria import CriteriaLevel


def demo_single_stock(ts_code: str, days: int = 500):
    """
    单股票完整验证演示

    Args:
        ts_code: 股票代码
        days: 回测天数
    """
    print(f"\n{'=' * 70}")
    print(f"少妇战法统计检验演示: {ts_code}")
    print(f"回测周期: {days} 天")
    print(f"{'=' * 70}\n")

    # 1. 运行带验证的回测
    print("【Step 1】运行回测 + 统计检验...")
    result = backtest_shaofu_with_validation(
        ts_code=ts_code,
        days=days,
        validation_level=CriteriaLevel.MODERATE,
    )

    if not result.trades:
        print(f"❌ {ts_code} 在 {days} 天内没有产生交易信号")
        return

    # 2. 打印基础指标
    print("\n【Step 2】基础绩效指标:")
    print(f"  总交易次数: {result.total_trades}")
    print(f"  胜率:       {result.win_rate:.1%}")
    print(f"  盈亏比:     {result.profit_factor:.2f}")
    print(f"  累计收益:   {result.total_return:+.2%}")
    print(f"  最大回撤:   {result.max_drawdown:.2%}")
    print(f"  夏普比率:   {result.sharpe_ratio:.2f}")

    # 3. 打印统计检验报告
    if result.validation_report:
        print("\n【Step 3】统计检验报告:")
        print(result.validation_report.generate_summary())

        # 4. 关键指标解读
        print("\n【Step 4】关键指标解读:")

        # 夏普 t 检验
        for criteria in result.validation_report.criteria_results:
            if criteria.name == "夏普t检验":
                if criteria.passed:
                    print(f"  ✅ {criteria.name}: p={criteria.actual_value}")
                    print(f"     → 夏普比率显著大于0，策略有效")
                else:
                    print(f"  ❌ {criteria.name}: p={criteria.actual_value}")
                    print(f"     → 夏普比率不显著，策略可能无效")

            elif criteria.name == "Bootstrap置信区间":
                if criteria.passed:
                    print(f"  ✅ {criteria.name}: {criteria.actual_value}")
                    print(f"     → 置信区间稳定，策略收益可靠")
                else:
                    print(f"  ❌ {criteria.name}: {criteria.actual_value}")
                    print(f"     → 置信区间下界太低，策略收益不稳定")

    # 5. 总体结论
    print(f"\n{'=' * 70}")
    if result.validation_report and result.validation_report.overall_passed:
        print("✅ 结论：策略通过验证，可以考虑实盘使用")
    else:
        print("❌ 结论：策略未通过验证，需要优化参数或放弃使用")
    print(f"{'=' * 70}\n")

    return result


def demo_parameter_sensitivity(ts_code: str):
    """
    参数敏感性演示

    测试不同参数下策略是否稳定
    """
    print(f"\n{'=' * 70}")
    print(f"参数敏感性测试: {ts_code}")
    print(f"{'=' * 70}\n")

    configs = [
        ("默认参数", LoopConfig()),
        ("宽松止损", LoopConfig(stop_loss_pct=-0.10)),
        ("严格止损", LoopConfig(stop_loss_pct=-0.05)),
        ("宽松J值", LoopConfig(j_threshold=15)),
        ("严格J值", LoopConfig(j_threshold=8)),
    ]

    results = []
    for name, config in configs:
        result = backtest_shaofu_with_validation(
            ts_code=ts_code,
            days=500,
            config=config,
            validation_level=CriteriaLevel.LOOSE,
        )

        if result.trades:
            results.append((name, result))
            status = "✅" if result.validation_report and result.validation_report.overall_passed else "❌"
            print(f"{status} {name:10s}: 胜率{result.win_rate:.0%} 夏普{result.sharpe_ratio:.2f} 回撤{result.max_drawdown:.1%}")
        else:
            print(f"⚠️  {name:10s}: 无交易")

    # 分析稳定性
    if results:
        sharpes = [r.sharpe_ratio for _, r in results]
        if sharpes:
            avg_sharpe = sum(sharpes) / len(sharpes)
            sharpe_range = max(sharpes) - min(sharpes)
            print(f"\n夏普比率范围: [{min(sharpes):.2f}, {max(sharpes):.2f}], 波动: {sharpe_range:.2f}")

            if sharpe_range < 1.0:
                print("✅ 参数稳定性良好")
            else:
                print("⚠️  参数敏感性较高，需要谨慎使用")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 scripts/demo_validation.py <股票代码> [--sensitivity]")
        print("示例: python3 scripts/demo_validation.py 600519.SH")
        print("      python3 scripts/demo_validation.py 600519.SH --sensitivity")
        sys.exit(1)

    ts_code = sys.argv[1]
    sensitivity_mode = "--sensitivity" in sys.argv

    if sensitivity_mode:
        demo_parameter_sensitivity(ts_code)
    else:
        demo_single_stock(ts_code)
