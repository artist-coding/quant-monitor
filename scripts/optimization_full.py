#!/usr/bin/env python3
"""
少妇战法全面参数优化

优化目标:
1. 基础版: j_threshold, stop_loss_pct, vol_shrink_threshold, lu_zhu_days
2. 增强版: min_signals, min_signal_strength, strategy_weights, strategy_enable

优化方法:
- 网格搜索 (Grid Search)
- 评估函数: 综合胜率、夏普比率、最大回撤、交易频率

用法:
    python3 scripts/optimization_full.py                    # 全面优化
    python3 scripts/optimization_full.py --quick            # 快速扫描
    python3 scripts/optimization_full.py --stocks 20        # 指定股票数
"""

import sys
import json
import time
import sqlite3
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any
from itertools import product

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.loop_engine import ShaofuLoopEngine, LoopConfig
from modules.loop_engine_enhanced import EnhancedShaofuLoopEngine, EnhancedLoopConfig
from modules.backtest_six_step import ShaofuBacktestResult, _calc_metrics
from modules.indicators import DailyData


# ============================================================================
# 数据加载
# ============================================================================

def load_klines(ts_code: str, days: int = 500) -> list[DailyData]:
    """从数据库加载 K 线数据"""
    db_path = "data/stock_data.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT trade_date, open, high, low, close, vol, amount, pct_chg
        FROM daily_kline
        WHERE ts_code = ?
        ORDER BY trade_date DESC
        LIMIT ?
    """, (ts_code, days))

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return []

    klines = []
    for row in reversed(rows):
        klines.append(DailyData(
            ts_code=ts_code,
            trade_date=row[0],
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            vol=float(row[5]),
            amount=float(row[6]) if row[6] else 0,
            pct_chg=float(row[7]) if row[7] else 0.0,
        ))
    return klines


def get_optimization_stocks(count: int = 100) -> list[str]:
    """获取优化用股票列表"""
    stocks_file = Path("data/optimization_stocks.txt")
    if stocks_file.exists():
        with open(stocks_file) as f:
            return [line.strip() for line in f if line.strip()][:count]
    return []


# ============================================================================
# 评估函数
# ============================================================================

@dataclass
class OptimizationResult:
    """优化结果"""
    params: dict
    metrics: dict
    score: float = 0.0  # 综合得分


def evaluate_backtest(results: list[ShaofuBacktestResult]) -> dict:
    """评估一组回测结果"""
    if not results:
        return {
            "avg_win_rate": 0,
            "avg_return": 0,
            "avg_sharpe": 0,
            "avg_max_drawdown": 0,
            "total_trades": 0,
            "stocks_with_trades": 0,
        }

    total_trades = sum(r.total_trades for r in results)
    stocks_with_trades = sum(1 for r in results if r.total_trades > 0)

    if stocks_with_trades == 0:
        return {
            "avg_win_rate": 0,
            "avg_return": 0,
            "avg_sharpe": 0,
            "avg_max_drawdown": 0,
            "total_trades": 0,
            "stocks_with_trades": 0,
        }

    avg_wr = sum(r.win_rate for r in results if r.total_trades > 0) / stocks_with_trades
    avg_ret = sum(r.total_return for r in results if r.total_trades > 0) / stocks_with_trades
    avg_sharpe = sum(r.sharpe_ratio for r in results if r.total_trades > 0) / stocks_with_trades
    avg_dd = sum(abs(r.max_drawdown) for r in results if r.total_trades > 0) / stocks_with_trades

    return {
        "avg_win_rate": avg_wr,
        "avg_return": avg_ret,
        "avg_sharpe": avg_sharpe,
        "avg_max_drawdown": avg_dd,
        "total_trades": total_trades,
        "stocks_with_trades": stocks_with_trades,
    }


def calculate_score(metrics: dict) -> float:
    """计算综合得分

    权重:
    - 胜率: 40%
    - 夏普比率: 30%
    - 平均收益: 20%
    - 最大回撤: -10% (越小越好)
    """
    wr = metrics["avg_win_rate"]
    sharpe = metrics["avg_sharpe"]
    ret = metrics["avg_return"]
    dd = metrics["avg_max_drawdown"]

    # 归一化
    wr_score = min(wr / 0.6, 1.0)  # 60% 胜率满分
    sharpe_score = min(max(sharpe, 0) / 2.0, 1.0)  # 夏普 2.0 满分
    ret_score = min(max(ret, 0) / 0.5, 1.0)  # 50% 收益满分
    dd_score = max(0, 1 - dd / 0.3)  # 30% 回撤零分

    score = (
        0.40 * wr_score +
        0.30 * sharpe_score +
        0.20 * ret_score +
        0.10 * dd_score
    )

    return score * 100  # 百分制


# ============================================================================
# 基础版参数优化
# ============================================================================

# 基础版参数空间
BASE_PARAMS = {
    "j_threshold": [5, 8, 12, 15, 20],  # KDJ J值阈值
    "stop_loss_pct": [-0.03, -0.05, -0.07, -0.10],  # 止损百分比
    "vol_shrink_threshold": [0.5, 0.6, 0.7, 0.8],  # 缩量阈值
    "min_holding_days": [3, 5, 7, 10],  # 最小持有天数
    "bbi_break_days": [2, 3, 4],  # BBI 破位天数
    "bbi_break_threshold": [0.005, 0.01, 0.015],  # BBI 破位阈值
}


def optimize_basic(stocks: list[str], days: int = 500, quick: bool = False):
    """优化基础版参数"""
    print("\n" + "=" * 70)
    print("基础版参数优化")
    print("=" * 70)

    # 快速模式只用部分参数
    params = BASE_PARAMS.copy()
    if quick:
        params = {
            "j_threshold": [8, 12, 15],
            "stop_loss_pct": [-0.05, -0.07, -0.10],
            "vol_shrink_threshold": [0.6, 0.7, 0.8],
            "min_holding_days": [3, 5],
            "bbi_break_days": [2, 3],
            "bbi_break_threshold": [0.01],
        }

    # 计算参数组合数
    param_combinations = list(product(
        params["j_threshold"],
        params["stop_loss_pct"],
        params["vol_shrink_threshold"],
        params["min_holding_days"],
        params["bbi_break_days"],
        params["bbi_break_threshold"],
    ))
    print(f"参数组合数: {len(param_combinations)}")
    print(f"股票数量: {len(stocks)}")
    print(f"预计运行时间: {len(param_combinations) * len(stocks) * 0.1 / 60:.1f} 分钟\n")

    # 加载所有 K 线数据
    print("加载 K 线数据...")
    all_klines = {}
    for ts_code in stocks:
        klines = load_klines(ts_code, days)
        if klines and len(klines) >= 50:
            all_klines[ts_code] = klines
    print(f"成功加载 {len(all_klines)} 只股票\n")

    best_results = []

    for idx, (j_th, sl_pct, vol_th, min_hold, bbi_days, bbi_th) in enumerate(param_combinations, 1):
        config = LoopConfig(
            j_threshold=j_th,
            stop_loss_pct=sl_pct,
            vol_shrink_threshold=vol_th,
            min_holding_days=min_hold,
            bbi_break_days=bbi_days,
            bbi_break_threshold=bbi_th,
        )
        engine = ShaofuLoopEngine(config)

        results = []
        for ts_code, klines in all_klines.items():
            trades = engine.run_stock(klines, ts_code=ts_code)
            if trades:
                result = ShaofuBacktestResult(ts_code=ts_code, trades=trades)
                _calc_metrics(result)
                results.append(result)

        metrics = evaluate_backtest(results)
        score = calculate_score(metrics)

        params_dict = {
            "j_threshold": j_th,
            "stop_loss_pct": sl_pct,
            "vol_shrink_threshold": vol_th,
            "min_holding_days": min_hold,
            "bbi_break_days": bbi_days,
            "bbi_break_threshold": bbi_th,
        }

        opt_result = OptimizationResult(
            params=params_dict,
            metrics=metrics,
            score=score,
        )
        best_results.append(opt_result)

        if idx % 10 == 0 or idx == len(param_combinations):
            print(f"  [{idx}/{len(param_combinations)}] 当前最佳得分: {max(r.score for r in best_results):.2f}")

    # 排序并输出 Top 10
    best_results.sort(key=lambda x: x.score, reverse=True)

    print(f"\n{'=' * 70}")
    print("Top 10 基础版参数组合")
    print(f"{'=' * 70}\n")

    print(f"{'排名':<4} {'得分':<8} {'胜率':<8} {'夏普':<8} {'收益':<10} {'回撤':<8} {'交易':<6} J阈值 止损   缩量  持有 BBI天 BBI阈值")
    print("-" * 100)

    for i, r in enumerate(best_results[:10], 1):
        p = r.params
        print(f"{i:<4} {r.score:<8.2f} {r.metrics['avg_win_rate']:<7.1%} "
              f"{r.metrics['avg_sharpe']:<8.2f} {r.metrics['avg_return']:<+9.1%} "
              f"{r.metrics['avg_max_drawdown']:<7.1%} {r.metrics['total_trades']:<6} "
              f"{p['j_threshold']:<6} {p['stop_loss_pct']:<5.0%} {p['vol_shrink_threshold']:<5.1f} "
              f"{p['min_holding_days']:<5} {p['bbi_break_days']:<5} {p['bbi_break_threshold']:<6}")

    return best_results[:10]


# ============================================================================
# 增强版参数优化
# ============================================================================

ENHANCED_PARAMS = {
    "min_signals": [1, 2, 3],
    "min_signal_strength": [1.0, 1.5, 2.0, 2.5],
    "enable_b2": [True, False],
    "enable_changan": [True, False],
    "enable_nana": [True, False],
    "enable_pinghang": [True, False],
}


def optimize_enhanced(stocks: list[str], days: int = 500, quick: bool = False):
    """优化增强版参数"""
    print("\n" + "=" * 70)
    print("增强版参数优化")
    print("=" * 70)

    params = ENHANCED_PARAMS.copy()
    if quick:
        params = {
            "min_signals": [1, 2],
            "min_signal_strength": [1.0, 1.5, 2.0],
            "enable_b2": [True, False],
            "enable_changan": [True, False],
            "enable_nana": [False],  # 固定简化
            "enable_pinghang": [False],
        }

    param_combinations = list(product(
        params["min_signals"],
        params["min_signal_strength"],
        params["enable_b2"],
        params["enable_changan"],
        params["enable_nana"],
        params["enable_pinghang"],
    ))
    print(f"参数组合数: {len(param_combinations)}")
    print(f"股票数量: {len(stocks)}")
    print(f"预计运行时间: {len(param_combinations) * len(stocks) * 0.15 / 60:.1f} 分钟\n")

    # 加载所有 K 线数据
    print("加载 K 线数据...")
    all_klines = {}
    for ts_code in stocks:
        klines = load_klines(ts_code, days)
        if klines and len(klines) >= 50:
            all_klines[ts_code] = klines
    print(f"成功加载 {len(all_klines)} 只股票\n")

    best_results = []

    for idx, (min_sig, min_str, b2, changan, nana, pinghang) in enumerate(param_combinations, 1):
        config = EnhancedLoopConfig(
            min_signals=min_sig,
            min_signal_strength=min_str,
            enable_b2=b2,
            enable_changan=changan,
            enable_nana=nana,
            enable_pinghang=pinghang,
        )
        engine = EnhancedShaofuLoopEngine(config)

        results = []
        for ts_code, klines in all_klines.items():
            trades = engine.run_stock(klines, ts_code=ts_code)
            if trades:
                result = ShaofuBacktestResult(ts_code=ts_code, trades=trades)
                _calc_metrics(result)
                results.append(result)

        metrics = evaluate_backtest(results)
        score = calculate_score(metrics)

        params_dict = {
            "min_signals": min_sig,
            "min_signal_strength": min_str,
            "enable_b2": b2,
            "enable_changan": changan,
            "enable_nana": nana,
            "enable_pinghang": pinghang,
        }

        opt_result = OptimizationResult(
            params=params_dict,
            metrics=metrics,
            score=score,
        )
        best_results.append(opt_result)

        if idx % 10 == 0 or idx == len(param_combinations):
            print(f"  [{idx}/{len(param_combinations)}] 当前最佳得分: {max(r.score for r in best_results):.2f}")

    # 排序并输出 Top 10
    best_results.sort(key=lambda x: x.score, reverse=True)

    print(f"\n{'=' * 70}")
    print("Top 10 增强版参数组合")
    print(f"{'=' * 70}\n")

    print(f"{'排名':<4} {'得分':<8} {'胜率':<8} {'夏普':<8} {'收益':<10} {'回撤':<8} {'交易':<6} min_sig  str   B2   长安  娜娜  平行")
    print("-" * 100)

    for i, r in enumerate(best_results[:10], 1):
        p = r.params
        b2_s = "✓" if p['enable_b2'] else "✗"
        ch_s = "✓" if p['enable_changan'] else "✗"
        na_s = "✓" if p['enable_nana'] else "✗"
        ph_s = "✓" if p['enable_pinghang'] else "✗"
        print(f"{i:<4} {r.score:<8.2f} {r.metrics['avg_win_rate']:<7.1%} "
              f"{r.metrics['avg_sharpe']:<8.2f} {r.metrics['avg_return']:<+9.1%} "
              f"{r.metrics['avg_max_drawdown']:<7.1%} {r.metrics['total_trades']:<6} "
              f"{p['min_signals']:<7} {p['min_signal_strength']:<4.1f} {b2_s:>3} {ch_s:>4} {na_s:>4} {ph_s:>4}")

    return best_results[:10]


# ============================================================================
# 主函数
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="少妇战法全面参数优化")
    parser.add_argument("--quick", action="store_true", help="快速扫描模式")
    parser.add_argument("--stocks", type=int, default=100, help="优化股票数量")
    parser.add_argument("--days", type=int, default=500, help="回测天数")
    parser.add_argument("--output", default="reports/optimization_results.json", help="输出文件")
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("少妇战法全面参数优化")
    print("=" * 70)
    print(f"  模式: {'快速扫描' if args.quick else '全面优化'}")
    print(f"  股票数: {args.stocks}")
    print(f"  回测天数: {args.days}")

    # 获取股票列表
    stocks = get_optimization_stocks(args.stocks)
    if not stocks:
        print("❌ 未找到优化股票列表")
        return

    # 1. 优化基础版
    basic_results = optimize_basic(stocks, args.days, args.quick)

    # 2. 优化增强版
    enhanced_results = optimize_enhanced(stocks, args.days, args.quick)

    # 3. 保存结果
    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stocks_count": len(stocks),
        "days": args.days,
        "quick_mode": args.quick,
        "basic_top10": [
            {
                "rank": i + 1,
                "score": r.score,
                "params": r.params,
                "metrics": r.metrics,
            }
            for i, r in enumerate(basic_results)
        ],
        "enhanced_top10": [
            {
                "rank": i + 1,
                "score": r.score,
                "params": r.params,
                "metrics": r.metrics,
            }
            for i, r in enumerate(enhanced_results)
        ],
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 70}")
    print(f"✅ 优化完成!")
    print(f"{'=' * 70}")
    print(f"  结果保存: {output_path}")
    print()


if __name__ == "__main__":
    main()
