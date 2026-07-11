#!/usr/bin/env python3
"""调试验证报告生成"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.backtest_six_step import backtest_shaofu_single, backtest_shaofu_with_validation
from modules.statistics.criteria import CriteriaLevel


ts_code = "601318.SH"
days = 500

# 1. 基础回测
print(f"运行基础回测: {ts_code}")
result = backtest_shaofu_single(ts_code, days=days)

print(f"交易次数: {result.total_trades}")
print(f"资金曲线长度: {len(result.equity_curve)}")
print(f"胜率: {result.win_rate:.1%}")
print(f"盈亏比: {result.profit_factor:.2f}")
print(f"夏普比率: {result.sharpe_ratio:.2f}")

# 2. 带验证的回测
print(f"\n运行带验证的回测...")
result2 = backtest_shaofu_with_validation(ts_code, days=days, validation_level=CriteriaLevel.MODERATE)

print(f"验证报告: {result2.validation_report}")
if result2.validation_report:
    print(f"达标项目: {sum(1 for r in result2.validation_report.criteria_results if r.passed)}/{len(result2.validation_report.criteria_results)}")
    print(result2.validation_report.generate_summary())
else:
    print("❌ 验证报告为 None")
    print(f"交易次数: {result2.total_trades}")
    print(f"资金曲线长度: {len(result2.equity_curve)}")
