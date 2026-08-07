#!/usr/bin/env python3
"""按交易日回补全市场未复权日线，支持断点续跑。"""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.data_sync import DataSyncer
from modules.database import get_connection, get_db_path
from modules.datasource import get_datasource


def _weekdays(start: str, end: str) -> list[str]:
    current = datetime.strptime(start, "%Y%m%d")
    last = datetime.strptime(end, "%Y%m%d")
    result: list[str] = []
    while current <= last:
        if current.weekday() < 5:
            result.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return result


def _complete_dates(start: str, end: str, min_rows: int) -> set[str]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT trade_date
            FROM daily_kline
            WHERE trade_date BETWEEN ? AND ?
            GROUP BY trade_date
            HAVING COUNT(*) >= ?
            """,
            (start, end, min_rows),
        ).fetchall()
    return {str(row["trade_date"]) for row in rows}


def _summary(start: str, end: str, min_rows: int) -> dict[str, int | str | None]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT ts_code) AS symbols,
                   MIN(trade_date) AS first_date,
                   MAX(trade_date) AS last_date
            FROM daily_kline
            WHERE trade_date BETWEEN ? AND ?
            """,
            (start, end),
        ).fetchone()
    return {
        "rows": int(row["rows"]),
        "symbols": int(row["symbols"]),
        "first_date": row["first_date"],
        "last_date": row["last_date"],
        "complete_days": len(_complete_dates(start, end, min_rows)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="回补全市场历史日线")
    parser.add_argument("--start", default="20160719")
    parser.add_argument("--end", default="20260719")
    parser.add_argument("--interval", type=float, default=1.3, help="请求最小间隔秒数")
    parser.add_argument("--min-rows", type=int, default=1000, help="判定单日已完整的最低行数")
    parser.add_argument("--expected-days", type=int, default=2427, help="预期交易日数量")
    parser.add_argument("--passes", type=int, default=2, help="最多扫描轮数")
    args = parser.parse_args()

    lock_path = Path(get_db_path()).with_suffix(".history-backfill.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ABORT another history backfill is already running", flush=True)
            return 2

        logging.basicConfig(level=logging.CRITICAL)
        weekdays = _weekdays(args.start, args.end)
        syncer = DataSyncer(datasource=get_datasource("tushare"))
        begun = time.time()

        for pass_no in range(1, args.passes + 1):
            complete = _complete_dates(args.start, args.end, args.min_rows)
            if len(complete) >= args.expected_days:
                break
            pending = [date for date in weekdays if date not in complete]
            print(
                f"PASS {pass_no}/{args.passes} pending={len(pending)} "
                f"complete={len(complete)}/{args.expected_days}",
                flush=True,
            )
            successes = 0
            empty_or_failed = 0
            for index, trade_date in enumerate(pending, 1):
                call_started = time.monotonic()
                result = syncer.sync_market_daily(
                    trade_date,
                    refresh_stock_basic=False,
                    check_trade_calendar=False,
                )
                if result["status"] == "success":
                    successes += 1
                else:
                    empty_or_failed += 1

                delay = max(0.0, args.interval - (time.monotonic() - call_started))
                if delay:
                    time.sleep(delay)
                if index == 1 or index % 25 == 0 or index == len(pending):
                    elapsed = time.time() - begun
                    rate = index / max(elapsed, 0.001)
                    eta = (len(pending) - index) / max(rate, 0.001)
                    print(
                        f"PROGRESS pass={pass_no} {index}/{len(pending)} date={trade_date} "
                        f"success={successes} empty_or_failed={empty_or_failed} "
                        f"elapsed_min={elapsed / 60:.1f} eta_min={eta / 60:.1f}",
                        flush=True,
                    )

            if pass_no < args.passes and len(_complete_dates(args.start, args.end, args.min_rows)) < args.expected_days:
                print("COOLDOWN seconds=65 before retry", flush=True)
                time.sleep(65)

        summary = _summary(args.start, args.end, args.min_rows)
        print(
            "FINAL " + " ".join(f"{key}={value}" for key, value in summary.items()),
            flush=True,
        )
        return 0 if int(summary["complete_days"]) >= args.expected_days else 1


if __name__ == "__main__":
    raise SystemExit(main())
