#!/usr/bin/env python3
"""Archive raw Tushare daily bars into the project SQLite database.

This intentionally uses the `daily` endpoint rather than `pro_bar(adj="qfq")`
so it does not call `adj_factor`, which is heavily rate-limited on some tokens.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import tushare as ts
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.database import get_connection, get_db_path, init_database  # noqa: E402


ARCHIVE_DATA_TYPE = "daily_kline_raw_10y"
LIMIT_THRESHOLD = 9.9


class SlidingRateLimiter:
    def __init__(self, rpm: int):
        self.rpm = rpm
        self.window: deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self.window and now - self.window[0] > 60:
            self.window.popleft()
        if len(self.window) >= self.rpm:
            sleep_for = 60 - (now - self.window[0]) + 0.25
            time.sleep(max(0.0, sleep_for))
            now = time.monotonic()
            while self.window and now - self.window[0] > 60:
                self.window.popleft()
        self.window.append(time.monotonic())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync raw Tushare daily K lines into SQLite.")
    parser.add_argument("--days", type=int, default=3650, help="Calendar days to look back.")
    parser.add_argument("--start-date", help="Start date YYYYMMDD. Overrides --days.")
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"), help="End date YYYYMMDD.")
    parser.add_argument("--rpm", type=int, default=45, help="Requests per minute for Tushare daily.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of stocks for a test run.")
    parser.add_argument("--force", action="store_true", help="Re-fetch codes already marked successful.")
    parser.add_argument("--progress-every", type=int, default=25, help="Print progress every N stocks.")
    return parser.parse_args()


def get_start_date(args: argparse.Namespace) -> str:
    if args.start_date:
        return args.start_date
    return (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")


def get_stock_codes(limit: int) -> list[str]:
    with get_connection() as conn:
        cursor = conn.cursor()
        sql = "SELECT ts_code FROM stock_basic ORDER BY ts_code"
        if limit > 0:
            sql += " LIMIT ?"
            cursor.execute(sql, (limit,))
        else:
            cursor.execute(sql)
        return [row["ts_code"] for row in cursor.fetchall()]


def get_done_codes(start_date: str, end_date: str) -> set[str]:
    marker = f"{start_date}-{end_date}"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT ts_code FROM sync_log
            WHERE data_type = ? AND status = 'success' AND message = ?
            """,
            (ARCHIVE_DATA_TYPE, marker),
        )
        return {row["ts_code"] for row in cursor.fetchall()}


def mark_sync(ts_code: str, last_date: str, status: str, message: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO sync_log (data_type, ts_code, last_date, status, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ARCHIVE_DATA_TYPE, ts_code, last_date, status, message),
        )


def insert_daily_rows(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0

    df = df.copy()
    for col in ["open", "high", "low", "close", "vol", "amount", "pct_chg"]:
        if col not in df.columns:
            df[col] = 0
    df = df.fillna(0)

    records = []
    for row in df.itertuples(index=False):
        row_dict = row._asdict()
        pct_chg = float(row_dict.get("pct_chg", 0) or 0)
        records.append(
            (
                row_dict["ts_code"],
                row_dict["trade_date"],
                float(row_dict.get("open", 0) or 0),
                float(row_dict.get("high", 0) or 0),
                float(row_dict.get("low", 0) or 0),
                float(row_dict.get("close", 0) or 0),
                float(row_dict.get("vol", 0) or 0),
                float(row_dict.get("amount", 0) or 0),
                pct_chg,
                None,
                1 if pct_chg >= LIMIT_THRESHOLD else 0,
                1 if pct_chg <= -LIMIT_THRESHOLD else 0,
            )
        )

    with get_connection() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO daily_kline
            (ts_code, trade_date, open, high, low, close, vol, amount,
             pct_chg, vol_ratio, is_limit_up, is_limit_down)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )
    return len(records)


def print_progress(done: int, total: int, rows: int, started_at: float) -> None:
    elapsed = time.time() - started_at
    rate = done / elapsed if elapsed > 0 else 0
    eta = (total - done) / rate if rate > 0 else 0
    db = get_db_path()
    db_size = db.stat().st_size / 1024 / 1024 if db.exists() else 0
    print(
        {
            "done": done,
            "total": total,
            "rows": rows,
            "elapsed_min": round(elapsed / 60, 1),
            "eta_min": round(eta / 60, 1),
            "db_size_mb": round(db_size, 1),
        },
        flush=True,
    )


def main() -> int:
    args = parse_args()
    load_dotenv(ROOT / ".env")
    init_database()

    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        raise SystemExit("TUSHARE_TOKEN is not set")

    ts.set_token(token)
    pro = ts.pro_api()
    api_url = os.environ.get("TUSHARE_API_URL", "")
    if api_url:
        pro._DataApi__http_url = api_url

    start_date = get_start_date(args)
    end_date = args.end_date
    marker = f"{start_date}-{end_date}"

    codes = get_stock_codes(args.limit)
    if not codes:
        raise SystemExit("No stock codes found in stock_basic")

    if not args.force:
        done_codes = get_done_codes(start_date, end_date)
        codes = [code for code in codes if code not in done_codes]

    print(
        {
            "db_path": str(get_db_path()),
            "codes_to_sync": len(codes),
            "start_date": start_date,
            "end_date": end_date,
            "rpm": args.rpm,
            "force": args.force,
        },
        flush=True,
    )

    limiter = SlidingRateLimiter(args.rpm)
    started_at = time.time()
    total_rows = 0
    ok = 0
    fail = 0

    for index, ts_code in enumerate(codes, start=1):
        try:
            limiter.wait()
            df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            count = insert_daily_rows(df)
            last_date = "" if df is None or df.empty else str(df["trade_date"].max())
            mark_sync(ts_code, last_date, "success", marker)
            total_rows += count
            ok += 1
        except Exception as exc:
            fail += 1
            mark_sync(ts_code, "", "failed", f"{marker}: {type(exc).__name__}: {str(exc)[:300]}")
            print({"failed": ts_code, "error": str(exc)[:300]}, flush=True)

        if index == 1 or index % args.progress_every == 0 or index == len(codes):
            print_progress(index, len(codes), total_rows, started_at)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM daily_kline")
        kline_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT ts_code) FROM daily_kline")
        stock_count = cursor.fetchone()[0]

    print(
        {
            "finished": True,
            "ok": ok,
            "failed": fail,
            "inserted_rows_this_run": total_rows,
            "daily_kline_total": kline_count,
            "daily_kline_stocks": stock_count,
            "marker": marker,
        },
        flush=True,
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
