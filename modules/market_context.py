"""大盘环境：全市场宽度 + 指数补充。

从原 daily_pipeline 拆出来独立成模块——每日编排器已删除，但大盘环境是
买点确认的**准入门槛**（大盘不好不选股），必须继续存在且不依赖编排器。

数据源按可得性分两层，首选宽度：
1. **市场宽度**——从每日同步的全市场 daily_kline 算涨跌家数比、涨跌停净差、
   中位数涨跌幅。零 API 成本，只要当天做过全市场同步就一定可用。
2. **指数**——沪深300 / 上证指数的涨跌幅与 MA5/MA20 位置。实测当前中转源的
   index_daily 接口配额只有 5 次/天且长期返回空，故只作补充。

宽度只统计**可交易标的**（排除 ST 与北交所，见 modules/universe.py）：
两者的涨跌停幅度分别是 ±5% 和 ±30%，而 is_limit_up 阈值写死 9.9%，
一个永远识别不到涨停、一个把涨 12% 误判成涨停，朝相反方向污染同一个统计。
"""

from __future__ import annotations

import logging
from typing import Any

from .database import get_connection

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
    # ST（±5%）和北交所（±30%）的涨跌停幅度都不是 ±10%，而 is_limit_up 的
    # 阈值写死 9.9%——前者的涨停永远识别不到、后者涨 12% 会被误判成涨停，
    # 两边朝相反方向污染同一个统计。宽度只算沪深非 ST 标的。
    from .universe import TRADABLE_PREDICATE

    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*),
                   SUM(CASE WHEN k.pct_chg > 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN k.pct_chg < 0 THEN 1 ELSE 0 END),
                   SUM(COALESCE(k.is_limit_up, 0)),
                   SUM(COALESCE(k.is_limit_down, 0))
            FROM daily_kline k
            JOIN stock_basic b ON b.ts_code = k.ts_code
            WHERE k.trade_date = ? AND k.pct_chg IS NOT NULL AND {TRADABLE_PREDICATE}
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
                f"""
                SELECT k.pct_chg FROM daily_kline k
                JOIN stock_basic b ON b.ts_code = k.ts_code
                WHERE k.trade_date = ? AND k.pct_chg IS NOT NULL AND {TRADABLE_PREDICATE}
                ORDER BY k.pct_chg LIMIT 1 OFFSET ?
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
