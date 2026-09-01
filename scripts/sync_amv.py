#!/usr/bin/env python3
"""每交易日收盘后把活跃市值表拉下来并入库。

活跃市值是选股的总开关（``modules/amv.py``），它停更一天，那天就等于
没有开关可用——``get_regime`` 会向前回退到上一条，于是拿着几天前的区间
继续做决策，而且**不会报任何错**。这个脚本就是为了让它别停更。

分两步，各自可以单独跑：

1. **下载**：``scripts/sync_amv_baidu.sh`` 从百度网盘分享链接转存+下载+归档，
   在 stdout 上回一个 ``STATUS=``/``FILE=`` 契约。它不碰数据库。
2. **入库**：把归档文件整表 upsert 进 ``amv_daily``，再按规则重算多空区间。
   整表幂等，所以上游没更新时重跑也无所谓。

之所以哪怕 ``STATUS=unchanged`` 也照样入库：下载成功而入库失败是会发生的
（磁盘满、库被锁），只看下载状态的话那一天就永久丢了。重新导一遍很便宜。

退出码：

- 0 = 活跃市值已跟上行情库
- 1 = 下载失败、入库失败，或活跃市值落后超过 STALE_TRADE_DAYS 个交易日
  （落后太多不是"今天出得晚"，是通路断了，必须让 systemd 记 failed）
- 2 = 入库成功但落后 1~3 个交易日，多半是上游当天还没出数据。
  systemd 单元里用 ``SuccessExitStatus=2`` 把它当成功，日志里仍有 WARNING。
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import amv  # noqa: E402  —— 顺带触发 modules/__init__ 里的 .env 加载
from modules.database import get_connection, init_database  # noqa: E402

logger = logging.getLogger("sync_amv")

DOWNLOADER = ROOT / "scripts" / "sync_amv_baidu.sh"

# 活跃市值落后行情库超过这么多个交易日，就当通路坏了而不是"今天出得晚"。
# 定 3 是留出"周五出数据晚 + 周末"这种情况，再多就该有人去看了。
STALE_TRADE_DAYS = 3


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


def download() -> tuple[str, Path | None, str]:
    """跑下载脚本，解析它 stdout 上的契约。

    stderr 直接继承下去（journalctl / 终端能看到下载进度），
    只有 stdout 是结构化的。

    Returns:
        (status, 归档文件路径, 失败原因)
    """
    if not DOWNLOADER.exists():
        return "failed", None, f"下载脚本不见了: {DOWNLOADER}"

    proc = subprocess.run(
        ["bash", str(DOWNLOADER)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    fields = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        if value:
            fields[key.strip()] = value.strip()

    status = fields.get("STATUS", "failed")
    if status == "failed" or proc.returncode != 0:
        # 契约缺失时（脚本被 kill、bash 本身报错）也走这里，别把它当成功。
        return "failed", None, fields.get("REASON") or f"下载脚本退出码 {proc.returncode}"

    path = fields.get("FILE")
    if not path:
        return "failed", None, "下载脚本没有回 FILE=，无法确定归档文件"
    return status, Path(path), ""


def market_days_ahead(amv_date: str) -> tuple[str | None, int]:
    """行情库比活跃市值多出几个交易日。

    刻意数 ``daily_kline`` 里的实际交易日而不是查 ``trade_cal``：
    交易日历经常整年整年地缺（``trade_cal`` 接口限流 1 次/小时，多年回补时
    很容易只拉到第一年），拿它当基准会在日历缺失时算出 0，把"落后很久"报成"正常"。

    Returns:
        (行情库最新交易日, 领先的交易日数)
    """
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(trade_date) FROM daily_kline").fetchone()
        latest = str(row[0]) if row and row[0] else None
        if latest is None:
            return None, 0
        ahead = conn.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM daily_kline WHERE trade_date > ?", (amv_date,)
        ).fetchone()[0]
    return latest, int(ahead)


def main() -> int:
    parser = argparse.ArgumentParser(description="拉取并入库活跃市值（选股总开关）")
    parser.add_argument("--file", help="直接导入指定文件，跳过下载（csv / xlsx / zip）")
    parser.add_argument("--skip-download", action="store_true", help="不下载，只把已归档的最新文件重新入库")
    parser.add_argument("--dry-run", action="store_true", help="只解析不落库，核对列名映射用")
    parser.add_argument(
        "--log-file",
        default=str(ROOT / "data" / "logs" / "sync_amv.log"),
        help="日志文件路径，传空字符串则只输出到 stdout",
    )
    args = parser.parse_args()

    _setup_logging(Path(args.log_file) if args.log_file else None)
    init_database(verbose=False)

    # ---- 1. 拿到要导入的文件 ----
    if args.file:
        target = Path(args.file)
        status = "manual"
    elif args.skip_download:
        archive_dir = Path(os.environ.get("AMV_BAIDU_ARCHIVE_DIR") or ROOT / "data" / "amv" / "baidu")
        prefix = os.environ.get("AMV_BAIDU_PREFIX", "活跃市值")
        candidates = sorted(
            (p for p in archive_dir.glob(f"{prefix}_*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        if not candidates:
            logger.error("%s 下没有 %s_* 归档文件，没得导", archive_dir, prefix)
            return 1
        target = candidates[-1]
        status = "local"
    else:
        status, downloaded, reason = download()
        # downloaded is None 也当失败：download() 只在 failed 时返回 None，
        # 但把它显式写出来，免得以后改 download() 时这里静默拿到 None 再去 open。
        if status == "failed" or downloaded is None:
            logger.error("下载失败: %s", reason)
            return 1
        target = downloaded
        logger.info("下载: %s -> %s", status, target)

    # ---- 2. 入库 ----
    try:
        res = amv.import_history(target, dry_run=args.dry_run)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        logger.error("入库失败: %s", exc)
        return 1

    logger.info(
        "%s%d 行（跳过 %d），覆盖 %s ~ %s；表头: %s",
        "【dry-run，未落库】" if args.dry_run else "导入 ",
        res["imported"],
        res["skipped"],
        res["start"],
        res["end"],
        ", ".join(res["columns"]),
    )
    if args.dry_run:
        for row in res["preview"]:
            logger.info("  样例 %s  收盘=%s  标注=%s", row["trade_date"], row["close"], row["regime_imported"] or "-")
        return 0

    # ---- 3. 报状态 ----
    day = amv.get_regime()
    print()
    print(amv.format_amv_status(day, amv.regime_segments(5)))

    if day is None:
        logger.error("导入完库里还是空的，检查一下文件内容")
        return 1

    market, ahead = market_days_ahead(day.trade_date)
    if ahead <= 0:
        return 0

    if ahead > STALE_TRADE_DAYS:
        # 落后超过阈值就不再是"今天的数据出得晚"，是这条通路已经断了：
        # 分享链接还能转存、文件也还在，只是上游不再更新。这种坏法不报错的话
        # 没有任何征兆——get_regime 会一直拿几周前的区间当今天用，
        # 该停手的时候照样放行选股。所以让 systemd 记 failed。
        logger.error(
            "活跃市值最新到 %s，行情库已到 %s，落后 %d 个交易日（阈值 %d）——"
            "这不是当天数据出得晚，是上游停更或分享链接换了，去查 %s",
            day.trade_date,
            market,
            ahead,
            STALE_TRADE_DAYS,
            DOWNLOADER.name,
        )
        return 1

    # 落后一两天：多半是上游当天还没出，晚一点那次会补上。不当失败，
    # 但必须说出来——不然选股拿着旧区间跑，日志里一片安静。
    logger.warning(
        "活跃市值最新到 %s，行情库已到 %s，落后 %d 个交易日——"
        "选股会拿 %s 的区间当今天用；上游多半还没出数据，晚一点那次会补上",
        day.trade_date,
        market,
        ahead,
        day.trade_date,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
