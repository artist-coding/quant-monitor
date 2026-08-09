"""每日收盘后编排器。

把原本散落在各处的"同步 → 刷指标 → 评分 → 决策"手工步骤串成一条
可重跑、可观测的链：

    1. 确定目标交易日（非交易日直接 skipped）
    2. 交易日历缓存（trade_cal，按整年拉取，规避 1 次/分钟限流）
    3. 全市场日线（daily_kline）
    4. 宽基指数日线（index_daily）
    5. 票池 K 线补齐 + 指标缓存重算（indicator_cache）
    6. 票池综合评分落库（daily_scores）+ 大盘环境快照
    7. 主线/行业强度排名落库（theme_strength）        ← 阶段1
    8. 票池买点确认落库（buy_decisions）              ← 阶段1

步骤 7 必须排在 8 之前：买点确认的主线层直接读 theme_strength 表。

设计约束：
- **每一步都能独立失败而不中断整条链**。任何一步抛异常只记录到 errors 里，
  后续步骤照常执行。这样即使 Tushare 某个接口挂了，本地能算的部分仍然算完。
- **全程幂等**。所有落库都是 INSERT OR REPLACE，同一天重跑不会产生脏数据。
- 只产出买入侧结论，不下单。三态里的"今日尾盘卖出"需要盘中快照才能赶在
  收盘前给出，属于后续的盘中任务；实盘/模拟盘撮合是阶段2 的事。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from .data_sync.syncer import _normalize_trade_date
from .database import get_connection, init_database

logger = logging.getLogger(__name__)


# ==================== 大盘环境常量 ====================

# 主参考指数：沪深300（覆盖大盘蓝筹，代表性最强）
MARKET_PRIMARY_INDEX = "000300.SH"
# 辅参考指数：上证指数（沪深300 缺数据时降级使用）
MARKET_FALLBACK_INDEX = "000001.SH"

# 强度基准分：50 分代表"多空均衡、无明确方向"
_STRENGTH_BASE = 50.0
# 当日涨跌幅的换算系数：涨跌 1% 折算 10 分，并在 ±8 分处截断。
# 截断值刻意小于方向阈值的 ±10，使涨跌幅无法单独决定方向（见下方阈值注释），
# 同时避免单日暴涨暴跌（如 ±5%）把强度顶到极值、淹没均线信息。
_PCT_TO_SCORE = 10.0
_PCT_SCORE_CAP = 8.0
# 收盘价站上 MA5（短期均线）加 15 分，跌破减 15 分
_MA5_WEIGHT = 15.0
# 收盘价站上 MA20（月线，中期趋势分水岭）加 15 分，跌破减 15 分
_MA20_WEIGHT = 15.0
# 方向判定阈值：>=60 偏多，<=40 偏空，中间视为中性。
# 越过阈值需要 ±10 分，而涨跌幅单项上限是 ±8 分（见 _PCT_SCORE_CAP），
# 因此单靠涨跌幅无法定方向，必须叠加至少一条均线——
# 这样可以过滤掉"跌势中的单日反弹"这类噪音。
_DIR_LONG_THRESHOLD = 60.0
_DIR_SHORT_THRESHOLD = 40.0

# ==================== 市场宽度常量 ====================
#
# 为什么需要宽度：实测当前 Tushare 中转源的 index_daily 接口基本不可用
# （000001.SH / 399001.SZ / 399006.SZ / 000905.SH / 000852.SH / 000016.SH
# 在 65 秒间隔下全部返回 0 行，000300.SH 也只偶尔成功），
# 只依赖指数会让大盘环境永远降级成 NEUTRAL/50，等于没有。
# 而全市场日线每天都在同步，5500 只股票的涨跌分布本身就是更强的大盘信号
# ——它反映的是"多少票在涨"，比单一指数（被权重股绑架）更贴近实际赚钱效应。

# A 股在市约 5500 只。样本不足此数说明当天不是全市场同步日
# （历史遗留的部分同步日只有约 712 只，且是按 ts_code 排序截断的偏样本），
# 据此算宽度会得出错误结论，直接判为不可用。
_BREADTH_MIN_SAMPLE = 3000
# 上涨家数占比的方向阈值
_BREADTH_LONG_RATIO = 60.0
_BREADTH_SHORT_RATIO = 40.0
# 涨停/跌停净差对强度的放大系数（涨跌停反映的是情绪极值，权重给高一点）
_BREADTH_LIMIT_WEIGHT = 2.0


def _build_syncer():
    """构造 DataSyncer（独立成函数，便于测试 monkeypatch 掉真实网络）。"""
    from .data_sync import DataSyncer
    from .datasource import get_datasource

    return DataSyncer(datasource=get_datasource("tushare"))


def _clamp(value: float, low: float, high: float) -> float:
    """把数值夹在 [low, high] 区间内。"""
    return max(low, min(high, value))


def _load_index_rows(ts_code: str, trade_date: str, limit: int = 20) -> list[tuple[str, float, float]]:
    """读取某指数截至 trade_date 的最近 limit 根日线（新→旧）。"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT trade_date, close, pct_chg
            FROM index_daily
            WHERE ts_code = ? AND trade_date <= ? AND close IS NOT NULL
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (ts_code, trade_date, limit),
        ).fetchall()
    return [(str(r[0]), float(r[1]), float(r[2] or 0.0)) for r in rows]


def compute_market_breadth(trade_date: str) -> dict[str, Any]:
    """从全市场日线算市场宽度（不依赖 index_daily 接口）。

    宽度衡量的是"多少票在涨"，即赚钱效应，比单一指数更贴近实际盘感——
    指数可能被少数权重股拉起来，而同期八成个股在跌。

    Args:
        trade_date: 目标交易日 YYYYMMDD

    Returns:
        {"available": bool, ...}；available=False 时其余字段不可用，原因见 reason
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN pct_chg < 0 THEN 1 ELSE 0 END),
                   SUM(COALESCE(is_limit_up, 0)),
                   SUM(COALESCE(is_limit_down, 0))
            FROM daily_kline
            WHERE trade_date = ? AND pct_chg IS NOT NULL
            """,
            (trade_date,),
        ).fetchone()

        total = int(row[0] or 0)
        if total < _BREADTH_MIN_SAMPLE:
            return {
                "available": False,
                "total": total,
                "reason": f"{trade_date} 仅 {total} 只样本（阈值 {_BREADTH_MIN_SAMPLE}），非全市场同步日，宽度不可信",
            }

        up, down = int(row[1] or 0), int(row[2] or 0)
        limit_up, limit_down = int(row[3] or 0), int(row[4] or 0)
        # 中位数比均值抗极端值：少数暴涨暴跌股不会带偏整体判断
        median = float(
            conn.execute(
                """
                SELECT pct_chg FROM daily_kline
                WHERE trade_date = ? AND pct_chg IS NOT NULL
                ORDER BY pct_chg LIMIT 1 OFFSET ?
                """,
                (trade_date, total // 2),
            ).fetchone()[0]
        )

    up_ratio = up / total * 100
    # 强度以"上涨家数占比"为主体（占比 50% 即中性 50 分），
    # 再用涨跌停净差做微调——涨跌停是情绪极值，同样的占比下涨停多的市场更强。
    limit_skew = (limit_up - limit_down) / total * 100
    strength = _clamp(up_ratio + limit_skew * _BREADTH_LIMIT_WEIGHT, 0.0, 100.0)

    if up_ratio >= _BREADTH_LONG_RATIO and median > 0:
        direction = "LONG"
    elif up_ratio <= _BREADTH_SHORT_RATIO and median < 0:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    return {
        "available": True,
        "market_dir": direction,
        "median_pct_chg": round(median, 4),
        "strength": round(strength, 2),
        "total": total,
        "up": up,
        "down": down,
        "up_ratio": round(up_ratio, 2),
        "limit_up": limit_up,
        "limit_down": limit_down,
    }


def compute_market_context(trade_date: str) -> dict[str, Any]:
    """计算大盘环境（阶段0 最小可用版本）。

    数据源按可得性分两层：

    1. **市场宽度（首选）**——从全市场 daily_kline 算涨跌家数比、涨跌停净差、
       中位数涨跌幅。每日全市场同步只要成功，这一层就一定可用。
    2. **指数（补充）**——沪深300 / 上证指数的涨跌幅与 MA5/MA20 位置。
       当前中转数据源的 index_daily 接口基本不可用，故只作为补充，
       在宽度不可用时才用它定方向。

    刻意不做多因子加权、不做择时模型——这里是阶段1 真实大盘因子的地基，
    优先保证正确、可读、可复现。

    Args:
        trade_date: 目标交易日 YYYYMMDD

    Returns:
        {
            "market_dir": "LONG" | "NEUTRAL" | "SHORT",
            "market_pct_chg": float,      # 大盘涨跌幅（%）：宽度可用时取全市场中位数，否则取指数
            "market_strength": float,     # 0-100 强度，50 为均衡
            "detail": {...}               # 计算依据与数据来源，便于排障
        }
    """
    detail: dict[str, Any] = {
        "primary": MARKET_PRIMARY_INDEX,
        "fallback": MARKET_FALLBACK_INDEX,
        "trade_date": trade_date,
    }

    # ── 第一层：市场宽度（首选，只要当天做过全市场同步就一定可用）──
    try:
        breadth = compute_market_breadth(trade_date)
    except Exception as exc:  # 表不存在 / DB 异常都降级为"无数据"
        breadth = {"available": False, "reason": f"宽度计算异常: {exc}"}
    detail["breadth"] = breadth

    # ── 第二层：指数（补充；当前数据源多半拿不到）──
    rows: list[tuple[str, float, float]] = []
    used_code = ""
    try:
        for code in (MARKET_PRIMARY_INDEX, MARKET_FALLBACK_INDEX):
            rows = _load_index_rows(code, trade_date)
            if rows:
                used_code = code
                break
    except Exception as exc:
        detail["error"] = str(exc)
        rows = []

    if breadth.get("available"):
        # 宽度可用即以它为准：它直接反映赚钱效应，而指数会被权重股绑架。
        detail["source"] = "breadth"
        if rows:
            # 指数拿得到就顺带记下来，供排障与阶段1 对比，但不参与定方向
            detail["index_ref"] = {"ts_code": used_code, "latest_date": rows[0][0], "pct_chg": rows[0][2]}
        return {
            "market_dir": breadth["market_dir"],
            "market_pct_chg": breadth["median_pct_chg"],
            "market_strength": breadth["strength"],
            "detail": detail,
        }

    if not rows:
        detail["source"] = "none"
        detail["reason"] = (
            "市场宽度不可用（当日非全市场同步日），且 index_daily 也无数据"
            "（沪深300 与上证指数均为空），大盘环境降级为中性"
        )
        return {
            "market_dir": "NEUTRAL",
            "market_pct_chg": 0.0,
            "market_strength": _STRENGTH_BASE,
            "detail": detail,
        }

    detail["source"] = "index"

    latest_date, close, pct_chg = rows[0]
    closes = [r[1] for r in rows]

    # MA5 / MA20：样本不足时置 None，对应权重不参与计算（不做 0 填充，
    # 否则新上市或刚回填的指数会被误判成"跌破均线"）。
    ma5 = sum(closes[:5]) / 5 if len(closes) >= 5 else None
    ma20 = sum(closes[:20]) / 20 if len(closes) >= 20 else None

    strength = _STRENGTH_BASE
    contributions: dict[str, float] = {}

    pct_part = _clamp(pct_chg * _PCT_TO_SCORE, -_PCT_SCORE_CAP, _PCT_SCORE_CAP)
    strength += pct_part
    contributions["pct_chg"] = round(pct_part, 2)

    if ma5 is not None:
        ma5_part = _MA5_WEIGHT if close >= ma5 else -_MA5_WEIGHT
        strength += ma5_part
        contributions["ma5"] = ma5_part
    if ma20 is not None:
        ma20_part = _MA20_WEIGHT if close >= ma20 else -_MA20_WEIGHT
        strength += ma20_part
        contributions["ma20"] = ma20_part

    strength = round(_clamp(strength, 0.0, 100.0), 2)

    if strength >= _DIR_LONG_THRESHOLD:
        market_dir = "LONG"
    elif strength <= _DIR_SHORT_THRESHOLD:
        market_dir = "SHORT"
    else:
        market_dir = "NEUTRAL"

    detail.update(
        {
            "ts_code": used_code,
            "latest_date": latest_date,
            "is_current": latest_date == trade_date,
            "close": round(close, 4),
            "ma5": round(ma5, 4) if ma5 is not None else None,
            "ma20": round(ma20, 4) if ma20 is not None else None,
            "bars": len(rows),
            "contributions": contributions,
        }
    )
    if latest_date != trade_date:
        detail["reason"] = f"index_daily 无 {trade_date} 数据，回退使用最近一根 {latest_date}"

    return {
        "market_dir": market_dir,
        "market_pct_chg": round(pct_chg, 4),
        "market_strength": strength,
        "detail": detail,
    }


def _new_step(status: str = "pending") -> dict[str, Any]:
    """构造单个步骤的结果骨架。"""
    return {"status": status, "elapsed": 0.0, "rows": 0, "message": ""}


def _year_cal_cached(year: str, exchange: str = "SSE") -> bool:
    """判断某自然年的交易日历是否已在本地缓存。

    交易日历一年约 365 行，这里用 300 行作为"整年已落库"的下限判据；
    命中即跳过 sync_trade_cal，避免白白消耗 1 次/分钟的限流额度。
    """
    try:
        with get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM trade_cal WHERE exchange = ? AND cal_date LIKE ?",
                (exchange, f"{year}%"),
            ).fetchone()[0]
        return int(count) >= 300
    except Exception:
        return False


def _index_coverage() -> tuple[int, str]:
    """返回 index_daily 已落库的指数只数与最新交易日（空库返回 (0, "")）。"""
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT COUNT(DISTINCT ts_code), MAX(trade_date) FROM index_daily").fetchone()
        return int(row[0] or 0), str(row[1] or "")
    except Exception:
        return 0, ""


def _save_daily_scores(data_dates: dict[str, str], scored: list[Any], market: dict[str, Any]) -> int:
    """把票池评分写入 daily_scores（INSERT OR REPLACE 保证重跑幂等）。

    Args:
        data_dates: {ts_code: 该票评分实际所用的最新K线日期}。
            **不能用编排器的 target 当主键**：screener.analyze_stock 固定取"库里最新
            150 根"，数据没同步上来时 target 与实际数据日会不一致，按 target 落库
            等于把旧评分写成新日期的行。这里按每只票的真实数据日落库。
        scored: StockScore 列表
        market: compute_market_context 的返回值
    """
    if not scored:
        return 0

    records = []
    for score in scored:
        trade_date = data_dates.get(score.ts_code)
        if not trade_date:
            # 拿不到数据日就不写——宁可少一行，也不要写一行日期存疑的记录
            logger.warning("跳过 %s 的评分落库：未取到实际数据日期", score.ts_code)
            continue
        records.append(
            (
                score.ts_code,
                trade_date,
                getattr(score, "name", "") or "",
                float(getattr(score, "score", 0) or 0),
                float(getattr(score, "b1_score", 0) or 0),
                float(getattr(score, "trend_score", 0) or 0),
                float(getattr(score, "volume_score", 0) or 0),
                float(getattr(score, "risk_score", 0) or 0),
                getattr(score, "rating", "") or "",
                market["market_dir"],
                market["market_pct_chg"],
                market["market_strength"],
                json.dumps(list(getattr(score, "reasons", []) or []), ensure_ascii=False),
                json.dumps(list(getattr(score, "warnings", []) or []), ensure_ascii=False),
            )
        )

    with get_connection() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO daily_scores
            (ts_code, trade_date, name, score, b1_score, trend_score, volume_score,
             risk_score, rating, market_dir, market_pct_chg, market_strength,
             reasons, warnings)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )
    return len(records)


def run_daily_pipeline(
    trade_date: str | None = None,
    *,
    skip_market: bool = False,
    skip_index: bool = False,
    skip_indicators: bool = False,
    skip_scores: bool = False,
    skip_themes: bool = False,
    skip_buy: bool = False,
    watchlist_days: int = 250,
    theme_lookback: int = 5,
    pick_top_n: int = 5,
    pick_min_group_strength: float = 50.0,
    pick_max_per_group: int | None = None,
) -> dict[str, Any]:
    """执行每日收盘后全流程。

    Args:
        trade_date: 目标交易日 YYYYMMDD；None 表示今天
        skip_market: 跳过全市场日线同步
        skip_index: 跳过宽基指数日线同步
        skip_indicators: 跳过票池 K 线补齐与指标缓存重算
        skip_scores: 跳过票池评分落库
        skip_themes: 跳过主线/行业强度排名
        skip_buy: 跳过票池买点确认
        watchlist_days: 票池指标缓存回溯天数。默认 250 是有原因的——双线战法
            需要 ≥115 根 K 线，而 sync_indicator_cache 逐日重算，前 115 行的
            双线字段必然为 0，250 天可留出约 135 行可用数据。
        theme_lookback: 主线强度的统计窗口（交易日）
        pick_top_n: 第二阶段最多选几只
        pick_min_group_strength: 第二阶段的主线/行业强度门槛
        pick_max_per_group: 第二阶段每个主线/行业最多选几只（None=不限，允许集中）

    Returns:
        结构化摘要 dict，含 status / trade_date / steps / market /
        top_scores / theme_ranking / buy_decisions / final_picks / errors
    """
    started = time.perf_counter()
    raw_target = (trade_date or datetime.now().strftime("%Y%m%d")).strip()
    # 必须在任何步骤之前校验：target 是 daily_scores 的主键组成部分，
    # 格式错误的日期会写进一条永远不会被正确重跑覆盖的脏行。
    # 下游各步骤都有自己的 try/except，光靠它们兜不住——异常会被吞掉后继续往下跑。
    try:
        target = _normalize_trade_date(raw_target)
    except ValueError as exc:
        return {
            "status": "failed",
            "trade_date": raw_target,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "elapsed": round(time.perf_counter() - started, 3),
            "is_trade_day": None,
            "steps": {},
            "market": {},
            "watchlist_count": 0,
            "top_scores": [],
            "theme_ranking": {},
            "buy_decisions": [],
            "final_picks": [],
            "warnings": [],
            "errors": [f"交易日期格式错误: {exc}"],
        }

    result: dict[str, Any] = {
        "status": "success",
        "trade_date": target,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed": 0.0,
        "is_trade_day": None,
        "steps": {},
        "market": {},
        "watchlist_count": 0,
        "top_scores": [],
        "theme_ranking": {},
        "buy_decisions": [],
        "final_picks": [],
        "warnings": [],
        "errors": [],
    }
    steps: dict[str, dict[str, Any]] = result["steps"]

    def _finish(status: str) -> dict[str, Any]:
        result["status"] = status
        result["elapsed"] = round(time.perf_counter() - started, 3)
        return result

    init_database(verbose=False)

    try:
        syncer = _build_syncer()
    except Exception as exc:
        result["errors"].append(f"DataSyncer 构造失败: {exc}")
        logger.error("DataSyncer 构造失败: %s", exc)
        return _finish("failed")

    # ── 步骤 1：确定目标交易日 ──
    is_open: bool | None = None
    try:
        is_open = syncer.is_trade_day(target)
    except Exception as exc:
        result["errors"].append(f"交易日判断失败: {exc}")
        logger.warning("交易日判断失败 %s: %s", target, exc)
    result["is_trade_day"] = is_open

    if is_open is False:
        steps["trade_day_check"] = {
            **_new_step("skipped"),
            "message": f"{target} 非交易日，整条流水线跳过",
        }
        return _finish("skipped")
    if is_open is None:
        # 日历不可用（限流 / 网络）时不阻断，按"可能是交易日"继续跑。
        result["warnings"].append(f"交易日历不可用，无法确认 {target} 是否为交易日，按交易日继续执行")
    steps["trade_day_check"] = {**_new_step("success"), "message": f"is_trade_day={is_open}"}

    # ── 步骤 2：交易日历缓存（整年粒度，规避 1 次/分钟限流）──
    step = _new_step()
    t0 = time.perf_counter()
    try:
        year = target[:4]
        if _year_cal_cached(year):
            step.update(status="skipped", message=f"{year} 年交易日历已缓存")
        elif is_open is None:
            # is_trade_day 内部未命中缓存时已经拉过一次整年日历并失败（多半是
            # trade_cal 的 1 次/分钟限流）。同一分钟内重试必然再失败，白白多打
            # 一次接口，因此这里直接跳过，不重复调用。
            step.update(
                status="skipped",
                message=f"{year} 年交易日历拉取已在交易日判断阶段失败（接口限流 1 次/分钟），本步不重复调用",
            )
        else:
            rows = syncer.sync_trade_cal(f"{year}0101", f"{year}1231")
            step.update(
                status="success" if rows else "failed",
                rows=rows,
                message=f"{year} 年交易日历入库 {rows} 条",
            )
            if not rows:
                result["errors"].append(f"交易日历同步失败: {year}")
    except Exception as exc:
        step.update(status="failed", message=str(exc))
        result["errors"].append(f"交易日历同步异常: {exc}")
        logger.error("交易日历同步异常: %s", exc)
    step["elapsed"] = round(time.perf_counter() - t0, 3)
    steps["trade_cal"] = step

    # ── 步骤 3：全市场日线 ──
    step = _new_step()
    t0 = time.perf_counter()
    if skip_market:
        step.update(status="skipped", message="--skip-market")
    else:
        try:
            # 交易日判断已在步骤 1 完成，这里关掉重复检查，避免再打一次限流接口
            market_result = syncer.sync_market_daily(trade_date=target, check_trade_calendar=False)
            step.update(
                status=market_result.get("status", "failed"),
                rows=int(market_result.get("market_rows", 0) or 0),
                message=str(market_result.get("message", "")),
            )
            step["db_rows_for_date"] = market_result.get("db_rows_for_date", 0)
            if step["status"] == "failed":
                result["errors"].append(f"全市场日线同步失败: {step['message']}")
        except Exception as exc:
            step.update(status="failed", message=str(exc))
            result["errors"].append(f"全市场日线同步异常: {exc}")
            logger.error("全市场日线同步异常: %s", exc)
    step["elapsed"] = round(time.perf_counter() - t0, 3)
    steps["market_daily"] = step

    # ── 步骤 4：宽基指数日线 ──
    step = _new_step()
    t0 = time.perf_counter()
    if skip_index:
        step.update(status="skipped", message="--skip-index")
    else:
        try:
            index_result = syncer.sync_all_index_daily()
            total = sum(int(v or 0) for v in index_result.values())
            ok = sum(1 for v in index_result.values() if int(v or 0) > 0)
            covered, latest = _index_coverage()
            step["per_index"] = index_result
            step["covered_indexes"] = covered
            step["latest_index_date"] = latest
            # rows=0 有两种截然不同的含义，必须靠库里的实际覆盖情况区分：
            #   a) 本地已是最新 → 正常，success
            #   b) 全部调用失败 → 库里一条都没有，要让人看见
            if total == 0 and covered == 0:
                # 记 warning 而不是 error：实测当前中转数据源的 index_daily 接口
                # 长期不可用（7 个指数在 65 秒间隔下全部返回空），这一步几乎天天失败。
                # 天天报 error 只会让人对错误列表脱敏，真正的故障反而被淹没。
                # 而且大盘环境的首选数据源已经是全市场宽度（不依赖指数），
                # 指数缺失不再意味着大盘环境不可用——只是少了一路补充信息。
                step.update(
                    status="failed",
                    rows=0,
                    message=(
                        f"{len(index_result)} 个指数全部同步失败，index_daily 仍为空"
                        "（该数据源的 index_daily 接口长期不可用；大盘环境已改用全市场宽度，不受影响）"
                    ),
                )
                result["warnings"].append(step["message"])
            else:
                step.update(
                    status="success",
                    rows=total,
                    message=(
                        f"{ok}/{len(index_result)} 个指数有新增数据，共 {total} 条；"
                        f"库内覆盖 {covered} 个指数，最新 {latest or 'N/A'}"
                    ),
                )
        except Exception as exc:
            step.update(status="failed", message=str(exc))
            result["errors"].append(f"指数日线同步异常: {exc}")
            logger.error("指数日线同步异常: %s", exc)
    step["elapsed"] = round(time.perf_counter() - t0, 3)
    steps["index_daily"] = step

    # ── 票池（来源是 DB 的 watchlist 表）──
    codes: list[str] = []
    try:
        from . import watchlist as watchlist_mod

        codes = [w["ts_code"] for w in watchlist_mod.list_watch() if w.get("ts_code")]
    except Exception as exc:
        result["errors"].append(f"读取票池失败: {exc}")
        logger.error("读取票池失败: %s", exc)
    result["watchlist_count"] = len(codes)

    # ── 步骤 5：票池 K 线补齐 + 指标缓存重算 ──
    step = _new_step()
    t0 = time.perf_counter()
    if skip_indicators:
        step.update(status="skipped", message="--skip-indicators")
    elif not codes:
        step.update(status="skipped", message="票池为空")
    else:
        kline_rows = 0
        indicator_rows = 0
        failed: list[str] = []
        for code in codes:
            try:
                kline_rows += int(syncer.sync_daily_kline(code) or 0)
            except Exception as exc:
                failed.append(f"{code} K线:{exc}")
                logger.warning("票池 K 线同步失败 %s: %s", code, exc)
            try:
                indicator_rows += int(syncer.sync_indicator_cache(code, days=watchlist_days) or 0)
            except Exception as exc:
                failed.append(f"{code} 指标:{exc}")
                logger.warning("票池指标缓存失败 %s: %s", code, exc)
        step.update(
            status="failed" if len(failed) >= len(codes) * 2 else "success",
            rows=indicator_rows,
            message=f"{len(codes)} 只票，新增K线 {kline_rows} 条，指标缓存 {indicator_rows} 行",
        )
        step["kline_rows"] = kline_rows
        step["failed"] = failed
        if failed:
            result["errors"].extend(failed)
    step["elapsed"] = round(time.perf_counter() - t0, 3)
    steps["watchlist_indicators"] = step

    # ── 大盘环境（DB 只读，无论是否评分都算一次，便于排障）──
    try:
        market = compute_market_context(target)
    except Exception as exc:
        market = {
            "market_dir": "NEUTRAL",
            "market_pct_chg": 0.0,
            "market_strength": _STRENGTH_BASE,
            "detail": {"error": str(exc)},
        }
        result["errors"].append(f"大盘环境计算异常: {exc}")
        logger.error("大盘环境计算异常: %s", exc)
    result["market"] = market

    # ── 步骤 6：票池评分落库 ──
    step = _new_step()
    t0 = time.perf_counter()
    if skip_scores:
        step.update(status="skipped", message="--skip-scores")
    elif not codes:
        step.update(status="skipped", message="票池为空")
    else:
        try:
            from . import screener as screener_mod

            from .screener.data import get_recent_klines

            scored = []
            failed = []
            stale: list[str] = []
            # screener.analyze_stock 内部固定取"库里最新 150 根"，没有 end_date 参数，
            # 因此评分描述的其实是"最新数据日"而不是 target。若拿 target 当主键落库，
            # 数据没同步上来时就会把旧评分写成新日期的行——静默的脏数据。
            # 这里显式取出每只票的实际数据日，用它作为写库日期，并把不一致记进 warnings。
            data_dates: dict[str, str] = {}
            for code in codes:
                try:
                    klines = get_recent_klines(code, 150)
                    if not klines:
                        failed.append(f"{code} 评分:无K线数据")
                        continue
                    data_date = klines[-1].trade_date
                    data_dates[code] = data_date
                    if data_date != target:
                        stale.append(f"{code}@{data_date}")
                    scored.append(screener_mod.analyze_stock(code, klines=klines))
                except Exception as exc:
                    failed.append(f"{code} 评分:{exc}")
                    logger.warning("票池评分失败 %s: %s", code, exc)

            written = _save_daily_scores(data_dates, scored, market)
            if stale:
                result["warnings"].append(
                    f"以下票的最新K线日期不是 {target}，评分已按其实际数据日落库: {', '.join(stale)}"
                )
            scored.sort(key=lambda s: getattr(s, "score", 0) or 0, reverse=True)
            result["top_scores"] = [
                {
                    "ts_code": s.ts_code,
                    "name": getattr(s, "name", "") or "",
                    "score": round(float(getattr(s, "score", 0) or 0), 2),
                    "rating": getattr(s, "rating", "") or "",
                }
                for s in scored
            ]
            step.update(
                status="failed" if failed and not written else "success",
                rows=written,
                message=f"{written} 只票评分已写入 daily_scores（{target}）",
            )
            step["failed"] = failed
            if failed:
                result["errors"].extend(failed)
        except Exception as exc:
            step.update(status="failed", message=str(exc))
            result["errors"].append(f"票池评分落库异常: {exc}")
            logger.error("票池评分落库异常: %s", exc)
    step["elapsed"] = round(time.perf_counter() - t0, 3)
    steps["daily_scores"] = step

    # ── 步骤 7：主线强度排名 ──
    #
    # 必须排在买点确认之前：买点确认的主线层直接读 theme_strength 表。
    # 即使用户一条主线都没导入，这一步也有价值——它会把 stock_basic.industry
    # 的行业强度排出来，给买点确认当兜底参照系。
    step = _new_step()
    t0 = time.perf_counter()
    if skip_themes:
        step.update(status="skipped", message="--skip-themes")
    else:
        try:
            from .themes import DEFAULT_LOOKBACK, rank_themes

            ranking = rank_themes(target, lookback=theme_lookback)
            themes_ranked = ranking.get("themes") or []
            industries_ranked = ranking.get("industries") or []
            step.update(
                status="success" if ranking.get("written") else "failed",
                rows=int(ranking.get("written", 0) or 0),
                message=(
                    f"窗口 {theme_lookback} 日：主线 {len(themes_ranked)} 条、"
                    f"行业 {len(industries_ranked)} 个已排名"
                ),
            )
            step["top_themes"] = [
                {"theme": g.name, "strength": round(g.strength, 2), "excess": round(g.excess, 2), "rank": g.rank}
                for g in themes_ranked[:5]
            ]
            step["top_industries"] = [
                {"theme": g.name, "strength": round(g.strength, 2), "excess": round(g.excess, 2), "rank": g.rank}
                for g in industries_ranked[:5]
            ]
            result["theme_ranking"] = {
                "lookback": theme_lookback,
                "window": ranking.get("window") or [],
                "themes": step["top_themes"],
                "industries": step["top_industries"],
            }
            if not themes_ranked:
                result["warnings"].append(
                    "尚未导入任何主线成员，买点确认的主线层将退回行业分类兜底"
                    "（用 `zt theme import <json>` 导入外部判定器的产出）"
                )
            for dropped in ranking.get("dropped_themes") or []:
                result["warnings"].append(f"主线未参与排名: {dropped}")
            if ranking.get("reason"):
                result["warnings"].append(f"主线强度排名: {ranking['reason']}")
        except Exception as exc:
            step.update(status="failed", message=str(exc))
            result["errors"].append(f"主线强度排名异常: {exc}")
            logger.error("主线强度排名异常: %s", exc)
    step["elapsed"] = round(time.perf_counter() - t0, 3)
    steps["theme_strength"] = step

    # ── 步骤 8：票池买点确认 ──
    step = _new_step()
    t0 = time.perf_counter()
    if skip_buy:
        step.update(status="skipped", message="--skip-buy")
    elif not codes:
        step.update(status="skipped", message="票池为空")
    else:
        try:
            from .buy_decision import apply_picks, confirm_buy_batch, save_buy_decisions, select_final_picks

            decisions = confirm_buy_batch(codes, target, market=market, theme_lookback=theme_lookback)

            # 第二阶段：从 BUY 里按主线/行业强弱挑最终标的。
            # 必须在落库之前——pick_rank 要跟决策写在同一行，日后才能归因
            # "入选的那几只是不是真的比落选的 BUY 走得好"。
            selection = select_final_picks(
                decisions,
                top_n=pick_top_n,
                min_group_strength=pick_min_group_strength,
                max_per_group=pick_max_per_group,
            )
            apply_picks(decisions, selection)

            written = save_buy_decisions(decisions)
            counts = {a: sum(1 for d in decisions if d.action == a) for a in ("BUY", "WATCH", "NONE")}
            step.update(
                status="success",
                rows=written,
                message=(
                    f"买入 {counts['BUY']}  观察 {counts['WATCH']}  不买 {counts['NONE']}"
                    f"  → 最终选出 {len(selection['picks'])} 只"
                ),
            )
            step["counts"] = counts
            result["final_picks"] = [
                {
                    "rank": e["rank"],
                    "ts_code": e["decision"].ts_code,
                    "name": e["decision"].name,
                    "score": round(e["decision"].score, 2),
                    "base_strategy": e["decision"].base_strategy,
                    "group": e["group"],
                    "group_kind": e["group_kind"],
                    "group_strength": e["group_strength"],
                }
                for e in selection["picks"]
            ]
            # BUY 却没入选的要报出来：这是"系统认为可以买、但被板块判断刷掉"的票，
            # 静默丢掉会让人以为它根本没通过买点确认。
            for e in selection["rejected"]:
                if e["decision"].action == "BUY":
                    result["warnings"].append(
                        f"{e['decision'].ts_code} 买点成立（{e['decision'].score:.1f}）但未入选: {e['reason']}"
                    )
            # 买点确认和评分一样，用的是每只票库里的真实数据日；数据没同步上来时
            # 决策日会早于 target。落库按真实日期，这里把不一致如实报出来。
            drifted = [f"{d.ts_code}@{d.trade_date}" for d in decisions if d.trade_date and d.trade_date != target]
            if drifted:
                result["warnings"].append(
                    f"以下票的买点确认基于其最新数据日而非 {target}: {', '.join(drifted)}"
                )
            no_data = [d.ts_code for d in decisions if not d.trade_date]
            if no_data:
                result["errors"].extend(f"{c} 买点确认:无可用K线" for c in no_data)
            result["buy_decisions"] = [
                {
                    "ts_code": d.ts_code,
                    "name": d.name,
                    "trade_date": d.trade_date,
                    "action": d.action,
                    "score": round(d.score, 2),
                    "base_strategy": d.base_strategy,
                    "theme": (d.theme or {}).get("theme", ""),
                    "theme_kind": (d.theme or {}).get("kind", ""),
                    "vetoes": d.vetoes,
                }
                for d in sorted(decisions, key=lambda x: x.score, reverse=True)
            ]
        except Exception as exc:
            step.update(status="failed", message=str(exc))
            result["errors"].append(f"买点确认异常: {exc}")
            logger.error("买点确认异常: %s", exc)
    step["elapsed"] = round(time.perf_counter() - t0, 3)
    steps["buy_decisions"] = step

    # ── 汇总状态：全失败=failed，部分失败=partial，无错误=success ──
    # trade_day_check 是元步骤（只是"判断了一下"），不代表任何实质工作，
    # 把它算进来会让 all(failed) 恒为 False —— 整条链全崩也只报 partial，
    # systemd 看到 exit 0 就当成功了，故障永远没人发现。
    _META_STEPS = {"trade_day_check"}
    executed = [s for name, s in steps.items() if name not in _META_STEPS and s["status"] in ("success", "failed")]
    if not result["errors"]:
        return _finish("success")
    if executed and all(s["status"] == "failed" for s in executed):
        return _finish("failed")
    return _finish("partial")


def format_pipeline_summary(result: dict[str, Any]) -> str:
    """把 run_daily_pipeline 的返回值渲染成人类可读摘要。"""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"每日流水线 {result.get('trade_date', '')}  status={result.get('status', '')}")
    lines.append("=" * 60)
    lines.append(f"耗时: {result.get('elapsed', 0)}s   票池: {result.get('watchlist_count', 0)} 只")

    icons = {"success": "✓", "skipped": "-", "failed": "✗", "pending": "?"}
    lines.append("\n【步骤】")
    for name, step in (result.get("steps") or {}).items():
        icon = icons.get(step.get("status", ""), "?")
        lines.append(
            f"  {icon} {name:<22} {step.get('status', ''):<8} "
            f"{step.get('elapsed', 0):>7.2f}s  rows={step.get('rows', 0)}  {step.get('message', '')}"
        )

    market = result.get("market") or {}
    if market:
        lines.append("\n【大盘环境】")
        lines.append(
            f"  方向={market.get('market_dir', '')}  涨跌幅={market.get('market_pct_chg', 0)}%  "
            f"强度={market.get('market_strength', 0)}"
        )
        detail = market.get("detail") or {}
        if detail.get("reason"):
            lines.append(f"  说明: {detail['reason']}")

    tops = result.get("top_scores") or []
    if tops:
        lines.append("\n【票池评分 TOP】")
        for item in tops[:10]:
            lines.append(f"  {item['ts_code']:<12} {item['name']:<8} {item['score']:>6.1f}  {item['rating']}")

    ranking = result.get("theme_ranking") or {}
    if ranking:
        window = ranking.get("window") or []
        span = f" ({window[0]}~{window[-1]})" if window else ""
        lines.append(f"\n【主线强度 · 窗口{ranking.get('lookback', 0)}日{span}】")
        themes = ranking.get("themes") or []
        if themes:
            lines.append("  主线: " + "  ".join(f"{t['theme']}({t['strength']:.0f})" for t in themes))
        else:
            lines.append("  主线: 未导入")
        inds = ranking.get("industries") or []
        if inds:
            lines.append("  行业: " + "  ".join(f"{t['theme']}({t['strength']:.0f})" for t in inds))

    buys = result.get("buy_decisions") or []
    if buys:
        counts = {a: sum(1 for b in buys if b["action"] == a) for a in ("BUY", "WATCH", "NONE")}
        lines.append(f"\n【买点确认】买入 {counts['BUY']}  观察 {counts['WATCH']}  不买 {counts['NONE']}")
        for b in buys:
            if b["action"] == "NONE" and not b["vetoes"]:
                continue  # 无信号的票不逐条刷屏，只显示有结论或被否决的
            theme = b.get("theme") or "-"
            if b.get("theme_kind") == "industry":
                theme += "(行业)"
            tail = b["vetoes"][0] if b["vetoes"] else f"{b['base_strategy']} · 主线{theme}"
            lines.append(f"  {b['action']:<6} {b['ts_code']:<12} {b['name']:<8} {b['score']:>6.1f}  {tail}")

    picks = result.get("final_picks") or []
    if picks:
        lines.append("\n【最终选股 · 按主线/行业强弱】")
        for p in picks:
            group = p["group"] + ("(行业)" if p["group_kind"] == "industry" else "")
            lines.append(
                f"  #{p['rank']} {p['ts_code']:<12} {p['name']:<8} 确认分{p['score']:>6.1f}  "
                f"{group}(强度{p['group_strength']:.1f})  {p['base_strategy']}"
            )
    elif buys and any(b["action"] == "BUY" for b in buys):
        lines.append("\n【最终选股】买点成立的票均未通过主线/行业筛选（详见警告）")

    warnings = result.get("warnings") or []
    if warnings:
        lines.append("\n【警告】")
        for w in warnings:
            lines.append(f"  ! {w}")

    errors = result.get("errors") or []
    if errors:
        lines.append(f"\n【错误 {len(errors)} 条】")
        for e in errors[:10]:
            lines.append(f"  ✗ {e}")

    return "\n".join(lines)
