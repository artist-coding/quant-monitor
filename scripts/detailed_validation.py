#!/usr/bin/env python3
"""
详细验证分析

分析每只股票的验证报告，找出未通过的原因
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.backtest_six_step import backtest_shaofu_with_validation
from modules.statistics.criteria import CriteriaLevel


def detailed_analysis(ts_code: str, days: int = 500):
    """详细分析单只股票"""
    print(f"\n{'=' * 80}")
    print(f"详细验证分析: {ts_code}")
    print(f"{'=' * 80}\n")

    result = backtest_shaofu_with_validation(
        ts_code=ts_code,
        days=days,
        validation_level=CriteriaLevel.MODERATE,
    )

    if not result.trades:
        print(f"❌ {ts_code} 在 {days} 天内没有产生交易信号")
        return

    # 基础指标
    print("【基础绩效指标】")
    print(f"  总交易次数: {result.total_trades}")
    print(f"  胜率:       {result.win_rate:.1%}")
    print(f"  盈亏比:     {result.profit_factor:.2f}")
    print(f"  累计收益:   {result.total_return:+.2%}")
    print(f"  最大回撤:   {result.max_drawdown:.2%}")
    print(f"  夏普比率:   {result.sharpe_ratio:.2f}")
    print()

    # 详细验证报告
    if result.validation_report:
        print("【验证报告】")
        print(f"  总体结果: {'✅ 通过' if result.validation_report.overall_passed else '❌ 未通过'}")
        print(f"  达标项目: {sum(1 for r in result.validation_report.criteria_results if r.passed)}/{len(result.validation_report.criteria_results)}")
        print()

        print("【各项指标详情】")
        for criteria in result.validation_report.criteria_results:
            status = "✅" if criteria.passed else "❌"
            print(f"  {status} {criteria.name}:")
            print(f"     实际值: {criteria.actual_value}")
            print(f"     阈值: {criteria.threshold}")
            print(f"     说明: {criteria.message}")
            print()

    # 交易明细
    print("【交易明细】")
    for i, trade in enumerate(result.trades[-5:], 1):
        pnl = trade.pnl_pct
        marker = "+" if pnl > 0 else ""
        print(f"  {i}. {trade.entry_date} -> {trade.exit_date}: {marker}{pnl:.2f}%")
    print()

    return result


def compare_stocks(ts_codes: list[str], days: int = 500):
    """对比多只股票"""
    print(f"\n{'=' * 80}")
    print(f"股票对比分析 ({len(ts_codes)} 只)")
    print(f"{'=' * 80}\n")

    results = []

    for ts_code in ts_codes:
        result = backtest_shaofu_with_validation(
            ts_code=ts_code,
            days=days,
            validation_level=CriteriaLevel.LOOSE,  # 使用宽松模式
        )

        if result.trades:
            results.append({
                "ts_code": ts_code,
                "result": result,
                "win_rate": result.win_rate,
                "profit_factor": result.profit_factor,
                "sharpe_ratio": result.sharpe_ratio,
                "total_return": result.total_return,
            })

    # 排序并展示
    print("【按夏普比率排序】")
    for r in sorted(results, key=lambda x: x["sharpe_ratio"], reverse=True):
        result = r["result"]
        passed_count = 0
        total_count = 0
        if result.validation_report:
            passed_count = sum(1 for c in result.validation_report.criteria_results if c.passed)
            total_count = len(result.validation_report.criteria_results)

        print(
            f"  {r['ts_code']}: "
            f"夏普{r['sharpe_ratio']:.2f} "
            f"胜率{r['win_rate']:.0%} "
            f"盈亏比{r['profit_factor']:.2f} "
            f"收益{r['total_return']:+.1%} "
            f"达标{passed_count}/{total_count}"
        )

    print()

    # 分析最佳股票
    if results:
        best = max(results, key=lambda x: x["sharpe_ratio"])
        print(f"【最佳股票: {best['ts_code']}】")
        detailed_analysis(best["ts_code"], days)


if __name__ == "__main__":
    # 测试股票
    test_stocks = [
        "600519.SH",  # 贵州茅台
        "000858.SZ",  # 五粮液
        "601318.SH",  # 中国平安（之前看到夏普 1.95）
        "600036.SH",  # 招商银行
    ]

    if len(sys.argv) > 1:
        test_stocks = sys.argv[1:]

    # 详细分析每只股票
    for ts_code in test_stocks:
        detailed_analysis(ts_code, days=500)

    # 对比分析
    compare_stocks(test_stocks, days=500)
