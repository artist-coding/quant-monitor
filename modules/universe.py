"""可交易股票池：ST 与北交所的统一过滤规则。

一个模块管住"哪些票根本不进入视野"，避免同一条规则在买点确认、主线强度、
大盘宽度、全市场选股里各写一遍然后慢慢走样。

排除两类
--------

**ST / \\*ST**（库内 207 只）。风险警示股，退市风险高，且涨跌停限制是 ±5%
而不是 ±10%——同步器的 ``is_limit_up`` 阈值写死 9.9%，ST 的涨停**永远不会**
被识别到，把它们算进分组统计只会系统性地压低涨停率。

**北交所**（库内 334 只，``.BJ`` 后缀 / ``market='北交所'``）。涨跌幅限制
±30%，同样的 9.9% 阈值会把"涨了 12%"误判成涨停，方向相反地污染同一个统计。
流动性口径也和沪深两市不可比。

判定口径与其局限
----------------

ST 只能靠 ``stock_basic.name`` 判断，而那是**当前**名称——历史回测时，
一只 2019 年戴帽、现在已摘帽的票会被当成正常股，反之亦然。本地没有名称
变更历史，这个偏差修不掉，用历史日期跑批时需要知道。

``stock_basic`` 缺失的代码一律视为不可交易：查过库里 245 个这样的代码，
最后交易日全部落在 2016~2019，是已退市标的；在市股票 100% 有基础信息记录
（20260807 当日 5535 只无一缺失）。既然连名字都拿不到，也就无从确认它不是 ST。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .database import get_connection

# 北交所标识：后缀比代码前缀稳（历史上 43/83/87/92 开头都用过）
BSE_SUFFIX = ".BJ"
BSE_MARKET = "北交所"

# 排除原因的固定文案，落库和展示共用
REASON_ST = "ST/风险警示股（涨跌停±5%，退市风险）"
REASON_BSE = "北交所（涨跌停±30%，与沪深口径不可比）"
REASON_UNKNOWN = "无 stock_basic 记录（已退市或基础信息未同步），无法确认是否 ST"

# 供 SQL 直接拼接的谓词。用法固定为：
#     FROM daily_kline k JOIN stock_basic b ON b.ts_code = k.ts_code
#     WHERE ... AND {TRADABLE_PREDICATE}
# 必须用 INNER JOIN——LEFT JOIN 时 b.name 为 NULL，`NOT LIKE` 对 NULL 求值为
# NULL 而非 TRUE，那些无基础信息的代码会被 WHERE 静默滤掉，看着像生效了，
# 实则是靠三值逻辑的巧合，换个写法（比如加 OR）就会漏。INNER JOIN 才是显式的。
TRADABLE_PREDICATE = f"b.name NOT LIKE '%ST%' AND b.ts_code NOT LIKE '%{BSE_SUFFIX}'"


def exclusion_reason(ts_code: str, name: str | None) -> str:
    """返回排除原因；空字符串表示可交易。

    Args:
        ts_code: 股票代码
        name: stock_basic 里的名称；None 表示库里没有这条记录
    """
    if (ts_code or "").upper().endswith(BSE_SUFFIX):
        return REASON_BSE
    if name is None:
        return REASON_UNKNOWN
    if "ST" in name.upper():
        return REASON_ST
    return ""


def is_tradable(ts_code: str, name: str | None) -> bool:
    return not exclusion_reason(ts_code, name)


def _load_names(codes: Sequence[str]) -> dict[str, str]:
    """批量取名称。不在 stock_basic 里的代码不会出现在返回值中。"""
    if not codes:
        return {}
    out: dict[str, str] = {}
    with get_connection() as conn:
        # SQLite 的变量上限默认 999，分批查
        for i in range(0, len(codes), 500):
            chunk = list(codes[i : i + 500])
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT ts_code, name FROM stock_basic WHERE ts_code IN ({placeholders})", chunk
            ).fetchall()
            out.update({str(r[0]): str(r[1] or "") for r in rows})
    return out


def filter_tradable(codes: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    """把代码列表拆成 (可交易, {被排除的代码: 原因})，保持原顺序。"""
    codes = list(codes)
    names = _load_names(codes)
    kept: list[str] = []
    excluded: dict[str, str] = {}
    for code in codes:
        reason = exclusion_reason(code, names.get(code))
        if reason:
            excluded[code] = reason
        else:
            kept.append(code)
    return kept, excluded


def tradable_codes(trade_date: str | None = None) -> list[str]:
    """当前（或某交易日有行情的）可交易股票代码。"""
    sql = f"SELECT b.ts_code FROM stock_basic b WHERE {TRADABLE_PREDICATE}"
    params: tuple = ()
    if trade_date:
        sql = (
            "SELECT DISTINCT b.ts_code FROM daily_kline k "
            "JOIN stock_basic b ON b.ts_code = k.ts_code "
            f"WHERE k.trade_date = ? AND {TRADABLE_PREDICATE}"
        )
        params = (trade_date,)
    with get_connection() as conn:
        return [str(r[0]) for r in conn.execute(sql + " ORDER BY 1", params).fetchall()]
