#!/usr/bin/env python3
"""每个交易日收盘后同步全市场日线，并顺带补齐此前漏掉的交易日。

由 systemd user timer（``quant-monitor-sync.timer``）在交易日固定时间触发，
也可以直接手工执行。真正的同步交给 ``DataSyncer.sync_market_daily``——
它自带交易日校验、空结果退避重试、``(ts_code, trade_date)`` 幂等 upsert。

这一层只补 ``sync_market_daily`` 不管的三件事：

1. **算出该补哪几天。** timer 只在触发当天跑一次；机器关机、服务没起来、
   或者接口那天恰好抽风，落下的交易日不会有任何东西回头补。这里拿库里
   最新日期和本地交易日历一比，把缺口一并补上。
2. **清代理。** 本机 socks5 代理会打断 Tushare 通路，且报错指向错误方向
   （表现为连接超时，看着像对端问题）。systemd 单元里已经 UnsetEnvironment，
   手工执行时这里再兜一层。
3. **按天限速。** 中转 API 限流时**不报错、直接返回空**，连续快跑会整段漏数据。
   每天之间留 ``--sleep`` 秒，且 stock_basic 只在第一天刷一次。

退出码：0 = 全部成功或无事可做；1 = 有交易日没补上（systemd 会记 failed）。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 必须在 import tushare/requests 之前清掉：本机 socks5 代理会打断数据通路。
for _var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.data_sync import DataSyncer  # noqa: E402
from modules.database import get_connection, init_database  # noqa: E402
from modules.datasource import get_datasource  # noqa: E402

logger = logging.getLogger("sync_daily_kline")

# 一次最多补几天。超过这个数多半是长期没跑，属于"回补"而不是"每日同步"，
# 交给 scripts/backfill_market_history.py 走低速通道，别在这里硬刷限流接口。
DEFAULT_MAX_DAYS = 15
# 每天之间的间隔秒数。中转 API 实测 ~37 次/分以内稳定，每天约 2 次调用，
# 3 秒足够；调小会重蹈"限流→返回空→整段缺口"的覆辙。
DEFAULT_SLEEP = 3.0


def _setup_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def pending_trade_days(end_date: str, max_days: int) -> tuple[list[str], int]:
    """列出库里缺的交易日（升序）。

    以 ``daily_kline`` 的最新日期为起点，向后取本地交易日历里 is_open=1 的日子。
    交易日历没覆盖到 end_date 时返回空——宁可这天不同步，也不要在没有日历的
    情况下瞎猜（trade_cal 接口限流 1 次/分钟且静默返回空，现拉多半拉不到）。

    Returns:
        (要补的日期列表, 因 max_days 被截掉的天数)
    """
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(trade_date) FROM daily_kline").fetchone()
        last = str(row[0]) if row and row[0] else None

        if last is None:
            logger.warning("daily_kline 是空的，本脚本只做增量；首次全量请用 zt sync sync")
            return [], 0

        days = [
            str(r[0])
            for r in conn.execute(
                "SELECT cal_date FROM trade_cal "
                "WHERE exchange = 'SSE' AND is_open = 1 AND cal_date > ? AND cal_date <= ? "
                "ORDER BY cal_date",
                (last, end_date),
            )
        ]

        covered = conn.execute(
            "SELECT 1 FROM trade_cal WHERE exchange = 'SSE' AND cal_date = ?", (end_date,)
        ).fetchone()

    if covered is None:
        logger.error(
            "交易日历没有覆盖到 %s，跳过本次同步。先跑 `zt sync trade-cal --start %s0101 --end %s1231`",
            end_date,
            end_date[:4],
            end_date[:4],
        )
        return [], 0

    logger.info("库内最新交易日 %s，截至 %s 待补 %d 天", last, end_date, len(days))
    if len(days) > max_days:
        dropped = len(days) - max_days
        # 不做静默截断：漏掉的天数必须说出来，否则看日志会以为已经补全。
        logger.warning(
            "待补 %d 天超过上限 %d，本次只补最早的 %d 天（%s ~ %s），剩余 %d 天下次继续；"
            "缺口很大时改用 scripts/backfill_market_history.py",
            len(days),
            max_days,
            max_days,
            days[0],
            days[max_days - 1],
            dropped,
        )
        return days[:max_days], dropped
    return days, 0


def sync_one(syncer: DataSyncer, date: str, refresh_stock_basic: bool, retries: int) -> bool:
    """同步一个交易日，失败重试。返回是否成功（skipped 也算成功）。"""
    for attempt in range(1, retries + 1):
        try:
            result = syncer.sync_market_daily(
                trade_date=date,
                refresh_stock_basic=refresh_stock_basic,
                check_trade_calendar=True,
            )
        except Exception as exc:  # noqa: BLE001 — 单日失败不能中断后面的日子
            logger.warning("%s 同步异常（第 %d/%d 次）: %s", date, attempt, retries, exc)
        else:
            status = result.get("status")
            if status == "success":
                logger.info("%s ✓ 入库 %d 条", date, result.get("market_rows", 0))
                return True
            if status == "skipped":
                logger.info("%s - %s", date, result.get("message", "跳过"))
                return True
            logger.warning(
                "%s 同步失败（第 %d/%d 次）: %s", date, attempt, retries, result.get("message", "")
            )
        if attempt < retries:
            # 递增退避：失败最常见的原因是限流，等的时间要够跨过限流窗口。
            backoff = 30 * attempt
            logger.info("%s 秒后重试 %s", backoff, date)
            time.sleep(backoff)
    logger.error("%s 连续 %d 次未同步成功，留给下次补", date, retries)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="收盘后同步全市场日线（含缺口补齐）")
    parser.add_argument("--date", help="截止交易日 YYYYMMDD，默认今天")
    parser.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS, help="一次最多补几天")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help="每天之间的间隔秒数")
    parser.add_argument("--retries", type=int, default=3, help="单日失败重试次数")
    parser.add_argument(
        "--log-file",
        default=str(ROOT / "data" / "logs" / "sync_daily_kline.log"),
        help="日志文件路径，传空字符串则只输出到 stdout",
    )
    args = parser.parse_args()

    _setup_logging(Path(args.log_file) if args.log_file else None)

    end_date = args.date or datetime.now().strftime("%Y%m%d")
    init_database(verbose=False)

    days, dropped = pending_trade_days(end_date, args.max_days)
    if not days:
        logger.info("没有需要补的交易日，结束")
        return 0

    syncer = DataSyncer(datasource=get_datasource("tushare"))
    failed: list[str] = []
    for i, date in enumerate(days):
        # stock_basic 只在第一天刷：它每天变化极小，每天刷一遍纯属浪费限流额度。
        if not sync_one(syncer, date, refresh_stock_basic=(i == 0), retries=args.retries):
            failed.append(date)
        if i < len(days) - 1 and args.sleep > 0:
            time.sleep(args.sleep)

    ok = len(days) - len(failed)
    logger.info("本次完成 %d/%d 天%s", ok, len(days), f"，失败: {', '.join(failed)}" if failed else "")
    if dropped:
        logger.info("另有 %d 天因上限未处理，下次运行继续", dropped)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
