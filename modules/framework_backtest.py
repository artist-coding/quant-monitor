"""买点框架的历史回放回测：逐日重跑 confirm_buy + select_final_picks，看 30 日后的涨跌。

回答的问题只有一个：**这套框架自己选出来的票，买了之后 30 个交易日赚不赚钱。**

口径
----

- **逐日回放，不是每日买入。** 从 ``start`` 到 ``end`` 的每个交易日都完整跑一遍
  框架（活跃市值门槛 → 逐票 B1 确认 → 主线/行业筛选）。多数交易日的产出是空的，
  这本身就是框架的一部分。
- **不接入 kimi。** ``theme_members`` 是空表，第二阶段的分组因此全部落到行业兜底
  （``rank_themes`` 在历史日期上只排得出 93 个行业分组）。这正是"只看指标选股"
  的口径——主线归属需要外部判定器，回测里没有也不该有。
- **次日开盘买入。** ``SCORE_BUY`` 的注释写的就是"明日开盘可买"，收盘后出信号、
  次日开盘成交是这套框架自己的语义。开盘一字涨停买不进的单子会被单独标出。
- **持有 30 个交易日。** 决策日 D 记为第 0 天，买入 D+1 开盘，卖出 D+30 收盘，
  天数按**全市场交易日历**数，不按个股自己的 K 线数——停牌 10 天的票不该因此
  多持有 10 天。

止损的两种口径
--------------

用户的原话是"跌幅大于 20% 就按 20% 算，因为大于 20% 我最终选股时会强止损"。
这句话有两种读法，数值差别不小，所以两种都算：

- ``stop``：**路径止损**。持有期内最低价一旦触及买入价的 −20%，这笔就记 −20%
  （不管它后来涨没涨回来）。这是"强止损"的真实行为。
- ``floor``：**只封底**。只把第 30 天的最终收益率截断到 −20%。这是字面读法，
  也是止损的乐观上界——它默许了"中途跌 30% 又涨回 −5%"这种没被止损打掉的情形。

真实结果落在两者之间，偏向 ``stop``。另外一并保留未截断的 ``raw`` 作对照。

基准
----

没有基准的收益率没有意义。每个决策日都算一遍**同期全市场等权 30 日收益**
（当日有行情的全部可交易票，同样是次日开盘买、第 30 日收盘卖），
选股收益减去它才是这套框架真正的超额。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .buy_decision import (
    DEFAULT_MIN_GROUP_STRENGTH,
    DEFAULT_TOP_N,
    FRESH_BARS,
    BuyDecision,
    apply_picks,
    confirm_buy,
    select_final_picks,
)
from .database import get_db_path
from .universe import TRADABLE_PREDICATE

logger = logging.getLogger(__name__)


# ==================== 常量 ====================

# 持有期（交易日）。用户给定：看之后 30 个交易日的涨跌幅。
HOLD_DAYS = 30

# 强止损线。用户给定：跌超 20% 就止损，故收益率下界为 -20%。
STOP_LOSS = -0.20

# 回放时每只票取多少根 K 线。与 scan_market 的默认值一致，
# 换个数字就不是在回测"这套框架"了。
KLINE_DAYS = 150

# 一字涨停的判定：开=高=低 且 涨幅 >= 9.9%。与同步器 is_limit_up 的阈值一致。
# 这种票次日开盘根本买不进，单独标出来，免得回测收益里混进买不到的单子。
_LIMIT_UP_PCT = 9.9


@dataclass
class Trade:
    """一笔回放出来的买入信号及其 30 日后的结果。"""

    ts_code: str
    name: str
    decision_date: str  # 出信号的交易日（收盘后）
    action: str  # BUY / WATCH
    score: float
    pick_rank: int  # 第二阶段入选名次；0 = 未入选
    group: str  # 主线/行业名
    group_strength: float
    regime: str  # 决策日的活跃市值区间

    entry_date: str = ""
    entry_price: float = 0.0
    exit_date: str = ""
    exit_price: float = 0.0
    lowest: float = 0.0  # 持有期内最低价
    highest: float = 0.0  # 持有期内最高价
    # 止损是否发生在最高点**之前**。决定"卖在最高点"这一假设成不成立：
    # 先跌破 -20% 再涨上去的，人早被打出去了，看不到那个高点。
    stopped_before_peak: bool = False
    unbuyable: bool = False  # 次日一字涨停，实际买不进
    resolved: bool = False  # 拿得到完整的进出场价

    @property
    def ret_raw(self) -> float:
        """未截断的 30 日收益率。"""
        if not self.resolved or self.entry_price <= 0:
            return 0.0
        return self.exit_price / self.entry_price - 1.0

    @property
    def ret_stop(self) -> float:
        """路径止损口径：期间最低价触及 -20% 即记 -20%。"""
        if not self.resolved or self.entry_price <= 0:
            return 0.0
        if self.lowest <= self.entry_price * (1.0 + STOP_LOSS):
            return STOP_LOSS
        return self.ret_raw

    @property
    def ret_floor(self) -> float:
        """只封底口径：仅把最终收益率截断到 -20%。"""
        return max(self.ret_raw, STOP_LOSS)

    @property
    def ret_peak(self) -> float:
        """上帝视角：没被止损的票一律卖在持有期最高价。

        **这是收益的天花板，不是可实现的收益。** 卖在波段最高点需要事后才知道
        哪一天是最高点。它的用处是给出上界：如果连这个数都不够好，那么再怎么
        优化卖点也救不回来；反过来，它与 ``ret_stop`` 的差距就是"卖点还有多少
        改进空间"。

        止损优先级按时间先后判定，不是"期间碰过就算"——见 stopped_before_peak。
        """
        if not self.resolved or self.entry_price <= 0:
            return 0.0
        if self.stopped_before_peak:
            return STOP_LOSS
        return self.highest / self.entry_price - 1.0

    @property
    def ret_peak_nostop(self) -> float:
        """纯上界：不设止损，一律卖在最高价。恒 >= 0（最高价不低于开盘价）。"""
        if not self.resolved or self.entry_price <= 0:
            return 0.0
        return self.highest / self.entry_price - 1.0


@dataclass
class DayResult:
    """单个决策日的回放结果。"""

    trade_date: str
    regime: str
    gate_passed: bool
    scanned: int = 0
    buy_count: int = 0
    watch_count: int = 0
    picks: list[Trade] = field(default_factory=list)
    all_buys: list[Trade] = field(default_factory=list)
    baseline: float | None = None  # 同期全市场等权 30 日收益（持有到期）
    # 同期全市场等权、**每只票也卖在最高价**的基准。给选股开上帝视角时，
    # 必须给基准开同样的视角，否则是拿开挂的策略比不开挂的市场。
    baseline_peak: float | None = None
    baseline_n: int = 0


# ==================== 数据装载 ====================


def _connect() -> sqlite3.Connection:
    """回测自己开一条长连接。

    ``database.get_connection`` 每次调用都新建连接并设 PRAGMA，
    回放要读几千只票、判几十万次，逐次建连接的开销比查询本身还大。
    """
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    # 回放是多进程持续读，同时可能有同步任务在写。不设 busy_timeout 的话，
    # 写方拿不到锁会直接抛 "database is locked" 而不是等一等——
    # 补历史数据的长任务就是这么被回测挤死过一次。
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def load_calendar(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    """全市场交易日历（库内有行情的日期），升序。"""
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (start, end),
        )
    ]


def load_regimes(conn: sqlite3.Connection, calendar: Sequence[str]) -> dict[str, str]:
    """活跃市值区间：{交易日: 区间}，缺失的交易日沿用前一日。

    沿用是必须的，不是补丁：``amv.get_regime`` 取的就是"不晚于目标日的最近一条"，
    而区间本身是个状态机——某天没录入不等于那天没有区间。要求当日精确命中会把
    这些日子静默判成"不选股"，等于凭空给框架加了一道它没有的门槛。
    """
    rows = dict((str(r[0]), str(r[1])) for r in conn.execute("SELECT trade_date, regime FROM amv_daily"))
    out: dict[str, str] = {}
    carried = ""
    for d in calendar:
        carried = rows.get(d, carried)
        if carried:
            out[d] = carried
    return out


def load_universe(conn: sqlite3.Connection, start: str, end: str) -> dict[str, str]:
    """回测股票池：区间内有过行情的可交易票 → 名称。

    ST 与北交所按 ``universe.TRADABLE_PREDICATE`` 排除。注意这是**当前**名称，
    历史上戴过帽又摘掉的票会被当成正常股——本地没有名称变更历史，修不掉，
    ``universe`` 的模块文档里已写明这个偏差。
    """
    rows = conn.execute(
        f"""
        SELECT DISTINCT b.ts_code, b.name
        FROM daily_kline k JOIN stock_basic b ON b.ts_code = k.ts_code
        WHERE k.trade_date BETWEEN ? AND ? AND {TRADABLE_PREDICATE}
        ORDER BY b.ts_code
        """,
        (start, end),
    ).fetchall()
    return {str(r[0]): str(r[1] or "") for r in rows}


def load_stock_klines(conn: sqlite3.Connection, ts_code: str, start: str, end: str) -> list:
    """取单只票的 DailyData 序列（升序），字段口径与 strategies.core.get_kline_data 一致。

    直接查 daily_kline，不联 indicator_cache / moneyflow：那两张表一个只覆盖
    7 只票、一个是空的，联表只是给每只票多加两次全表扫描。MDC 字段由
    ``attach_mdc_fields`` 现算，资金流字段本来就恒为 0。
    """
    from .indicators import DailyData

    rows = conn.execute(
        """
        SELECT ts_code, trade_date, open, high, low, close, vol, amount, pct_chg
        FROM daily_kline WHERE ts_code = ? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (ts_code, start, end),
    ).fetchall()

    out: list = []
    for i, r in enumerate(rows):
        prev_close = float(rows[i - 1]["close"]) if i > 0 else float(r["close"])
        prev_vol = float(rows[i - 1]["vol"]) if i > 0 else float(r["vol"])
        close, vol = float(r["close"]), float(r["vol"])
        out.append(
            DailyData(
                ts_code=str(r["ts_code"]),
                trade_date=str(r["trade_date"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=close,
                vol=vol,
                amount=float(r["amount"] or 0),
                pct_chg=float(r["pct_chg"] or 0),
                prev_close=prev_close,
                is_rise=close > prev_close,
                is_beidou=vol >= prev_vol * 2,
                is_suoliang=vol <= prev_vol * 0.5,
                is_jiayin=close < float(r["open"]) and close > prev_close,
                is_yinxian=close < prev_close,
                is_fangliang_yinxian=close < prev_close and vol > prev_vol * 1.5,
            )
        )
    return out


def precompute_theme_strength(
    dates: Sequence[str], lookback: int, *, progress_every: int = 20, on_progress: Any = None
) -> int:
    """把每个决策日的分组强度快照算出来落库。

    这一步不能省：``get_stock_theme_strength`` 取的是"不晚于目标日的最近一期"
    快照，而库里原本只有最后一次实盘扫描留下的那一天。缺快照时它返回 None，
    第二阶段会把所有候选判成"无主线/行业归属"全部落选——回测结果会是**空的**，
    而且空得很像"框架什么都没选出来"，非常容易误读。

    ``theme_members`` 是空表，所以算出来的只有行业分组和全市场基准，
    没有主线分组——这正是"不接入 kimi"的回测口径。
    """
    from .themes import rank_themes

    done = 0
    for i, d in enumerate(dates, start=1):
        try:
            rank_themes(d, lookback, persist=True)
            done += 1
        except Exception as exc:
            logger.warning("分组强度快照失败 %s: %s", d, exc)
        if progress_every and i % progress_every == 0:
            msg = f"分组强度快照 {i}/{len(dates)}"
            logger.info(msg)
            if on_progress:
                on_progress(msg)
    return done


def missing_theme_dates(conn: sqlite3.Connection, dates: Sequence[str], lookback: int) -> list[str]:
    """还没有分组强度快照的日期。"""
    have = {
        str(r[0])
        for r in conn.execute("SELECT DISTINCT trade_date FROM theme_strength WHERE lookback = ?", (lookback,))
    }
    return [d for d in dates if d not in have]


# ==================== 前瞻收益 ====================


def _forward(
    klines: Sequence[Any],
    pos: dict[str, int],
    calendar: Sequence[str],
    cal_pos: dict[str, int],
    decision_date: str,
) -> tuple[str, float, str, float, float, bool, float, bool] | None:
    """算一笔从 decision_date 出发的 30 日持仓结果。

    Returns:
        (入场日, 入场价, 出场日, 出场价, 期间最低价, 是否一字涨停买不进,
         期间最高价, 止损是否发生在最高点之前)；拿不到完整进出场时返回 None。
    """
    ci = cal_pos.get(decision_date)
    if ci is None or ci + HOLD_DAYS >= len(calendar):
        return None

    entry_date = calendar[ci + 1]
    ei = pos.get(entry_date)
    if ei is None:  # 次日停牌，买不进
        return None
    entry_bar = klines[ei]
    if entry_bar.open <= 0:
        return None

    # 出场日按交易日历数；个股当天停牌则取之前最近的一根
    target_exit = calendar[ci + HOLD_DAYS]
    xi = pos.get(target_exit)
    if xi is None:
        xi = None
        for j in range(ei, len(klines)):
            if klines[j].trade_date <= target_exit:
                xi = j
            else:
                break
        if xi is None or xi == ei:
            return None

    lowest = min(klines[j].low for j in range(ei, xi + 1))

    # 最高点及其发生日；并列时取**最早**那天（更早卖出，对"卖在高点"这个
    # 假设更有利，与它上界的定位一致）
    peak_i = ei
    highest = klines[ei].high
    for j in range(ei + 1, xi + 1):
        if klines[j].high > highest:
            highest, peak_i = klines[j].high, j

    # 止损是否发生在最高点之前。日线看不到日内高低点的先后，同一天内
    # 既破止损又创新高时按**先破止损**处理——宁可低估这个上界。
    stop_price = entry_bar.open * (1.0 + STOP_LOSS)
    stopped_before_peak = any(klines[j].low <= stop_price for j in range(ei, peak_i + 1))

    unbuyable = (
        entry_bar.open == entry_bar.high == entry_bar.low and float(entry_bar.pct_chg or 0) >= _LIMIT_UP_PCT
    )
    return (
        entry_bar.trade_date, entry_bar.open, klines[xi].trade_date, klines[xi].close,
        lowest, unbuyable, highest, stopped_before_peak,
    )


# ==================== 回放 ====================


def replay_stock(
    conn: sqlite3.Connection,
    ts_code: str,
    name: str,
    decision_dates: Sequence[str],
    calendar: Sequence[str],
    cal_pos: dict[str, int],
    regimes: dict[str, str],
    *,
    theme_lookback: int | None,
    load_start: str,
    load_end: str,
    gate: bool = True,
) -> tuple[list[tuple[str, BuyDecision, tuple]], dict[str, tuple[float, float]], list[str]]:
    """把一只票在所有决策日上跑一遍。

    Returns:
        (命中列表, 基准贡献, 实际判定过的日期)。命中列表元素为
        (决策日, 决策, 前瞻结果元组)，只含 BUY/WATCH。基准贡献为
        {决策日: (持有到期收益, 卖在最高点收益)}，用于汇总全市场等权基准——
        顺手在这里算，省得为基准再遍历一次全库。两个口径都要：给选股开
        "卖在最高点"的上帝视角时，基准必须开同样的视角才可比。
    """
    klines = load_stock_klines(conn, ts_code, load_start, load_end)
    if len(klines) < 31:
        return [], {}, []
    pos = {k.trade_date: i for i, k in enumerate(klines)}

    hits: list[tuple[str, BuyDecision, tuple]] = []
    contrib: dict[str, tuple[float, float]] = {}
    evaluated: list[str] = []

    for d in decision_dates:
        i = pos.get(d)
        if i is None or i < 30:  # 停牌，或该日之前 K 线不足 _MIN_BARS
            continue

        fwd = _forward(klines, pos, calendar, cal_pos, d)
        if fwd is not None and fwd[1] > 0:
            entry = fwd[1]
            peak_ret = STOP_LOSS if fwd[7] else fwd[6] / entry - 1.0
            contrib[d] = (fwd[3] / entry - 1.0, peak_ret)

        # 活跃市值区间是框架的总开关：gate=True 时空头区间一根 K 线都不判。
        # gate=False 则连空头日一起判，用来回答"这道门槛到底挡掉了什么"——
        # 挡掉的如果是一批赚钱的信号，那门槛就是在误伤。
        # 每笔信号都带着当日区间（Trade.regime），汇总时按区间拆开看即可。
        if gate and regimes.get(d) != "多头区间":
            continue
        evaluated.append(d)

        window = klines[max(0, i - KLINE_DAYS + 1) : i + 1]
        # KDJ 会被缓存在 K 线对象上，而对象在各个窗口之间是共享的。
        # 不清掉的话，判定用的是**上一个窗口**算出来的 J 值——差别虽小
        # （KDJ 每日衰减 2/3，百来根预热后基本收敛），但 J 是硬阈值比较，
        # 没有理由留这个不确定性。
        for k in window[-FRESH_BARS:]:
            k.kdj_k = k.kdj_d = k.kdj_j = None

        dec = confirm_buy(
            ts_code,
            d,
            market={},
            klines=window,
            theme_lookback=theme_lookback,
            skip_market_gate=True,
            mdc_scope=FRESH_BARS,
            name=name,
        )
        if dec.action in ("BUY", "WATCH"):
            dec.detail = {}  # 明细占内存且回测用不上，几十万条累起来是 GB 级
            hits.append((d, dec, fwd))

    return hits, contrib, evaluated


def _replay_chunk(job: tuple) -> tuple[list, dict[str, float], dict[str, int]]:
    """一个子进程负责一批票。

    每个进程自己开连接（sqlite3 连接不能跨进程传），回放是纯读，互不干扰。
    """
    (codes, names, decision_dates, calendar, cal_pos, regimes, theme_lookback, load_start, load_end, gate) = job
    conn = _connect()
    hits_all: list = []
    contrib_all: dict[str, float] = {}
    contrib_peak: dict[str, float] = {}
    contrib_n: dict[str, int] = {}
    evaluated_n: dict[str, int] = {}
    try:
        for code in codes:
            hits, contrib, evaluated = replay_stock(
                conn,
                code,
                names[code],
                decision_dates,
                calendar,
                cal_pos,
                regimes,
                theme_lookback=theme_lookback,
                load_start=load_start,
                load_end=load_end,
                gate=gate,
            )
            hits_all.extend(hits)
            for d, (ret, peak) in contrib.items():
                contrib_all[d] = contrib_all.get(d, 0.0) + ret
                contrib_peak[d] = contrib_peak.get(d, 0.0) + peak
                contrib_n[d] = contrib_n.get(d, 0) + 1
            for d in evaluated:
                evaluated_n[d] = evaluated_n.get(d, 0) + 1
    finally:
        conn.close()
    return hits_all, contrib_all, {"n": contrib_n, "evaluated": evaluated_n, "peak": contrib_peak}


def run_backtest(
    start: str,
    end: str,
    *,
    workers: int = 1,
    gate: bool = True,
    top_n: int = DEFAULT_TOP_N,
    min_group_strength: float = DEFAULT_MIN_GROUP_STRENGTH,
    max_per_group: int | None = None,
    include_watch: bool = False,
    theme_lookback: int | None = None,
    limit_codes: int = 0,
    progress_every: int = 200,
    on_progress: Any = None,
) -> dict[str, Any]:
    """回放 [start, end] 区间内的每个交易日。

    Args:
        start / end: 决策日区间。end 之后还需要 HOLD_DAYS 个交易日的数据才能结算，
            不足的决策日会被自动剔除。
        workers: 并行进程数。回放是按票切分的纯读任务，天然可并行；
            1 = 单进程（调试用）。
        gate: True = 按框架现状，只在活跃市值多头区间选股；
            False = 空头区间也照跑，产出的信号带 ``regime`` 标记，
            用来量化这道总开关的贡献。计算量约为 gate=True 的 2.4 倍。
        limit_codes: 只跑前 N 只票（调试用），0 = 全池。

    Returns:
        {"days": [DayResult], "trades": [Trade], "universe": int, "params": {...}}
    """
    conn = _connect()
    started = time.perf_counter()

    # K 线要往前多取 KLINE_DAYS 根做预热，往后多取 HOLD_DAYS 根做结算
    full_cal = [str(r[0]) for r in conn.execute("SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date")]
    cal_pos = {d: i for i, d in enumerate(full_cal)}
    if start not in cal_pos:
        start = next((d for d in full_cal if d >= start), full_cal[-1])
    lo = max(0, cal_pos[start] - KLINE_DAYS - 10)
    load_start, load_end = full_cal[lo], full_cal[-1]

    decision_dates = [d for d in full_cal if start <= d <= end and cal_pos[d] + HOLD_DAYS < len(full_cal)]
    regimes = load_regimes(conn, full_cal)
    universe = load_universe(conn, start, end)
    codes = list(universe)
    if limit_codes:
        codes = codes[:limit_codes]

    logger.info("回放 %d 只票 × %d 个交易日", len(codes), len(decision_dates))

    by_date: dict[str, list[tuple[BuyDecision, tuple]]] = {d: [] for d in decision_dates}
    scanned: dict[str, int] = {d: 0 for d in decision_dates}
    base_sum: dict[str, float] = {d: 0.0 for d in decision_dates}
    base_peak_sum: dict[str, float] = {d: 0.0 for d in decision_dates}
    base_n: dict[str, int] = {d: 0 for d in decision_dates}

    chunk = max(1, len(codes) // max(1, workers * 4))
    batches = [codes[i : i + chunk] for i in range(0, len(codes), chunk)]
    jobs = [
        (
            b, {c: universe[c] for c in b}, decision_dates, full_cal, cal_pos,
            regimes, theme_lookback, load_start, load_end, gate,
        )
        for b in batches
    ]

    def _absorb(res: tuple) -> None:
        hits, contrib, counts = res
        for d, dec, fwd in hits:
            by_date[d].append((dec, fwd))
        for d, total in contrib.items():
            base_sum[d] += total
        for d, total in counts["peak"].items():
            base_peak_sum[d] += total
        for d, n in counts["n"].items():
            base_n[d] += n
        for d, n in counts["evaluated"].items():
            scanned[d] += n

    if workers <= 1:
        for i, job in enumerate(jobs, start=1):
            _absorb(_replay_chunk(job))
            if progress_every:
                elapsed = time.perf_counter() - started
                msg = f"回放进度 {i}/{len(jobs)} 批  已耗时 {elapsed / 60:.1f}min"
                logger.info(msg)
                if on_progress:
                    on_progress(msg)
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_replay_chunk, job) for job in jobs]
            for i, fut in enumerate(as_completed(futures), start=1):
                _absorb(fut.result())
                elapsed = time.perf_counter() - started
                eta = elapsed / i * (len(futures) - i)
                msg = f"回放进度 {i}/{len(futures)} 批  已耗时 {elapsed / 60:.1f}min  预计还需 {eta / 60:.1f}min"
                logger.info(msg)
                if on_progress:
                    on_progress(msg)

    # 第二阶段：逐日按主线/行业强弱筛选
    days: list[DayResult] = []
    trades: list[Trade] = []
    for d in decision_dates:
        entries = by_date[d]
        regime = regimes.get(d, "?")
        day = DayResult(
            trade_date=d,
            regime=regime,
            gate_passed=(regime == "多头区间"),
            scanned=scanned[d],
            baseline=(base_sum[d] / base_n[d]) if base_n[d] else None,
            baseline_peak=(base_peak_sum[d] / base_n[d]) if base_n[d] else None,
            baseline_n=base_n[d],
        )
        decisions = [dec for dec, _ in entries]
        fwd_of = {dec.ts_code: fwd for dec, fwd in entries}
        day.buy_count = sum(1 for x in decisions if x.action == "BUY")
        day.watch_count = sum(1 for x in decisions if x.action == "WATCH")

        selection = select_final_picks(
            decisions,
            top_n=top_n,
            min_group_strength=min_group_strength,
            max_per_group=max_per_group,
            include_watch=include_watch,
        )
        apply_picks(decisions, selection)
        group_of = {
            e["decision"].ts_code: (e["group"], e["group_strength"])
            for e in selection.get("picks", []) + selection.get("rejected", [])
        }

        for dec in decisions:
            grp, gstr = group_of.get(dec.ts_code, ("", 0.0))
            t = Trade(
                ts_code=dec.ts_code,
                name=dec.name,
                decision_date=d,
                action=dec.action,
                score=dec.score,
                pick_rank=dec.pick_rank,
                group=grp,
                group_strength=gstr,
                regime=regime,
            )
            fwd = fwd_of.get(dec.ts_code)
            if fwd:
                (
                    t.entry_date, t.entry_price, t.exit_date, t.exit_price,
                    t.lowest, t.unbuyable, t.highest, t.stopped_before_peak,
                ) = fwd
                t.resolved = True
            trades.append(t)
            if dec.action == "BUY":
                day.all_buys.append(t)
            if dec.pick_rank:
                day.picks.append(t)
        day.picks.sort(key=lambda x: x.pick_rank)
        days.append(day)

    conn.close()
    return {
        "days": days,
        "trades": trades,
        "universe": len(codes),
        "decision_dates": len(decision_dates),
        "elapsed": round(time.perf_counter() - started, 1),
        "params": {
            "start": start,
            "end": end,
            "gate": gate,
            "hold_days": HOLD_DAYS,
            "stop_loss": STOP_LOSS,
            "top_n": top_n,
            "min_group_strength": min_group_strength,
            "max_per_group": max_per_group,
            "include_watch": include_watch,
            "kline_days": KLINE_DAYS,
        },
    }


# ==================== 统计 ====================


def summarize(trades: Iterable[Trade], *, label: str = "") -> dict[str, Any]:
    """一组交易的收益统计。三种止损口径各算一版。"""
    items = [t for t in trades if t.resolved]
    out: dict[str, Any] = {
        "label": label,
        "n": len(items),
        "n_unbuyable": sum(1 for t in items if t.unbuyable),
    }
    if not items:
        return out

    for key in ("raw", "stop", "floor", "peak", "peak_nostop"):
        rets = [getattr(t, f"ret_{key}") for t in items]
        rets_sorted = sorted(rets)
        n = len(rets)
        out[key] = {
            "mean": sum(rets) / n,
            "median": rets_sorted[n // 2] if n % 2 else (rets_sorted[n // 2 - 1] + rets_sorted[n // 2]) / 2,
            "win_rate": sum(1 for r in rets if r > 0) / n,
            "best": rets_sorted[-1],
            "worst": rets_sorted[0],
            "p25": rets_sorted[int(n * 0.25)],
            "p75": rets_sorted[int(n * 0.75)],
        }
    out["stop_hit_rate"] = sum(1 for t in items if t.lowest <= t.entry_price * (1 + STOP_LOSS)) / len(items)
    # 卖在最高点这个假设被止损作废的比例（跌破 -20% 发生在最高点之前）
    out["peak_denied_rate"] = sum(1 for t in items if t.stopped_before_peak) / len(items)
    return out


def _pct(x: float | None) -> str:
    return "  -  " if x is None else f"{x * 100:+6.2f}%"


def format_summary(result: dict[str, Any]) -> str:
    """回测结果的人类可读报告。"""
    days: list[DayResult] = result["days"]
    trades: list[Trade] = result["trades"]
    p = result["params"]

    bull_days = [d for d in days if d.gate_passed]
    signal_days = [d for d in bull_days if d.buy_count]
    # 正文口径始终是"框架实际会产出的东西"= 多头区间的信号。gate=False 时
    # 空头区间也跑了，但那批只作对照，不能混进正文把结论稀释掉。
    picks = [t for t in trades if t.pick_rank and t.regime == "多头区间"]
    buys = [t for t in trades if t.action == "BUY" and t.regime == "多头区间"]

    lines = [
        "=" * 90,
        f"买点框架历史回放  {p['start']} ~ {p['end']}   持有 {p['hold_days']} 个交易日   耗时 {result['elapsed']}s",
        "=" * 90,
        f"股票池 {result['universe']} 只   决策日 {result['decision_dates']} 个"
        f"（活跃市值多头 {len(bull_days)} 个，其中 {len(signal_days)} 个真的出了信号）",
        f"参数: top_n={p['top_n']}  组强度门槛={p['min_group_strength']}  "
        f"每组上限={p['max_per_group'] or '不限'}  含WATCH={p['include_watch']}",
        "",
    ]

    # 基准
    base = [d.baseline for d in days if d.baseline is not None]
    base_bull = [d.baseline for d in bull_days if d.baseline is not None]
    peak_bull = [d.baseline_peak for d in bull_days if d.baseline_peak is not None]
    if base:
        lines.append(
            f"【基准】全市场等权 30 日收益：全部决策日均值 {_pct(sum(base) / len(base))}"
            + (f"   多头区间日均值 {_pct(sum(base_bull) / len(base_bull))}" if base_bull else "")
        )
        if peak_bull:
            # 给选股开上帝视角就必须给基准开同样的视角，否则不可比
            lines.append(
                f"      同一批票若也卖在各自 30 日最高点（同样受 -20% 止损约束）："
                f"多头区间日均值 {_pct(sum(peak_bull) / len(peak_bull))}"
            )
        lines.append("")

    def _block(title: str, group: list[Trade]) -> None:
        s = summarize(group)
        lines.append(f"【{title}】信号 {s['n']} 笔" + (f"（其中 {s['n_unbuyable']} 笔次日一字涨停买不进）" if s.get("n_unbuyable") else ""))
        if not s["n"]:
            lines.append("  无")
            return
        lines.append(f"  {'口径':<22} {'平均':>9} {'中位':>9} {'胜率':>8} {'25分位':>9} {'75分位':>9} {'最好':>9} {'最差':>9}")
        for key, name in (
            ("raw", "未截断"),
            ("stop", "路径止损 -20%"),
            ("floor", "只封底 -20%"),
            ("peak", "卖在最高点(上界)"),
            ("peak_nostop", "卖在最高点·无止损"),
        ):
            b = s[key]
            lines.append(
                f"  {name:<22} {_pct(b['mean']):>9} {_pct(b['median']):>9} {b['win_rate'] * 100:>7.1f}% "
                f"{_pct(b['p25']):>9} {_pct(b['p75']):>9} {_pct(b['best']):>9} {_pct(b['worst']):>9}"
            )
        lines.append(
            f"  期间触及 -20% 的比例: {s['stop_hit_rate'] * 100:.1f}%"
            f"   其中止损发生在最高点之前（卖高点作废）: {s['peak_denied_rate'] * 100:.1f}%"
        )

    _block("最终选股（第二阶段入选）", picks)
    lines.append("")
    _block("全部 BUY（第一阶段通过，未经主线筛选）", buys)
    lines.append("")

    # 总开关对照：只有 gate=False 跑过才有空头区间的信号可比
    if not p.get("gate", True):
        bull_buys = buys
        bear_buys = [t for t in trades if t.action == "BUY" and t.regime != "多头区间"]
        lines.append("【活跃市值总开关的对照】把空头区间也跑了一遍，看这道门槛挡掉的是什么")
        lines.append(f"  {'区间':<10} {'信号':>7} {'路径止损均值':>14} {'胜率':>8} {'同期基准':>10} {'超额':>10}")
        for label, grp in (("多头(放行)", bull_buys), ("空头(拦截)", bear_buys)):
            s = summarize(grp)
            if not s["n"]:
                continue
            dates_in = {t.decision_date for t in grp if t.resolved}
            bl = [d.baseline for d in days if d.trade_date in dates_in and d.baseline is not None]
            bl_mean = sum(bl) / len(bl) if bl else None
            excess = None if bl_mean is None else s["stop"]["mean"] - bl_mean
            lines.append(
                f"  {label:<10} {s['n']:>7} {_pct(s['stop']['mean']):>14} {s['stop']['win_rate'] * 100:>7.1f}% "
                f"{_pct(bl_mean):>10} {_pct(excess):>10}"
            )
        lines.append("")

    # 按入选名次
    if picks:
        lines.append("【按入选名次】")
        lines.append(f"  {'名次':<6} {'笔数':>6} {'路径止损均值':>14} {'胜率':>8}")
        for rank in range(1, p["top_n"] + 1):
            grp = [t for t in picks if t.pick_rank == rank]
            s = summarize(grp)
            if s["n"]:
                lines.append(
                    f"  #{rank:<5} {s['n']:>6} {_pct(s['stop']['mean']):>14} {s['stop']['win_rate'] * 100:>7.1f}%"
                )
        lines.append("")

    return "\n".join(lines)
