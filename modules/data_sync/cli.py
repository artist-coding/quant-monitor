"""Command line interface for data synchronization."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .syncer import DataSyncer


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="Tushare 数据同步工具")
    parser.add_argument(
        "action",
        choices=["init", "sync", "market-daily", "status", "stk-factor"],
        help="操作: market-daily=收盘后全市场日线，其他为初始化/历史同步/状态/官方指标",
    )
    parser.add_argument("--ts_code", help="股票代码，如 000001.SZ")
    parser.add_argument("--days", type=int, default=730, help="同步天数")
    parser.add_argument("--indicators", action="store_true", help="同步完成后计算并缓存技术指标（indicator_cache 表）")
    parser.add_argument(
        "--skip-indicators",
        action="store_true",
        help="跳过指标缓存同步（默认单只股票自动同步，批量需指定 --indicators）",
    )
    parser.add_argument("--date", help="market-daily 交易日期 YYYYMMDD，默认今天")
    parser.add_argument("--no-refresh-stock-basic", action="store_true", help="market-daily 不刷新股票列表")
    parser.add_argument("--skip-calendar-check", action="store_true", help="market-daily 跳过交易日历检查")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    if args.action == "init":
        from ..database import init_database

        init_database()
        print("数据库初始化完成")

    elif args.action == "sync":
        syncer = DataSyncer()

        if args.ts_code:
            # 同步单只股票
            syncer.sync_daily_kline(args.ts_code)
            # 单只股票默认同步指标缓存（除非显式跳过）
            if not args.skip_indicators:
                print(f"正在同步指标缓存: {args.ts_code} ...")
                syncer.sync_indicator_cache(args.ts_code, days=args.days)
        else:
            # 批量同步所有股票
            syncer.sync_stock_basic()
            syncer.sync_all_daily_kline(days=args.days)
            # 批量同步指标缓存（需显式指定 --indicators）
            if args.indicators and not args.skip_indicators:
                print("正在批量同步指标缓存...")
                syncer.sync_all_indicators()

    elif args.action == "market-daily":
        syncer = DataSyncer()
        result = syncer.sync_market_daily(
            trade_date=args.date,
            refresh_stock_basic=not args.no_refresh_stock_basic,
            check_trade_calendar=not args.skip_calendar_check,
        )
        print(result)
        if result["status"] == "failed":
            raise SystemExit(1)

        print("同步完成")
        print(syncer.get_sync_status())

    elif args.action == "stk-factor":
        syncer = DataSyncer()

        if args.ts_code:
            print(f"正在同步 Tushare 官方指标: {args.ts_code} ...")
            start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
            end_date = datetime.now().strftime("%Y%m%d")
            count = syncer.sync_stk_factor(args.ts_code, start_date=start_date, end_date=end_date)
            print(f"同步完成，{count} 条")
        else:
            print("正在批量同步 Tushare 官方指标...")
            results = syncer.sync_all_stk_factor(days=args.days)
            success = sum(1 for v in results.values() if v > 0)
            print(f"批量同步完成，成功 {success}/{len(results)}")

    elif args.action == "status":
        from ..datasource import SqliteDataSource

        syncer = DataSyncer(datasource=SqliteDataSource())
        status = syncer.get_sync_status()
        print("=" * 50)
        print(f"数据库: {status['db_path']}")
        print(f"股票数量: {status['stock_count']}")
        print(f"K线数据: {status['kline_count']}")
        print("-" * 50)
        print("同步状态:")
        for s in status["sync_status"]:
            print(f"  {s['data_type']}: {s['last_date']} ({s['status']})")


if __name__ == "__main__":
    main()
