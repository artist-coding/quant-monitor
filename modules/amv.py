"""活跃市值（AMV）多空区间：选股的总开关。

活跃市值是本系统判断"今天能不能选股"的**唯一**依据。全市场宽度
（``market_context``）不再承担这个角色，降级为辅助建仓参考。

区间规则
--------

设 ``p`` 为当日涨幅（百分数），``p_prev`` 为前一交易日涨幅：

    若 p < -2.3                          → 空头区间
    否则若 p >= 4 或 (p + p_prev) >= 4   → 多头区间
    否则                                  → 沿用前一日区间

两条容易写错的细节，都是拿 8180 个交易日的官方标注反推出来的：

1. **空头触发优先于两日累计多头。** 1993-03-08 涨 10.77% 进多头，次日跌 5.44%，
   两日累计仍有 +5.33%，但标注是空头——单日暴跌直接压过累计涨幅。
   先判空头、再判多头，顺序反了会有 18 天判错。
2. **涨幅必须用收盘价现算，不能用四舍五入到两位小数的显示值。**
   1993-10-11 显示 -2.30%（实为 -2.295082%，多头）与 1997-11-11 显示 -2.30%
   （实为 -2.302724%，空头）差在万分之一上。用显示值会有 10 天判错。

按上述规则回放 1993-01-04 ~ 2026-08-07 共 8180 个交易日，与官方标注
**逐日 100% 吻合**（见 tests/test_amv.py 的回归测试）。

数据来源
--------

- 历史：``zt amv import <csv>``，导入"0AMV-YYMMDD-增强.csv"。
- 每日：收盘后由用户提供，``zt amv add <日期> --close <收盘价>``。
  也接受 ``--pct``，但**精度不足**——落在 -2.3% 边界附近时结论可能相反，
  所以能给收盘价就给收盘价。
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .database import get_connection

logger = logging.getLogger(__name__)


# ==================== 区间常量 ====================
#
# 这三个数字是用户给定的交易规则，不是我调出来的参数。
# 拿 8180 个交易日的官方标注验证过，改动任何一个都会偏离标注。

# 进入多头：单日涨幅 >= 4%，或连续两日累计涨幅 >= 4%
BULL_THRESHOLD = 4.0
# 累计窗口的天数（"连续 1 天或两天"）
BULL_WINDOW = 2
# 进入空头：单日跌幅**超过** 2.3%（严格小于，-2.3% 整不触发）
BEAR_THRESHOLD = -2.3

REGIME_BULL = "多头区间"
REGIME_BEAR = "空头区间"


@dataclass
class AmvDay:
    trade_date: str
    close: float
    pct_chg: float | None
    regime: str
    regime_imported: str = ""

    @property
    def can_select(self) -> bool:
        """该区间下是否允许选股/新建仓。"""
        return self.regime == REGIME_BULL


def _norm_date(value: str) -> str:
    """把 1993-01-04 / 19930104 / 1993/1/4 统一成 YYYYMMDD。"""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 8:
        raise ValueError(f"无法解析日期: {value!r}")
    return digits


def _parse_pct(value: str | float | None) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().rstrip("%")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ==================== 区间状态机 ====================


def classify(pcts: Iterable[float | None], initial: str | None = None) -> list[str]:
    """按日涨幅序列回放多空区间。

    Args:
        pcts: 按时间升序的日涨幅（百分数）；首日可为 None
        initial: 序列开始前的区间状态，None 表示未定

    Returns:
        与输入等长的区间列表；状态尚未确定时为空字符串
    """
    out: list[str] = []
    state = initial
    prev: float | None = None
    for p in pcts:
        if p is not None:
            # 顺序不能反：单日暴跌压过两日累计涨幅
            if p < BEAR_THRESHOLD:
                state = REGIME_BEAR
            elif p >= BULL_THRESHOLD or (prev is not None and p + prev >= BULL_THRESHOLD):
                state = REGIME_BULL
            prev = p
        out.append(state or "")
    return out


def recompute_regimes() -> int:
    """用收盘价重算全序列的涨幅与区间，写回库。

    涨幅一律由收盘价现算而不信任导入值：导入的 CSV 只给到两位小数，
    在 -2.3% 边界上会翻车（详见模块文档）。

    Returns:
        更新的行数
    """
    with get_connection() as conn:
        rows = conn.execute("SELECT trade_date, close FROM amv_daily ORDER BY trade_date").fetchall()
        if not rows:
            return 0

        dates = [str(r[0]) for r in rows]
        closes = [float(r[1]) for r in rows]
        pcts: list[float | None] = [None]
        for i in range(1, len(closes)):
            prev = closes[i - 1]
            pcts.append((closes[i] - prev) / prev * 100 if prev else None)

        regimes = classify(pcts)
        conn.executemany(
            "UPDATE amv_daily SET pct_chg = ?, regime = ? WHERE trade_date = ?",
            [(p, r, d) for d, p, r in zip(dates, pcts, regimes)],
        )
    return len(dates)


# ==================== 导入与录入 ====================


def import_history(csv_path: str | Path) -> dict[str, Any]:
    """导入「0AMV-YYMMDD-增强.csv」历史数据。

    保留原始的「区间」列到 regime_imported，供回归测试比对；
    实际生效的 regime 由 recompute_regimes 按规则重算。
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到文件: {path}")

    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(text.splitlines())

    records = []
    for row in reader:
        raw_date = row.get("date") or row.get("日期") or ""
        if not raw_date.strip():
            continue
        try:
            trade_date = _norm_date(raw_date)
            close = float(row.get("close") or row.get("收盘") or 0)
        except (ValueError, TypeError):
            continue
        if close <= 0:
            continue
        records.append(
            (
                trade_date,
                float(row.get("open") or 0) or None,
                float(row.get("high") or 0) or None,
                float(row.get("low") or 0) or None,
                close,
                float(row.get("volume") or 0) or None,
                float(row.get("amount") or 0) or None,
                (row.get("区间") or "").strip(),
            )
        )

    if not records:
        raise ValueError(f"{path} 中没有解析出任何有效行情行")

    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO amv_daily
              (trade_date, open, high, low, close, volume, amount, regime_imported)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                open = excluded.open, high = excluded.high, low = excluded.low,
                close = excluded.close, volume = excluded.volume, amount = excluded.amount,
                regime_imported = excluded.regime_imported
            """,
            records,
        )

    recompute_regimes()
    return {
        "imported": len(records),
        "start": records[0][0],
        "end": records[-1][0],
        "source": str(path),
    }


def add_daily(
    trade_date: str,
    *,
    close: float | None = None,
    pct_chg: float | None = None,
) -> AmvDay:
    """录入单日活跃市值（收盘后由用户提供）。

    close 与 pct_chg 至少给一个。**优先给 close**——只给涨幅时，
    若它是四舍五入过的百分数，在 -2.3% 边界上可能得出相反的区间结论。
    只给 pct_chg 时，用它和上一日收盘反推一个收盘价存进去，
    保证 recompute_regimes 的口径统一。
    """
    date = _norm_date(trade_date)
    if close is None and pct_chg is None:
        raise ValueError("close 与 pct_chg 至少提供一个")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT close FROM amv_daily WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1", (date,)
        ).fetchone()
        prev_close = float(row[0]) if row else None

        if close is None:
            if prev_close is None:
                raise ValueError(f"{date} 之前没有任何活跃市值数据，只给涨幅无法反推收盘价，请提供 --close")
            close = prev_close * (1 + pct_chg / 100.0)
            logger.warning(
                "只提供了涨幅 %.4f%%，已按上一日收盘 %.4f 反推收盘价 %.4f；"
                "涨幅若是四舍五入值，在 -2.3%% 边界附近可能影响区间判定",
                pct_chg,
                prev_close,
                close,
            )

        conn.execute(
            """
            INSERT INTO amv_daily (trade_date, close)
            VALUES (?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET close = excluded.close
            """,
            (date, float(close)),
        )

    recompute_regimes()
    day = get_day(date)
    if day is None:  # 理论上不可能：刚写进去
        raise RuntimeError(f"{date} 写入后读取失败")
    return day


# ==================== 查询 ====================


def get_day(trade_date: str) -> AmvDay | None:
    """精确取某一天。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT trade_date, close, pct_chg, COALESCE(regime,''), COALESCE(regime_imported,'') "
            "FROM amv_daily WHERE trade_date = ?",
            (_norm_date(trade_date),),
        ).fetchone()
    if row is None:
        return None
    return AmvDay(str(row[0]), float(row[1]), row[2], str(row[3]), str(row[4]))


def get_regime(trade_date: str | None = None) -> AmvDay | None:
    """取截至 trade_date 的最近一条活跃市值记录（含区间）。

    不要求精确匹配：活跃市值由用户手工录入，可能比行情库晚一天。
    向前回退到最近一条，调用方可通过 trade_date 字段看出用的是哪天的数据。
    """
    with get_connection() as conn:
        if trade_date:
            row = conn.execute(
                "SELECT trade_date, close, pct_chg, COALESCE(regime,''), COALESCE(regime_imported,'') "
                "FROM amv_daily WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
                (_norm_date(trade_date),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT trade_date, close, pct_chg, COALESCE(regime,''), COALESCE(regime_imported,'') "
                "FROM amv_daily ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
    if row is None:
        return None
    return AmvDay(str(row[0]), float(row[1]), row[2], str(row[3]), str(row[4]))


def recent(limit: int = 20, end_date: str | None = None) -> list[AmvDay]:
    sql = (
        "SELECT trade_date, close, pct_chg, COALESCE(regime,''), COALESCE(regime_imported,'') FROM amv_daily "
        + ("WHERE trade_date <= ? " if end_date else "")
        + "ORDER BY trade_date DESC LIMIT ?"
    )
    params = (_norm_date(end_date), limit) if end_date else (limit,)
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [AmvDay(str(r[0]), float(r[1]), r[2], str(r[3]), str(r[4])) for r in reversed(rows)]


def regime_segments(limit: int = 20) -> list[dict[str, Any]]:
    """最近 limit 段多空区间（起止日期与天数）。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT trade_date, COALESCE(regime,'') FROM amv_daily WHERE regime != '' ORDER BY trade_date"
        ).fetchall()
    segments: list[dict[str, Any]] = []
    for date, regime in rows:
        if segments and segments[-1]["regime"] == regime:
            segments[-1]["end"] = str(date)
            segments[-1]["days"] += 1
        else:
            segments.append({"regime": str(regime), "start": str(date), "end": str(date), "days": 1})
    return segments[-limit:]


def verify_against_imported() -> dict[str, Any]:
    """把重算的区间与导入的官方标注逐日比对（回归用）。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT trade_date, COALESCE(regime,''), COALESCE(regime_imported,'') "
            "FROM amv_daily WHERE regime_imported != '' ORDER BY trade_date"
        ).fetchall()
    mismatches = [
        {"trade_date": str(d), "computed": str(c), "imported": str(i)} for d, c, i in rows if str(c) != str(i)
    ]
    total = len(rows)
    return {
        "total": total,
        "matched": total - len(mismatches),
        "mismatches": mismatches,
        "accuracy": round((total - len(mismatches)) / total * 100, 4) if total else 0.0,
    }


def format_amv_status(day: AmvDay | None, segments: list[dict[str, Any]] | None = None) -> str:
    """当前区间状态的人类可读输出。"""
    if day is None:
        return "活跃市值库为空。先 `zt amv import <csv>` 导入历史，再用 `zt amv add` 逐日录入。"

    icon = "可选股" if day.can_select else "停止选股"
    lines = [
        "=" * 62,
        f"活跃市值 {day.trade_date}   收盘 {day.close:,.2f}"
        + (f"   涨幅 {day.pct_chg:+.4f}%" if day.pct_chg is not None else ""),
        f"区间: {day.regime or '未定'}   →   {icon}",
        "=" * 62,
        f"规则: 单日跌幅 < {BEAR_THRESHOLD}% → 空头；"
        f"单日或连续两日累计涨幅 ≥ {BULL_THRESHOLD}% → 多头；否则沿用",
    ]
    if segments:
        lines.append("\n【最近区间】")
        for s in segments:
            lines.append(f"  {s['regime']}  {s['start']} ~ {s['end']}  ({s['days']} 个交易日)")
    return "\n".join(lines)
