#!/usr/bin/env python3
"""
少妇战法真实数据测试

从数据库中选择股票，运行基础版和增强版回测，对比结果
"""

import sys
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3
from modules.loop_engine import ShaofuLoopEngine, LoopConfig
from modules.loop_engine_enhanced import EnhancedShaofuLoopEngine, EnhancedLoopConfig
from modules.backtest_six_step import ShaofuBacktestResult, _calc_metrics
from modules.indicators import DailyData


def get_test_stocks(db_path: str, count: int = 15) -> list[str]:
    """从数据库中选择测试股票"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 选择有足够数据的股票（至少 250 天）
    cursor.execute("""
        SELECT ts_code, COUNT(*) as cnt
        FROM daily_kline
        GROUP BY ts_code
        HAVING cnt >= 250
        ORDER BY RANDOM()
        LIMIT ?
    """, (count,))

    stocks = [row[0] for row in cursor.fetchall()]
    conn.close()

    return stocks


def load_klines(ts_code: str, days: int = 500) -> list[DailyData]:
    """从数据库加载 K 线数据"""
    import os
    db_path = os.getenv('DB_PATH', 'data/stock_data.db')
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

    # 转换为 DailyData 对象（按日期升序）
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
            amount=float(row[6]),
            pct_chg=float(row[7]) if row[7] is not None else 0.0,
        ))

    return klines


def run_backtest(ts_code: str, days: int = 500):
    """运行单只股票的回测"""
    # 加载数据
    klines = load_klines(ts_code, days)
    if not klines or len(klines) < 50:
        return None, None

    # 基础版
    base_engine = ShaofuLoopEngine(LoopConfig())
    base_trades = base_engine.run_stock(klines, ts_code=ts_code)

    base_result = None
    if base_trades:
        base_result = ShaofuBacktestResult(ts_code=ts_code, trades=base_trades)
        _calc_metrics(base_result)

    # 增强版
    enhanced_config = EnhancedLoopConfig(
        enable_b2=True,
        enable_changan=True,
        enable_nana=True,
        enable_pinghang=True,
        min_signals=2,
    )
    enhanced_engine = EnhancedShaofuLoopEngine(enhanced_config)
    enhanced_trades = enhanced_engine.run_stock(klines, ts_code=ts_code)

    enhanced_result = None
    if enhanced_trades:
        enhanced_result = ShaofuBacktestResult(ts_code=ts_code, trades=enhanced_trades)
        _calc_metrics(enhanced_result)

    return base_result, enhanced_result


def main():
    """主函数"""
    print("\n" + "=" * 80)
    print("少妇战法真实数据测试")
    print("=" * 80 + "\n")

    # 1. 获取测试股票
    db_path = Path('data/stock_data.db')
    if not db_path.exists():
        print("❌ 数据库不存在")
        return

    print("【Step 1】选择测试股票...")
    test_stocks = get_test_stocks(str(db_path), count=15)
    print(f"  选择了 {len(test_stocks)} 只股票")
    for i, stock in enumerate(test_stocks, 1):
        print(f"  {i}. {stock}")

    # 2. 运行回测
    print(f"\n【Step 2】运行回测（500 天）...\n")

    results = []
    for i, ts_code in enumerate(test_stocks, 1):
        print(f"[{i}/{len(test_stocks)}] {ts_code}...", end=" ", flush=True)

        base_result, enhanced_result = run_backtest(ts_code, days=500)

        if base_result and base_result.total_trades > 0:
            base_wr = base_result.win_rate
            base_ret = base_result.total_return
            base_sharpe = base_result.sharpe_ratio

            if enhanced_result and enhanced_result.total_trades > 0:
                enh_wr = enhanced_result.win_rate
                enh_ret = enhanced_result.total_return
                improvement = (enh_wr - base_wr) / base_wr if base_wr > 0 else 0

                print(f"基础 {base_wr:.0%}/{base_ret:+.1%} → 增强 {enh_wr:.0%}/{enh_ret:+.1%} ({improvement:+.0%})")

                results.append({
                    'ts_code': ts_code,
                    'base_trades': base_result.total_trades,
                    'base_wr': base_wr,
                    'base_ret': base_ret,
                    'base_sharpe': base_sharpe,
                    'enh_trades': enhanced_result.total_trades,
                    'enh_wr': enh_wr,
                    'enh_ret': enh_ret,
                    'improvement': improvement,
                })
            else:
                print(f"基础 {base_wr:.0%}/{base_ret:+.1%} → 增强 无交易")
                results.append({
                    'ts_code': ts_code,
                    'base_trades': base_result.total_trades,
                    'base_wr': base_wr,
                    'base_ret': base_ret,
                    'base_sharpe': base_sharpe,
                    'enh_trades': 0,
                    'enh_wr': 0,
                    'enh_ret': 0,
                    'improvement': 0,
                })
        else:
            print("基础版无交易")

    # 3. 汇总结果
    if results:
        print(f"\n{'=' * 80}")
        print("【汇总结果】")
        print(f"{'=' * 80}\n")

        # 表格输出
        print(f"{'股票':<12} {'基础交易':>8} {'基础胜率':>8} {'基础收益':>10} {'增强交易':>8} {'增强胜率':>8} {'增强收益':>10} {'提升':>8}")
        print("-" * 80)

        for r in sorted(results, key=lambda x: x['enh_wr'], reverse=True):
            print(f"{r['ts_code']:<12} {r['base_trades']:>8} {r['base_wr']:>7.0%} {r['base_ret']:>+9.1%} "
                  f"{r['enh_trades']:>8} {r['enh_wr']:>7.0%} {r['enh_ret']:>+9.1%} {r['improvement']:>+7.0%}")

        # 统计
        avg_base_wr = sum(r['base_wr'] for r in results) / len(results)
        avg_enh_wr = sum(r['enh_wr'] for r in results if r['enh_trades'] > 0) / max(1, sum(1 for r in results if r['enh_trades'] > 0))

        print(f"\n平均胜率:")
        print(f"  基础版: {avg_base_wr:.1%}")
        print(f"  增强版: {avg_enh_wr:.1%}")

        if avg_enh_wr > avg_base_wr:
            print(f"  提升: {(avg_enh_wr - avg_base_wr) / avg_base_wr:+.1%}")

        # 找出最佳股票
        best = max(results, key=lambda x: x['enh_wr'] if x['enh_trades'] > 0 else 0)
        print(f"\n🏆 最佳股票: {best['ts_code']}")
        print(f"   基础版: {best['base_wr']:.0%} 胜率, {best['base_ret']:+.1%} 收益")
        print(f"   增强版: {best['enh_wr']:.0%} 胜率, {best['enh_ret']:+.1%} 收益")

        # 4. 对最佳股票运行统计检验
        print(f"\n{'=' * 80}")
        print(f"【统计检验】{best['ts_code']}")
        print(f"{'=' * 80}\n")

        try:
            from modules.backtest_six_step import backtest_shaofu_with_validation
            from modules.statistics.criteria import CriteriaLevel

            result = backtest_shaofu_with_validation(
                best['ts_code'],
                days=500,
                validation_level=CriteriaLevel.MODERATE,
            )

            if result.validation_report:
                print(result.validation_report.generate_summary())
        except Exception as e:
            print(f"⚠️  统计检验失败: {e}")

    print(f"\n{'=' * 80}")
    print("测试完成")
    print(f"{'=' * 80}\n")


if __name__ == "__main__":
    main()
