#!/usr/bin/env python3
"""
少妇战法优化工作流

完整的优化流程：
1. 批量验证多只股票
2. 找出适合少妇战法的股票
3. 参数敏感性分析
4. 策略集成优化
5. 生成优化报告
"""

import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.backtest_six_step import backtest_shaofu_with_validation, backtest_shaofu_single
from modules.statistics.criteria import CriteriaLevel, validate_strategy
from modules.statistics.sensitivity import analyze_all_parameters
from modules.statistics.ensemble import (
    backtest_with_ensemble,
    EnsembleConfig,
    EnsembleMethod,
)
from modules.loop_engine import LoopConfig


def phase1_batch_validation(ts_codes: list[str], days: int = 500) -> list[dict]:
    """
    Phase 1: 批量验证

    找出适合少妇战法的股票
    """
    print(f"\n{'=' * 80}")
    print(f"Phase 1: 批量验证 ({len(ts_codes)} 只股票, {days} 天)")
    print(f"{'=' * 80}\n")

    results = []

    for i, ts_code in enumerate(ts_codes, 1):
        print(f"[{i}/{len(ts_codes)}] {ts_code}...", end=" ", flush=True)

        try:
            result = backtest_shaofu_with_validation(
                ts_code=ts_code,
                days=days,
                validation_level=CriteriaLevel.MODERATE,
            )

            if not result.trades:
                print("❌ 无交易")
                continue

            passed = result.validation_report and result.validation_report.overall_passed

            status = "✅" if passed else "❌"
            print(
                f"{status} 交易{result.total_trades}笔 "
                f"胜率{result.win_rate:.0%} "
                f"盈亏比{result.profit_factor:.2f} "
                f"夏普{result.sharpe_ratio:.2f}"
            )

            results.append({
                "ts_code": ts_code,
                "result": result,
                "passed": passed,
                "win_rate": result.win_rate,
                "sharpe_ratio": result.sharpe_ratio,
                "total_return": result.total_return,
            })

        except Exception as e:
            print(f"⚠️  错误: {e}")

    # 汇总
    passed_results = [r for r in results if r["passed"]]
    print(f"\n✅ 通过: {len(passed_results)}/{len(results)}")

    return results


def phase2_parameter_sensitivity(ts_code: str, days: int = 500) -> dict:
    """
    Phase 2: 参数敏感性分析

    找出稳健的参数范围
    """
    print(f"\n{'=' * 80}")
    print(f"Phase 2: 参数敏感性分析 ({ts_code})")
    print(f"{'=' * 80}\n")

    base_config = LoopConfig()

    try:
        report = analyze_all_parameters(
            strategy_name=f"少妇战法-{ts_code}",
            base_config=base_config,
            ts_code=ts_code,
            days=days,
        )

        print(report.generate_summary())

        return {
            "ts_code": ts_code,
            "report": report,
            "is_robust": report.is_robust,
            "robust_params": report.robust_params,
            "sensitive_params": report.sensitive_params,
        }

    except Exception as e:
        print(f"⚠️  错误: {e}")
        return {"ts_code": ts_code, "is_robust": False}


def phase3_ensemble_optimization(ts_code: str, days: int = 500) -> dict:
    """
    Phase 3: 策略集成优化

    尝试不同的集成方法，提高胜率
    """
    print(f"\n{'=' * 80}")
    print(f"Phase 3: 策略集成优化 ({ts_code})")
    print(f"{'=' * 80}\n")

    methods = [
        ("基础(B1)", EnsembleConfig(method=EnsembleMethod.VOTING, min_signals=1)),
        ("投票(2信号)", EnsembleConfig(method=EnsembleMethod.VOTING, min_signals=2)),
        ("投票(3信号)", EnsembleConfig(method=EnsembleMethod.VOTING, min_signals=3)),
    ]

    results = []

    for name, config in methods:
        print(f"测试 {name}...", end=" ", flush=True)

        try:
            result = backtest_with_ensemble(
                ts_code=ts_code,
                days=days,
                config=config,
            )

            print(
                f"交易{result.total_trades}笔 "
                f"胜率{result.win_rate:.0%} "
                f"盈亏比{result.profit_factor:.2f} "
                f"信号数{result.signals_per_trade:.1f}"
            )

            results.append({
                "method": name,
                "config": config,
                "result": result,
                "win_rate": result.win_rate,
                "total_trades": result.total_trades,
            })

        except Exception as e:
            print(f"⚠️  错误: {e}")

    # 找出最佳方法
    if results:
        best = max(results, key=lambda x: x["win_rate"] if x["total_trades"] > 0 else 0)
        print(f"\n✅ 最佳方法: {best['method']} (胜率 {best['win_rate']:.0%})")
        return best
    else:
        return {"method": "无", "win_rate": 0.0}


def generate_optimization_report(
    batch_results: list[dict],
    sensitivity_results: list[dict],
    ensemble_results: list[dict],
) -> str:
    """
    生成完整的优化报告
    """
    lines = [
        f"{'=' * 80}",
        "少妇战法优化报告",
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"{'=' * 80}",
        "",
    ]

    # Phase 1 汇总
    lines.append("【Phase 1: 批量验证】")
    passed = [r for r in batch_results if r["passed"]]
    lines.append(f"  测试股票: {len(batch_results)} 只")
    lines.append(f"  通过验证: {len(passed)} 只")
    lines.append("")

    if passed:
        lines.append("  推荐股票:")
        for r in sorted(passed, key=lambda x: x["sharpe_ratio"], reverse=True)[:5]:
            lines.append(
                f"    {r['ts_code']}: "
                f"夏普{r['sharpe_ratio']:.2f} "
                f"胜率{r['win_rate']:.0%} "
                f"收益{r['total_return']:+.1%}"
            )
        lines.append("")

    # Phase 2 汇总
    lines.append("【Phase 2: 参数敏感性】")
    robust = [r for r in sensitivity_results if r.get("is_robust", False)]
    lines.append(f"  分析股票: {len(sensitivity_results)} 只")
    lines.append(f"  参数稳健: {len(robust)} 只")
    lines.append("")

    if robust:
        lines.append("  稳健参数:")
        for r in robust[:3]:
            lines.append(f"    {r['ts_code']}: {', '.join(r.get('robust_params', []))}")
        lines.append("")

    # Phase 3 汇总
    lines.append("【Phase 3: 策略集成】")
    improved = [r for r in ensemble_results if r.get("win_rate", 0) > 0]
    lines.append(f"  优化股票: {len(ensemble_results)} 只")
    lines.append(f"  胜率提升: {len(improved)} 只")
    lines.append("")

    if improved:
        lines.append("  最佳集成方法:")
        for r in improved[:3]:
            lines.append(f"    {r.get('ts_code', 'N/A')}: {r['method']} (胜率 {r['win_rate']:.0%})")
        lines.append("")

    # 总结
    lines.append(f"{'=' * 80}")
    lines.append("【优化建议】")
    lines.append("")

    if passed:
        lines.append(f"1. 推荐股票池: {', '.join(r['ts_code'] for r in passed[:5])}")
    else:
        lines.append("1. 推荐股票池: 无（所有股票都未通过验证）")

    lines.append("")
    lines.append("2. 参数建议:")
    lines.append("   - j_threshold: 12 (SOP 标准)")
    lines.append("   - stop_loss_pct: -0.07 (7% 止损)")
    lines.append("   - bbi_break_days: 2 (BBI 两日破位)")

    lines.append("")
    lines.append("3. 集成方法:")
    lines.append("   - 推荐使用投票法（2信号）")
    lines.append("   - 可以集成 B1 + B2 或 B1 + 长安")

    lines.append("")
    lines.append(f"{'=' * 80}")

    return "\n".join(lines)


def main():
    """主流程"""
    # 默认测试股票
    test_stocks = [
        "600519.SH",  # 贵州茅台
        "000858.SZ",  # 五粮液
        "002594.SZ",  # 比亚迪
        "600036.SH",  # 招商银行
        "601318.SH",  # 中国平安
    ]

    # 如果命令行传入了股票代码，使用命令行的
    if len(sys.argv) > 1:
        test_stocks = sys.argv[1:]

    print(f"\n少妇战法优化工作流")
    print(f"测试股票: {', '.join(test_stocks)}")
    print(f"回测周期: 500 天\n")

    # Phase 1: 批量验证
    batch_results = phase1_batch_validation(test_stocks, days=500)

    # Phase 2: 参数敏感性（只对通过验证的股票）
    passed_stocks = [r["ts_code"] for r in batch_results if r["passed"]]
    sensitivity_results = []
    if passed_stocks:
        print(f"\n对通过验证的股票进行参数敏感性分析: {', '.join(passed_stocks[:3])}")
        for ts_code in passed_stocks[:3]:  # 最多分析3只
            result = phase2_parameter_sensitivity(ts_code, days=500)
            sensitivity_results.append(result)

    # Phase 3: 策略集成（只对通过验证的股票）
    ensemble_results = []
    if passed_stocks:
        print(f"\n对通过验证的股票进行策略集成优化: {', '.join(passed_stocks[:3])}")
        for ts_code in passed_stocks[:3]:
            result = phase3_ensemble_optimization(ts_code, days=500)
            result["ts_code"] = ts_code
            ensemble_results.append(result)

    # 生成报告
    report = generate_optimization_report(batch_results, sensitivity_results, ensemble_results)
    print("\n" + report)

    # 保存报告
    report_file = Path(__file__).parent.parent / "reports" / f"optimization_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    report_file.parent.mkdir(exist_ok=True)
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n报告已保存: {report_file}")


if __name__ == "__main__":
    main()
