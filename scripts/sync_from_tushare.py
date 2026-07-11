#!/usr/bin/env python3
"""
从 tushare.db 导入最新数据到 stock_data.db

将 tushare-data-bridge 同步的最新 K 线数据合并到项目数据库
"""

import sqlite3
from pathlib import Path
from datetime import datetime


def import_latest_data():
    """导入最新数据"""
    tushare_db = "/Users/chenlei/.kimi/daimon/skills/tushare-data-bridge/data/tushare.db"
    our_db = "data/stock_data.db"

    print("\n" + "=" * 60)
    print("数据同步：tushare.db → stock_data.db")
    print("=" * 60 + "\n")

    if not Path(tushare_db).exists():
        print(f"❌ 源数据库不存在: {tushare_db}")
        return

    if not Path(our_db).exists():
        print(f"❌ 目标数据库不存在: {our_db}")
        return

    # 1. 读取源数据库最新数据
    conn_src = sqlite3.connect(tushare_db)
    cursor_src = conn_src.cursor()

    # 2. 获取目标数据库的最新日期
    conn_dst = sqlite3.connect(our_db)
    cursor_dst = conn_dst.cursor()

    cursor_dst.execute("SELECT MAX(trade_date) FROM daily_kline")
    our_max_date = cursor_dst.fetchone()[0]
    print(f"📅 目标数据库最新日期: {our_max_date}")

    # 获取目标数据库中已有的股票列表
    cursor_dst.execute("SELECT DISTINCT ts_code FROM daily_kline")
    our_stocks = set(row[0] for row in cursor_dst.fetchall())
    print(f"📊 目标数据库股票数: {len(our_stocks)}")

    # 3. 读取源数据库的新数据
    cursor_src.execute("""
        SELECT ts_code, trade_date, open, high, low, close, vol, amount,
               pct_chg, pre_close
        FROM daily
        WHERE trade_date > ?
        ORDER BY trade_date ASC
    """, (our_max_date,))

    new_rows = cursor_src.fetchall()
    conn_src.close()

    print(f"\n📥 找到新数据: {len(new_rows):,} 条")

    if not new_rows:
        print("✅ 数据已是最新，无需同步")
        conn_dst.close()
        return

    # 4. 按股票分组统计
    stock_new_data = {}
    for row in new_rows:
        ts_code = row[0]
        if ts_code not in stock_new_data:
            stock_new_data[ts_code] = []
        stock_new_data[ts_code].append(row)

    # 5. 导入数据
    inserted = 0
    updated_stocks = set()
    skipped_stocks = set()

    print(f"\n开始导入...")

    for ts_code, rows in stock_new_data.items():
        if ts_code not in our_stocks:
            # 这只股票不在我们的数据库中，跳过
            skipped_stocks.add(ts_code)
            continue

        for row in rows:
            ts_code_r, trade_date, open_p, high, low, close, vol, amount, pct_chg, pre_close = row

            # 转换数据类型
            try:
                open_f = float(open_p) if open_p else 0
                high_f = float(high) if high else 0
                low_f = float(low) if low else 0
                close_f = float(close) if close else 0
                vol_f = float(vol) if vol else 0
                amount_f = float(amount) if amount else 0
                pct_chg_f = float(pct_chg) if pct_chg else 0
                pre_close_f = float(pre_close) if pre_close else 0

                # 计算其他字段
                change = close_f - pre_close_f if pre_close_f > 0 else 0
                is_limit_up = pct_chg_f >= 9.5 if not ts_code_r.endswith('BJ') else pct_chg_f >= 29
                is_limit_down = pct_chg_f <= -9.5 if not ts_code_r.endswith('BJ') else pct_chg_f <= -29

                # 插入数据
                cursor_dst.execute("""
                    INSERT OR REPLACE INTO daily_kline
                    (ts_code, trade_date, open, high, low, close, vol, amount,
                     pct_chg, pre_close, change, is_limit_up, is_limit_down)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (ts_code_r, trade_date, open_f, high_f, low_f, close_f,
                      vol_f, amount_f, pct_chg_f, pre_close_f, change,
                      1 if is_limit_up else 0, 1 if is_limit_down else 0))

                inserted += 1
                updated_stocks.add(ts_code_r)

            except (ValueError, TypeError) as e:
                print(f"  ⚠️  数据转换错误 {ts_code_r} {trade_date}: {e}")
                continue

    # 6. 提交并关闭
    conn_dst.commit()

    # 7. 验证
    cursor_dst.execute("SELECT COUNT(*) FROM daily_kline")
    new_total = cursor_dst.fetchone()[0]
    cursor_dst.execute("SELECT MAX(trade_date) FROM daily_kline")
    new_max_date = cursor_dst.fetchone()[0]

    conn_dst.close()

    # 8. 输出结果
    print(f"\n{'=' * 60}")
    print("✅ 同步完成!")
    print(f"{'=' * 60}")
    print(f"  新增记录: {inserted:,} 条")
    print(f"  更新股票: {len(updated_stocks)} 只")
    print(f"  跳过股票: {len(skipped_stocks)} 只（不在我们的数据库中）")
    print(f"  总记录数: {new_total:,} 条")
    print(f"  最新日期: {new_max_date}")
    print()


if __name__ == "__main__":
    import_latest_data()
