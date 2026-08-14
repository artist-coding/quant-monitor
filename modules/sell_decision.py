"""卖出决策引擎：逐步放飞阶梯（持仓口径）。

买入侧的对称面。买点由 ``buy_decision.confirm_buy`` 回答"该不该买"，
这里回答持仓后的唯一问题："今天该卖多少"。产出是三态：

    HOLD（不动） / REDUCE（减仓，给出比例） / EXIT（清仓）

逐步放飞阶梯（用户给定的规则，本模块不发明新战法）
--------------------------------------------------

    1. 涨幅超过 +20%      → 卖出现有仓位的 1/3   （落袋，余下的让利润飞）
    2. 出现 S1（初级逃顶） → 卖出现有仓位的 1/2
    3. 出现 S2（顶背离）   → 再卖出现有仓位的 1/2
    4. 出现 S3（清仓级）   → 全部卖出

S3 的口径已经扩充：除了原本的"最后逃生"（S1 后反抽无力），
绿肥红瘦、阶梯放量下跌、顶部大风车三个出货形态也归入 S3——
它们在 ``strategies.sell_signals`` 里直接以 ``StrategyType.S3`` 发信号，
所以本模块只看信号类型，不需要知道具体形态。

三个不显然的口径决定
--------------------

- **每一级对同一笔持仓只触发一次。** S1 连续三天都在报，不能天天砍半——
  第一次砍完，后面的 S1 说的还是同一件事。哪些级已经执行过由
  ``PositionState`` 记录，调用方（未来的模拟盘）负责在两次评估之间传递它。
- **同日多级按乘法复合，从"现有仓位"逐级往下算。** 用户的原话是
  "卖出现有仓位的 1/2"——S1 砍半之后 S2 再砍的是剩下的一半。
  同一天 S1、S2 首次同时出现：先 1/2 再 1/2，共卖出 3/4。
  S3 短路一切：直接 100%。
- **+20% 用收盘价对成本价判定。** 日线系统看不到盘中，"涨幅超过 20%"
  取"当日收盘 >= 成本 × 1.20"。这与 framework_backtest 用当日最低价判
  -20% 强止损是同一种保守取向：止损从严（碰到就算），止盈从缓（收住才算）。

信号新鲜度是调用方的责任
------------------------

``evaluate_sell`` 是纯函数：传进来的信号一律当作"现在有效"。
``detect_all_strategies`` 返回的是整段历史的信号，直接喂会把三个月前的
S1 当成今天的卖出理由——``evaluate_today`` 已按最近 N 根 K 线过滤
（与 watchlist 的新鲜度闸门同一口径），自己组装信号流时请照做。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

# ==================== 阶梯常量（用户给定，不是本模块发明的） ====================

# +20% 落袋级：收盘相对成本价的触发线与卖出比例
PROFIT_RELEASE_THRESHOLD = 0.20
PROFIT_RELEASE_FRACTION = 1.0 / 3.0

# S1 / S2 级：各卖出"当时现有仓位"的一半
S1_FRACTION = 0.5
S2_FRACTION = 0.5

# 信号新鲜度：只有落在最近 N 根 K 线内的信号才算"现在出现"。
# 与 watchlist._SIGNAL_FRESH_BARS 同一口径——K 线根数而非自然日，停牌长假不误伤。
SIGNAL_FRESH_BARS = 5


@dataclass
class PositionState:
    """一笔持仓在阶梯上的位置。

    模拟盘/实盘跟踪时在两次评估之间原样传递；每级触发过一次就不再触发。
    ``entry_price`` 是买入成本（多次买入取均价）；<= 0 表示成本未知，
    此时 +20% 级无法判定，只跑 S1/S2/S3 三级。
    """

    entry_price: float = 0.0
    took_profit: bool = False  # +20% 落袋 1/3 是否已执行
    s1_done: bool = False  # S1 砍半是否已执行
    s2_done: bool = False  # S2 砍半是否已执行


@dataclass
class SellDecision:
    """某个交易日对一笔持仓的卖出结论。"""

    ts_code: str
    trade_date: str = ""
    action: str = "HOLD"  # HOLD / REDUCE / EXIT
    sell_fraction: float = 0.0  # 应卖出的比例（相对当前持仓，0~1）
    triggered: list[dict[str, Any]] = field(default_factory=list)  # 本次触发的梯级
    state: PositionState = field(default_factory=PositionState)  # 评估后的阶梯状态
    profit_pct: float | None = None  # 收盘相对成本的涨幅；成本未知为 None
    notes: list[str] = field(default_factory=list)


def _strategy_value(sig: Any) -> str:
    """从 StrategySignal / dict / 裸字符串里取出战法标识（"S1"/"S2"/"S3"…）。"""
    strategy = sig
    if isinstance(sig, dict):
        strategy = sig.get("strategy", "")
    else:
        strategy = getattr(sig, "strategy", sig)
    value = getattr(strategy, "value", strategy)
    # StrategyType.S1.value 是 "S1"；name 也是 "S1"。value 足够。
    return str(value)


def evaluate_sell(
    position: PositionState,
    today_price: float,
    signals: Iterable[Any],
    *,
    ts_code: str = "",
    trade_date: str = "",
) -> SellDecision:
    """对一笔持仓跑一遍逐步放飞阶梯（纯函数，不读库）。

    Args:
        position: 阶梯状态。函数不修改传入对象，更新后的副本放在返回值的 state 里。
        today_price: 当日收盘价（+20% 级的判定价）
        signals: **当前有效**的信号集合——新鲜度过滤是调用方的责任（见模块 docstring）。
            元素可以是 StrategySignal、dict（含 strategy 键）或裸字符串。

    Returns:
        SellDecision。sell_fraction 是相对当前持仓的比例：
        0 → HOLD；1 → EXIT；中间值 → REDUCE。
    """
    decision = SellDecision(ts_code=ts_code, trade_date=trade_date, state=replace(position))

    kinds = {_strategy_value(s) for s in signals}
    has_s1 = "S1" in kinds
    has_s2 = "S2" in kinds
    has_s3 = "S3" in kinds

    if position.entry_price > 0 and today_price > 0:
        decision.profit_pct = round(today_price / position.entry_price - 1.0, 4)

    # ── S3：清仓级，短路一切 ──
    if has_s3:
        decision.action = "EXIT"
        decision.sell_fraction = 1.0
        decision.triggered.append({"rung": "S3", "fraction": 1.0, "reason": "S3清仓信号：全部卖出"})
        return decision

    remaining = 1.0  # 相对"当前持仓"的剩余比例，逐级乘法递减

    # ── +20% 落袋级 ──
    if position.entry_price > 0:
        if not position.took_profit and today_price >= position.entry_price * (1 + PROFIT_RELEASE_THRESHOLD):
            remaining *= 1 - PROFIT_RELEASE_FRACTION
            decision.state.took_profit = True
            decision.triggered.append(
                {
                    "rung": "PROFIT_20",
                    "fraction": PROFIT_RELEASE_FRACTION,
                    "reason": f"涨幅 {decision.profit_pct * 100:+.1f}% 超过 +20%：卖出现有仓位 1/3 落袋",
                }
            )
    else:
        decision.notes.append("成本价未知，+20% 落袋级未参与判定")

    # ── S1：砍半 ──
    if has_s1 and not position.s1_done:
        remaining *= 1 - S1_FRACTION
        decision.state.s1_done = True
        decision.triggered.append(
            {"rung": "S1", "fraction": S1_FRACTION, "reason": "S1 初级逃顶：卖出现有仓位 1/2"}
        )

    # ── S2：再砍半 ──
    if has_s2 and not position.s2_done:
        remaining *= 1 - S2_FRACTION
        decision.state.s2_done = True
        decision.triggered.append(
            {"rung": "S2", "fraction": S2_FRACTION, "reason": "S2 顶背离确认：再卖出现有仓位 1/2"}
        )

    decision.sell_fraction = round(1.0 - remaining, 4)
    if decision.sell_fraction <= 0:
        decision.action = "HOLD"
    elif decision.sell_fraction >= 1:
        decision.action = "EXIT"
    else:
        decision.action = "REDUCE"
    return decision


# ==================== 数据接入（读库版入口） ====================


def _recent_trade_dates(ts_code: str, bars: int = SIGNAL_FRESH_BARS) -> set[str]:
    """该票最近 bars 个交易日的日期集合；库里没 K 线时返回空集（不设闸门）。"""
    from .database import get_connection

    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT trade_date FROM daily_kline WHERE ts_code = ? ORDER BY trade_date DESC LIMIT ?",
                (ts_code, bars),
            ).fetchall()
        return {str(r["trade_date"]) for r in rows}
    except Exception:
        return set()


def _holding_entry_price(ts_code: str) -> float:
    """从交易记录反推持仓均价；无持仓或无记录返回 0。"""
    try:
        from .trade_manager import trade_manager

        holding = trade_manager.get_stock_holding(ts_code)
        if holding.get("current_qty", 0) > 0:
            return float(holding.get("avg_cost", 0) or 0)
    except Exception as exc:
        logger.debug("读取持仓均价失败 %s: %s", ts_code, exc)
    return 0.0


def evaluate_today(
    ts_code: str,
    *,
    entry_price: float | None = None,
    state: PositionState | None = None,
    days: int = 120,
) -> SellDecision:
    """对一只票按库内最新数据跑逐步放飞阶梯。

    Args:
        entry_price: 持仓成本。None 时尝试从 trade_records 反推均价；
            反推不到则成本未知，只跑 S1/S2/S3 三级。
        state: 阶梯状态。None 表示"未执行过任何梯级"——没有模拟盘落库之前，
            调用方（CLI/诊断）拿到的是"假设各级都还没卖过，今天该做什么"。

    Returns:
        SellDecision（trade_date 为库内最新 K 线日期；无 K 线时为空串、HOLD）
    """
    from .strategies import detect_all_strategies
    from .strategies.core import get_kline_data

    klines = get_kline_data(ts_code, days=SIGNAL_FRESH_BARS + 1)
    if not klines:
        return SellDecision(ts_code=ts_code, notes=["库内无 K 线数据"])
    today = klines[-1]

    if entry_price is None:
        entry_price = _holding_entry_price(ts_code)
    position = replace(state) if state is not None else PositionState()
    position.entry_price = float(entry_price or 0)

    signals = detect_all_strategies(ts_code, days=days)
    fresh_dates = _recent_trade_dates(ts_code)
    if fresh_dates:
        signals = [s for s in signals if s.trade_date in fresh_dates]

    return evaluate_sell(
        position,
        today["close"],
        signals,
        ts_code=ts_code,
        trade_date=str(today["trade_date"]),
    )


def format_sell_decision(d: SellDecision) -> str:
    """逐步放飞结论的人类可读输出。"""
    icon = {"EXIT": "[清仓]", "REDUCE": "[减仓]", "HOLD": "[持有]"}.get(d.action, d.action)
    lines = [
        "=" * 66,
        f"逐步放飞  {d.ts_code}  {d.trade_date}   {icon}"
        + (f"  应卖出现有仓位的 {d.sell_fraction * 100:.1f}%" if d.sell_fraction > 0 else ""),
        "=" * 66,
    ]
    if d.profit_pct is not None:
        lines.append(f"  成本价 {d.state.entry_price:.2f}，当前浮动 {d.profit_pct * 100:+.1f}%")
    if d.triggered:
        lines.append("  【触发梯级】")
        lines.extend(f"    * {t['reason']}" for t in d.triggered)
    else:
        lines.append("  今日未触发任何梯级：+20%落袋 / S1砍半 / S2再砍半 / S3清仓")
    for note in d.notes:
        lines.append(f"  ! {note}")
    return "\n".join(lines)
