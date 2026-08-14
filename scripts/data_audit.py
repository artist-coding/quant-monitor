#!/usr/bin/env python3
"""底层行情数据审计 —— 检查库里的真实数据对不对，而不是读写通不通。

与 ``e2e_data_integrity.py`` 的分工：那个脚本在临时库上验证「写进去能读出来、
字段不串位」，属于代码回归测试；这个脚本跑在**生产库**上，验证「存的数是对的」。

跑法::

    python3 scripts/data_audit.py                 # 全部本地检查（不联网，约 1 分钟）
    python3 scripts/data_audit.py --crosscheck 5  # 额外抽 5 个交易日与数据源逐行比对
    python3 scripts/data_audit.py --since 20240101

检查项及其由来（每一条都对应一次真实事故）：

1. **硬性不变量** —— OHLC 关系、非负、字段格式。
2. **交易日历完整性** —— 库里完全缺失的交易日、覆盖率异常低的日子。
   2026-08 发现 20170418 全市场只入库 592/3017 只。
3. **逐票缺口** —— 缺口会让「最近 N 行」的窗口静默缝合不相邻的 K 线，
   把 KDJ/MACD 算成垃圾。2026-08-07 那批选股就是这么错的。
4. **复权口径一致性** —— 均价(amount*10/vol) 必须落在 [low, high] 内。
   价格若被前复权而成交额没有，这一项会大面积失败。
5. **涨跌停标记** —— 必须与按板块阈值一致。两条同步路径曾用不同阈值，
   导致双创股涨 10~19.5% 被标成涨停，主线强度系统性抬高。
6. **活跃市值(amv_daily)** —— 选股总开关，pct_chg 与 close 链、regime 与规则
   必须逐行自洽；还会检出「盘中快照」这种未收盘就落库的行。

退出码：0 = 无致命问题，1 = 有致命问题。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

FATAL = 0
WARN = 0

# 按板块的涨跌停容差，与 modules/data_sync/syncer.py 的 _daily_limit_threshold 同源。
# 这里内联一份 SQL 版，避免为了一个阈值把整个 syncer（连带 Tushare 依赖）拖进来。
_THRESHOLD_SQL = """(CASE WHEN ts_code LIKE '%.BJ' THEN 29.0
                          WHEN substr(ts_code,1,3) IN ('300','301','688','689') THEN 19.5
                          ELSE 9.5 END)"""


def _hdr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _ok(label: str, detail: str = "") -> None:
    print(f"  ✅ {label}" + (f" — {detail}" if detail else ""))


def _bad(label: str, detail: str = "") -> None:
    global FATAL
    FATAL += 1
    print(f"  ❌ {label}" + (f" — {detail}" if detail else ""))


def _warn(label: str, detail: str = "") -> None:
    global WARN
    WARN += 1
    print(f"  ⚠️  {label}" + (f" — {detail}" if detail else ""))


def check_invariants(conn: sqlite3.Connection, since: str) -> None:
    _hdr("1. daily_kline 硬性不变量")
    row = conn.execute(
        f"""
        SELECT COUNT(*) total,
               SUM(open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) null_ohlc,
               SUM(close <= 0 OR open <= 0)                                      nonpos,
               SUM(high < low OR high < close OR high < open
                   OR low > close OR low > open)                                 ohlc_bad,
               SUM(vol < 0 OR amount < 0)                                        neg,
               SUM(length(trade_date) <> 8 OR ts_code NOT LIKE '______.__')      fmt_bad
        FROM daily_kline WHERE trade_date >= '{since}'
        """
    ).fetchone()
    total = row[0]
    print(f"  区间 {since} 起共 {total:,} 行")
    for key, label in [(1, "OHLC 有 NULL"), (2, "价格非正"), (3, "OHLC 大小关系错误"),
                       (4, "成交量/额为负"), (5, "代码或日期格式异常")]:
        n = row[key] or 0
        (_bad if n else _ok)(label, f"{n} 行" if n else "0 行")


def check_calendar(conn: sqlite3.Connection, since: str) -> list[str]:
    _hdr("2. 交易日历完整性")
    cal = [str(r[0]) for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date >= ? ORDER BY trade_date", (since,))]
    if not cal:
        _bad("区间内没有任何 K 线")
        return []
    print(f"  库内交易日 {len(cal)} 天（{cal[0]} ~ {cal[-1]}）")

    # 与官方日历比对（trade_cal 覆盖到哪算到哪）
    cal_days = {str(r[0]) for r in conn.execute("SELECT cal_date FROM trade_cal WHERE is_open=1")}
    if cal_days:
        lo, hi = min(cal_days), max(cal_days)
        overlap = {d for d in cal if lo <= d <= hi}
        miss = sorted(cal_days - overlap)
        extra = sorted(overlap - cal_days)
        if miss:
            _bad(f"trade_cal 有、库里没有（{lo}~{hi} 段）", f"{len(miss)} 天，如 {miss[:5]}")
        elif extra:
            _bad(f"库里有、trade_cal 没有（{lo}~{hi} 段）", f"{len(extra)} 天，如 {extra[:5]}")
        else:
            _ok(f"与 trade_cal 逐日吻合（{lo}~{hi}）")
        if hi < cal[-1]:
            _warn("trade_cal 覆盖不全", f"只到 {hi}，{hi} 之后无官方日历可校验")
    else:
        _warn("trade_cal 表为空", "无法用官方日历校验，只能用「连续工作日空档」启发式")

    # 连续工作日空档：超过 10 个工作日的空档不可能是法定假期
    d0 = date(int(cal[0][:4]), int(cal[0][4:6]), int(cal[0][6:]))
    d1 = date(int(cal[-1][:4]), int(cal[-1][4:6]), int(cal[-1][6:]))
    have, gap_run, runs, d = set(cal), [], [], d0
    while d <= d1:
        s = d.strftime("%Y%m%d")
        if d.weekday() < 5:
            if s in have:
                if gap_run:
                    runs.append(gap_run)
                gap_run = []
            else:
                gap_run.append(s)
        d += timedelta(days=1)
    if gap_run:
        runs.append(gap_run)
    long_runs = [r for r in runs if len(r) > 10]
    if long_runs:
        for r in long_runs:
            _bad("超长空档（不可能是法定假期）", f"{r[0]} ~ {r[-1]}，{len(r)} 个工作日")
    else:
        _ok("无超过 10 个连续工作日的空档")

    # 逐日覆盖率：以「首末日跨越该日的股票数」为在市基准
    per_stock = conn.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM daily_kline WHERE trade_date >= ? GROUP BY ts_code", (since,)
    ).fetchall()
    idx = {d: i for i, d in enumerate(cal)}
    delta: dict[int, int] = defaultdict(int)
    for mn, mx in per_stock:
        delta[idx[str(mn)]] += 1
        delta[idx[str(mx)] + 1] -= 1
    per_date = dict(conn.execute(
        "SELECT trade_date, COUNT(*) FROM daily_kline WHERE trade_date >= ? GROUP BY trade_date", (since,)))
    run = 0
    low = []
    for i, d in enumerate(cal):
        run += delta.get(i, 0)
        act = per_date.get(d, 0)
        if run and act / run < 0.60:
            low.append((d, run, act))
    if low:
        for d, exp, act in low[:10]:
            _bad("当日覆盖率异常低（几乎肯定是漏同步）", f"{d}：在市 {exp} 只，只有 {act} 只有数据")
    else:
        _ok("没有覆盖率低于 60% 的交易日")
    return cal


def check_stock_gaps(conn: sqlite3.Connection, cal: list[str], since: str) -> None:
    _hdr("3. 逐票缺口（会让「最近 N 行」窗口缝合不相邻 K 线）")
    if not cal:
        return
    idx = {d: i for i, d in enumerate(cal)}
    rows = conn.execute(
        """SELECT ts_code, MIN(trade_date), MAX(trade_date), COUNT(*) FROM daily_kline
           WHERE trade_date >= ? AND ts_code NOT LIKE '%.BJ' GROUP BY ts_code""", (since,)
    ).fetchall()
    live = {str(r[0]) for r in conn.execute("SELECT ts_code FROM stock_basic")}
    buckets: dict[str, int] = defaultdict(int)
    worst = []
    for code, mn, mx, cnt in rows:
        if code not in live:
            continue  # 退市股不参与选股，缺口无意义
        n = (idx[str(mx)] - idx[str(mn)] + 1) - cnt
        if n <= 0:
            continue
        buckets["1" if n == 1 else "2-5" if n <= 5 else "6-20" if n <= 20 else "21-60" if n <= 60 else ">60"] += 1
        worst.append((n, code))
    total = sum(buckets.values())
    print(f"  在市非北交所股票中，有缺口的 {total} 只，规模分布 {dict(buckets)}")
    if buckets[">60"]:
        _warn("有缺口超过 60 个交易日的票", f"{buckets['>60']} 只（长期停牌或数据洞，选股时应被 window_gaps 否决）")
    for n, code in sorted(worst, reverse=True)[:5]:
        print(f"     {code} 缺 {n} 天")
    _ok("缺口本身不是错误", "但递推指标会失真，靠 strategies.core.window_gaps 在判定时拦截")


def check_price_mode(conn: sqlite3.Connection, since: str) -> None:
    _hdr("4. 复权口径一致性（均价必须落在当日 [low, high] 内）")
    row = conn.execute(
        f"""SELECT COUNT(*), SUM(CASE WHEN amount*10.0/vol BETWEEN low*0.995 AND high*1.005 THEN 1 ELSE 0 END)
            FROM daily_kline WHERE trade_date >= '{since}' AND vol > 0"""
    ).fetchone()
    n, ok = row[0] or 0, row[1] or 0
    if not n:
        _warn("无可检查行")
        return
    pct = ok / n * 100
    if pct >= 99.9:
        _ok(f"自洽率 {pct:.3f}%", f"{n:,} 行；价格与成交额同口径（不复权）")
    else:
        _bad(f"自洽率仅 {pct:.2f}%", "价格与成交额口径不一致，怀疑复权价与原始成交额混存")


def check_limit_flags(conn: sqlite3.Connection, since: str) -> None:
    _hdr("5. 涨跌停标记（themes 的 limit_up_count 与大盘宽度都读它）")
    bad = conn.execute(
        f"""SELECT COUNT(*) FROM daily_kline
            WHERE trade_date >= '{since}' AND pct_chg IS NOT NULL
              AND (is_limit_up   <> (CASE WHEN pct_chg >=  {_THRESHOLD_SQL} THEN 1 ELSE 0 END)
                OR is_limit_down <> (CASE WHEN pct_chg <= -{_THRESHOLD_SQL} THEN 1 ELSE 0 END))"""
    ).fetchone()[0]
    if bad:
        _bad("标记与按板块阈值不一致", f"{bad} 行；跑 scripts/data_audit.py --fix-limit-flags 重算")
    else:
        _ok("标记与按板块阈值逐行一致")
    _warn("已知未覆盖的特例", "ST（±5%）与新股前 5 日（无限制）仍按普通阈值判定")


def check_amv(conn: sqlite3.Connection) -> None:
    _hdr("6. amv_daily（活跃市值 —— 选股总开关）")
    try:
        from modules.amv import classify
    except Exception as exc:  # pragma: no cover
        _warn("无法导入 modules.amv", str(exc))
        return
    rows = conn.execute(
        "SELECT trade_date, close, pct_chg, COALESCE(regime,''), COALESCE(regime_imported,'') "
        "FROM amv_daily ORDER BY trade_date"
    ).fetchall()
    if not rows:
        _bad("amv_daily 为空", "总开关无数据，选股会被全量拦截")
        return
    print(f"  共 {len(rows)} 行（{rows[0][0]} ~ {rows[-1][0]}）")

    chain_bad = [r[0] for i, r in enumerate(rows) if i and (
        r[2] is None or abs((r[1] / rows[i - 1][1] - 1) * 100 - r[2]) > 1e-6)]
    (_bad if chain_bad else _ok)("pct_chg 与 close 链自洽",
                                 f"{len(chain_bad)} 行不符，如 {chain_bad[:5]}" if chain_bad else "逐行吻合")

    calc = classify([r[2] for r in rows])
    regime_bad = [rows[i][0] for i in range(len(rows)) if rows[i][3] != calc[i]]
    (_bad if regime_bad else _ok)("regime 与规则重算一致",
                                  f"{len(regime_bad)} 行不符，如 {regime_bad[:5]}" if regime_bad else "逐行吻合")

    imported = [r for r in rows if r[4]]
    imp_bad = [r[0] for r in imported if r[3] != r[4]]
    (_bad if imp_bad else _ok)("与人工导入标注一致",
                               f"{len(imp_bad)} 行不符" if imp_bad else f"{len(imported)} 条标注零分歧")

    # 盘中快照：只有 close、没有 OHLC/成交量的行，说明当日未收盘就落了库
    snap = conn.execute(
        "SELECT trade_date, created_at FROM amv_daily WHERE open IS NULL OR volume IS NULL ORDER BY trade_date"
    ).fetchall()
    snap = [s for s in snap if str(s[0]) >= "20200101"]
    if snap:
        _warn("疑似盘中快照（缺 OHLC/成交量）",
              "、".join(f"{s[0]}(录于 {s[1]})" for s in snap[:3]) + "；未收盘的 close 会让区间判定提前生效")
    else:
        _ok("无盘中快照行")

    kl = {str(r[0]) for r in conn.execute("SELECT DISTINCT trade_date FROM daily_kline")}
    amv = {str(r[0]) for r in rows}
    lack = sorted(kl - amv)
    if lack:
        _bad("有 K 线但无活跃市值的交易日", f"{len(lack)} 天，如 {lack[:5]}")
    else:
        _ok("每个交易日都有活跃市值记录")


def crosscheck(conn: sqlite3.Connection, cal: list[str], n: int) -> None:
    _hdr(f"7. 与 Tushare 数据源逐行比对（随机抽 {n} 个交易日）")
    from modules.datasource import TushareDataSource

    ds = TushareDataSource()
    fields = ["open", "high", "low", "close", "vol", "amount", "pct_chg"]
    step = max(1, len(cal) // max(n, 1))
    picks = cal[::step][:n]
    for d in picks:
        try:
            df = ds.get_daily_by_trade_date(d)
        except Exception as exc:
            _warn(f"{d} 拉取失败", str(exc)[:80])
            continue
        if df is None or df.empty:
            _warn(f"{d} 数据源返回空", "中转 API 限流时会静默返回空，无法与非交易日区分——请低速重试")
            continue
        src = {r.ts_code: r for r in df.itertuples()}
        db = {str(r[0]): r for r in conn.execute(
            "SELECT ts_code,open,high,low,close,vol,amount,pct_chg FROM daily_kline WHERE trade_date=?", (d,))}
        missing, extra = len(set(src) - set(db)), len(set(db) - set(src))
        mism = 0
        for code in set(src) & set(db):
            s, b = src[code], db[code]
            for i, f in enumerate(fields, start=1):
                sv, bv = getattr(s, f, None), b[i]
                if sv is None or bv is None:
                    continue
                if abs(float(sv) - float(bv)) > max(abs(float(sv)) * 1e-4, 0.005):
                    mism += 1
        if missing or extra or mism:
            _bad(f"{d} 与数据源不一致", f"缺 {missing} 只、多 {extra} 只、字段不符 {mism} 处")
        else:
            _ok(f"{d} 与数据源逐行一致", f"{len(src)} 只 × {len(fields)} 字段")


def fix_limit_flags(conn: sqlite3.Connection) -> None:
    _hdr("修复：按板块阈值重算 is_limit_up / is_limit_down")
    cur = conn.execute(
        f"""UPDATE daily_kline
            SET is_limit_up   = (CASE WHEN pct_chg >=  {_THRESHOLD_SQL} THEN 1 ELSE 0 END),
                is_limit_down = (CASE WHEN pct_chg <= -{_THRESHOLD_SQL} THEN 1 ELSE 0 END)
            WHERE pct_chg IS NOT NULL
              AND (is_limit_up   <> (CASE WHEN pct_chg >=  {_THRESHOLD_SQL} THEN 1 ELSE 0 END)
                OR is_limit_down <> (CASE WHEN pct_chg <= -{_THRESHOLD_SQL} THEN 1 ELSE 0 END))"""
    )
    conn.commit()
    print(f"  已重算 {cur.rowcount} 行")
    print("  ⚠️  涨停家数变了，受影响日期的 theme_strength 需要重跑 `zt theme rank <日期>`")


def main() -> int:
    ap = argparse.ArgumentParser(description="生产库行情数据审计")
    ap.add_argument("--db", default=str(PROJECT_ROOT / "data" / "stock_data.db"))
    ap.add_argument("--since", default="20160101", help="只检查该日期之后的数据")
    ap.add_argument("--crosscheck", type=int, default=0, metavar="N", help="额外抽 N 个交易日与数据源比对（联网）")
    ap.add_argument("--fix-limit-flags", action="store_true", help="按板块阈值重算涨跌停标记")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    print("=" * 72)
    print(f"行情数据审计  库: {args.db}")
    print("=" * 72)

    if args.fix_limit_flags:
        fix_limit_flags(conn)
        return 0

    check_invariants(conn, args.since)
    cal = check_calendar(conn, args.since)
    check_stock_gaps(conn, cal, args.since)
    check_price_mode(conn, args.since)
    check_limit_flags(conn, args.since)
    check_amv(conn)
    if args.crosscheck:
        crosscheck(conn, cal, args.crosscheck)

    print("\n" + "=" * 72)
    print(f"审计完成：致命问题 {FATAL} 项，提示 {WARN} 项")
    print("=" * 72)
    return 1 if FATAL else 0


if __name__ == "__main__":
    sys.exit(main())
