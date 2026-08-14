import os
import sqlite3
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field
from enum import Enum
from modules.database import get_db_connection


from ..indicators import DailyData


def _klines_dict_to_daily(klines: list[dict]) -> list[DailyData]:
    """将 strategies 模块用的 dict klines 转为 indicators 模块用的 DailyData"""
    return _dict_to_daily(klines)


def _ensure_daily_klines(klines: list) -> list[DailyData]:
    """确保输入序列是 list[DailyData]。若是 list[dict] 则自动转换。"""
    if not klines:
        return []
    if isinstance(klines[0], DailyData):
        return klines
    return _dict_to_daily(klines)


# ==================== K 线窗口连续性 ====================
#
# get_kline_data 取的是「最近 N 行」而不是「最近 N 个交易日」。库里但凡缺了几天，
# 窗口就会静默地把远期 K 线缝到近期后面，而 KDJ/MACD/BBI 全是递推指标，
# 一缝就整段失真且不报任何错。2026-08-07 那批选股就是这么错的：立讯精密的
# 150 行窗口一路回溯到 2024-04，20250224 直接接 20260717，算出 J=5.76 触发 B1，
# 数据补齐后真值是 15.10，根本不该触发。
#
# 这里提供「窗口 vs 全市场交易日历」的比对，让调用方能把「数据有洞」显式变成
# 一条否决理由，而不是一个看起来很正常的买点。

_MARKET_CALENDAR: list[str] | None = None

# 最近 N 个交易日内的缺口才算数：KDJ 的 K/D 递推权重按 (2/3)^n 衰减，
# 30 根之外的缺口对当日值的影响已不足万分之一，苛求全窗口无洞会误伤太多长停票。
GAP_CHECK_BARS = 30
# 这个窗口里缺到几天就判定「不可信」。1~2 天多是停牌核查，影响有限；
# 5 天及以上要么是数据洞，要么是长期停牌复牌——两种情况下指标都不该采信。
GAP_SEVERE_DAYS = 5


def market_calendar(refresh: bool = False) -> list[str]:
    """全市场交易日历（从 daily_kline 归纳，进程内缓存）。

    没有独立的日历表可用：``trade_cal`` 只有 2019-2023，且中转 API 的
    trade_cal 接口限流严重、拉空是常态。而 daily_kline 里「有任何一只票有行情」
    的日子就是交易日，这个归纳在全市场覆盖完整的前提下与官方日历等价
    （实测 2019-2023 段与 trade_cal 逐日吻合，0 差异）。
    """
    global _MARKET_CALENDAR
    if _MARKET_CALENDAR is None or refresh:
        conn = get_db_connection()
        try:
            _MARKET_CALENDAR = [
                str(r[0]) for r in conn.execute("SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date")
            ]
        finally:
            conn.close()
    return _MARKET_CALENDAR


def window_gaps(klines: list, last_n: int = GAP_CHECK_BARS) -> dict[str, Any]:
    """检查 K 线窗口末尾 ``last_n`` 根在交易日历上是否连续。

    Args:
        klines: 已按日期升序排列的 K 线（dict 或 DailyData 均可）
        last_n: 只看末尾多少根

    Returns:
        ``{"missing": 缺失交易日数, "max_gap": 最大单个断裂, "span": 跨越的交易日数,
           "bars": 实际根数, "worst": (前一根日期, 后一根日期, 中间缺几天) | None,
           "severe": bool}``
        取不到日历或数据不足时返回 ``missing=0`` 的空结论（不误报）。
    """
    empty = {"missing": 0, "max_gap": 0, "span": 0, "bars": 0, "worst": None, "severe": False}
    if not klines:
        return empty

    def _date(k: Any) -> str:
        return k["trade_date"] if isinstance(k, dict) else k.trade_date

    window = klines[-last_n:] if last_n and len(klines) > last_n else klines
    if len(window) < 2:
        return empty

    cal = market_calendar()
    pos = {d: i for i, d in enumerate(cal)}
    dates = [_date(k) for k in window]
    if dates[0] not in pos or dates[-1] not in pos:
        return empty

    span = pos[dates[-1]] - pos[dates[0]] + 1
    worst = None
    max_gap = 0
    for a, b in zip(dates, dates[1:]):
        if a not in pos or b not in pos:
            continue
        gap = pos[b] - pos[a] - 1
        if gap > max_gap:
            max_gap = gap
            worst = (a, b, gap)

    missing = span - len(window)
    return {
        "missing": missing,
        "max_gap": max_gap,
        "span": span,
        "bars": len(window),
        "worst": worst,
        "severe": missing >= GAP_SEVERE_DAYS,
    }


class StrategyType(Enum):
    """战法类型"""

    # 基础战法
    B1 = "B1"  # 买点1
    B2 = "B2"  # 买点2（确认）
    B3 = "B3"  # 买点3
    SB1 = "SB1"  # 超级B1

    # 复合战法
    CHANGAN = "长安战法"  # 三日确认战法
    SI_FEN_ZHI_SAN = "四分之三阴量"  # 假突破识别
    NANA = "娜娜图形"  # 连续放量涨+缩量回调
    CHAOFAN = "超级B1"  # 超级买点

    # 异动战法
    YIDONG_DILIAN = "异动+地量地价"  # 异动后缩量买点

    # 特殊形态
    PINGHANG = "平行重炮"  # 双阳夹阴
    KENGQI = "坑里起好货"  # 填坑战法
    DUIchen = "对称VA"  # 对称战法

    # 逃顶信号
    S1 = "S1"  # 初级逃顶（丑陋大绿帽）
    S2 = "S2"  # 确认逃顶（MACD顶背离）
    S3 = "S3"  # 最后逃生（反抽无力）

    # 主力阶段
    XISHOU = "吸筹"  # 麒麟会吸筹阶段
    LASHENG = "拉升"  # 麒麟会拉升阶段
    PAIFA = "派发"  # 麒麟会派发阶段
    LUOLUO = "回落"  # 麒麟会回落阶段

    # 观察/提示
    WATCH = "观察"  # 阶段判断、提示信号


class Priority(Enum):
    """信号优先级"""

    CRITICAL = 3  # 紧急：止损、逃顶
    OPPORTUNITY = 2  # 机会：买点、战法
    OBSERVE = 1  # 观察：提示、减仓、阶段判断


class Action(Enum):
    """交易建议"""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WATCH = "WATCH"


@dataclass
class StrategySignal:
    """战法信号"""

    ts_code: str
    trade_date: str
    strategy: StrategyType
    confidence: float  # 置信度 0-1
    description: str
    details: dict[str, Any] = field(default_factory=dict)

    # 交易建议
    action: str = "WATCH"  # BUY/SELL/HOLD/WATCH
    target_price: float | None = None
    stop_loss: float | None = None
    risk_ratio: float | None = None

    # 扩展字段（部分策略使用）
    price: float | None = None  # 信号产生时的价格
    reason: str | None = None  # 信号原因说明

    # 信号优先级（由策略检测函数自动填入）
    priority: Priority = Priority.OBSERVE


def get_kline_data(ts_code: str, days: int = 120) -> list[dict]:
    """
    获取K线数据，并关联指标缓存与资金流数据
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # 联表查询：K线 + 指标缓存(RSI/DMI) + 资金流
    # 注意：先按 DESC 取最近 N 条，再在 Python 端反转回正序
    # （SQLite 在子查询里包一层即可避免 ORDER BY + LIMIT 顺序冲突）
    cursor.execute(
        """
        SELECT
            k.ts_code, k.trade_date, k.open, k.high, k.low, k.close, k.vol, k.amount, k.pct_chg,
            i.rsi6, i.adx, i.dmi_plus, i.dmi_minus,
            m.buy_lg_amount, m.buy_elg_amount, m.sell_lg_amount, m.sell_elg_amount, m.net_mf
        FROM (
            SELECT ts_code, trade_date, open, high, low, close, vol, amount, pct_chg
            FROM daily_kline
            WHERE ts_code = ?
            ORDER BY trade_date DESC
            LIMIT ?
        ) k
        LEFT JOIN indicator_cache i ON k.ts_code = i.ts_code AND k.trade_date = i.trade_date
        LEFT JOIN moneyflow m ON k.ts_code = m.ts_code AND k.trade_date = m.trade_date
        ORDER BY k.trade_date ASC
    """,
        (ts_code, days),
    )

    rows = cursor.fetchall()
    conn.close()

    data_list = []
    for i, row in enumerate(rows):
        prev_close = rows[i - 1]["close"] if i > 0 else row["close"]
        prev_vol = rows[i - 1]["vol"] if i > 0 else row["vol"]

        data_list.append(
            {
                "ts_code": row["ts_code"],
                "trade_date": row["trade_date"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "vol": row["vol"],
                "amount": row["amount"],
                "pct_chg": row["pct_chg"],
                "prev_close": prev_close,
                "prev_vol": prev_vol,
                "is_rise": row["close"] > prev_close,
                "is_beidou": row["vol"] >= prev_vol * 2,
                "is_suoliang": row["vol"] <= prev_vol * 0.5,
                "is_jiayin": row["close"] < row["open"] and row["close"] > prev_close,
                "is_yinxian": row["close"] < prev_close,
                "is_fangliang_yinxian": row["close"] < prev_close and row["vol"] > prev_vol * 1.5,
                # MDC 扩展字段（LEFT JOIN 可能为 NULL，统一 fallback）
                "rsi6": row["rsi6"] or 0,
                "adx": row["adx"] or 0,
                "dmi_plus": row["dmi_plus"] or 0,
                "dmi_minus": row["dmi_minus"] or 0,
                "net_mf": row["net_mf"] or 0,
                "large_inflow": (row["buy_lg_amount"] or 0) + (row["buy_elg_amount"] or 0),
                "large_outflow": (row["sell_lg_amount"] or 0) + (row["sell_elg_amount"] or 0),
            }
        )

    return data_list


# MDC 多维验证字段分两类处理，语义不同：
# - 指标类（RSI/DMI）：0 是无意义的"假值"，必须还原为 None（无数据）
# - 资金流类：下游存在裸算术（如 large_inflow - large_outflow），None 会直接 TypeError，
#   故保持 0 语义（"无净流入/流出" 与 "无数据" 在下游行为一致）
_MDC_INDICATOR_FIELDS = ("rsi6", "adx", "dmi_plus", "dmi_minus")
_MDC_FLOW_FIELDS = ("net_mf", "large_inflow", "large_outflow")


def _mdc_num(value: Any) -> float | None:
    """MDC 指标字段归一化：None / 非数值 / 0 一律视为"无数据"，返回 None。

    get_kline_data 对 LEFT JOIN 出来的 NULL 做了 ``or 0`` 的 fallback，
    但 ``rsi6=0`` 这种"假读数"一旦被当作有效值参与 ``rsi6 < 25``（极端超卖）
    之类的比较就会得出完全错误的结论——没数据的票会全员命中加分项，
    因此这里统一还原成 None，让下游的 ``(rsi6 or 50) < 25`` 真值判断跳过加分。
    """
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num != 0 else None


def _mdc_flow(value: Any) -> float:
    """MDC 资金流字段归一化：无数据 / 非数值一律返回 0。"""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _dict_to_daily(klines: list[dict]) -> list[DailyData]:
    """将 Dict K 线列表转换为 indicators.DailyData，完整映射形态特征与 MDC 多维验证属性"""
    from ..indicators import DailyData

    result = []
    for i, k in enumerate(klines):
        prev_close = klines[i - 1]["close"] if i > 0 else k["close"]
        daily = DailyData(
            ts_code=k["ts_code"],
            trade_date=k["trade_date"],
            open=k["open"],
            high=k["high"],
            low=k["low"],
            close=k["close"],
            vol=k["vol"],
            amount=k.get("amount", k["close"] * k["vol"]),
            pct_chg=k.get("pct_chg", 0),
            prev_close=prev_close,
            is_rise=k.get("is_rise", False),
            is_beidou=k.get("is_beidou", False),
            is_suoliang=k.get("is_suoliang", False),
            is_jiayin=k.get("is_jiayin", False),
            is_yinxian=k.get("is_yinxian", False),
            is_fangliang_yinxian=k.get("is_fangliang_yinxian", False),
        )
        # MDC 多维验证字段（RSI/ADX/DMI/资金流）。
        # 用 setattr 而非构造函数关键字参数，是为了兼容 DailyData 尚未声明这些字段的情况；
        # 待 indicators 层按契约补上声明后，本处行为完全不变。
        # 传进来的 dict 可能根本没有这些键（如测试 fixture 造的裸 K 线），故一律用 k.get 容错。
        for name in _MDC_INDICATOR_FIELDS:
            setattr(daily, name, _mdc_num(k.get(name)))
        for name in _MDC_FLOW_FIELDS:
            setattr(daily, name, _mdc_flow(k.get(name)))
        result.append(daily)
    return result


def _calc_kdj(klines: list[dict]) -> tuple[float, float, float]:
    """通过 indicators.py 计算 KDJ (遗留调用向后兼容)"""
    from ..indicators import calculate_kdj

    daily = _dict_to_daily(klines)
    return calculate_kdj(daily)


def _calc_bbi(klines: list[dict]) -> float:
    """通过 indicators.py 计算 BBI (遗留调用向后兼容)"""
    from ..indicators import calculate_bbi

    daily = _dict_to_daily(klines)
    return calculate_bbi(daily)


def _get_kdj(klines: list[DailyData], index: int) -> tuple[float, float, float]:
    """获取 KDJ，有属性直接读取，无属性则动态计算并缓存"""
    today = klines[index]
    if getattr(today, "kdj_j", None) is not None:
        return today.kdj_k or 0.0, today.kdj_d or 0.0, today.kdj_j or 0.0
    from ..indicators import calculate_kdj

    k, d, j = calculate_kdj(klines[: index + 1])
    today.kdj_k, today.kdj_d, today.kdj_j = k, d, j
    return k, d, j


def _get_bbi(klines: list[DailyData], index: int) -> float:
    """获取 BBI，有属性直接读取，无属性则动态计算并缓存"""
    today = klines[index]
    if getattr(today, "bbi", None) is not None:
        return today.bbi or 0.0
    from ..indicators import calculate_bbi

    bbi = calculate_bbi(klines[: index + 1])
    today.bbi = bbi
    return bbi


def _get_macd_dif(klines: list[DailyData], index: int) -> float:
    """获取 MACD DIF，有属性直接读取，无属性则动态计算并缓存"""
    today = klines[index]
    if getattr(today, "macd_dif", None) is not None:
        return today.macd_dif or 0.0
    from ..indicators import calculate_macd

    difs, _, _ = calculate_macd(klines[: index + 1])
    for i in range(len(difs)):
        klines[i].macd_dif = difs[i]
    return today.macd_dif or 0.0
