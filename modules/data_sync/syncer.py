"""Data syncer coordinating batch synchronization, logging and status."""

from __future__ import annotations

import os
import time
import logging
import threading
import concurrent.futures
from datetime import datetime, timedelta
from typing import Any, Optional

from ..database import get_connection, get_db_path, init_database
from ..datasource import DataSource, TushareDataSource
from .rate_limiter import _rate_limit_global, _MAX_SYNC_WORKERS
from .indicator_cache import (
    _get_indicator_funcs,
    _compute_day_indicators,
    _build_indicator_row,
    _INDICATOR_INSERT_COLUMNS,
)
from .fetcher import DataFetcher

logger = logging.getLogger(__name__)

# 涨跌停阈值（主板 10%，此处用 9.9% 容差）
# 注意：创业板(300xxx)/科创板(688xxx) 实际为 20%，ST 为 5%，
# 新股前 5 日无限制。当前简化处理，v2.11.0 计划按 market 字段动态调整。
_LIMIT_THRESHOLD = 9.9

# 中转 API 配置（从环境变量读取）
TUSHARE_API_URL = os.environ.get("TUSHARE_API_URL", "")
VERIFY_TOKEN_URL = os.environ.get("TUSHARE_VERIFY_TOKEN_URL", "")

# 默认同步的宽基指数列表（作为大盘环境的补充信息）
#
# **只放 3 个是被配额逼的**：实测该中转源 index_daily 限额 5 次/天
# （错误文案原文「频率超限(5次/天)」）。原来一次要 7 个指数，第 6 个起必然失败；
# 叠加旧的 3 次重试后，一轮就能打出 21 次请求，当天配额瞬间烧光——这正是
# index_daily 表长期一行都没有的真正原因，而不是接口不可用。
# 留 2 次余量给手动补数和失败重跑。
#
# 注意：大盘环境的**首选**数据源是全市场宽度（market_context.compute_market_breadth，
# 从 daily_kline 算，零 API 成本），指数只是补充。想加指数前先确认配额够用。
DEFAULT_INDEXES: list[str] = [
    "000300.SH",  # 沪深300：覆盖大盘蓝筹，代表性最强
    "000001.SH",  # 上证指数：最常被引用的大盘口径
    "399006.SZ",  # 创业板指：成长股情绪，与沪深300 形成风格对照
]


def _is_rate_limit_error(exc: Exception) -> bool:
    """判断异常是否为 Tushare 的接口配额超限。

    中转 API 把限流以普通异常抛出，文案形如
    「抱歉，您访问接口(index_daily)频率超限(5次/天)」。
    """
    text = str(exc)
    return "频率超限" in text or "rate limit" in text.lower()


def _normalize_trade_date(trade_date: str | None) -> str:
    """校验并规范化 YYYYMMDD 交易日期。"""
    value = trade_date or datetime.now().strftime("%Y%m%d")
    try:
        return datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"交易日期格式错误: {value}，应为 YYYYMMDD") from exc


def _daily_limit_threshold(ts_code: str) -> float:
    """按板块给出涨跌停识别容差；ST/新股的特殊规则后续再扩展。"""
    code = ts_code.split(".", 1)[0]
    if ts_code.endswith(".BJ"):
        return 29.0
    if code.startswith(("300", "301", "688", "689")):
        return 19.5
    return 9.5


class DataSyncer:
    """数据同步器"""

    def __init__(self, token: str | None = None, datasource: DataSource | None = None):
        self.token = token or os.environ.get("TUSHARE_TOKEN")

        # 依赖注入 DataSource；默认使用 Tushare 数据源以保持向后兼容
        if datasource is None:
            # 仅在构造默认 Tushare 数据源时检查 JNB 模式环境配置
            data_mode = os.getenv("DATA_MODE", "websearch")
            if data_mode == "jnb":
                if not self.token:
                    raise ValueError("JNB 模式下未设置 TUSHARE_TOKEN，请检查 .env 文件。")
                if not TUSHARE_API_URL:
                    raise ValueError(
                        "JNB 模式下未设置 TUSHARE_API_URL，请在 .env 中配置中转 API 地址。\n"
                        "示例：TUSHARE_API_URL=https://tt.xiaodefa.cn"
                    )
            datasource = TushareDataSource(token=self.token)
        self._datasource = datasource
        self._fetcher = DataFetcher(self._datasource)

        # 向后兼容：保留 instance-level attrs（外部可能引用）
        # 但实际限流走模块级 _GLOBAL_LIMITER
        self.last_request_time: dict[str, float] = {}

    def _rate_limit(self, api_name: str):
        """线程安全的限流控制（v2.10.0 P1-4 改为调模块级 _GLOBAL_LIMITER）"""
        # v2.10.0：原 per-instance lock 改用模块级 multiprocessing 安全限流器
        _rate_limit_global()
        # 保留旧字段更新，便于外部观察（不影响实际限流）
        self.last_request_time[api_name] = time.time()

    def _call_api_with_retry(self, api_name: str, func, *args, **kwargs):
        """带退避算法和限流控制的 API 调用封装。

        **限流错误不重试**：Tushare 的配额是按接口分别计的，且颗粒度很粗
        （实测 index_daily 5 次/天、stock_basic 1 次/小时、trade_cal 1 次/分钟）。
        原来的 1s/2s 退避远小于任何一个窗口，重试必然再次失败，却要多消耗
        2 次配额——7 个指数 × 3 次重试 = 21 次请求打一个 5 次/天的额度，
        等于自己把配额烧光。碰到限流直接抛出，交给下一次定时运行。
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self._rate_limit(api_name)
                return func(*args, **kwargs)
            except Exception as e:
                if _is_rate_limit_error(e):
                    logger.warning("[%s] 触发接口配额限制，不重试（等下次调度）: %s", api_name, e)
                    raise
                if attempt == max_retries - 1:
                    raise e
                sleep_time = 2**attempt
                logger.warning(
                    f"[{api_name}] API 调用异常: {e}, 等待 {sleep_time} 秒后重试 ({attempt + 1}/{max_retries})"
                )
                time.sleep(sleep_time)

    def _log_sync(self, data_type: str, ts_code: str | None, last_date: str, status: str, message: str = ""):
        """记录同步日志"""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sync_log (data_type, ts_code, last_date, status, message)
                VALUES (?, ?, ?, ?, ?)
            """,
                (data_type, ts_code, last_date, status, message),
            )

    def _get_last_date(self, data_type: str, ts_code: str | None = None) -> str | None:
        """获取最后同步日期"""
        with get_connection() as conn:
            cursor = conn.cursor()
            if ts_code:
                cursor.execute(
                    """
                    SELECT last_date FROM sync_log
                    WHERE data_type = ? AND ts_code = ? AND status = 'success'
                    ORDER BY created_at DESC LIMIT 1
                """,
                    (data_type, ts_code),
                )
            else:
                cursor.execute(
                    """
                    SELECT last_date FROM sync_log
                    WHERE data_type = ? AND ts_code IS NULL AND status = 'success'
                    ORDER BY created_at DESC LIMIT 1
                """,
                    (data_type,),
                )
            result = cursor.fetchone()
            return result["last_date"] if result else None

    # ==================== 批量同步基础设施 ====================

    def _batch_sync(self, task_name: str, sync_fn, ts_codes: list[str]) -> dict[str, int]:
        """通用批量同步：并发执行 + 进度追踪 + 异常处理

        消除 sync_all_daily_kline / sync_all_indicators / sync_all_stk_factor /
        sync_all_daily_basic 四个方法中的重复模式。

        Args:
            task_name: 任务名称（用于日志，如"日线数据"）
            sync_fn: 同步函数 callable(ts_code) -> count
            ts_codes: 股票代码列表

        Returns:
            dict[ts_code] = count
        """
        results: dict[str, int] = {}
        if not ts_codes:
            return results

        total = len(ts_codes)
        logger.info(f"开始批量同步{task_name}，共 {total} 只股票...")

        progress_lock = threading.Lock()
        completed = 0

        def _worker(ts_code: str) -> tuple[str, int]:
            nonlocal completed
            try:
                count = sync_fn(ts_code)
                with progress_lock:
                    completed += 1
                    if completed % 10 == 0:
                        logger.info(f"进度: {completed}/{total}")
                return ts_code, count
            except Exception as e:
                logger.error(f"{task_name}同步失败 {ts_code}: {e}")
                with progress_lock:
                    completed += 1
                return ts_code, 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_SYNC_WORKERS) as executor:
            futures = [executor.submit(_worker, code) for code in ts_codes]
            for future in concurrent.futures.as_completed(futures):
                code, count = future.result()
                results[code] = count

        success_count = sum(1 for v in results.values() if v > 0)
        logger.info(f"批量{task_name}同步完成，成功 {success_count}/{total}")
        return results

    @staticmethod
    def _fetch_all_codes(query: str) -> list[str]:
        """从数据库查询股票代码列表"""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            return [row[0] for row in cursor.fetchall()]

    # ==================== 股票基本信息 ====================

    def sync_stock_basic(self) -> int:
        """
        同步股票基本信息
        股票信息基本不变化，每周同步一次即可
        """
        logger.info("开始同步股票基本信息...")
        try:
            df = self._call_api_with_retry(
                "stock_basic",
                self._fetcher.fetch_stock_basic,
            )

            if df is None or len(df) == 0:
                logger.warning("获取股票基本信息失败")
                return 0

            # 每日任务会重复刷新股票列表，必须使用 upsert，不能 append 后撞主键。
            columns = ["ts_code", "name", "area", "industry", "market", "list_date", "is_hs"]
            for column in columns:
                if column not in df.columns:
                    df[column] = ""
            df = df[columns].fillna("")
            records = [tuple(row) for row in df.itertuples(index=False, name=None)]
            with get_connection() as conn:
                conn.executemany(
                    """
                    INSERT INTO stock_basic (ts_code, name, area, industry, market, list_date, is_hs)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ts_code) DO UPDATE SET
                        name = excluded.name,
                        area = excluded.area,
                        industry = excluded.industry,
                        market = excluded.market,
                        list_date = excluded.list_date,
                        is_hs = excluded.is_hs
                    """,
                    records,
                )

            self._log_sync("stock_basic", None, datetime.now().strftime("%Y%m%d"), "success")
            logger.info(f"股票基本信息同步完成，共 {len(df)} 只")
            return len(df)

        except Exception as e:
            logger.error(f"股票基本信息同步失败: {e}")
            self._log_sync("stock_basic", None, "", "failed", str(e))
            return 0

    # ==================== 交易日历 ====================

    def sync_trade_cal(self, start_date: str, end_date: str, exchange: str = "SSE") -> int:
        """同步一段区间的交易日历到本地 trade_cal 表。

        Tushare 的 trade_cal 接口被限流到 1 次/分钟，因此建议调用方按"整年"
        为粒度拉取，尽量减少 API 调用次数。入库使用 INSERT OR REPLACE，重复
        执行幂等。

        Args:
            start_date: 起始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD
            exchange: 交易所代码，默认 SSE（上交所）

        Returns:
            入库行数；失败返回 0
        """
        init_database(verbose=False)
        try:
            df = self._call_api_with_retry(
                "trade_cal",
                self._fetcher.fetch_trade_cal,
                exchange,
                start_date,
                end_date,
            )
            if df is None or len(df) == 0:
                logger.warning("交易日历为空: %s %s~%s", exchange, start_date, end_date)
                self._log_sync("trade_cal", None, end_date, "failed", "交易日历返回空")
                return 0

            records = []
            for row in df.itertuples(index=False):
                row_dict = row._asdict()
                cal_date = str(row_dict.get("cal_date", "") or "")
                if not cal_date:
                    continue
                records.append(
                    (
                        str(row_dict.get("exchange", "") or exchange),
                        cal_date,
                        int(row_dict.get("is_open", 0) or 0),
                        row_dict.get("pretrade_date"),
                    )
                )

            if not records:
                self._log_sync("trade_cal", None, end_date, "failed", "交易日历无有效行")
                return 0

            with get_connection() as conn:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO trade_cal (exchange, cal_date, is_open, pretrade_date)
                    VALUES (?, ?, ?, ?)
                    """,
                    records,
                )

            self._log_sync("trade_cal", None, end_date, "success", f"rows={len(records)}, exchange={exchange}")
            logger.info("交易日历同步完成: %s %s~%s, %s 条", exchange, start_date, end_date, len(records))
            return len(records)

        except Exception as e:
            logger.error("交易日历同步失败 %s %s~%s: %s", exchange, start_date, end_date, e)
            self._log_sync("trade_cal", None, "", "failed", str(e)[:500])
            return 0

    def _query_trade_cal(self, trade_date: str, exchange: str) -> bool | None:
        """从本地 trade_cal 表查询某日是否为交易日；未命中返回 None。"""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT is_open FROM trade_cal WHERE exchange = ? AND cal_date = ?",
                (exchange, trade_date),
            ).fetchone()
        if row is None:
            return None
        return bool(row[0])

    def is_trade_day(self, trade_date: str, exchange: str = "SSE") -> bool | None:
        """判断某日是否为交易日。

        查询顺序：本地 trade_cal 缓存 → 拉取该日期所在自然年的整年日历后再查。
        若 API 限流或异常导致仍拿不到结果，返回 None 表示"未知"，**不抛异常**，
        由调用方自行决定降级策略。

        Args:
            trade_date: 日期 YYYYMMDD
            exchange: 交易所代码，默认 SSE

        Returns:
            True=交易日 / False=非交易日 / None=未知
        """
        target = _normalize_trade_date(trade_date)
        init_database(verbose=False)

        cached = self._query_trade_cal(target, exchange)
        if cached is not None:
            return cached

        # 未命中：一次性补全该自然年的整年日历，减少限流接口的调用次数
        year = target[:4]
        self.sync_trade_cal(f"{year}0101", f"{year}1231", exchange)

        cached = self._query_trade_cal(target, exchange)
        if cached is None:
            logger.warning("交易日历不可用，无法判断 %s 是否为交易日（exchange=%s）", target, exchange)
        return cached

    # ==================== 收盘后全市场日线 ====================

    def sync_market_daily(
        self,
        trade_date: str | None = None,
        refresh_stock_basic: bool = True,
        check_trade_calendar: bool = True,
    ) -> dict[str, Any]:
        """按交易日一次拉取并入库全 A 股日线。

        该路径使用 Tushare ``daily(trade_date=...)`` 的未复权行情，适合每天
        收盘后归档。``daily_kline`` 的 ``(ts_code, trade_date)`` 唯一约束保证
        重复执行为幂等 upsert。
        """
        target = _normalize_trade_date(trade_date)
        init_database(verbose=False)

        result: dict[str, Any] = {
            "status": "failed",
            "trade_date": target,
            "stock_basic_rows": 0,
            "market_rows": 0,
            "db_rows_for_date": 0,
            "price_mode": "raw",
            "message": "",
        }

        try:
            if check_trade_calendar:
                is_open = self.is_trade_day(target)
                if is_open is False:
                    result.update(status="skipped", message="非交易日，无需同步")
                    self._log_sync("market_daily_raw", None, target, "skipped", result["message"])
                    return result
                if is_open is None:
                    # 交易日历不可用（多为接口限流）时不硬失败：继续同步，
                    # 由后续 daily 接口的空结果自然兜底，避免整条流水线中断。
                    logger.warning("交易日历不可用，跳过 %s 的交易日校验，继续尝试同步", target)

            if refresh_stock_basic:
                result["stock_basic_rows"] = self.sync_stock_basic()

            df = self._call_api_with_retry(
                "market_daily",
                self._fetcher.fetch_market_daily,
                target,
            )
            if df is None or df.empty:
                raise RuntimeError(f"{target} 全市场日线为空，可能尚未完成盘后入库")

            required = {"ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"}
            missing = sorted(required.difference(df.columns))
            if missing:
                raise RuntimeError(f"全市场日线缺少字段: {', '.join(missing)}")

            frame = df.copy()
            frame["trade_date"] = frame["trade_date"].astype(str)
            frame = frame[frame["trade_date"] == target].drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
            if frame.empty:
                raise RuntimeError(f"接口返回数据不包含目标日期 {target}")

            records = []
            for row in frame.itertuples(index=False):
                row_dict = row._asdict()
                ts_code = str(row_dict["ts_code"])
                pct_chg = float(row_dict.get("pct_chg", 0) or 0)
                threshold = _daily_limit_threshold(ts_code)
                records.append(
                    (
                        ts_code,
                        target,
                        float(row_dict.get("open", 0) or 0),
                        float(row_dict.get("high", 0) or 0),
                        float(row_dict.get("low", 0) or 0),
                        float(row_dict.get("close", 0) or 0),
                        float(row_dict.get("vol", 0) or 0),
                        float(row_dict.get("amount", 0) or 0),
                        pct_chg,
                        None,
                        1 if pct_chg >= threshold else 0,
                        1 if pct_chg <= -threshold else 0,
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
                db_rows = conn.execute(
                    "SELECT COUNT(*) FROM daily_kline WHERE trade_date = ?",
                    (target,),
                ).fetchone()[0]

            result.update(
                status="success",
                market_rows=len(records),
                db_rows_for_date=db_rows,
                message=f"{target} 全市场日线已入库",
            )
            self._log_sync(
                "market_daily_raw",
                None,
                target,
                "success",
                f"rows={len(records)}, db_rows={db_rows}, price_mode=raw",
            )
            logger.info("全市场日线同步完成: %s, %s 条", target, len(records))
            return result
        except Exception as exc:
            result["message"] = str(exc)
            self._log_sync("market_daily_raw", None, target, "failed", str(exc)[:500])
            logger.error("全市场日线同步失败 %s: %s", target, exc)
            return result

    # ==================== 日线K线数据 ====================

    def sync_daily_kline(self, ts_code: str, start_date: str | None = None, end_date: str | None = None) -> int:
        """
        同步单只股票的日线数据（增量更新）

        Args:
            ts_code: 股票代码，如 '000001.SZ'
            start_date: 开始日期，格式 YYYYMMDD，None 表示从数据库最后一条开始
            end_date: 结束日期，格式 YYYYMMDD，None 表示到最新

        Returns:
            更新条数
        """
        # 增量更新：获取最后同步日期
        if start_date is None:
            last_date = self._get_last_date("daily_kline", ts_code)
            if last_date:
                # 从后一天开始
                last_dt = datetime.strptime(last_date, "%Y%m%d")
                start_date = (last_dt + timedelta(days=1)).strftime("%Y%m%d")

        # 默认从2年前开始
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        try:
            df = self._call_api_with_retry(
                "daily_kline",
                self._fetcher.fetch_daily_kline,
                ts_code,
                start_date,
                end_date,
            )

            if df is None or len(df) == 0:
                return 0

            # 计算量比（需要历史数据，这里先跳过，由指标计算模块处理）
            # 计算涨跌停标记
            df["is_limit_up"] = df["pct_chg"].apply(lambda x: 1 if x >= _LIMIT_THRESHOLD else 0)
            df["is_limit_down"] = df["pct_chg"].apply(lambda x: 1 if x <= -_LIMIT_THRESHOLD else 0)

            with get_connection() as conn:
                cursor = conn.cursor()

                # 准备批量插入的数据
                records = []
                for row in df.itertuples(index=False):
                    row_dict = row._asdict()
                    records.append(
                        (
                            row_dict["ts_code"],
                            row_dict["trade_date"],
                            row_dict["open"],
                            row_dict["high"],
                            row_dict["low"],
                            row_dict["close"],
                            row_dict["vol"],
                            row_dict["amount"],
                            row_dict.get("pct_chg", 0),
                            None,  # vol_ratio later
                            row_dict.get("is_limit_up", 0),
                            row_dict.get("is_limit_down", 0),
                        )
                    )

                cursor.executemany(
                    """
                    INSERT OR REPLACE INTO daily_kline
                    (ts_code, trade_date, open, high, low, close, vol, amount,
                     pct_chg, vol_ratio, is_limit_up, is_limit_down)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    records,
                )

            # 更新同步日志
            latest_date = df["trade_date"].max()
            self._log_sync("daily_kline", ts_code, latest_date, "success")

            logger.info(f"日线数据同步完成: {ts_code}, {len(df)} 条, {start_date}-{latest_date}")
            return len(df)

        except Exception as e:
            logger.error(f"日线数据同步失败 {ts_code}: {e}")
            self._log_sync("daily_kline", ts_code, "", "failed", str(e))
            return 0

    def sync_missing(self, ts_codes: list[str], days: int = 730) -> dict[str, int]:
        """
        同步 ts_codes 中"在 daily_kline 表里完全缺失"的股票（增量补齐）

        与 sync_all_daily_kline 的区别：
        - sync_all_daily_kline：所有 ts_codes 都同步（已有的会跳过早于 2 天的部分）
        - sync_missing：只在 daily_kline 表里完全没有数据的才同步

        用于"自选股清单第一次接入"或"补齐漏掉的股票"场景

        Args:
            ts_codes: 股票代码列表
            days: 同步天数

        Returns:
            每只股票的更新条数
        """
        if not ts_codes:
            return {}

        with get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join(["?"] * len(ts_codes))
            cursor.execute(
                f"SELECT DISTINCT ts_code FROM daily_kline WHERE ts_code IN ({placeholders})",
                ts_codes,
            )
            have = {row["ts_code"] for row in cursor.fetchall()}

        missing = [c for c in ts_codes if c not in have]
        logger.info(f"sync_missing: 共 {len(ts_codes)} 只，已有 {len(have)} 只，需补齐 {len(missing)} 只")

        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        results = {}
        for code in missing:
            count = self.sync_daily_kline(code, start_date=start_date)
            results[code] = count
        return results

    def sync_all_daily_kline(self, ts_codes: list[str] | None = None, days: int = 730) -> dict[str, int]:
        """批量同步日线数据（并发，含智能跳过）"""
        if ts_codes is None:
            ts_codes = self._fetch_all_codes("SELECT ts_code FROM stock_basic")

        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        def _sync_one(code: str) -> int:
            # 近2天已同步则跳过
            last_date = self._get_last_date("daily_kline", code)
            if last_date and (datetime.now() - datetime.strptime(last_date, "%Y%m%d")).days < 2:
                return 0
            return self.sync_daily_kline(code, start_date, end_date)

        return self._batch_sync("日线数据", _sync_one, ts_codes)

    # ==================== 指数日线 ====================

    def sync_index_daily(self, ts_code: str, start_date: str | None = None, end_date: str | None = None) -> int:
        """同步单个指数的日线行情（增量更新）。

        Args:
            ts_code: 指数代码，如 '000001.SH'
            start_date: 起始日期 YYYYMMDD；None 表示从 sync_log 记录的最后成功
                日期次日开始，无记录则默认回看 730 天
            end_date: 结束日期 YYYYMMDD；None 表示今天

        Returns:
            入库条数；失败返回 0
        """
        init_database(verbose=False)

        # 增量更新：接着上次成功同步的日期往后拉
        if start_date is None:
            last_date = self._get_last_date("index_daily", ts_code)
            if last_date:
                last_dt = datetime.strptime(last_date, "%Y%m%d")
                start_date = (last_dt + timedelta(days=1)).strftime("%Y%m%d")

        if start_date is None:
            start_date = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        try:
            df = self._call_api_with_retry(
                "index_daily",
                self._fetcher.fetch_index_daily,
                ts_code,
                start_date,
                end_date,
            )

            if df is None or len(df) == 0:
                # 空结果有两种成因：该区间真的没有行情，或接口限流被静默吞掉。
                # 必须落一条 sync_log，否则 index_daily 一直是 0 行却查不到任何失败记录。
                self._log_sync("index_daily", ts_code, "", "failed", f"接口返回空（{start_date}~{end_date}），疑似限流")
                logger.warning("指数日线返回空: %s %s~%s（疑似接口限流）", ts_code, start_date, end_date)
                return 0

            records = []
            for row in df.itertuples(index=False):
                row_dict = row._asdict()
                records.append(
                    (
                        str(row_dict.get("ts_code", "") or ts_code),
                        str(row_dict.get("trade_date", "") or ""),
                        row_dict.get("open"),
                        row_dict.get("high"),
                        row_dict.get("low"),
                        row_dict.get("close"),
                        row_dict.get("pre_close"),
                        row_dict.get("change"),
                        row_dict.get("pct_chg"),
                        row_dict.get("vol"),
                        row_dict.get("amount"),
                    )
                )

            with get_connection() as conn:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO index_daily
                    (ts_code, trade_date, open, high, low, close,
                     pre_close, change, pct_chg, vol, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    records,
                )

            latest_date = max(rec[1] for rec in records)
            self._log_sync("index_daily", ts_code, latest_date, "success", f"rows={len(records)}")
            logger.info("指数日线同步完成: %s, %s 条, %s-%s", ts_code, len(records), start_date, latest_date)
            return len(records)

        except Exception as e:
            logger.error("指数日线同步失败 %s: %s", ts_code, e)
            self._log_sync("index_daily", ts_code, "", "failed", str(e)[:500])
            return 0

    def sync_all_index_daily(
        self,
        ts_codes: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, int]:
        """批量同步指数日线；ts_codes 默认为 DEFAULT_INDEXES。

        **刻意串行，不走 _batch_sync 的 5 线程并发**：index_daily 是强限流接口
        （实测该中转源上 65 秒间隔逐个请求仍大量返回空），而 _rate_limit 只有
        全局 180 rpm 的桶、没有 per-endpoint 概念，5 个并发请求会瞬间打满这个
        接口自己的配额，结果是一条都拿不到。指数只有 7 个，串行的代价可以忽略。
        """
        if ts_codes is None:
            ts_codes = list(DEFAULT_INDEXES)

        results: dict[str, int] = {}
        for code in ts_codes:
            try:
                results[code] = self.sync_index_daily(code, start_date, end_date)
            except Exception as exc:
                logger.error("指数日线同步失败 %s: %s", code, exc)
                results[code] = 0
        success = sum(1 for v in results.values() if v > 0)
        logger.info("指数日线批量同步完成，成功 %s/%s", success, len(ts_codes))
        return results

    # ==================== 指标缓存 ====================

    def sync_indicator_cache(self, ts_code: str, days: int = 120) -> int:
        """同步单只股票技术指标到 indicator_cache 表

        计算流程：
        1. 加载 K 线数据
        2. 预计算 KDJ / MACD 全量序列（O(n)，避免循环内 O(n²)）
        3. 逐日计算指标 → 构建 INSERT 行 → 批量写入
        """
        try:
            f = _get_indicator_funcs()
            klines = f.get_kline_data(ts_code, days)
            if not klines:
                return 0

            # 预计算 O(n) 序列
            kdj_seq = f.precompute_kdj_sequence(klines) if len(klines) >= 9 else None
            if len(klines) >= 30:
                macd_dif_seq, macd_dea_seq, macd_hist_seq = f.precompute_macd_sequence(klines)
            else:
                macd_dif_seq = macd_dea_seq = macd_hist_seq = None

            cols = [c.strip() for c in _INDICATOR_INSERT_COLUMNS.split(",")]
            insert_sql = f"INSERT OR REPLACE INTO indicator_cache ({_INDICATOR_INSERT_COLUMNS}) VALUES ({','.join(['?'] * len(cols))})"

            with get_connection() as conn:
                cursor = conn.cursor()
                for i, kline in enumerate(klines):
                    sub_klines = klines[: i + 1]
                    yesterday = sub_klines[-2] if len(sub_klines) > 1 else None

                    ind = _compute_day_indicators(
                        f,
                        sub_klines,
                        kline,
                        yesterday,
                        kdj_seq,
                        macd_dif_seq,
                        macd_dea_seq,
                        macd_hist_seq,
                        i,
                    )
                    row = _build_indicator_row(ts_code, kline.trade_date, ind)
                    cursor.execute(insert_sql, row)

            self._log_sync("indicator_cache", ts_code, klines[-1].trade_date, "success")
            logger.info(f"指标缓存同步完成: {ts_code}, {len(klines)} 条")
            return len(klines)

        except Exception as e:
            logger.error(f"指标缓存同步失败 {ts_code}: {e}")
            self._log_sync("indicator_cache", ts_code, "", "failed", str(e))
            return 0

    def sync_all_indicators(self, ts_codes: list[str] | None = None) -> dict[str, int]:
        """批量同步指标缓存（并发）"""
        if ts_codes is None:
            ts_codes = self._fetch_all_codes("SELECT DISTINCT ts_code FROM daily_kline")
        return self._batch_sync("指标缓存", self.sync_indicator_cache, ts_codes)

    def sync_daily_and_compute(self, ts_codes: list[str] | None = None, days: int = 730) -> dict[str, int]:
        """
        一站式：同步日线 K 线 + 同步指标缓存

        这是 scripts/sync_and_compute.py 业务逻辑的接收方
        （v2.10.0 之前是 ~300 行的内联实现）

        Args:
            ts_codes: 股票代码列表，None = 全市场
            days: 同步天数

        Returns:
            每只股票的指标更新条数（dict[ts_code] = count）
        """
        kline_results = self.sync_all_daily_kline(ts_codes=ts_codes, days=days)
        # 同步哪些股票有数据，传给指标计算
        if ts_codes is None:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT ts_code FROM daily_kline")
                ts_codes_for_indic = [row["ts_code"] for row in cursor.fetchall()]
        else:
            ts_codes_for_indic = [c for c, n in kline_results.items() if n > 0]
        return self.sync_all_indicators(ts_codes=ts_codes_for_indic or None)

    # ==================== Tushare 官方指标（用于 diff 验证） ====================

    def sync_stk_factor(self, ts_code: str, start_date: str | None = None, end_date: str | None = None) -> int:
        """
        同步单只股票的 Tushare 官方技术指标（stk_factor 接口）

        Args:
            ts_code: 股票代码
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD

        Returns:
            更新条数
        """
        try:
            if start_date is None:
                start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
            if end_date is None:
                end_date = datetime.now().strftime("%Y%m%d")

            df = self._call_api_with_retry(
                "stk_factor",
                self._fetcher.fetch_stk_factor,
                ts_code,
                start_date,
                end_date,
            )

            if df is None or len(df) == 0:
                return 0

            # 字段映射：Tushare 字段名 -> 数据库字段名
            field_map = {
                "ts_code": "ts_code",
                "trade_date": "trade_date",
                "close": "close",
                "macd_dif": "macd_dif",
                "macd_dea": "macd_dea",
                "macd": "macd",
                "kdj_k": "kdj_k",
                "kdj_d": "kdj_d",
                "kdj_j": "kdj_j",
                "rsi_6": "rsi_6",
                "rsi_12": "rsi_12",
                "rsi_24": "rsi_24",
                "boll_upper": "boll_upper",
                "boll_mid": "boll_mid",
                "boll_lower": "boll_lower",
                "cci": "cci",
            }

            with get_connection() as conn:
                cursor = conn.cursor()
                records = []
                for row in df.itertuples(index=False):
                    row_dict = row._asdict()
                    values = [row_dict.get(field_map.get(k, k), 0) for k in field_map.keys()]
                    records.append(values)

                cursor.executemany(
                    """
                    INSERT OR REPLACE INTO tushare_indicator_cache
                    (ts_code, trade_date, close, macd_dif, macd_dea, macd,
                     kdj_k, kdj_d, kdj_j, rsi_6, rsi_12, rsi_24,
                     boll_upper, boll_mid, boll_lower, cci)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    records,
                )

            latest_date = df["trade_date"].max()
            self._log_sync("stk_factor", ts_code, latest_date, "success")
            logger.info(f"Tushare 指标同步完成: {ts_code}, {len(df)} 条")
            return len(df)

        except Exception as e:
            logger.error(f"Tushare 指标同步失败 {ts_code}: {e}")
            self._log_sync("stk_factor", ts_code, "", "failed", str(e))
            return 0

    def sync_all_stk_factor(self, ts_codes: list[str] | None = None, days: int = 365) -> dict[str, int]:
        """批量同步 Tushare 官方指标（并发）"""
        if ts_codes is None:
            ts_codes = self._fetch_all_codes("SELECT ts_code FROM stock_basic")

        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        return self._batch_sync("Tushare指标", lambda code: self.sync_stk_factor(code, start_date, end_date), ts_codes)

    # ==================== 每日估值指标 (PE/PB/PS) ====================

    def ensure_daily_basic_columns(self):
        """确保 daily_kline 表包含 PE/PB/PS/总市值/流通市值 列"""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(daily_kline)")
            existing = {row[1] for row in cursor.fetchall()}

            for col_name, col_type in [
                ("pe", "REAL"),
                ("pe_ttm", "REAL"),
                ("pb", "REAL"),
                ("ps", "REAL"),
                ("ps_ttm", "REAL"),
                ("total_mv", "REAL"),
                ("circ_mv", "REAL"),
            ]:
                if col_name not in existing:
                    cursor.execute(f"ALTER TABLE daily_kline ADD COLUMN {col_name} {col_type}")
                    logger.info(f"Added column {col_name} to daily_kline")

    def sync_daily_basic(self, ts_code: str, start_date: str = "", end_date: str = "") -> int:
        """
        同步单只股票的每日估值指标（PE/PB/PS/市值等）

        使用 Tushare daily_basic 接口，数据写入 daily_kline 表对应列。

        Args:
            ts_code: 股票代码
            start_date: 起始日期 YYYYMMDD，默认 2 年前
            end_date: 结束日期 YYYYMMDD，默认今天

        Returns:
            更新条数
        """
        try:
            self.ensure_daily_basic_columns()
            self._rate_limit("daily_basic")

            if not start_date:
                start_date = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")
            if not end_date:
                end_date = datetime.now().strftime("%Y%m%d")

            df = self._fetcher.fetch_daily_basic(ts_code, start_date, end_date)

            if df is None or len(df) == 0:
                return 0

            with get_connection() as conn:
                cursor = conn.cursor()
                for row in df.itertuples(index=False):
                    row_dict = row._asdict()
                    cursor.execute(
                        """
                        UPDATE daily_kline SET
                            pe = ?, pe_ttm = ?, pb = ?, ps = ?, ps_ttm = ?,
                            total_mv = ?, circ_mv = ?
                        WHERE ts_code = ? AND trade_date = ?
                    """,
                        (
                            row_dict.get("pe"),
                            row_dict.get("pe_ttm"),
                            row_dict.get("pb"),
                            row_dict.get("ps"),
                            row_dict.get("ps_ttm"),
                            row_dict.get("total_mv"),
                            row_dict.get("circ_mv"),
                            row_dict["ts_code"],
                            row_dict["trade_date"],
                        ),
                    )

            self._log_sync("daily_basic", ts_code, end_date, "success")
            return len(df)

        except Exception as e:
            logger.error(f"每日估值指标同步失败 {ts_code}: {e}")
            self._log_sync("daily_basic", ts_code, "", "failed", str(e))
            return 0

    def sync_all_daily_basic(self, ts_codes: list[str] | None = None, days: int = 730) -> dict[str, int]:
        """批量同步每日估值指标（并发）"""
        self.ensure_daily_basic_columns()
        if ts_codes is None:
            ts_codes = self._fetch_all_codes("SELECT DISTINCT ts_code FROM daily_kline ORDER BY ts_code")

        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        return self._batch_sync("估值指标", lambda code: self.sync_daily_basic(code, start_date, end_date), ts_codes)

    # ==================== 资金流向 ====================

    def sync_moneyflow(self, ts_code: str, trade_date: str) -> int:
        """
        同步单只股票的单日资金流向

        Args:
            ts_code: 股票代码
            trade_date: 交易日期，格式 YYYYMMDD

        Returns:
            更新条数
        """
        try:
            self._rate_limit("moneyflow")
            df = self._fetcher.fetch_moneyflow(ts_code, trade_date)

            if df is None or len(df) == 0:
                return 0

            with get_connection() as conn:
                cursor = conn.cursor()
                records = []
                for row in df.itertuples(index=False):
                    row_dict = row._asdict()
                    records.append(
                        (
                            row_dict["ts_code"],
                            row_dict["trade_date"],
                            row_dict.get("buy_sm_amount"),
                            row_dict.get("buy_md_amount"),
                            row_dict.get("buy_lg_amount"),
                            row_dict.get("buy_elg_amount"),
                            row_dict.get("sell_sm_amount"),
                            row_dict.get("sell_md_amount"),
                            row_dict.get("sell_lg_amount"),
                            row_dict.get("sell_elg_amount"),
                            row_dict.get("net_mf"),
                            row_dict.get("pct_mf"),
                        )
                    )

                cursor.executemany(
                    """
                    INSERT OR REPLACE INTO moneyflow
                    (ts_code, trade_date, buy_sm_amount, buy_md_amount,
                     buy_lg_amount, buy_elg_amount, sell_sm_amount,
                     sell_md_amount, sell_lg_amount, sell_elg_amount,
                     net_mf, pct_mf)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    records,
                )

            self._log_sync("moneyflow", ts_code, trade_date, "success")
            return len(df)

        except Exception as e:
            logger.error(f"资金流向同步失败 {ts_code} {trade_date}: {e}")
            self._log_sync("moneyflow", ts_code, "", "failed", str(e))
            return 0

    # ==================== 工具方法 ====================

    def get_sync_status(self) -> dict[str, Any]:
        """获取同步状态"""
        init_database(verbose=False)
        with get_connection() as conn:
            cursor = conn.cursor()

            # 各表数据量
            cursor.execute("SELECT COUNT(*) as cnt FROM stock_basic")
            stock_count = cursor.fetchone()["cnt"]

            cursor.execute("SELECT COUNT(*) as cnt FROM daily_kline")
            kline_count = cursor.fetchone()["cnt"]

            # 最后同步时间
            cursor.execute("""
                SELECT data_type, last_date, status, created_at
                FROM sync_log
                WHERE id IN (
                    SELECT MAX(id) FROM sync_log GROUP BY data_type
                )
            """)
            sync_status = [dict(row) for row in cursor.fetchall()]

            return {
                "stock_count": stock_count,
                "kline_count": kline_count,
                "db_path": str(get_db_path()),
                "sync_status": sync_status,
            }
