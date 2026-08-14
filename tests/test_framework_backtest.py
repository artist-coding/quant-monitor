"""买点框架历史回放回测（modules/framework_backtest.py）测试。

重点测三件容易悄悄算错、错了又看不出来的事：

- **前瞻收益的取价**：买在决策日的**次日开盘**、卖在第 30 个**交易日历日**的收盘，
  停牌与一字涨停各有各的处理。
- **两种止损口径**：路径止损（期间最低触及即 -20%）与只封底（仅截断最终值）
  必须是两个不同的数，混同就等于默认止损从不失效。
- **区间沿用**：活跃市值缺某日时要沿用前一日，而不是当成"不选股"。

不打网络。涉及库的用例走临时库 fixture。
"""

from __future__ import annotations

import pytest

from modules import framework_backtest as fb
from modules.framework_backtest import HOLD_DAYS, STOP_LOSS, Trade
from modules.indicators import DailyData


def _bars(specs: list[tuple[str, float, float, float, float]], ts_code="600000.SH") -> list[DailyData]:
    """(日期, 开, 高, 低, 收) → DailyData 序列。"""
    out = []
    for d, o, h, low, c in specs:
        out.append(
            DailyData(
                ts_code=ts_code,
                trade_date=d,
                open=o,
                high=h,
                low=low,
                close=c,
                vol=1000.0,
                amount=1000.0 * c,
                pct_chg=0.0,
                prev_close=o,
            )
        )
    return out


def _calendar(n: int, start: int = 20240101) -> tuple[list[str], dict[str, int]]:
    """造 n 个连续的"交易日"（日期只要单调递增即可，不必是真日历）。"""
    cal = [str(start + i) for i in range(n)]
    return cal, {d: i for i, d in enumerate(cal)}


def _flat_series(n: int, price: float = 10.0, start: int = 20240101) -> list[DailyData]:
    return _bars([(str(start + i), price, price, price, price) for i in range(n)])


# ==================== 前瞻收益取价 ====================


class TestForward:
    def test_买次日开盘_卖第30个交易日收盘(self):
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        bars[1].open = 10.0  # 决策日 index0 → 次日买入价
        bars[HOLD_DAYS].close = 12.0  # 第 30 个交易日收盘卖出
        pos = {k.trade_date: i for i, k in enumerate(bars)}

        got = fb._forward(bars, pos, cal, cal_pos, cal[0])
        assert got is not None
        entry_date, entry_price, exit_date, exit_price, lowest, unbuyable, highest, denied = got
        assert entry_date == cal[1]
        assert entry_price == 10.0
        assert exit_date == cal[HOLD_DAYS]
        assert exit_price == 12.0
        assert unbuyable is False

    def test_持有期不足则不结算(self):
        """决策日之后没有 30 个交易日的，整笔作废而不是用最后一根凑数。"""
        cal, cal_pos = _calendar(HOLD_DAYS)  # 少一天
        bars = _flat_series(HOLD_DAYS)
        pos = {k.trade_date: i for i, k in enumerate(bars)}
        assert fb._forward(bars, pos, cal, cal_pos, cal[0]) is None

    def test_次日停牌买不进(self):
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        del bars[1]  # 次日无 K 线 = 停牌
        pos = {k.trade_date: i for i, k in enumerate(bars)}
        assert fb._forward(bars, pos, cal, cal_pos, cal[0]) is None

    def test_出场日停牌则取之前最近一根(self):
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        bars[HOLD_DAYS - 1].close = 11.0
        del bars[HOLD_DAYS]  # 第 30 个交易日停牌
        pos = {k.trade_date: i for i, k in enumerate(bars)}

        got = fb._forward(bars, pos, cal, cal_pos, cal[0])
        assert got is not None
        assert got[2] == cal[HOLD_DAYS - 1]
        assert got[3] == 11.0

    def test_持有期天数按交易日历数而非个股K线数(self):
        """个股停牌 5 天不该让它多持有 5 天——出场日始终是日历上的第 30 天。"""
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        for idx in range(5, 10):  # 中间停牌 5 天
            bars[idx].trade_date = "_del"
        bars = [b for b in bars if b.trade_date != "_del"]
        pos = {k.trade_date: i for i, k in enumerate(bars)}

        got = fb._forward(bars, pos, cal, cal_pos, cal[0])
        assert got is not None
        assert got[2] == cal[HOLD_DAYS]

    def test_期间最低价含买入当日(self):
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        bars[1].low = 7.5  # 买入当日就砸下来
        pos = {k.trade_date: i for i, k in enumerate(bars)}
        got = fb._forward(bars, pos, cal, cal_pos, cal[0])
        assert got[4] == 7.5

    def test_期间最低价不含出场日之后(self):
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        bars[HOLD_DAYS + 1].low = 1.0  # 卖掉之后才崩，与本笔无关
        pos = {k.trade_date: i for i, k in enumerate(bars)}
        got = fb._forward(bars, pos, cal, cal_pos, cal[0])
        assert got[4] == 10.0

    def test_一字涨停标记买不进(self):
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        b = bars[1]
        b.open = b.high = b.low = b.close = 11.0
        b.pct_chg = 10.0
        pos = {k.trade_date: i for i, k in enumerate(bars)}
        got = fb._forward(bars, pos, cal, cal_pos, cal[0])
        assert got[5] is True

    def test_平开平走但没涨停不算买不进(self):
        """开=高=低 只是没波动，不涨停就买得进——不能只看四价合一。"""
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        bars[1].pct_chg = 0.0
        pos = {k.trade_date: i for i, k in enumerate(bars)}
        got = fb._forward(bars, pos, cal, cal_pos, cal[0])
        assert got[5] is False


# ==================== 止损口径 ====================


def _trade(entry: float, exit_: float, lowest: float, highest: float = 0.0, denied: bool = False) -> Trade:
    return Trade(
        ts_code="600000.SH", name="测试", decision_date="20240101", action="BUY",
        score=80.0, pick_rank=1, group="半导体", group_strength=70.0, regime="多头区间",
        entry_date="20240102", entry_price=entry, exit_date="20240210",
        exit_price=exit_, lowest=lowest, highest=highest or max(entry, exit_),
        stopped_before_peak=denied, resolved=True,
    )


class TestStopLoss:
    def test_未触及止损时三种口径一致(self):
        t = _trade(10.0, 11.0, 9.5)
        assert t.ret_raw == pytest.approx(0.10)
        assert t.ret_stop == pytest.approx(0.10)
        assert t.ret_floor == pytest.approx(0.10)

    def test_中途触及止损但涨回来_路径口径记满额亏损(self):
        """这是两种口径唯一分岔的地方，也是"强止损"的真实行为：
        跌到 -25% 时已经割了，后面涨回 +10% 与这笔无关。"""
        t = _trade(10.0, 11.0, 7.5)
        assert t.ret_raw == pytest.approx(0.10)
        assert t.ret_stop == pytest.approx(STOP_LOSS)
        assert t.ret_floor == pytest.approx(0.10)

    def test_最终跌超20_两种口径都截断到20(self):
        t = _trade(10.0, 7.0, 6.9)
        assert t.ret_raw == pytest.approx(-0.30)
        assert t.ret_stop == pytest.approx(STOP_LOSS)
        assert t.ret_floor == pytest.approx(STOP_LOSS)

    def test_恰好跌到20整触发止损(self):
        t = _trade(10.0, 9.0, 8.0)
        assert t.ret_stop == pytest.approx(STOP_LOSS)

    def test_未结算的笔收益为0而不是报错(self):
        t = Trade(
            ts_code="600000.SH", name="", decision_date="20240101", action="BUY",
            score=0.0, pick_rank=0, group="", group_strength=0.0, regime="多头区间",
        )
        assert t.ret_raw == 0.0 and t.ret_stop == 0.0


class TestPeakExit:
    """"卖在波段最高点"的上界口径。

    关键是**顺序**：先跌破 -20% 再创新高的，人已经被止损打出去，
    看不到那个高点；先创新高再跌破的，已经卖在高点上了。
    只看"期间有没有触及过 -20%"会把前者也算成吃到高点，把上界抬得虚高。
    """

    def test_没触及止损时按最高价结算(self):
        t = _trade(10.0, 11.0, 9.5, highest=13.0)
        assert t.ret_peak == pytest.approx(0.30)
        assert t.ret_peak_nostop == pytest.approx(0.30)

    def test_先创新高后跌破止损_仍吃到高点(self):
        t = _trade(10.0, 7.0, 7.5, highest=13.0, denied=False)
        assert t.ret_peak == pytest.approx(0.30)

    def test_先跌破止损后创新高_吃不到高点(self):
        t = _trade(10.0, 12.0, 7.5, highest=13.0, denied=True)
        assert t.ret_peak == pytest.approx(STOP_LOSS)
        # 无止损口径不受顺序影响，仍是纯上界
        assert t.ret_peak_nostop == pytest.approx(0.30)

    def test_上界不低于持有到期收益(self):
        """最高价不低于收盘价，所以同一笔的上界必然 >= 未截断收益。"""
        for exit_, high in ((12.0, 12.0), (8.0, 10.5), (10.0, 10.0)):
            t = _trade(10.0, exit_, 9.5, highest=high)
            assert t.ret_peak >= t.ret_raw - 1e-12

    def test_forward_标注止损先于最高点(self):
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        bars[3].low = 7.5           # 第 3 天先破止损
        bars[10].high = 13.0        # 第 10 天才创新高
        pos = {k.trade_date: i for i, k in enumerate(bars)}
        got = fb._forward(bars, pos, cal, cal_pos, cal[0])
        assert got[6] == 13.0       # 最高价
        assert got[7] is True       # 止损先于最高点

    def test_forward_最高点在前则不算作废(self):
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        bars[3].high = 13.0         # 先创新高
        bars[10].low = 7.5          # 之后才破止损
        pos = {k.trade_date: i for i, k in enumerate(bars)}
        got = fb._forward(bars, pos, cal, cal_pos, cal[0])
        assert got[6] == 13.0
        assert got[7] is False

    def test_forward_同日既破止损又创新高按破止损算(self):
        """日线看不到日内先后，宁可低估上界。"""
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        bars[5].high = 13.0
        bars[5].low = 7.5
        pos = {k.trade_date: i for i, k in enumerate(bars)}
        got = fb._forward(bars, pos, cal, cal_pos, cal[0])
        assert got[7] is True

    def test_forward_最高价并列取最早那天(self):
        cal, cal_pos = _calendar(40)
        bars = _flat_series(40)
        bars[3].high = 13.0
        bars[20].high = 13.0
        bars[10].low = 7.5          # 落在两个并列高点之间
        pos = {k.trade_date: i for i, k in enumerate(bars)}
        got = fb._forward(bars, pos, cal, cal_pos, cal[0])
        # 取最早的高点 → 止损发生在它之后 → 不作废
        assert got[7] is False


# ==================== 区间沿用 ====================


class TestRegimes:
    def test_缺失日沿用前一日(self, temp_db):
        from modules.database import get_connection

        with get_connection() as conn:
            conn.executemany(
                "INSERT INTO amv_daily (trade_date, close, pct_chg, regime) VALUES (?,?,?,?)",
                [("20240101", 100.0, 5.0, "多头区间"), ("20240104", 90.0, -5.0, "空头区间")],
            )
        conn2 = fb._connect()
        try:
            got = fb.load_regimes(conn2, ["20240101", "20240102", "20240103", "20240104", "20240105"])
        finally:
            conn2.close()
        assert got["20240102"] == "多头区间"  # 沿用
        assert got["20240103"] == "多头区间"
        assert got["20240104"] == "空头区间"
        assert got["20240105"] == "空头区间"  # 继续沿用新状态

    def test_首条之前的日期不臆造区间(self, temp_db):
        from modules.database import get_connection

        with get_connection() as conn:
            conn.execute(
                "INSERT INTO amv_daily (trade_date, close, pct_chg, regime) VALUES (?,?,?,?)",
                ("20240104", 100.0, 5.0, "多头区间"),
            )
        conn2 = fb._connect()
        try:
            got = fb.load_regimes(conn2, ["20240101", "20240104"])
        finally:
            conn2.close()
        assert "20240101" not in got
        assert got["20240104"] == "多头区间"


# ==================== 统计 ====================


class TestSummarize:
    def test_只统计结算得了的笔(self):
        good = _trade(10.0, 11.0, 9.5)
        pending = Trade(
            ts_code="X", name="", decision_date="20240101", action="BUY",
            score=0.0, pick_rank=0, group="", group_strength=0.0, regime="多头区间",
        )
        s = fb.summarize([good, pending])
        assert s["n"] == 1

    def test_胜率与均值(self):
        trades = [_trade(10.0, 12.0, 9.9), _trade(10.0, 9.0, 8.9), _trade(10.0, 11.0, 9.9)]
        s = fb.summarize(trades)
        assert s["n"] == 3
        assert s["raw"]["win_rate"] == pytest.approx(2 / 3)
        assert s["raw"]["mean"] == pytest.approx((0.2 - 0.1 + 0.1) / 3)

    def test_止损命中率(self):
        trades = [_trade(10.0, 11.0, 7.0), _trade(10.0, 11.0, 9.9)]
        s = fb.summarize(trades)
        assert s["stop_hit_rate"] == pytest.approx(0.5)
        assert s["stop"]["mean"] == pytest.approx((STOP_LOSS + 0.10) / 2)

    def test_空集合不报错(self):
        s = fb.summarize([])
        assert s["n"] == 0 and "raw" not in s


# ==================== MDC 口径等价（回测提速的前提） ====================


def test_mdc_只算尾部与全算的判定完全一致():
    """``mdc_scope=FRESH_BARS`` 是回测能跑完的前提，它必须不改变任何判定。

    这里用构造数据锁住"只算尾部 N 根"与"全算"在 detect_b1 读到的那几根上
    取值相同——真实数据上的全量比对在开发时做过（297 只票逐位一致），
    但那依赖生产库，不适合放进单测。
    """
    from modules.buy_decision import FRESH_BARS, attach_mdc_fields

    def _series() -> list[DailyData]:
        out = []
        price = 10.0
        for i in range(60):
            price *= 1.01 if i % 3 else 0.985
            out.append(
                DailyData(
                    ts_code="600000.SH", trade_date=str(20240101 + i),
                    open=price * 0.99, high=price * 1.02, low=price * 0.97, close=price,
                    vol=1000.0 + i, amount=price * 1000.0, pct_chg=0.0, prev_close=price,
                )
            )
        return out

    full, tail = _series(), _series()
    attach_mdc_fields(full)
    attach_mdc_fields(tail, FRESH_BARS)

    for i in range(len(full) - FRESH_BARS, len(full)):
        for attr in ("rsi6", "adx", "dmi_plus", "dmi_minus"):
            assert getattr(full[i], attr) == getattr(tail[i], attr), f"{attr} @ {i}"
    # 尾部之前的根本不该被算（省下来的正是这部分）
    assert tail[0].rsi6 is None
