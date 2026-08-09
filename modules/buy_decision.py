"""阶段1 · 买点确认引擎（日线口径）。

把系统里**已有的**日线战法按"否决 → 触发 → 确认 → 环境 → 主线"五层串起来，
对每只票给出一个三态里的买入侧结论：``BUY`` / ``WATCH`` / ``NONE``。

设计原则
--------

1. **只用系统里现成的战法，不新编战法。** 本模块不发明任何形态判据，
   每一层调用的都是 ``modules.strategies`` / ``modules.indicators`` 里
   已经存在并被测试覆盖的检测函数。本模块新增的只有"怎么把它们组合起来"
   ——即各层的加减分权重和最终阈值，这些集中在下面的常量区，逐条注明理由。

2. **纯日线，不碰分时。** 量比战法 ``detect_volume_ratio_strategy`` 的 6 场景
   矩阵阈值（>40/>20/10~20）是分钟级量比口径，套在"当日量÷5日均量"上实测
   1540 个交易日里买入侧 0 触发（最大量比只有 5.49），故本模块**不使用它**。
   成交量维度改用阈值本就是日线口径的 ``detect_volume_attack``（量比>3 且涨>2%）、
   ``detect_double_gun``（双枪：主力建仓确认）以及 B1 自带的缩量判定。

3. **一票否决优先于一切加分。** MACD 的 ``macd_veto``（DIF<0 且无底背离）在
   ``complex_patterns.detect_macd_signals`` 里就叫"一票否决权"，这里如实执行：
   命中即 NONE，不再看后面任何加分。否则会出现"战法信号很漂亮但趋势根本没转"
   的伪买点。

4. **卖出侧不在本模块。** 三态里的"今日尾盘卖出"需要盘中快照才能在收盘前给出，
   属于后续的盘中任务，此处只产出买入侧结论。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from .database import get_connection

logger = logging.getLogger(__name__)


# ==================== 分层常量 ====================
#
# 下面所有数字是本模块唯一"新增"的东西——战法本身一律复用现成实现。
# 每条都写清楚为什么是这个量级，方便日后用 daily_scores/buy_decisions 的
# 历史数据做归因时逐项调参。

# 买点战法信号的有效期（交易日）。买点是时效性极强的东西：B1 说的是"今天缩量
# 回调到位"，5 天前的 B1 早就该被证实或证伪了。取 3 根 K 线——足够容纳
# "B1 出现后等一天确认"的常规节奏，又不至于把上周的旧信号当成今天的买点。
FRESH_BARS = 3

# 最终判定阈值
SCORE_BUY = 65.0  # ≥ 判 BUY：明日开盘可买
SCORE_WATCH = 45.0  # ≥ 判 WATCH：进观察名单，等确认
# < SCORE_WATCH → NONE

# ── MACD 层 ──
# MACD 在系统里的定位是"趋势的确认者与陷阱识别器"，不是买点触发器，
# 所以单项权重都小于触发层的战法置信度，只做修正。
_MACD_BOTTOM_DIVERGENCE = 10.0  # 底背离：趋势终结信号，B1 遇上它是最好的组合
_MACD_GOLD_CROSS = 8.0  # 金叉：常规转强
_MACD_DEAD_TRAP = 8.0  # 死叉多（空中加油）：原上涨趋势延续
_MACD_DIF_POSITIVE = 5.0  # DIF 在 0 轴上方：多头区间
_MACD_GOLD_TRAP = -12.0  # 金叉空：语料里明写"最恶毒的诱多"，扣得比金叉加的多
_MACD_DEAD_CROSS = -8.0
_MACD_TOP_DIVERGENCE = -15.0  # 顶背离：见顶信号，此时买入是接盘

# ── 成交量层（纯日线口径）──
_VOL_ATTACK = 10.0  # 量比>3 且涨>2%，资金进场的直接痕迹
_VOL_DOUBLE_GUN = 8.0  # 双枪：两根放量阳夹一堆缩量阴，主力建仓确认
_VOL_B1_SUOLIANG = 5.0  # B1 当日缩量：B1 的最佳形态就是"缩量回调"
_VOL_FANGLIANG_YIN = -8.0  # 放量阴线：抛压

# ── 大盘环境层 ──
# A 股个股与大盘的相关性极高，逆势做多的胜率明显更差，所以 SHORT 的扣分
# 大于 LONG 的加分（不对称是有意的：宁可错过，不可在跌势里加仓）。
_MARKET_LONG = 8.0
_MARKET_SHORT = -12.0
# 强度对 50 的偏离再做一档微调，最多 ±5 分
_MARKET_STRENGTH_SCALE = 10.0
_MARKET_STRENGTH_CAP = 5.0

# ── 共振层 ──
# 多个**互不相同**的买点战法在有效期内同时命中，是相互独立的证据在指向同一个
# 结论，比单一战法可信。只数不同战法，不数同一战法在不同日期的重复命中
# ——B3 连着三天报同一个中继形态，那是一个证据不是三个。
_RESONANCE_PER_EXTRA = 4.0
_RESONANCE_CAP = 12.0

# ── 主线层 ──
# 主线强度是 0-100 的百分位（见 themes.py），50 为中位行业水平。
# 权重换算成 (strength-50)/50 × WEIGHT，即最强主线 +12、最弱主线 -12。
_THEME_WEIGHT = 12.0
# 行业分类只是没有主线归属时的兜底参照，不是真正的"炒作主线"，权重减半。
_INDUSTRY_WEIGHT = 6.0

# 买入侧战法：这些检测器的 action 都是 BUY（已核对 base_strategies /
# compound_strategies 的实现）。四分之三阴量是 SELL（假突破识别），不在此列。
_BUY_DETECTORS = (
    ("B1", "detect_b1", True),
    ("B2", "detect_b2", True),
    ("B3", "detect_b3", False),
    ("SB1", "detect_sb1", False),
    ("长安战法", "detect_changan", True),
    ("娜娜图形", "detect_nana", True),
    ("异动+地量地价", "detect_yidong_dilian", False),
    ("平行重炮", "detect_pinghang", False),
    ("坑里起好货", "detect_kengqi", False),
    ("对称VA", "detect_duichen_va", False),
)


@dataclass
class BuyDecision:
    """单只票在某个交易日的买点确认结论。"""

    ts_code: str
    trade_date: str = ""
    name: str = ""
    action: str = "NONE"  # BUY / WATCH / NONE
    score: float = 0.0
    confidence: float = 0.0
    base_strategy: str = ""
    triggers: list[dict[str, Any]] = field(default_factory=list)
    confirms: list[str] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)
    market: dict[str, Any] = field(default_factory=dict)
    theme: dict[str, Any] | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> tuple:
        return (
            self.ts_code,
            self.trade_date,
            self.name,
            self.action,
            round(self.score, 2),
            round(self.confidence, 4),
            self.base_strategy,
            json.dumps(self.triggers, ensure_ascii=False),
            json.dumps(self.confirms, ensure_ascii=False),
            json.dumps(self.vetoes, ensure_ascii=False),
            str(self.market.get("market_dir", "")),
            float(self.market.get("market_strength", 0) or 0),
            (self.theme or {}).get("theme", ""),
            float((self.theme or {}).get("strength", 0) or 0),
            int((self.theme or {}).get("rank", 0) or 0),
            json.dumps(self.detail, ensure_ascii=False, default=str),
        )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _num(mapping: dict[str, Any] | None, key: str, default: float) -> float:
    """从 dict 里取一个数值，缺失或非数值时回落到 default。

    **不能写成 `float(d.get(k, default) or default)`**：强度 0 是完全合法的取值
    （空头到底的大盘、垫底的主线），而 `0 or 50` 会把它悄悄变成 50，
    正好把最该扣分的情形变成不扣分。
    """
    if not mapping:
        return default
    value = mapping.get(key, default)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_index(klines: Sequence[Any], trade_date: str | None) -> int:
    """定位判定所用的 K 线下标：**不晚于** trade_date 的最近一根。

    刻意不要求精确匹配。目标日的数据还没同步上来时（票池同步失败、停牌、
    或者干脆就是拿未来日期在跑），精确匹配会直接判"找不到"，而调用方多半
    会把这条空决策按目标日落库——这正是阶段0 在 daily_scores 上踩过的坑：
    日期存疑的脏行永远不会被正确重跑覆盖。

    改成向前回退后，语义变成"截至 trade_date，用能拿到的最新数据判定"，
    决策上带的是那根 K 线的**真实日期**，与目标日不符时由调用方报出漂移。

    trade_date 为 None 取最后一根；早于全部数据则返回 -1。
    """
    if not klines:
        return -1
    if not trade_date:
        return len(klines) - 1
    for i in range(len(klines) - 1, -1, -1):
        if klines[i].trade_date <= trade_date:
            return i
    return -1


# ==================== 各层 ====================


def _collect_vetoes(klines: list, index: int) -> tuple[list[str], dict[str, Any]]:
    """一票否决层：命中任一项，后面所有加分都不看了。"""
    from .indicators import calculate_macd, detect_kirin_stage, detect_three_waves
    from .indicators.price_patterns.complex_patterns import detect_macd_signals

    window = klines[: index + 1]
    vetoes: list[str] = []
    detail: dict[str, Any] = {}

    dif, dea, macd = calculate_macd(window)
    macd_sig: dict[str, Any] = {}
    if dif and dea and macd:
        macd_sig = detect_macd_signals(window, dif, dea, macd)
        detail["macd"] = {
            "dif": round(dif[-1], 4),
            "dea": round(dea[-1], 4),
            **{k: v for k, v in macd_sig.items()},
        }
        if macd_sig.get("macd_veto"):
            vetoes.append(f"MACD一票否决（DIF={dif[-1]:.4f}<0 且无底背离）")
    else:
        detail["macd"] = {"reason": f"K线不足以计算 MACD（{len(window)} 根）"}

    try:
        kirin = detect_kirin_stage(window)
        detail["kirin"] = kirin
        if kirin.get("stage") == "派发":
            vetoes.append(f"麒麟会·派发阶段（{kirin.get('sub_type', '')}，主力在出货）")
    except Exception as exc:  # 单个检测器异常不能拖垮整条决策
        logger.warning("麒麟阶段检测异常: %s", exc)
        detail["kirin"] = {"error": str(exc)}

    try:
        wave = detect_three_waves(window)
        detail["wave"] = wave
        if wave.get("wave") == "冲刺波" and float(wave.get("confidence", 0) or 0) >= 0.5:
            vetoes.append(f"三波理论·冲刺波（涨幅 {wave.get('stats', {}).get('gain_pct', '?')}%，不看）")
    except Exception as exc:
        logger.warning("三波理论检测异常: %s", exc)
        detail["wave"] = {"error": str(exc)}

    detail["_macd_sig"] = macd_sig
    return vetoes, detail


def _collect_triggers(klines: list, index: int) -> list[dict[str, Any]]:
    """触发层：最近 FRESH_BARS 根内命中的买入战法，按置信度降序。"""
    from .indicators import detect_kirin_stage
    from .strategies import base_strategies, compound_strategies

    modules_by_name = {**vars(base_strategies), **vars(compound_strategies)}
    start = max(0, index - FRESH_BARS + 1)

    triggers: list[dict[str, Any]] = []
    for i in range(start, index + 1):
        window = klines[: i + 1]
        # 麒麟阶段是 B1/B2/长安/娜娜 的背景参数，每个 i 只算一次
        kirin_context = None
        try:
            kirin_context = detect_kirin_stage(window)
        except Exception as exc:
            logger.debug("麒麟阶段检测失败 index=%d: %s", i, exc)

        for label, fn_name, wants_kirin in _BUY_DETECTORS:
            fn = modules_by_name.get(fn_name)
            if fn is None:
                continue
            try:
                sig = fn(klines, i, kirin_context=kirin_context) if wants_kirin else fn(klines, i)
            except Exception as exc:
                logger.debug("战法 %s 在 index=%d 检测异常: %s", label, i, exc)
                continue
            if sig is None:
                continue
            triggers.append(
                {
                    "strategy": label,
                    "trade_date": sig.trade_date,
                    "confidence": round(float(sig.confidence or 0), 4),
                    "description": sig.description or "",
                    "bars_ago": index - i,
                    "stop_loss": sig.stop_loss,
                }
            )

    triggers.sort(key=lambda t: (t["confidence"], -t["bars_ago"]), reverse=True)
    return triggers


def _score_macd(macd_sig: dict[str, Any]) -> tuple[float, list[str]]:
    """MACD 确认层。"""
    if not macd_sig:
        return 0.0, []
    delta = 0.0
    notes: list[str] = []
    rules = (
        ("is_bottom_divergence", _MACD_BOTTOM_DIVERGENCE, "MACD底背离（趋势终结，反转建仓）"),
        ("macd_gold_cross", _MACD_GOLD_CROSS, "MACD金叉"),
        ("is_dead_fake", _MACD_DEAD_TRAP, "MACD死叉多（空中加油，原上涨趋势延续）"),
        ("is_dif_positive", _MACD_DIF_POSITIVE, "DIF在0轴上方（多头区间）"),
        ("is_gold_fake", _MACD_GOLD_TRAP, "MACD金叉空（诱多陷阱）"),
        ("macd_dead_cross", _MACD_DEAD_CROSS, "MACD死叉"),
        ("is_top_divergence", _MACD_TOP_DIVERGENCE, "MACD顶背离（见顶，此时买入是接盘）"),
    )
    for key, weight, label in rules:
        if macd_sig.get(key):
            delta += weight
            notes.append(f"{label} {weight:+.0f}")
    return delta, notes


def _score_volume(klines: list, index: int, triggers: list[dict[str, Any]]) -> tuple[float, list[str], dict]:
    """成交量确认层（纯日线口径，不使用量比战法 6 场景矩阵）。"""
    from .indicators import detect_double_gun, detect_volume_attack

    window = klines[: index + 1]
    today = klines[index]
    delta = 0.0
    notes: list[str] = []
    detail: dict[str, Any] = {}

    try:
        attack = detect_volume_attack(window)
        detail["volume_attack"] = attack
        if attack.get("is_attack"):
            delta += _VOL_ATTACK
            notes.append(f"{attack['desc']} {_VOL_ATTACK:+.0f}")
    except Exception as exc:
        logger.debug("量比攻击检测异常: %s", exc)

    try:
        gun = detect_double_gun(window)
        detail["double_gun"] = gun
        if gun.get("is_double_gun"):
            delta += _VOL_DOUBLE_GUN
            notes.append(f"双枪战法（主力建仓确认，间隔{gun.get('double_gun_gap_days', 0)}天） {_VOL_DOUBLE_GUN:+.0f}")
    except Exception as exc:
        logger.debug("双枪检测异常: %s", exc)

    has_b1 = any(t["strategy"] in ("B1", "SB1") for t in triggers)
    if has_b1 and getattr(today, "is_suoliang", False):
        delta += _VOL_B1_SUOLIANG
        notes.append(f"B1当日缩量（回调到位的最佳形态） {_VOL_B1_SUOLIANG:+.0f}")

    if getattr(today, "is_fangliang_yinxian", False):
        delta += _VOL_FANGLIANG_YIN
        notes.append(f"当日放量阴线（抛压） {_VOL_FANGLIANG_YIN:+.0f}")

    return delta, notes, detail


def _score_market(market: dict[str, Any]) -> tuple[float, list[str]]:
    """大盘环境层。"""
    direction = str(market.get("market_dir", "NEUTRAL"))
    strength = _num(market, "market_strength", 50.0)

    delta = 0.0
    notes: list[str] = []
    if direction == "LONG":
        delta += _MARKET_LONG
        notes.append(f"大盘偏多 {_MARKET_LONG:+.0f}")
    elif direction == "SHORT":
        delta += _MARKET_SHORT
        notes.append(f"大盘偏空（逆势做多胜率差） {_MARKET_SHORT:+.0f}")

    tilt = _clamp((strength - 50.0) / _MARKET_STRENGTH_SCALE, -_MARKET_STRENGTH_CAP, _MARKET_STRENGTH_CAP)
    if abs(tilt) >= 0.5:
        delta += tilt
        notes.append(f"大盘强度{strength:.1f} {tilt:+.1f}")
    return delta, notes


def _score_theme(theme: dict[str, Any] | None) -> tuple[float, list[str]]:
    """主线层。"""
    if not theme:
        return 0.0, []
    weight = _THEME_WEIGHT if theme.get("kind") == "theme" else _INDUSTRY_WEIGHT
    strength = _num(theme, "strength", 50.0)
    delta = (strength - 50.0) / 50.0 * weight
    kind_label = "主线" if theme.get("kind") == "theme" else "行业(兜底)"
    note = (
        f"{kind_label}「{theme.get('theme', '')}」强度{strength:.1f} "
        f"排名{theme.get('rank', 0)}/{theme.get('total', 0)} {delta:+.1f}"
    )
    return delta, [note]


# ==================== 主入口 ====================


def confirm_buy(
    ts_code: str,
    trade_date: str | None = None,
    *,
    market: dict[str, Any] | None = None,
    klines: list | None = None,
    days: int = 150,
    theme_lookback: int | None = None,
) -> BuyDecision:
    """对单只票做买点确认。

    Args:
        ts_code: 股票代码
        trade_date: 目标交易日 YYYYMMDD；None 表示用库里最新一根 K 线
        market: 预先算好的大盘环境（批量调用时传入，避免每只票重算一次）
        klines: 预先取好的 DailyData 序列（须含 MDC 字段，见 strategies.core.get_kline_data）
        days: 未传 klines 时的回溯 K 线根数
        theme_lookback: 主线强度的统计窗口；None 用 themes.DEFAULT_LOOKBACK
    """
    from .strategies.core import _dict_to_daily, get_kline_data

    decision = BuyDecision(ts_code=ts_code, trade_date=trade_date or "")

    if klines is None:
        raw = get_kline_data(ts_code, days)
        klines = _dict_to_daily(raw) if raw else []

    if not klines:
        decision.detail["reason"] = "无 K 线数据"
        return decision

    index = _resolve_index(klines, trade_date)
    if index < 0:
        # 目标日早于全部数据。trade_date 清空，避免这条空决策被当成目标日的结论落库。
        decision.trade_date = ""
        decision.detail["reason"] = f"{trade_date} 早于库内最早的 K 线（{klines[0].trade_date}），无数据可判"
        return decision
    # 战法检测普遍需要 20 根以上历史（detect_b3 要 20、detect_duichen_va 更多）
    if index < 20:
        decision.trade_date = klines[index].trade_date
        decision.detail["reason"] = f"{klines[index].trade_date} 之前只有 {index} 根 K 线，不足以检测战法"
        return decision

    decision.trade_date = klines[index].trade_date
    decision.name = _lookup_name(ts_code)

    # ── 第一层：一票否决 ──
    vetoes, veto_detail = _collect_vetoes(klines, index)
    macd_sig = veto_detail.pop("_macd_sig", {})
    decision.detail.update(veto_detail)
    decision.vetoes = vetoes

    # ── 第二层：触发 ──
    triggers = _collect_triggers(klines, index)
    decision.triggers = triggers

    if vetoes:
        decision.action = "NONE"
        decision.detail["stopped_at"] = "veto"
        return decision
    if not triggers:
        decision.action = "NONE"
        decision.detail["stopped_at"] = "no_trigger"
        decision.detail["reason"] = f"最近 {FRESH_BARS} 个交易日内无任何买入战法信号"
        return decision

    # 显式取置信度最高的那个，不依赖 _collect_triggers 的返回顺序——
    # 排序是那边的实现细节，主流程不该建立在一个隐式契约上。
    best = max(triggers, key=lambda t: (float(t.get("confidence", 0) or 0), -int(t.get("bars_ago", 0) or 0)))
    decision.base_strategy = best["strategy"]
    score = float(best["confidence"]) * 100.0
    breakdown: dict[str, float] = {"base": round(score, 2)}
    confirms: list[str] = [f"{best['strategy']}触发（{best['trade_date']}，置信度{best['confidence']:.2f}）"]

    # ── 共振：多个不同战法同时指向买入 ──
    distinct = {t["strategy"] for t in triggers}
    resonance = min(_RESONANCE_PER_EXTRA * (len(distinct) - 1), _RESONANCE_CAP)
    if resonance > 0:
        score += resonance
        others = sorted(distinct - {best["strategy"]})
        confirms.append(f"{len(distinct)}个战法共振（{'、'.join(others)}） {resonance:+.0f}")
    breakdown["resonance"] = round(resonance, 2)

    # ── 第三层：MACD 确认 ──
    macd_delta, macd_notes = _score_macd(macd_sig)
    score += macd_delta
    breakdown["macd"] = round(macd_delta, 2)
    confirms.extend(macd_notes)

    # ── 第四层：成交量确认 ──
    vol_delta, vol_notes, vol_detail = _score_volume(klines, index, triggers)
    score += vol_delta
    breakdown["volume"] = round(vol_delta, 2)
    confirms.extend(vol_notes)
    decision.detail["volume"] = vol_detail

    # ── 第五层：大盘环境 ──
    if market is None:
        from .daily_pipeline import compute_market_context

        try:
            market = compute_market_context(decision.trade_date)
        except Exception as exc:
            logger.warning("大盘环境计算失败 %s: %s", decision.trade_date, exc)
            market = {"market_dir": "NEUTRAL", "market_pct_chg": 0.0, "market_strength": 50.0}
    decision.market = market
    mkt_delta, mkt_notes = _score_market(market)
    score += mkt_delta
    breakdown["market"] = round(mkt_delta, 2)
    confirms.extend(mkt_notes)

    # ── 第六层：主线 ──
    try:
        from .themes import DEFAULT_LOOKBACK, get_stock_theme_strength

        decision.theme = get_stock_theme_strength(
            ts_code, decision.trade_date, theme_lookback or DEFAULT_LOOKBACK
        )
    except Exception as exc:
        logger.warning("主线强度读取失败 %s: %s", ts_code, exc)
        decision.theme = None
    theme_delta, theme_notes = _score_theme(decision.theme)
    score += theme_delta
    breakdown["theme"] = round(theme_delta, 2)
    confirms.extend(theme_notes)

    # ── 判定 ──
    decision.score = _clamp(score, 0.0, 100.0)
    # confidence 是"这个买点成不成立"的概率感，和 score 同源但保留 0-1 量纲，
    # 便于日后和战法自身的 confidence 直接比较
    decision.confidence = round(decision.score / 100.0, 4)
    decision.confirms = confirms
    decision.detail["breakdown"] = breakdown

    if decision.score >= SCORE_BUY:
        decision.action = "BUY"
    elif decision.score >= SCORE_WATCH:
        decision.action = "WATCH"
    else:
        decision.action = "NONE"
    return decision


def _lookup_name(ts_code: str) -> str:
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT name FROM stock_basic WHERE ts_code = ?", (ts_code,)).fetchone()
        return str(row[0]) if row and row[0] else ""
    except Exception:
        return ""


def confirm_buy_batch(
    codes: Sequence[str],
    trade_date: str | None = None,
    *,
    market: dict[str, Any] | None = None,
    days: int = 150,
    theme_lookback: int | None = None,
) -> list[BuyDecision]:
    """批量买点确认。大盘环境只算一次，复用给所有票。"""
    if market is None and trade_date:
        from .daily_pipeline import compute_market_context

        try:
            market = compute_market_context(trade_date)
        except Exception as exc:
            logger.warning("大盘环境计算失败 %s: %s", trade_date, exc)

    out: list[BuyDecision] = []
    for code in codes:
        try:
            out.append(
                confirm_buy(code, trade_date, market=market, days=days, theme_lookback=theme_lookback)
            )
        except Exception as exc:
            logger.error("买点确认失败 %s: %s", code, exc)
            failed = BuyDecision(ts_code=code, trade_date=trade_date or "")
            failed.detail["error"] = str(exc)
            out.append(failed)
    return out


def save_buy_decisions(decisions: Sequence[BuyDecision]) -> int:
    """落库 buy_decisions（INSERT OR REPLACE，重跑幂等）。

    只写有实际交易日的记录——拿不到数据日的决策写进去也没法复盘。
    """
    rows = [d.as_row() for d in decisions if d.trade_date]
    if not rows:
        return 0
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO buy_decisions
            (ts_code, trade_date, name, action, score, confidence, base_strategy,
             triggers, confirms, vetoes, market_dir, market_strength,
             theme, theme_strength, theme_rank, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def format_buy_decision(d: BuyDecision) -> str:
    """单只票的买点确认详情（人类可读）。"""
    icon = {"BUY": "[买入]", "WATCH": "[观察]", "NONE": "[不买]"}.get(d.action, d.action)
    lines = [
        "=" * 66,
        f"{d.ts_code} {d.name}  {d.trade_date}   {icon}  确认分={d.score:.1f}",
        "=" * 66,
    ]
    if d.vetoes:
        lines.append("【一票否决】")
        lines.extend(f"  x {v}" for v in d.vetoes)
    if d.triggers:
        lines.append(f"【买点战法（最近 {FRESH_BARS} 个交易日）】")
        for t in d.triggers[:6]:
            lines.append(
                f"  * {t['trade_date']} {t['strategy']:<12} 置信度={t['confidence']:.2f}  {t['description'][:52]}"
            )
    elif not d.vetoes:
        lines.append(f"【买点战法】最近 {FRESH_BARS} 个交易日内无信号")
    if d.confirms:
        lines.append("【确认项】")
        lines.extend(f"  - {c}" for c in d.confirms)
    breakdown = (d.detail or {}).get("breakdown")
    if breakdown:
        lines.append("【分数构成】 " + "  ".join(f"{k}={v:+.1f}" for k, v in breakdown.items()))
    if (d.detail or {}).get("reason"):
        lines.append(f"【说明】{d.detail['reason']}")
    return "\n".join(lines)


def format_buy_summary(decisions: Sequence[BuyDecision]) -> str:
    """批量买点确认的汇总表。"""
    if not decisions:
        return "无买点确认结果"
    order = {"BUY": 0, "WATCH": 1, "NONE": 2}
    ordered = sorted(decisions, key=lambda d: (order.get(d.action, 3), -d.score))
    lines = [
        f"{'代码':<12} {'名称':<8} {'结论':<7} {'确认分':>7} {'触发战法':<14} {'主线':<12} 备注",
        "-" * 100,
    ]
    for d in ordered:
        theme = (d.theme or {}).get("theme", "") or "-"
        if (d.theme or {}).get("kind") == "industry":
            theme = f"{theme}(行业)"
        if d.vetoes:
            note = d.vetoes[0]
        elif not d.triggers:
            note = f"最近{FRESH_BARS}日无买点信号"
        else:
            note = (d.confirms[0] if d.confirms else "")
        lines.append(
            f"{d.ts_code:<12} {(d.name or '')[:8]:<8} {d.action:<7} {d.score:>7.1f} "
            f"{(d.base_strategy or '-'):<14} {theme[:12]:<12} {note[:40]}"
        )
    counts = {a: sum(1 for d in decisions if d.action == a) for a in ("BUY", "WATCH", "NONE")}
    lines.append("-" * 100)
    lines.append(f"合计 {len(decisions)} 只：买入 {counts['BUY']}  观察 {counts['WATCH']}  不买 {counts['NONE']}")
    return "\n".join(lines)
