#!/usr/bin/env python3
"""调试夏普比率计算"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.backtest_six_step import backtest_shaofu_single
from modules.statistics import sharpe_t_test


ts_code = "601318.SH"
days = 500

# 1. 基础回测
result = backtest_shaofu_single(ts_code, days=days)

print(f"基础夏普比率: {result.sharpe_ratio:.2f}")
print(f"资金曲线: {result.equity_curve}")
print()

# 2. 计算日收益率
equity_curve = result.equity_curve
daily_returns = []
for i in range(1, len(equity_curve)):
    if equity_curve[i - 1] > 0:
        daily_returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])

print(f"日收益率数量: {len(daily_returns)}")
print(f"日收益率: {daily_returns}")
print()

# 3. 手动计算夏普
if daily_returns:
    avg_ret = sum(daily_returns) / len(daily_returns)
    import math
    if len(daily_returns) > 1:
        variance = sum((r - avg_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std_ret = math.sqrt(variance)
        sharpe_manual = (avg_ret / std_ret) * math.sqrt(252) if std_ret > 0 else 0.0
        print(f"手动计算夏普: {sharpe_manual:.2f}")
        print(f"平均日收益: {avg_ret:.6f}")
        print(f"日收益标准差: {std_ret:.6f}")
    print()

# 4. 统计检验
test_result = sharpe_t_test(daily_returns)
print(f"统计检验夏普: {test_result.sharpe_ratio:.2f}")
print(f"t统计量: {test_result.t_statistic:.4f}")
print(f"p值: {test_result.p_value:.4f}")
print(f"标准误: {test_result.standard_error:.6f}")
print(f"CI: [{test_result.ci_lower:.2f}, {test_result.ci_upper:.2f}]")
