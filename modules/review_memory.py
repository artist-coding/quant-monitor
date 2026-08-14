"""复盘案例库：人工复盘记忆的案例层（episodic memory）。

设计口径：

- **复盘的发起权在人**。用户盘后看到一个值得研究的买点（无论框架有没有选中），
  用 ``zt review add`` 把它录进来。系统不自动生产"错过案例"——绝大多数大涨
  本来就不在框架的能力圈内，自动扫涨幅榜只会往案例库里灌噪音。
- **录入即归因回放**。在案例时点用回测同款口径重放 ``confirm_buy``，把
  "图形不错但框架没选"翻译成结构化记录：卡在哪一层（``stopped_at``）、
  各维度几分（``breakdown``）、总开关放不放行（``gate_blocked``）。
- **前瞻收益随录随算**。次日开盘价买入口径（与 ``framework_backtest._forward``
  一致），未来数据不够时留空，之后 ``zt review settle`` 补齐。

案例只负责"记住"。聚合成教训、派生假设、回测验证是后续层的事——
本模块刻意不做任何自动改参数的动作：未经回测验证的直觉不进打分。

回放口径与 ``framework_backtest.replay_stock`` 的三处对齐（都是踩过的坑）：

1. K 线直接查 ``daily_kline`` 现算指标，不联 ``indicator_cache``（补数后没重算）；
2. 窗口按该股自身 K 线切最近 ``KLINE_DAYS`` 根，切片前清掉最后 ``FRESH_BARS``
   根上缓存的 KDJ（对象跨窗口共享，不清会读到上个窗口的 J 值）；
3. 判定前确保当日分组强度快照存在，否则主线归属恒为 None。

与 ``replay_stock`` 的一处刻意不同：这里不清 ``dec.detail``——归因明细
正是案例的价值所在（回测清它是为了省几十万条决策的内存，这里一次只有一条）。
"""

from __future__ import annotations

import bisect
import json
import logging
from typing import Any

from . import framework_backtest as fb
from .amv import _norm_date
from .buy_decision import FRESH_BARS, confirm_buy
from .database import ensure_review_cases_table, get_connection
from .watchlist import normalize_ts_code

logger = logging.getLogger(__name__)

# 前瞻收益窗口（交易日）。上限与回测的 HOLD_DAYS 对齐，
# 短窗口用来区分"启动快慢"——同样 +30 日赚 15%，5 日内到和磨到第 25 天不是一种买点。
HORIZONS = (5, 10, 20, fb.HOLD_DAYS)

VALID_SOURCES = ("manual", "missed", "failed")

_STOPPED_LABELS = {
    "excluded": "第0层排除（ST/北交所，不进票池）",
    "market_gate": "活跃市值总开关拦截",
    "veto": "一票否决",
    "no_trigger": "B1 未触发",
    "scored": "走完全部打分流程",
    "no_data": "数据不足，无法判定",
}


# ==================== 归因回放 ====================


def _replay_attribution(
    conn, ts_code: str, case_date: str, *, theme_lookback: int | None = None, precompute_theme: bool = True
) -> dict[str, Any]:
    """在案例时点重放框架，返回归因 + 前瞻收益。

    总开关（活跃市值区间）单独记录而不是让它截断判定：复盘要的是完整答案
    ——"当日空头区间会被拦，但即便放行，B1 也没触发"比只知道前半句有用得多。
    """
    from .themes import DEFAULT_LOOKBACK

    lookback = theme_lookback or DEFAULT_LOOKBACK

    row = conn.execute("SELECT name FROM stock_basic WHERE ts_code = ?", (ts_code,)).fetchone()
    name = str(row[0]) if row and row[0] else ""

    calendar = [str(r[0]) for r in conn.execute("SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date")]
    if not calendar:
        raise ValueError("库内没有任何行情数据，请先同步日线")
    cal_pos = {d: i for i, d in enumerate(calendar)}
    regimes = fb.load_regimes(conn, calendar)

    klines = fb.load_stock_klines(conn, ts_code, "00000000", "99999999")
    if not klines:
        raise ValueError(f"{ts_code} 库内无 K 线数据")
    dates = [k.trade_date for k in klines]
    i = bisect.bisect_right(dates, case_date) - 1
    if i < 0:
        raise ValueError(f"{case_date} 早于 {ts_code} 最早的 K 线（{dates[0]}），无数据可复盘")
    decision_date = dates[i]
    pos = {d: j for j, d in enumerate(dates)}

    regime = regimes.get(decision_date, "")
    gate_blocked = regime != "多头区间"

    if precompute_theme:
        try:
            todo = fb.missing_theme_dates(conn, [decision_date], lookback)
            if todo:
                fb.precompute_theme_strength(todo, lookback, progress_every=0)
        except Exception as exc:
            logger.warning("分组强度快照补算失败 %s: %s", decision_date, exc)

    window = klines[max(0, i - fb.KLINE_DAYS + 1) : i + 1]
    for k in window[-FRESH_BARS:]:
        k.kdj_k = k.kdj_d = k.kdj_j = None

    dec = confirm_buy(
        ts_code,
        decision_date,
        market={},
        klines=window,
        theme_lookback=lookback,
        skip_market_gate=True,
        mdc_scope=FRESH_BARS,
        name=name,
    )

    stopped_at = dec.detail.get("stopped_at") or ("scored" if dec.detail.get("breakdown") else "no_data")
    forward = _forward_multi(klines, pos, calendar, cal_pos, decision_date)

    decision_payload = {
        "ts_code": dec.ts_code,
        "trade_date": dec.trade_date,
        "name": dec.name,
        "action": dec.action,
        "score": dec.score,
        "confidence": dec.confidence,
        "base_strategy": dec.base_strategy,
        "triggers": dec.triggers,
        "confirms": dec.confirms,
        "vetoes": dec.vetoes,
        "theme": dec.theme,
        "detail": dec.detail,
    }
    return {
        "name": name or dec.name,
        "decision_date": decision_date,
        "regime": regime,
        "gate_blocked": int(gate_blocked),
        "action": dec.action,
        "score": round(float(dec.score), 2),
        "stopped_at": stopped_at,
        "decision_json": json.dumps(decision_payload, ensure_ascii=False, default=str),
        **forward,
    }


def _forward_multi(klines, pos: dict[str, int], calendar, cal_pos: dict[str, int], decision_date: str) -> dict[str, Any]:
    """决策日次日开盘买入，各窗口收盘结算——口径对齐 ``framework_backtest._forward``。

    与回测不同的是窗口不齐时不返回 None 而是算多少记多少：
    案例经常是"三天前的买点"，先把 +5 日记上，剩下的等 settle。
    """
    out: dict[str, Any] = {
        "entry_date": "",
        "entry_price": None,
        "unbuyable": 0,
        "ret_5": None,
        "ret_10": None,
        "ret_20": None,
        "ret_30": None,
        "ret_peak_30": None,
        "settled": 0,
    }
    ci = cal_pos.get(decision_date)
    if ci is None or ci + 1 >= len(calendar):
        return out  # 还没有次日行情，待结算

    entry_date = calendar[ci + 1]
    ei = pos.get(entry_date)
    if ei is None or klines[ei].open <= 0:
        # 次日停牌买不进。与回测同口径按终态处理：没有仓位，无可结算。
        out["unbuyable"] = 1
        out["settled"] = 1
        return out
    entry_bar = klines[ei]
    out["entry_date"] = entry_bar.trade_date
    out["entry_price"] = entry_bar.open
    if entry_bar.open == entry_bar.high == entry_bar.low and float(entry_bar.pct_chg or 0) >= fb._LIMIT_UP_PCT:
        # 一字涨停名义上买不进。收益照算：复盘要回答的是"如果买到了会怎样"。
        out["unbuyable"] = 1

    settled = True
    xi_last = ei
    for h in HORIZONS:
        if ci + h >= len(calendar):
            settled = False
            continue
        target = calendar[ci + h]
        xi = pos.get(target)
        if xi is None:  # 目标日个股停牌，取不晚于目标日的最近一根
            xi = None
            for j in range(ei, len(klines)):
                if klines[j].trade_date <= target:
                    xi = j
                else:
                    break
        if xi is None:
            settled = False
            continue
        out[f"ret_{h}"] = klines[xi].close / entry_bar.open - 1.0
        xi_last = max(xi_last, xi)

    if out[f"ret_{fb.HOLD_DAYS}"] is not None:
        highest = max(klines[j].high for j in range(ei, xi_last + 1))
        out["ret_peak_30"] = highest / entry_bar.open - 1.0
    out["settled"] = 1 if settled else 0
    return out


# ==================== 案例 CRUD ====================


def add_case(
    code: str,
    date: str,
    *,
    note: str = "",
    tags: str = "",
    source: str = "manual",
    theme_lookback: int | None = None,
    precompute_theme: bool = True,
) -> dict[str, Any]:
    """录入（或刷新）一个复盘案例：归因回放 + 前瞻收益 + 落库。

    幂等：同一 (ts_code, case_date) 重复录入时刷新归因与收益，
    note/tags 传了非空值才覆盖，status/lesson 保持不动——
    重跑归因不应该抹掉已经做过的人工整理。
    """
    ts_code = normalize_ts_code(code)
    case_date = _norm_date(date)
    if source not in VALID_SOURCES:
        raise ValueError(f"source 必须是 {VALID_SOURCES} 之一")

    conn = fb._connect()
    try:
        replay = _replay_attribution(
            conn, ts_code, case_date, theme_lookback=theme_lookback, precompute_theme=precompute_theme
        )
    finally:
        conn.close()

    with get_connection() as c:
        ensure_review_cases_table(c)
        c.execute(
            """
            INSERT INTO review_cases (
                ts_code, name, case_date, decision_date, source, note, tags,
                regime, gate_blocked, action, score, stopped_at, decision_json,
                entry_date, entry_price, unbuyable,
                ret_5, ret_10, ret_20, ret_30, ret_peak_30, settled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts_code, case_date) DO UPDATE SET
                name = excluded.name,
                decision_date = excluded.decision_date,
                source = excluded.source,
                note = CASE WHEN excluded.note != '' THEN excluded.note ELSE review_cases.note END,
                tags = CASE WHEN excluded.tags != '' THEN excluded.tags ELSE review_cases.tags END,
                regime = excluded.regime,
                gate_blocked = excluded.gate_blocked,
                action = excluded.action,
                score = excluded.score,
                stopped_at = excluded.stopped_at,
                decision_json = excluded.decision_json,
                entry_date = excluded.entry_date,
                entry_price = excluded.entry_price,
                unbuyable = excluded.unbuyable,
                ret_5 = excluded.ret_5,
                ret_10 = excluded.ret_10,
                ret_20 = excluded.ret_20,
                ret_30 = excluded.ret_30,
                ret_peak_30 = excluded.ret_peak_30,
                settled = excluded.settled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                ts_code, replay["name"], case_date, replay["decision_date"], source, note, tags,
                replay["regime"], replay["gate_blocked"], replay["action"], replay["score"],
                replay["stopped_at"], replay["decision_json"],
                replay["entry_date"], replay["entry_price"], replay["unbuyable"],
                replay["ret_5"], replay["ret_10"], replay["ret_20"], replay["ret_30"],
                replay["ret_peak_30"], replay["settled"],
            ),
        )
        row = c.execute(
            "SELECT * FROM review_cases WHERE ts_code = ? AND case_date = ?", (ts_code, case_date)
        ).fetchone()
    return _row_to_dict(row)


def settle_open_cases() -> list[dict[str, Any]]:
    """给前瞻收益还没算满的案例补结算，返回本次有更新的案例。"""
    with get_connection() as c:
        ensure_review_cases_table(c)
        rows = c.execute(
            "SELECT id, ts_code, decision_date FROM review_cases WHERE settled = 0 ORDER BY case_date"
        ).fetchall()
    if not rows:
        return []

    conn = fb._connect()
    try:
        calendar = [str(r[0]) for r in conn.execute("SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date")]
        cal_pos = {d: i for i, d in enumerate(calendar)}
        updated: list[dict[str, Any]] = []
        for r in rows:
            klines = fb.load_stock_klines(conn, str(r["ts_code"]), "00000000", "99999999")
            if not klines:
                continue
            pos = {k.trade_date: j for j, k in enumerate(klines)}
            fwd = _forward_multi(klines, pos, calendar, cal_pos, str(r["decision_date"]))
            with get_connection() as c:
                c.execute(
                    """
                    UPDATE review_cases SET
                        entry_date = ?, entry_price = ?, unbuyable = ?,
                        ret_5 = ?, ret_10 = ?, ret_20 = ?, ret_30 = ?, ret_peak_30 = ?,
                        settled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        fwd["entry_date"], fwd["entry_price"], fwd["unbuyable"],
                        fwd["ret_5"], fwd["ret_10"], fwd["ret_20"], fwd["ret_30"], fwd["ret_peak_30"],
                        fwd["settled"], int(r["id"]),
                    ),
                )
                row = c.execute("SELECT * FROM review_cases WHERE id = ?", (int(r["id"]),)).fetchone()
            updated.append(_row_to_dict(row))
        return updated
    finally:
        conn.close()


def list_cases(*, limit: int = 20, source: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM review_cases WHERE 1=1"
    params: list[Any] = []
    if source:
        sql += " AND source = ?"
        params.append(source)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY case_date DESC, id DESC LIMIT ?"
    params.append(limit)
    with get_connection() as c:
        ensure_review_cases_table(c)
        rows = c.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_case(case_id: int) -> dict[str, Any] | None:
    with get_connection() as c:
        ensure_review_cases_table(c)
        row = c.execute("SELECT * FROM review_cases WHERE id = ?", (case_id,)).fetchone()
    return _row_to_dict(row) if row else None


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    raw = d.pop("decision_json", "") or ""
    try:
        d["decision"] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        d["decision"] = {}
    return d


# ==================== 渲染 ====================


def _pct(value: float | None) -> str:
    return f"{value * 100:+.1f}%" if value is not None else "-"


def format_case(case: dict[str, Any]) -> str:
    """单个案例的完整视图：人工记录 + 框架归因 + 前瞻收益。"""
    dec = case.get("decision") or {}
    lines = [
        f"#{case['id']} {case['ts_code']} {case.get('name') or ''} @ {case['case_date']}"
        f"  [{case['source']}]  {case['status']}"
    ]
    if case.get("decision_date") and case["decision_date"] != case["case_date"]:
        lines.append(f"  （{case['case_date']} 非该股交易日，归因用最近一根 {case['decision_date']}）")
    if case.get("note"):
        lines.append(f"复盘: {case['note']}")
    if case.get("tags"):
        lines.append(f"标签: {case['tags']}")

    lines.append("── 框架归因（回放当时的判断）──")
    if case.get("regime"):
        gate = "总开关拦截：当日不选股" if case["gate_blocked"] else "总开关放行"
        lines.append(f"活跃市值区间: {case['regime']} → {gate}")
    else:
        lines.append("活跃市值区间: 无数据 → 总开关拦截（视同不选股）")
    stopped = _STOPPED_LABELS.get(case.get("stopped_at", ""), case.get("stopped_at", ""))
    lines.append(f"个股判定: {case['action'] or 'NONE'}  {case['score']:.1f} 分  卡在: {stopped}")
    reason = (dec.get("detail") or {}).get("reason")
    if reason:
        lines.append(f"  说明: {reason}")
    for v in dec.get("vetoes") or []:
        lines.append(f"  否决: {v}")
    for t in dec.get("triggers") or []:
        lines.append(
            f"  触发: {t.get('strategy')}（{t.get('trade_date')}，置信度{float(t.get('confidence', 0) or 0):.2f}）"
        )
    breakdown = (dec.get("detail") or {}).get("breakdown")
    if breakdown:
        lines.append("  得分明细: " + " / ".join(f"{k} {v:+.1f}" for k, v in breakdown.items()))
    theme = dec.get("theme")
    if theme:
        lines.append(
            f"  分组归属: {theme.get('theme', '')}（强度 {theme.get('strength', 0)}，{theme.get('kind', '')}）"
        )

    lines.append("── 前瞻收益（次日开盘价买入口径）──")
    if case.get("unbuyable") and not case.get("entry_date"):
        lines.append("次日停牌，买不进。")
    elif not case.get("entry_date"):
        lines.append("尚无次日行情，待结算（zt review settle）。")
    else:
        head = f"{case['entry_date']} 开盘 {case['entry_price']:.2f} 买入"
        if case.get("unbuyable"):
            head += "（一字涨停，名义上买不进）"
        lines.append(head)
        rets = "   ".join(f"+{h}日 {_pct(case.get(f'ret_{h}'))}" for h in HORIZONS)
        rets += f"   30日内最高 {_pct(case.get('ret_peak_30'))}"
        if not case.get("settled"):
            rets += "   （未满窗口，zt review settle 补齐）"
        lines.append(rets)
    return "\n".join(lines)


def format_case_list(cases: list[dict[str, Any]]) -> str:
    if not cases:
        return "案例库为空。用 zt review add <代码> <日期> --note ... 录入第一个复盘案例。"
    lines = [f"{'ID':<4} {'日期':<9} {'代码':<10} {'名称':<8} {'来源':<7} {'归因':<12} {'+30日':>8}  备注"]
    lines.append("-" * 90)
    for c in cases:
        gate = "区间拦" if c["gate_blocked"] else ""
        stopped = c.get("stopped_at", "")
        attribution = f"{gate}{'/' if gate and stopped else ''}{stopped}"
        note = (c.get("note") or "").replace("\n", " ")
        if len(note) > 24:
            note = note[:24] + "…"
        lines.append(
            f"#{c['id']:<3} {c['case_date']:<9} {c['ts_code']:<10} {(c.get('name') or ''):<8} "
            f"{c['source']:<7} {attribution:<12} {_pct(c.get('ret_30')):>8}  {note}"
        )
    return "\n".join(lines)
