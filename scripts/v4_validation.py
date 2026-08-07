#!/usr/bin/env python3
"""v4.0.0 验收回测脚本 - 用真实数据验证系统能否赚钱"""
import sys, json, time
from pathlib import Path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from modules.database import get_connection
from modules.simulator import run_simulation, SimulationConfig, CostModel


def get_top_stocks(n=30):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT d.ts_code, s.name, COUNT(*) as cnt
            FROM daily_kline d JOIN stock_basic s ON d.ts_code = s.ts_code
            GROUP BY d.ts_code HAVING cnt >= 400 ORDER BY cnt DESC LIMIT ?
        """, (n,))
        return cur.fetchall()


def main():
    stocks = get_top_stocks(30)
    print(f"验证股票池: {len(stocks)} 只")
    print("=" * 80)

    config = SimulationConfig(
        initial_capital=1_000_000.0,
        max_positions=5,
        risk_per_trade=0.02,
        use_atr_sizing=True,
        max_position_pct=0.15,
        strategy_mode="resonance",
        min_resonance_score=0.35,
        position_score_threshold=70.0,
        signal_min_count=2,
        use_dynamic_slippage=True,
        cost_model=CostModel(),
        benchmark_code="000300.SH",
        allow_st=False,
        t1_lock=True,
        apply_price_limit=True,
        apply_halt_filter=True,
    )

    results = []
    total_start = time.time()

    for i, (ts_code, name, cnt) in enumerate(stocks):
        t0 = time.time()
        try:
            result = run_simulation(ts_codes=[ts_code], days=cnt, config=config)
            elapsed = time.time() - t0
            m = result.metrics
            entry = {
                "ts_code": ts_code, "name": name, "kline_days": cnt,
                "total_return": result.total_return,
                "max_drawdown": result.max_drawdown,
                "sharpe_ratio": result.sharpe_ratio,
                "win_rate": result.win_rate,
                "total_trades": result.total_trades,
                "avg_holding_days": result.avg_holding_days,
                "profit_factor": result.profit_factor,
                "annualized_return": m.annualized_return if m else 0,
                "calmar_ratio": m.calmar_ratio if m else 0,
                "benchmark_return": m.benchmark_return if m else 0,
                "sortino_ratio": m.sortino_ratio if m else 0,
                "elapsed": round(elapsed, 1),
            }
            results.append(entry)
            print(f"[{i+1:2d}/{len(stocks)}] {ts_code} {name:<8} return={result.total_return:+.2%} "
                  f"sharpe={result.sharpe_ratio:.2f} dd={result.max_drawdown:.2%} "
                  f"wr={result.win_rate:.1%} trades={result.total_trades:3d} ({elapsed:.1f}s)")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"[{i+1:2d}/{len(stocks)}] {ts_code} {name:<8} ERROR: {e} ({elapsed:.1f}s)")
            results.append({"ts_code": ts_code, "name": name, "error": str(e), "elapsed": round(elapsed, 1)})

    total_elapsed = time.time() - total_start
    print("\n" + "=" * 80)
    print(f"总耗时: {total_elapsed:.0f}s ({total_elapsed/60:.1f}分钟)")
    print("=" * 80)

    valid = [r for r in results if "error" not in r]
    traded = [r for r in valid if r["total_trades"] > 0]
    no_trade = [r for r in valid if r["total_trades"] == 0]
    errors = [r for r in results if "error" in r]

    print(f"\n总股票: {len(stocks)}  有交易: {len(traded)}  无交易: {len(no_trade)}  出错: {len(errors)}")

    if not traded:
        print("\n*** 没有产生任何交易！策略信号完全未触发。***")
        print("\n这比亏钱更严重——说明策略逻辑有根本性问题。")
        _save(results, stocks)
        return

    avg_sharpe = sum(r["sharpe_ratio"] for r in traded) / len(traded)
    avg_dd = sum(r["max_drawdown"] for r in traded) / len(traded)
    avg_wr = sum(r["win_rate"] for r in traded) / len(traded)
    avg_ret = sum(r["total_return"] for r in traded) / len(traded)
    avg_bench = sum(r["benchmark_return"] for r in traded) / len(traded)
    avg_calmar = sum(r["calmar_ratio"] for r in traded) / len(traded)
    beat_bench = sum(1 for r in traded if r["total_return"] > r["benchmark_return"])
    profitable = sum(1 for r in traded if r["total_return"] > 0)

    print("\n" + "=" * 80)
    print("v4.0.0 验收标准对比（基于有交易的股票均值）")
    print("=" * 80)
    print(f"  1. 夏普 > 0.5:         平均 {avg_sharpe:.2f}  {'✅ PASS' if avg_sharpe > 0.5 else '❌ FAIL'}")
    print(f"  2. 最大回撤 < 15%:     平均 {avg_dd:.2%}  {'✅ PASS' if avg_dd < 0.15 else '❌ FAIL'}")
    print(f"  3. 跑赢沪深300:        {beat_bench}/{len(traded)} 只跑赢  收益 {avg_ret:+.2%} vs 基准 {avg_bench:+.2%}  {'✅ PASS' if avg_ret > avg_bench else '❌ FAIL'}")
    print(f"  4. 胜率 > 40%:         平均 {avg_wr:.1%}  {'✅ PASS' if avg_wr > 0.40 else '❌ FAIL'}")
    print(f"  5. Walk-forward OOS/IS > 0.6:  未测（先验证基础性能）")
    print()
    print(f"  附加指标:")
    print(f"    Calmar 比率:         {avg_calmar:.2f}")
    print(f"    盈利股票占比:        {profitable}/{len(traded)} ({profitable/len(traded):.0%})")
    print(f"    平均交易笔数:        {sum(r['total_trades'] for r in traded)/len(traded):.1f}")
    print(f"    平均持仓天数:        {sum(r['avg_holding_days'] for r in traded)/len(traded):.1f}")

    pass_count = sum([
        avg_sharpe > 0.5,
        avg_dd < 0.15,
        avg_ret > avg_bench,
        avg_wr > 0.40,
    ])
    print(f"\n  通过: {pass_count}/4 项")
    if pass_count == 4:
        print("  结论: 基础性能达标，建议继续跑 walk-forward 验证过拟合")
    elif pass_count >= 2:
        print("  结论: 部分达标，需要针对性修复（见下方分析）")
    else:
        print("  结论: 基础性能不达标，建议暂停新功能，先修策略")

    _save(results, stocks)


def _save(results, stocks):
    report_dir = Path("data/reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path = report_dir / "v4_validation.json"
    with open(out_path, "w") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stock_count": len(stocks),
            "results": results,
        }, f, indent=2, ensure_ascii=False)
    print(f"\n详细结果已保存: {out_path}")


if __name__ == "__main__":
    main()
