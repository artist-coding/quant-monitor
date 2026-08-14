"""买点确认引擎（日线口径，全市场扫描用）。

**判定顺序**——活跃市值区间在最前，是准入总开关而不是加减分：

    第一阶段 confirm_buy —— 逐票判"这个时点该不该买"

        可交易性 → 活跃市值区间 → 一票否决 → B1 触发 → MACD/量能确认
                                                        ⇒ BUY / WATCH / NONE

    第二阶段 select_final_picks —— 从 BUY 里挑"最终买哪几只"

        按主线/行业强弱排序 + 强度门槛                  ⇒ 最终持仓候选

活跃市值是总开关，大盘宽度只是建仓参考
--------------------------------------

能不能选股由**活跃市值多空区间**（见 modules/amv.py）单独决定：
空头区间完全不选股、不新建仓，多头区间才放行。它是门槛不是分数——
写成加减分意味着一个 90 分的买点在空头区间里仍有 78 分、照样过 65 的线，
等于允许逆势加仓。做成门槛后不过关直接全盘停手，一根 K 线都不用读。

**全市场宽度（market_context）已不再决定能否选股**，降级为建仓轻重的参考
（``suggest_position``）：同样是多头区间，宽度 80 和宽度 55 该上的仓位不一样。

触发层只留 B1
-------------

B1（``strategies.base_strategies.detect_b1``）是唯一的买点触发器：
J < ``b1.j_threshold``（默认 13）+ 非绿砖（4 日内阴线 < 4 根）+ 缩量加分，
再叠 MDC 多维验证与麒麟阶段。
B2/B3/SB1 与各路复合战法都不再作为触发条件——它们要么是 B1 之后的确认
（B2/B3），要么口径与 B1 重叠，混在一起会让"为什么买"这件事说不清楚。

MDC 现算，不读 indicator_cache
------------------------------

B1 的 RSI6/ADX/DMI 加分项原本依赖 ``indicator_cache`` 联表，而那张表
只覆盖票池 7 只票——全市场扫描时这些字段全是 None，加分项一条都不触发。
现在改为从 K 线**现场计算**（``calculate_rsi_multi`` / ``calculate_dmi``），
5000 只票一视同仁。

布林带已从全仓库移除（用户不使用该指标），B1 原有的"触及布林下轨 +15%"
与资金流的"主力大单净流入 +10%"（moneyflow 表是空的，恒不触发）一并删除。

其他约束
--------

- **纯日线，不碰分时。** 量比战法 ``detect_volume_ratio_strategy`` 的 6 场景
  阈值（>40/>20/10~20）是分钟级量比口径，套在"当日量÷5日均量"上实测 1540 个
  交易日里买入侧 0 触发（最大量比只有 5.49），故不使用。成交量维度改用阈值
  本就是日线口径的 ``detect_volume_attack``、``detect_double_gun`` 与 B1 自带的缩量判定。
- **一票否决优先于一切加分。** MACD 的 ``macd_veto``（DIF<0 且无底背离）在
  ``complex_patterns.detect_macd_signals`` 里就叫"一票否决权"，此处如实执行。
- **只组合现成战法，不新编战法。** 本模块新增的只有各层权重与阈值，集中在
  下面的常量区并逐条注明理由。
- **卖出侧不在本模块。**
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
# 每条都写清楚为什么是这个量级，方便日后用 buy_decisions 的历史数据做归因时逐项调参。

# 买点战法信号的有效期（交易日）。买点是时效性极强的东西：B1 说的是"今天缩量
# 回调到位"，5 天前的 B1 早就该被证实或证伪了。取 3 根 K 线——足够容纳
# "B1 出现后等一天确认"的常规节奏，又不至于把上周的旧信号当成今天的买点。
FRESH_BARS = 3

# 最少 K 线根数。B1 自身只要 10 根，但 RSI6 要 25、DMI 要 30——
# 不够 30 根时 MDC 全缺，判出来的 B1 是裸 J 值，不如不判。
_MIN_BARS = 30

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

# ── 门槛：活跃市值多空区间（在触发层之前）──
#
# 选股的**总开关**是活跃市值（见 modules/amv.py），不是全市场宽度：
#   多头区间 → 可选股、可新建仓
#   空头区间 → 完全不选股
#
# 全市场宽度已降级为**辅助建仓参考**（见 suggest_position），只提示仓位轻重，
# 不再决定能不能选股。
GATE_ON = "on"  # 按活跃市值区间执行（默认）
GATE_OFF = "off"  # 忽略区间，照常选股（调试/回测用）
DEFAULT_MARKET_GATE = GATE_ON

# 活跃市值数据比目标日旧多少天就要提醒。区间本身是"沿用前一日"的状态机，
# 用稍旧的区间在规则上是成立的；真正的风险是那几天里发生过触发而没录入。
AMV_STALE_WARN_DAYS = 3

# ── 辅助建仓：把全市场宽度强度映射成仓位区间 ──
#
# **这组档位是建议值，不是用户给定的规则**——用户只说"辅助判断现在仓位大概
# 多少合适"，没给具体数字。改这里不影响选股结果，只影响提示文案。
_POSITION_BANDS = (
    (70.0, "重仓", "70~100%"),
    (60.0, "偏重", "50~70%"),
    (45.0, "半仓", "30~50%"),
    (30.0, "轻仓", "10~30%"),
    (0.0, "空仓观望", "0~10%"),
)

# 注：原有的"多战法共振"加分已随触发层收敛到 B1 一并移除——
# 只剩一个触发器时 distinct 恒为 1，共振分恒为 0，是死代码。

# ── 第二阶段：主线/行业筛选 ──
#
# 主线强度是 0-100 的百分位（见 themes.py），50 = 与中位数行业一样强。
# 它**不参与第一阶段打分**，只在这里决定"合格的买点里挑哪几个"。

# 默认最多选几只。真实组合同时持 3~8 只是常态，再多就管不过来也摊薄了主线收益。
DEFAULT_TOP_N = 5
# 主线/行业强度门槛：低于中位数行业的板块直接不选。
# 买点再漂亮，票在一个整体走弱的板块里也是逆水行舟。
DEFAULT_MIN_GROUP_STRENGTH = 50.0
# 行业分类只是没有主线归属时的兜底参照，不是真正的"炒作主线"。
# 用它筛选时门槛加严一档，因为行业归类粗、噪音大。
INDUSTRY_STRENGTH_PENALTY = 10.0

# 唯一的买点触发器。B2/B3/SB1 与各路复合战法已移除——它们要么是 B1 之后的
# 确认（B2/B3），要么口径与 B1 重叠，混在一起会让"为什么买"说不清楚。
_TRIGGER_NAME = "B1"


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
    # 第二阶段（主线/行业筛选）的结果，由 apply_picks 回填。0 = 未入选。
    pick_rank: int = 0
    pick_reason: str = ""

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
            int(self.pick_rank),
            self.pick_reason,
            json.dumps(self.detail, ensure_ascii=False, default=str),
        )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _latest_trade_date() -> str:
    """库内最新交易日（未指定日期时的默认目标日）。"""
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT MAX(trade_date) FROM daily_kline").fetchone()
        return str(row[0]) if row and row[0] else ""
    except Exception:
        return ""


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
    会把这条空决策按目标日落库——这是踩过的坑：
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

    from .strategies.core import GAP_CHECK_BARS, window_gaps

    window = klines[: index + 1]
    vetoes: list[str] = []
    detail: dict[str, Any] = {}

    # 数据连续性优先于一切指标：窗口在交易日历上有洞时，KDJ/MACD/BBI 这些递推指标
    # 算出来的是把不相邻的 K 线缝在一起的产物，看着正常、其实全错。这种情况下
    # 唯一正确的动作是拒判，而不是给一个「置信度 0.6 的 B1」。
    gaps = window_gaps(window)
    detail["data_gaps"] = gaps
    if gaps["severe"]:
        worst = gaps.get("worst")
        where = f"，最大断口 {worst[0]}→{worst[1]} 缺 {worst[2]} 天" if worst else ""
        vetoes.append(
            f"K线不连续：最近 {GAP_CHECK_BARS} 个交易日里缺 {gaps['missing']} 天{where}"
            "（数据缺失或长期停牌，递推指标不可信）"
        )

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


def attach_mdc_fields(klines: list, only_last: int | None = None) -> None:
    """就地给 K 线补上 B1 需要的 MDC 指标字段（RSI6 / DMI+ADX）。

    B1 的多维验证原本靠 ``strategies.core.get_kline_data`` 联 ``indicator_cache``
    取这些值，而那张表只覆盖票池 7 只票——全市场扫描时字段全是 None，
    "RSI 极端超卖 +5%""ADX 高位动能竭尽 +10%"
    这些加分项一条都不会触发，B1 只剩裸的 J 值判断。

    这里改为逐日现算。计算量是 O(n) 次调用 × n 根 K 线，150 根约 26ms，
    比联表查询贵，但换来的是全市场一视同仁的判定口径。

    Args:
        only_last: 只给**最后 N 根** K 线算 MDC，其余留空。默认 None = 全算。

            全算是浪费：这些字段的唯一读者是 ``detect_b1``，而它只读
            ``klines[index]``，``_collect_triggers`` 又只在最后 ``FRESH_BARS``
            根上试触发——150 根里只有 3 根的值会被读到。传 ``FRESH_BARS``
            得到的判定结果与全算**逐位相同**（每根仍按 ``window[:i+1]``
            的前缀计算，口径没变），只是省掉 147 根算了不看的。
            默认保持全算是为了不改动既有调用方的行为；批量回放（回测）
            必须传 ``FRESH_BARS``，否则 90% 的时间花在这里。

            注意"最后 N 根"是相对**传入序列的末尾**而言的。判定下标不在
            末尾时（停牌票回退取数），调用方必须先把序列截到判定下标，
            否则算的是末尾 3 根、读的是下标附近 3 根，两边对不上——
            ``confirm_buy`` 就是这么传的。

    资金流三项（net_mf/large_inflow/large_outflow）已不参与 B1 打分，
    moneyflow 表本来也是空的。
    """
    from .indicators import calculate_dmi, calculate_rsi_multi

    start = 0 if only_last is None else max(0, len(klines) - only_last)
    for i in range(start, len(klines)):
        k = klines[i]
        window = klines[: i + 1]
        n = len(window)
        # 各指标的最小样本量与 data_sync.indicator_cache 保持一致，
        # 免得同一只票在两条路径上算出不同的值
        if n >= 25:
            try:
                k.rsi6 = calculate_rsi_multi(window)[0]
            except Exception:
                pass
        if n >= 30:
            try:
                k.dmi_plus, k.dmi_minus, k.adx = calculate_dmi(window)
            except Exception:
                pass


def _collect_triggers(klines: list, index: int) -> list[dict[str, Any]]:
    """触发层：最近 FRESH_BARS 根内的 B1 信号，按置信度降序。"""
    from .indicators import detect_kirin_stage
    from .strategies.base_strategies import detect_b1

    start = max(0, index - FRESH_BARS + 1)
    triggers: list[dict[str, Any]] = []

    for i in range(start, index + 1):
        # 麒麟阶段是 B1 的背景参数（吸筹 +20%、回落 +10%、派发 −30%）
        kirin_context = None
        try:
            kirin_context = detect_kirin_stage(klines[: i + 1])
        except Exception as exc:
            logger.debug("麒麟阶段检测失败 index=%d: %s", i, exc)

        try:
            sig = detect_b1(klines, i, kirin_context=kirin_context)
        except Exception as exc:
            logger.debug("B1 在 index=%d 检测异常: %s", i, exc)
            continue
        if sig is None:
            continue
        triggers.append(
            {
                "strategy": _TRIGGER_NAME,
                "trade_date": sig.trade_date,
                "confidence": round(float(sig.confidence or 0), 4),
                "description": sig.description or "",
                "bars_ago": index - i,
                "stop_loss": sig.stop_loss,
            }
        )

    triggers.sort(key=lambda t: (t["confidence"], -t["bars_ago"]), reverse=True)
    return triggers


def check_market_gate(trade_date: str | None = None, gate: str = DEFAULT_MARKET_GATE) -> tuple[str, list[str], Any]:
    """选股总开关：活跃市值多空区间。

    Returns:
        (拦截原因, 警告列表, AmvDay 或 None)。拦截原因为空表示放行。
    """
    from .amv import get_regime

    try:
        day = get_regime(trade_date)
    except Exception as exc:
        logger.warning("活跃市值区间读取失败: %s", exc)
        day = None

    if gate == GATE_OFF:
        return "", ["已用 --market-gate off 忽略活跃市值区间"], day

    if day is None:
        # 活跃市值现在是总开关，没有它就无从判断能不能选股。
        # 这里选择拦截而不是放行：宁可提示去补数据，也不要在未知区间里开仓。
        return (
            "活跃市值无数据，无法判断多空区间。先 `zt amv import <csv>` 导入历史，"
            "再用 `zt amv add <日期> --close <收盘价>` 录入当日"
        ), [], None

    warnings: list[str] = []
    if trade_date and day.trade_date < trade_date:
        from datetime import datetime

        try:
            gap = (datetime.strptime(trade_date, "%Y%m%d") - datetime.strptime(day.trade_date, "%Y%m%d")).days
        except ValueError:
            gap = 0
        if gap >= AMV_STALE_WARN_DAYS:
            warnings.append(
                f"活跃市值最新只到 {day.trade_date}，比目标日 {trade_date} 落后 {gap} 天。"
                f"区间是「沿用前一日」的状态机，这几天里若发生过触发而未录入，结论会是错的"
            )

    if not day.can_select:
        return f"活跃市值处于{day.regime}（{day.trade_date}，涨幅 {day.pct_chg:+.2f}%），停止选股", warnings, day
    return "", warnings, day


def suggest_position(market: dict[str, Any] | None) -> dict[str, Any]:
    """把全市场宽度强度换算成建仓仓位建议。

    大盘宽度不再决定"能不能选股"（那是活跃市值区间的职责），只回答
    "现在大概该拿多重的仓位"。档位是建议值，见 _POSITION_BANDS 注释。
    """
    if not market:
        return {"level": "未知", "range": "-", "strength": None, "note": "大盘宽度不可用（当日非全市场同步日）"}
    strength = _num(market, "market_strength", 50.0)
    for floor, level, rng in _POSITION_BANDS:
        if strength >= floor:
            return {
                "level": level,
                "range": rng,
                "strength": strength,
                "market_dir": market.get("market_dir", ""),
                "note": f"大盘{market.get('market_dir', '?')}、强度 {strength:.1f} → 建议{level}（{rng}）",
            }
    return {"level": "空仓观望", "range": "0~10%", "strength": strength, "note": ""}


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


def _describe_market(market: dict[str, Any] | None) -> list[str]:
    """把大盘宽度渲染成一条建仓提示（不打分、不决定能否选股）。"""
    hint = suggest_position(market)
    return [f"建仓参考：{hint['note']}"] if hint.get("note") else []


def _describe_theme(theme: dict[str, Any] | None) -> list[str]:
    """把主线归属渲染成一条说明。

    **不打分**——主线只在第二阶段 select_final_picks 里起作用。
    这里挂出来是为了让第一阶段的输出自带上下文，看结论时不用再查一次表。
    """
    if not theme:
        return []
    kind_label = "主线" if theme.get("kind") == "theme" else "行业(兜底)"
    strength = _num(theme, "strength", 50.0)
    return [
        f"{kind_label}「{theme.get('theme', '')}」强度{strength:.1f} "
        f"排名{theme.get('rank', 0)}/{theme.get('total', 0)}（不计入确认分，用于第二阶段筛选）"
    ]


# ==================== 主入口 ====================


def confirm_buy(
    ts_code: str,
    trade_date: str | None = None,
    *,
    market: dict[str, Any] | None = None,
    klines: list | None = None,
    days: int = 150,
    theme_lookback: int | None = None,
    market_gate: str = DEFAULT_MARKET_GATE,
    skip_market_gate: bool = False,
    mdc_scope: int | None = None,
    name: str | None = None,
) -> BuyDecision:
    """对单只票做买点确认。

    Args:
        ts_code: 股票代码
        trade_date: 目标交易日 YYYYMMDD；None 表示用库里最新一根 K 线
        market: 预先算好的大盘环境（批量调用时传入，避免每只票重算一次）
        klines: 预先取好的 DailyData 序列。MDC 字段会被就地补算，
            不需要调用方保证已联 indicator_cache。
        days: 未传 klines 时的回溯 K 线根数
        theme_lookback: 主线强度的统计窗口；None 用 themes.DEFAULT_LOOKBACK
        market_gate: 大盘门槛，见 MARKET_GATE_* 常量
        skip_market_gate: 批量扫描时门槛已在外层统一判过，此处跳过重复判断
        mdc_scope: 透传给 :func:`attach_mdc_fields` 的 ``only_last``。
            传 ``FRESH_BARS`` 结果不变但快一个数量级，见该函数的说明。
        name: 股票名称。调用方已知时传入，省掉一次建连接查 stock_basic——
            历史回放要判几十万次，每次重查同一个静态字段是纯浪费。
    """
    from .strategies.core import _dict_to_daily, get_kline_data

    decision = BuyDecision(ts_code=ts_code, trade_date=trade_date or "")

    # ── 第零层：可交易性 ──
    # ST 与北交所直接出局，连 K 线都不用取。不静默跳过而是给一条带原因的 NONE：
    # 票池里放了 ST 的话，用户该看到"被排除了"，而不是这只票凭空消失。
    from .universe import exclusion_reason

    if name is None:
        name = _lookup_name(ts_code)
    excluded = exclusion_reason(ts_code, name if name else None)
    if excluded:
        decision.name = name
        decision.action = "NONE"
        decision.vetoes = [f"不可交易标的：{excluded}"]
        decision.detail["stopped_at"] = "excluded"
        # trade_date 留空：这不是"某一天的判断"，而是这只票根本不进视野，
        # 落库会污染按日归因的统计。
        decision.trade_date = ""
        return decision

    # ── 第一层：活跃市值区间门槛（在取 K 线和一切个股判断之前）──
    # 空头区间整体不选股，不存在"个股够强抵消区间"。放在最前还有个实际好处：
    # 全市场扫描时门槛不过关可以一根 K 线都不读就返回。
    if market is None and not skip_market_gate:
        from .market_context import compute_market_context

        try:
            market = compute_market_context(trade_date or _latest_trade_date())
        except Exception as exc:
            logger.warning("大盘环境计算失败: %s", exc)
            market = None
    decision.market = market or {}

    if not skip_market_gate:
        blocked, _warn, _day = check_market_gate(trade_date or _latest_trade_date(), market_gate)
        if blocked:
            decision.name = name
            decision.action = "NONE"
            decision.vetoes = [f"活跃市值门槛未通过：{blocked}"]
            decision.detail["stopped_at"] = "market_gate"
            decision.trade_date = ""
            return decision

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
    # B1 自身只要 10 根，但 RSI6 要 25、DMI 要 30、麒麟阶段更多。
    # 统一要求 30 根，低于此的票 MDC 全缺，判出来的 B1 是裸 J 值，没有意义。
    if index < _MIN_BARS:
        decision.trade_date = klines[index].trade_date
        decision.detail["reason"] = (
            f"{klines[index].trade_date} 之前只有 {index} 根 K 线，不足 {_MIN_BARS} 根，无法完整判定"
        )
        return decision

    decision.trade_date = klines[index].trade_date
    decision.name = name

    # B1 的 RSI/DMI 加分项需要这些字段；indicator_cache 只覆盖票池，
    # 全市场扫描必须现算，否则 B1 退化成裸 J 值判断。
    # 截到 index：判定只读这一根及之前，之后的 K 线算了也没人看；
    # 且 mdc_scope 的"最后 N 根"必须相对 index 而不是数组末尾。
    # 切片共享同一批对象，就地补值对原序列可见。
    attach_mdc_fields(klines[: index + 1], mdc_scope)

    # ── 第二层：一票否决 ──
    vetoes, veto_detail = _collect_vetoes(klines, index)
    macd_sig = veto_detail.pop("_macd_sig", {})
    decision.detail.update(veto_detail)
    decision.vetoes = vetoes

    # ── 第三层：B1 触发 ──
    triggers = _collect_triggers(klines, index)
    decision.triggers = triggers

    if vetoes:
        decision.action = "NONE"
        decision.detail["stopped_at"] = "veto"
        return decision
    if not triggers:
        decision.action = "NONE"
        decision.detail["stopped_at"] = "no_trigger"
        decision.detail["reason"] = f"最近 {FRESH_BARS} 个交易日内无 B1 买点信号"
        return decision

    # 显式取置信度最高的那个，不依赖 _collect_triggers 的返回顺序——
    # 排序是那边的实现细节，主流程不该建立在一个隐式契约上。
    best = max(triggers, key=lambda t: (float(t.get("confidence", 0) or 0), -int(t.get("bars_ago", 0) or 0)))
    decision.base_strategy = best["strategy"]
    score = float(best["confidence"]) * 100.0
    breakdown: dict[str, float] = {"base": round(score, 2)}
    confirms = _describe_market(market)
    confirms.append(f"{best['strategy']}触发（{best['trade_date']}，置信度{best['confidence']:.2f}）")

    # ── 第四层：MACD 确认 ──
    macd_delta, macd_notes = _score_macd(macd_sig)
    score += macd_delta
    breakdown["macd"] = round(macd_delta, 2)
    confirms.extend(macd_notes)

    # ── 第五层：成交量确认 ──
    vol_delta, vol_notes, vol_detail = _score_volume(klines, index, triggers)
    score += vol_delta
    breakdown["volume"] = round(vol_delta, 2)
    confirms.extend(vol_notes)
    decision.detail["volume"] = vol_detail

    # ── 主线归属（只挂载，不打分；第二阶段 select_final_picks 才用它）──
    try:
        from .themes import DEFAULT_LOOKBACK, get_stock_theme_strength

        decision.theme = get_stock_theme_strength(ts_code, decision.trade_date, theme_lookback or DEFAULT_LOOKBACK)
    except Exception as exc:
        logger.warning("主线强度读取失败 %s: %s", ts_code, exc)
        decision.theme = None
    confirms.extend(_describe_theme(decision.theme))

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
    market_gate: str = DEFAULT_MARKET_GATE,
) -> tuple[list[BuyDecision], str]:
    """批量买点确认。

    大盘门槛只判一次：不过关就直接返回空列表和拦截原因，一只票都不算
    ——这正是把大盘挪到触发层之前的意义，全市场扫描能省掉整轮 K 线读取。

    Returns:
        (决策列表, 大盘拦截原因)。拦截原因非空时决策列表为空。
    """
    if market is None:
        from .market_context import compute_market_context

        target = trade_date or _latest_trade_date()
        try:
            market = compute_market_context(target)
        except Exception as exc:
            logger.warning("大盘环境计算失败 %s: %s", target, exc)

    blocked, _warn, _day = check_market_gate(trade_date or _latest_trade_date(), market_gate)
    if blocked:
        return [], blocked

    out: list[BuyDecision] = []
    for code in codes:
        try:
            out.append(
                confirm_buy(
                    code,
                    trade_date,
                    market=market,
                    days=days,
                    theme_lookback=theme_lookback,
                    skip_market_gate=True,
                )
            )
        except Exception as exc:
            logger.error("买点确认失败 %s: %s", code, exc)
            failed = BuyDecision(ts_code=code, trade_date=trade_date or "")
            failed.detail["error"] = str(exc)
            out.append(failed)
    return out, ""


def scan_market(
    trade_date: str | None = None,
    *,
    market_gate: str = DEFAULT_MARKET_GATE,
    top_n: int = DEFAULT_TOP_N,
    min_group_strength: float = DEFAULT_MIN_GROUP_STRENGTH,
    max_per_group: int | None = None,
    include_watch: bool = False,
    limit: int = 0,
    theme_lookback: int | None = None,
    days: int = 150,
    progress_every: int = 500,
) -> dict[str, Any]:
    """全市场扫描：大盘门槛 → 逐票 B1 确认 → 主线/行业筛选。

    Args:
        trade_date: 目标交易日；None 用库内最新
        market_gate: 大盘门槛，见 MARKET_GATE_*
        limit: 只扫前 N 只（调试用），0 = 全市场
        其余参数见 select_final_picks

    Returns:
        {"trade_date", "market", "blocked", "scanned", "decisions",
         "selection", "elapsed"}；blocked 非空时表示大盘未过关，未扫任何票。
    """
    import time

    from .market_context import compute_market_context
    from .universe import tradable_codes

    started = time.perf_counter()
    target = trade_date or _latest_trade_date()

    result: dict[str, Any] = {
        "trade_date": target,
        "market": {},
        "amv": None,
        "position_hint": {},
        "warnings": [],
        "blocked": "",
        "scanned": 0,
        "decisions": [],
        "selection": {},
        "elapsed": 0.0,
    }
    if not target:
        result["blocked"] = "库内没有任何日线数据"
        return result

    try:
        market = compute_market_context(target)
    except Exception as exc:
        logger.warning("大盘环境计算失败 %s: %s", target, exc)
        market = {}
    result["market"] = market
    # 大盘宽度不再是门槛，只给建仓轻重的参考
    result["position_hint"] = suggest_position(market)

    # 总开关：活跃市值多空区间。不过关就到此为止，不读任何 K 线。
    blocked, gate_warnings, amv_day = check_market_gate(target, market_gate)
    result["warnings"].extend(gate_warnings)
    if amv_day is not None:
        result["amv"] = {
            "trade_date": amv_day.trade_date,
            "close": amv_day.close,
            "pct_chg": amv_day.pct_chg,
            "regime": amv_day.regime,
        }
    if blocked:
        result["blocked"] = blocked
        result["elapsed"] = round(time.perf_counter() - started, 2)
        return result

    codes = tradable_codes(target)
    if limit > 0:
        codes = codes[:limit]
    result["scanned"] = len(codes)

    decisions: list[BuyDecision] = []
    for i, code in enumerate(codes, start=1):
        if progress_every and i % progress_every == 0:
            logger.info("扫描进度 %d/%d，已命中 %d 只", i, len(codes), sum(1 for d in decisions if d.action == "BUY"))
        try:
            decisions.append(
                confirm_buy(
                    code,
                    target,
                    market=market,
                    days=days,
                    theme_lookback=theme_lookback,
                    skip_market_gate=True,
                )
            )
        except Exception as exc:
            logger.warning("买点确认失败 %s: %s", code, exc)

    selection = select_final_picks(
        decisions,
        top_n=top_n,
        min_group_strength=min_group_strength,
        max_per_group=max_per_group,
        include_watch=include_watch,
    )
    apply_picks(decisions, selection)

    result["decisions"] = decisions
    result["selection"] = selection
    result["elapsed"] = round(time.perf_counter() - started, 2)
    return result


# ==================== 第二阶段：主线/行业筛选 ====================


def _group_of(d: BuyDecision) -> tuple[str, str, float]:
    """取一只票用于第二阶段筛选的分组：(名称, kind, 有效强度)。

    有效强度 = 原始强度 −（行业兜底时的惩罚）。行业分类是 Tushare 的粗口径，
    "元器件"里既有光模块也有电阻厂，同一个强度读数的含金量不如用户手工圈定的主线，
    所以拿它筛选时门槛加严一档。
    """
    theme = d.theme or {}
    name = str(theme.get("theme") or "")
    kind = str(theme.get("kind") or "")
    if not name:
        return "", "", 0.0
    strength = _num(theme, "strength", 0.0)
    if kind == "industry":
        strength -= INDUSTRY_STRENGTH_PENALTY
    return name, kind, strength


def select_final_picks(
    decisions: Sequence[BuyDecision],
    *,
    top_n: int = DEFAULT_TOP_N,
    min_group_strength: float = DEFAULT_MIN_GROUP_STRENGTH,
    max_per_group: int | None = None,
    include_watch: bool = False,
) -> dict[str, Any]:
    """第二阶段：从买点确认通过的票里，按主线/行业强弱挑出最终持仓候选。

    排序主键是**分组强度**而不是买点确认分——这一阶段回答的是"钱该往哪个
    方向压"，同一条强主线里的第二名，通常好过一条弱主线里的第一名。
    同组内再按确认分排。

    Args:
        decisions: 第一阶段的产出
        top_n: 最多选几只
        min_group_strength: 分组强度门槛（行业兜底会先扣 INDUSTRY_STRENGTH_PENALTY）
        max_per_group: 每个主线/行业最多选几只。默认 None = 不限制，
            即允许把仓位压在同一条主线上（主线思维本就要集中）。
            想强制分散时设成 1~2。
        include_watch: 是否把 WATCH 也纳入候选

    Returns:
        {"picks": [...], "rejected": [...], "candidates": int, "params": {...}}
        picks/rejected 的元素都是 {"decision", "group", "group_kind",
        "group_strength", "rank", "reason"}
    """
    wanted = {"BUY", "WATCH"} if include_watch else {"BUY"}
    candidates = [d for d in decisions if d.action in wanted]

    scored: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for d in candidates:
        name, kind, strength = _group_of(d)
        entry = {
            "decision": d,
            "group": name,
            "group_kind": kind,
            "group_strength": round(strength, 2),
            "rank": 0,
            "reason": "",
        }
        if not name:
            # 没有主线也没有行业归属就没法做这一步的判断。不静默丢弃——
            # 这多半意味着 stock_basic 缺 industry 或主线排名那步失败了。
            entry["reason"] = "无主线/行业归属，无法参与筛选"
            rejected.append(entry)
            continue
        if strength < min_group_strength:
            label = "行业" if kind == "industry" else "主线"
            entry["reason"] = f"{label}「{name}」有效强度 {strength:.1f} 低于门槛 {min_group_strength:.0f}"
            rejected.append(entry)
            continue
        scored.append(entry)

    # 分组强度优先，同组内按确认分
    scored.sort(key=lambda e: (e["group_strength"], e["decision"].score), reverse=True)

    picks: list[dict[str, Any]] = []
    per_group: dict[str, int] = {}
    for entry in scored:
        group = entry["group"]
        if max_per_group is not None and per_group.get(group, 0) >= max_per_group:
            entry["reason"] = f"「{group}」已选满 {max_per_group} 只（分散约束）"
            rejected.append(entry)
            continue
        if len(picks) >= top_n:
            entry["reason"] = f"名额已满（top_n={top_n}）"
            rejected.append(entry)
            continue
        per_group[group] = per_group.get(group, 0) + 1
        entry["rank"] = len(picks) + 1
        label = "行业" if entry["group_kind"] == "industry" else "主线"
        entry["reason"] = f"{label}「{group}」强度 {entry['group_strength']:.1f}，确认分 {entry['decision'].score:.1f}"
        picks.append(entry)

    return {
        "picks": picks,
        "rejected": rejected,
        "candidates": len(candidates),
        "params": {
            "top_n": top_n,
            "min_group_strength": min_group_strength,
            "max_per_group": max_per_group,
            "include_watch": include_watch,
        },
    }


def apply_picks(decisions: Sequence[BuyDecision], selection: dict[str, Any]) -> None:
    """把第二阶段的入选名次写回各 BuyDecision（就地修改），供落库与展示。"""
    by_code = {d.ts_code: d for d in decisions}
    for entry in selection.get("picks", []) + selection.get("rejected", []):
        d = by_code.get(entry["decision"].ts_code)
        if d is None:
            continue
        d.pick_rank = int(entry["rank"])
        d.pick_reason = str(entry["reason"])


def format_final_picks(selection: dict[str, Any]) -> str:
    """第二阶段结果的人类可读输出。"""
    picks = selection.get("picks") or []
    rejected = selection.get("rejected") or []
    params = selection.get("params") or {}

    lines = [
        "=" * 86,
        f"最终选股（第二阶段 · 按主线/行业强弱）  候选 {selection.get('candidates', 0)} 只 → 入选 {len(picks)} 只",
        f"参数: top_n={params.get('top_n')}  强度门槛={params.get('min_group_strength')}  "
        f"每组上限={params.get('max_per_group') or '不限'}",
        "=" * 86,
    ]
    if picks:
        lines.append(f"{'#':>3} {'代码':<12} {'名称':<8} {'确认分':>7} {'分组':<14} {'组强度':>7}  触发战法")
        for e in picks:
            d = e["decision"]
            group = e["group"] + ("(行业)" if e["group_kind"] == "industry" else "")
            lines.append(
                f"{e['rank']:>3} {d.ts_code:<12} {(d.name or '')[:8]:<8} {d.score:>7.1f} "
                f"{group[:14]:<14} {e['group_strength']:>7.1f}  {d.base_strategy}"
            )
    else:
        lines.append("无入选标的。")

    if rejected:
        lines.append(f"\n【落选 {len(rejected)} 只】")
        for e in rejected:
            d = e["decision"]
            lines.append(f"  - {d.ts_code:<12} {(d.name or '')[:8]:<8} 确认分{d.score:>6.1f}  {e['reason']}")
    return "\n".join(lines)


def save_buy_decisions(decisions: Sequence[BuyDecision], *, only_actionable: bool = False) -> int:
    """落库 buy_decisions（INSERT OR REPLACE，重跑幂等）。

    只写有实际交易日的记录——拿不到数据日的决策写进去也没法复盘。

    Args:
        only_actionable: True 时只写 BUY/WATCH。全市场扫描一天产生 5000 条
            决策，其中 99% 是"最近3日无 B1 信号"的 NONE，全写进去只是噪音。
    """
    rows = [
        d.as_row()
        for d in decisions
        if d.trade_date and (not only_actionable or d.action in ("BUY", "WATCH"))
    ]
    if not rows:
        return 0
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO buy_decisions
            (ts_code, trade_date, name, action, score, confidence, base_strategy,
             triggers, confirms, vetoes, market_dir, market_strength,
             theme, theme_strength, theme_rank, pick_rank, pick_reason, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        lines.append(f"【B1 买点（最近 {FRESH_BARS} 个交易日）】")
        for t in d.triggers[:6]:
            lines.append(
                f"  * {t['trade_date']} {t['strategy']:<12} 置信度={t['confidence']:.2f}  {t['description'][:52]}"
            )
    elif not d.vetoes:
        lines.append(f"【B1 买点】最近 {FRESH_BARS} 个交易日内无信号")
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
        f"{'选':<3} {'代码':<12} {'名称':<8} {'结论':<7} {'确认分':>7} {'触发战法':<14} {'主线':<12} 备注",
        "-" * 104,
    ]
    for d in ordered:
        theme = (d.theme or {}).get("theme", "") or "-"
        if (d.theme or {}).get("kind") == "industry":
            theme = f"{theme}(行业)"
        if d.vetoes:
            note = d.vetoes[0]
        elif not d.triggers:
            note = f"最近{FRESH_BARS}日无 B1 信号"
        else:
            note = d.confirms[0] if d.confirms else ""
        mark = f"#{d.pick_rank}" if d.pick_rank else ""
        lines.append(
            f"{mark:<3} {d.ts_code:<12} {(d.name or '')[:8]:<8} {d.action:<7} {d.score:>7.1f} "
            f"{(d.base_strategy or '-'):<14} {theme[:12]:<12} {note[:40]}"
        )
    counts = {a: sum(1 for d in decisions if d.action == a) for a in ("BUY", "WATCH", "NONE")}
    lines.append("-" * 100)
    lines.append(f"合计 {len(decisions)} 只：买入 {counts['BUY']}  观察 {counts['WATCH']}  不买 {counts['NONE']}")
    return "\n".join(lines)


def format_scan_result(result: dict[str, Any], *, show_rejected: int = 15) -> str:
    """全市场扫描结果的人类可读输出。"""
    market = result.get("market") or {}
    lines = [
        "=" * 86,
        f"全市场扫描  {result.get('trade_date', '')}   耗时 {result.get('elapsed', 0)}s",
        "=" * 86,
    ]

    amv = result.get("amv") or {}
    if amv:
        pct = amv.get("pct_chg")
        lines.append(
            f"【活跃市值·总开关】{amv.get('regime', '?')}   {amv.get('trade_date', '')}   "
            f"收盘 {amv.get('close', 0):,.2f}" + (f"   涨幅 {pct:+.2f}%" if pct is not None else "")
        )

    breadth = (market.get("detail") or {}).get("breadth") or {}
    hint = result.get("position_hint") or {}
    if market:
        lines.append(
            f"【大盘宽度·建仓参考】{market.get('market_dir', '?')}  强度 {market.get('market_strength', 0)}  "
            f"中位涨跌 {market.get('market_pct_chg', 0)}%"
            + (
                f"  |  {breadth.get('up', 0)}涨/{breadth.get('down', 0)}跌  "
                f"涨停{breadth.get('limit_up', 0)}/跌停{breadth.get('limit_down', 0)}"
                if breadth.get("available")
                else ""
            )
        )
        if hint.get("level"):
            lines.append(f"                    建议仓位 {hint['level']}（{hint.get('range', '-')}）")

    for w in result.get("warnings") or []:
        lines.append(f"  ! {w}")

    if result.get("blocked"):
        lines.append("")
        lines.append(f"  ✗ {result['blocked']}")
        lines.append("  本次未扫描任何个股。")
        return "\n".join(lines)

    decisions = result.get("decisions") or []
    counts = {a: sum(1 for d in decisions if d.action == a) for a in ("BUY", "WATCH", "NONE")}
    stopped: dict[str, int] = {}
    for d in decisions:
        if d.action == "NONE":
            key = str(d.detail.get("stopped_at") or "other")
            stopped[key] = stopped.get(key, 0) + 1

    lines.append(
        f"【第一阶段】扫描 {result.get('scanned', 0)} 只 → "
        f"BUY {counts['BUY']}  WATCH {counts['WATCH']}  NONE {counts['NONE']}"
    )
    label = {
        "no_trigger": f"最近{FRESH_BARS}日无B1",
        "veto": "一票否决",
        "excluded": "不可交易",
        "market_gate": "大盘门槛",
        "other": "数据不足",
    }
    if stopped:
        parts = (f"{label.get(k, k)} {v}" for k, v in sorted(stopped.items(), key=lambda x: -x[1]))
        lines.append("  淘汰构成: " + "  ".join(parts))

    selection = result.get("selection") or {}
    lines.append("")
    lines.append(format_final_picks({**selection, "rejected": (selection.get("rejected") or [])[:show_rejected]}))

    total_rejected = len(selection.get("rejected") or [])
    if total_rejected > show_rejected:
        lines.append(f"  …另有 {total_rejected - show_rejected} 只落选未列出（--show-rejected 调整）")
    return "\n".join(lines)
