#!/usr/bin/env python3
"""
高效参数优化 v2

优化策略:
1. 分阶段优化: 先调关键参数, 再精细调优
2. 随机搜索 + 网格搜索混合
3. 使用子集股票快速评估

用法:
    python3 scripts/optimization_v2.py                    # 完整优化
    python3 scripts/optimization_v2.py --quick            # 快速版
"""

import sys
import json
import time
import sqlite3
import random
import argparse
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.loop_engine import ShaofuLoopEngine, LoopConfig
from modules.loop_engine_enhanced import EnhancedShaofuLoopEngine, EnhancedLoopConfig
from modules.backtest_six_step import ShaofuBacktestResult, _calc_metrics
from modules.indicators import DailyData


# ============================================================================
# 数据加载
# ============================================================================

def load_klines_batch(ts_codes: list[str], days: int = 500) -> dict[str, list[DailyData]]:
    """批量加载 K 线数据"""
    db_path = "data/stock_data.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    result = {}
    for ts_code in ts_codes:
        cursor.execute("""
            SELECT trade_date, open, high, low, close, vol, amount, pct_chg
            FROM daily_kline
            WHERE ts_code = ?
            ORDER BY trade_date DESC
            LIMIT ?
        """, (ts_code, days))

        rows = cursor.fetchall()
        if not rows or len(rows) < 50:
            continue

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
        result[ts_code] = klines

    conn.close()
    return result


def get_stocks(count: int = 50) -> list[str]:
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
class ParamScore:
    params: dict
    win_rate: float
    total_return: float
    sharpe: float
    max_dd: float
    trades: int
    score: float


def eval_results(results: list[ShaofuBacktestResult]) -> dict:
    """评估回测结果集"""
    valid = [r for r in results if r.total_trades > 0]
    if not valid:
        return {"wr": 0, "ret": 0, "sharpe": 0, "dd": 0, "trades": 0, "stocks": 0}

    # 聚合所有交易
    all_pnls = []
    equity = 100.0
    peak = 100.0
    max_dd = 0.0
    for r in valid:
        for t in r.trades:
            all_pnls.append(t.pnl_pct)
            equity *= 1 + t.pnl_pct / 100.0
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd

    wins = [p for p in all_pnls if p > 0]
    total_return = (equity / 100.0) - 1.0
    wr = len(wins) / len(all_pnls) if all_pnls else 0

    # 夏普比率（交易级别）
    if len(all_pnls) >= 3:
        avg_ret = sum(all_pnls) / len(all_pnls)
        var = sum((r - avg_ret) ** 2 for r in all_pnls) / (len(all_pnls) - 1)
        std = var ** 0.5 if var > 0 else 0
        sharpe = (avg_ret / std) * (252 / 10) ** 0.5 if std > 0 else 0
    else:
        sharpe = 0

    return {
        "wr": wr,
        "ret": total_return,
        "sharpe": sharpe,
        "dd": max_dd,
        "trades": len(all_pnls),
        "stocks": len(valid),
    }


def score_metrics(m: dict) -> float:
    """综合评分"""
    # 胜率权重 35%
    wr_s = min(m["wr"] / 0.5, 1.0) * 35
    # 收益权重 35%
    ret_s = min(max(m["ret"], 0) / 0.5, 1.0) * 35
    # 回撤惩罚 15%
    dd_s = max(0, 1 - m["dd"] / 0.25) * 15
    # 交易频率奖励 10%
    tr_s = min(m["trades"] / 500, 1.0) * 10
    # 夏普比率奖励 5%
    sh_s = min(max(m["sharpe"], 0) / 2.0, 1.0) * 5
    return wr_s + ret_s + dd_s + tr_s + sh_s


# ============================================================================
# 阶段 1: 关键参数优化 (J 阈值 + 止损)
# ============================================================================

def phase1_key_params(all_klines: dict, iterations: int = 100):
    """阶段 1: 只优化 J 阈值和止损百分比"""
    print("\n" + "=" * 70)
    print("阶段 1: 关键参数优化 (J 阈值 + 止损)")
    print("=" * 70)

    # 参数空间
    j_values = [5, 8, 10, 12, 15, 18, 20, 25]
    sl_values = [-0.03, -0.05, -0.07, -0.10, -0.15]
    vol_values = [0.5, 0.6, 0.7, 0.8]

    best = []
    combos = [(j, sl, v) for j in j_values for sl in sl_values for v in vol_values]
    print(f"参数组合数: {len(combos)}")

    for idx, (j_th, sl_pct, vol_th) in enumerate(combos, 1):
        config = LoopConfig(
            j_threshold=j_th,
            stop_loss_pct=sl_pct,
            vol_shrink_threshold=vol_th,
        )
        engine = ShaofuLoopEngine(config)

        results = []
        for ts_code, klines in all_klines.items():
            trades = engine.run_stock(klines, ts_code=ts_code)
            if trades:
                r = ShaofuBacktestResult(ts_code=ts_code, trades=trades)
                _calc_metrics(r)
                results.append(r)

        m = eval_results(results)
        s = score_metrics(m)

        best.append(ParamScore(
            params={"j_threshold": j_th, "stop_loss_pct": sl_pct, "vol_shrink": vol_th},
            win_rate=m["wr"],
            total_return=m["ret"],
            sharpe=m["sharpe"],
            max_dd=m["dd"],
            trades=m["trades"],
            score=s,
        ))

        if idx % 20 == 0 or idx == len(combos):
            print(f"  [{idx}/{len(combos)}] 最佳: {max(b.score for b in best):.2f}")

    best.sort(key=lambda x: x.score, reverse=True)
    return best


# ============================================================================
# 阶段 2: 精细调优 (固定 J/SL, 调其他参数)
# ============================================================================

def phase2_fine_tune(all_klines: dict, best_p1: list[ParamScore]):
    """阶段 2: 固定 J/SL/Vol, 调 min_holding_days, bbi_break_days, bbi_break_threshold"""
    print("\n" + "=" * 70)
    print("阶段 2: 精细调优")
    print("=" * 70)

    # 用阶段 1 的最佳参数
    top = best_p1[0].params
    print(f"使用阶段 1 最佳: J={top['j_threshold']}, SL={top['stop_loss_pct']}, Vol={top['vol_shrink']}")

    hold_values = [2, 3, 5, 7, 10]
    bbi_days_values = [1, 2, 3, 4]
    bbi_th_values = [0.005, 0.01, 0.015, 0.02]

    best = []
    combos = [(h, bd, bt) for h in hold_values for bd in bbi_days_values for bt in bbi_th_values]
    print(f"参数组合数: {len(combos)}")

    for idx, (hold, bbi_d, bbi_t) in enumerate(combos, 1):
        config = LoopConfig(
            j_threshold=top["j_threshold"],
            stop_loss_pct=top["stop_loss_pct"],
            vol_shrink_threshold=top["vol_shrink"],
            min_holding_days=hold,
            bbi_break_days=bbi_d,
            bbi_break_threshold=bbi_t,
        )
        engine = ShaofuLoopEngine(config)

        results = []
        for ts_code, klines in all_klines.items():
            trades = engine.run_stock(klines, ts_code=ts_code)
            if trades:
                r = ShaofuBacktestResult(ts_code=ts_code, trades=trades)
                _calc_metrics(r)
                results.append(r)

        m = eval_results(results)
        s = score_metrics(m)

        best.append(ParamScore(
            params={
                "j_threshold": top["j_threshold"],
                "stop_loss_pct": top["stop_loss_pct"],
                "vol_shrink": top["vol_shrink"],
                "min_holding_days": hold,
                "bbi_break_days": bbi_d,
                "bbi_break_threshold": bbi_t,
            },
            win_rate=m["wr"],
            total_return=m["ret"],
            sharpe=m["sharpe"],
            max_dd=m["dd"],
            trades=m["trades"],
            score=s,
        ))

        if idx % 10 == 0 or idx == len(combos):
            print(f"  [{idx}/{len(combos)}] 最佳: {max(b.score for b in best):.2f}")

    best.sort(key=lambda x: x.score, reverse=True)
    return best


# ============================================================================
# 阶段 3: 增强版优化
# ============================================================================

def phase3_enhanced(all_klines: dict):
    """阶段 3: 增强版参数优化"""
    print("\n" + "=" * 70)
    print("阶段 3: 增强版参数优化")
    print("=" * 70)

    # 参数空间 (精简)
    min_signals_vals = [1, 2]
    min_strength_vals = [1.0, 1.2, 1.5, 2.0]
    enable_combos = [
        (True, False, False, False),   # 仅 B1
        (True, True, False, False),    # B1 + B2
        (True, False, True, False),    # B1 + 长安
        (True, True, True, False),     # B1 + B2 + 长安
        (True, True, True, True),      # 全部启用
    ]

    combos = [(ms, mst, e) for ms in min_signals_vals for mst in min_strength_vals for e in enable_combos]
    print(f"参数组合数: {len(combos)}")

    best = []
    for idx, (ms, mst, (b2, ch, na, ph)) in enumerate(combos, 1):
        config = EnhancedLoopConfig(
            min_signals=ms,
            min_signal_strength=mst,
            enable_b2=b2,
            enable_changan=ch,
            enable_nana=na,
            enable_pinghang=ph,
        )
        engine = EnhancedShaofuLoopEngine(config)

        results = []
        for ts_code, klines in all_klines.items():
            trades = engine.run_stock(klines, ts_code=ts_code)
            if trades:
                r = ShaofuBacktestResult(ts_code=ts_code, trades=trades)
                _calc_metrics(r)
                results.append(r)

        m = eval_results(results)
        s = score_metrics(m)

        best.append(ParamScore(
            params={
                "min_signals": ms,
                "min_signal_strength": mst,
                "enable_b2": b2,
                "enable_changan": ch,
                "enable_nana": na,
                "enable_pinghang": ph,
            },
            win_rate=m["wr"],
            total_return=m["ret"],
            sharpe=m["sharpe"],
            max_dd=m["dd"],
            trades=m["trades"],
            score=s,
        ))

        if idx % 5 == 0 or idx == len(combos):
            print(f"  [{idx}/{len(combos)}] 最佳: {max(b.score for b in best):.2f}")

    best.sort(key=lambda x: x.score, reverse=True)
    return best


# ============================================================================
# 主函数
# ============================================================================

def print_top_results(title: str, results: list[ParamScore], n: int = 10):
    """打印 Top N 结果"""
    print(f"\n{'=' * 70}")
    print(f"Top {n} {title}")
    print(f"{'=' * 70}\n")
    print(f"{'排名':<4} {'得分':<8} {'胜率':<8} {'收益':<10} {'夏普':<8} {'回撤':<8} {'交易':<8} 参数")
    print("-" * 90)
    for i, r in enumerate(results[:n], 1):
        params_str = ", ".join(f"{k}={v}" for k, v in r.params.items())
        print(f"{i:<4} {r.score:<8.2f} {r.win_rate:<7.1%} {r.total_return:<+9.1%} "
              f"{r.sharpe:<8.2f} {r.max_dd:<7.1%} {r.trades:<8} {params_str}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--stocks", type=int, default=50)
    parser.add_argument("--days", type=int, default=500)
    args = parser.parse_args()

    stock_count = 20 if args.quick else args.stocks

    print("\n" + "=" * 70)
    print("高效参数优化 v2")
    print("=" * 70)
    print(f"  股票数: {stock_count}")
    print(f"  回测天数: {args.days}")

    # 加载数据
    print("\n加载 K 线数据...")
    stocks = get_stocks(stock_count)
    all_klines = load_klines_batch(stocks, args.days)
    print(f"成功加载 {len(all_klines)} 只股票\n")

    # 阶段 1: 关键参数
    p1_results = phase1_key_params(all_klines)
    print_top_results("基础版 - 阶段 1 (J/SL/Vol)", p1_results)

    # 阶段 2: 精细调优
    p2_results = phase2_fine_tune(all_klines, p1_results)
    print_top_results("基础版 - 阶段 2 (精细)", p2_results)

    # 阶段 3: 增强版
    p3_results = phase3_enhanced(all_klines)
    print_top_results("增强版", p3_results)

    # 保存结果
    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stocks": stock_count,
        "days": args.days,
        "phase1_top10": [
            {"rank": i + 1, "score": r.score, "params": r.params,
             "wr": r.win_rate, "ret": r.total_return, "sharpe": r.sharpe,
             "dd": r.max_dd, "trades": r.trades}
            for i, r in enumerate(p1_results[:10])
        ],
        "phase2_top10": [
            {"rank": i + 1, "score": r.score, "params": r.params,
             "wr": r.win_rate, "ret": r.total_return, "sharpe": r.sharpe,
             "dd": r.max_dd, "trades": r.trades}
            for i, r in enumerate(p2_results[:10])
        ],
        "phase3_top10": [
            {"rank": i + 1, "score": r.score, "params": r.params,
             "wr": r.win_rate, "ret": r.total_return, "sharpe": r.sharpe,
             "dd": r.max_dd, "trades": r.trades}
            for i, r in enumerate(p3_results[:10])
        ],
    }

    output_path = Path("reports/optimization_v2_results.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 70}")
    print(f"✅ 优化完成! 结果保存: {output_path}")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
