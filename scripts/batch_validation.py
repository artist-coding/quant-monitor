#!/usr/bin/env python3
"""
批量验证多只股票的少妇战法有效性

找出适合少妇战法的股票
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.backtest_six_step import backtest_shaofu_with_validation
from modules.statistics.criteria import CriteriaLevel


def batch_validation(ts_codes: list[str], days: int = 500):
    """
    批量验证多只股票

    Args:
        ts_codes: 股票代码列表
        days: 回测天数
    """
    print(f"\n{'=' * 80}")
    print(f"少妇战法批量验证: {len(ts_codes)} 只股票")
    print(f"回测周期: {days} 天")
    print(f"{'=' * 80}\n")

    results = []

    for i, ts_code in enumerate(ts_codes, 1):
        print(f"[{i}/{len(ts_codes)}] 验证 {ts_code}...", end=" ")

        try:
            result = backtest_shaofu_with_validation(
                ts_code=ts_code,
                days=days,
                validation_level=CriteriaLevel.MODERATE,
            )

            if not result.trades:
                print("❌ 无交易")
                continue

            # 判断是否通过验证
            passed = False
            if result.validation_report:
                passed = result.validation_report.overall_passed

            status = "✅ 通过" if passed else "❌ 未通过"
            print(
                f"{status} | "
                f"交易{result.total_trades}笔 "
                f"胜率{result.win_rate:.0%} "
                f"盈亏比{result.profit_factor:.2f} "
                f"收益{result.total_return:+.1%} "
                f"夏普{result.sharpe_ratio:.2f}"
            )

            results.append({
                "ts_code": ts_code,
                "result": result,
                "passed": passed,
            })

        except Exception as e:
            print(f"⚠️  错误: {e}")

    # 汇总报告
    print(f"\n{'=' * 80}")
    print("汇总报告")
    print(f"{'=' * 80}\n")

    passed_results = [r for r in results if r["passed"]]
    failed_results = [r for r in results if not r["passed"]]

    print(f"✅ 通过验证: {len(passed_results)} 只")
    print(f"❌ 未通过:   {len(failed_results)} 只")
    print(f"⚠️  无交易:   {len(ts_codes) - len(results)} 只\n")

    if passed_results:
        print("【推荐股票】")
        for r in sorted(passed_results, key=lambda x: x["result"].sharpe_ratio, reverse=True):
            result = r["result"]
            print(
                f"  {r['ts_code']}: "
                f"夏普{result.sharpe_ratio:.2f} "
                f"胜率{result.win_rate:.0%} "
                f"盈亏比{result.profit_factor:.2f} "
                f"收益{result.total_return:+.1%}"
            )

    print(f"\n{'=' * 80}")

    return results


if __name__ == "__main__":
    # 测试一组常见股票
    test_stocks = [
        "600519.SH",  # 贵州茅台（大盘蓝筹）
        "000858.SZ",  # 五粮液
        "002594.SZ",  # 比亚迪
        "600036.SH",  # 招商银行
        "601318.SH",  # 中国平安
        "000001.SZ",  # 平安银行
        "600276.SH",  # 恒瑞医药
        "002415.SZ",  # 海康威视
        "300750.SZ",  # 宁德时代
        "600900.SH",  # 长江电力
    ]

    # 如果命令行传入了股票代码，使用命令行的
    if len(sys.argv) > 1:
        test_stocks = sys.argv[1:]

    batch_validation(test_stocks, days=500)
