"""主线（炒作题材）子系统：成员由外部导入，强弱由本系统排序。

**职责边界**（这是本模块最重要的设计约束）：

- 系统**不判断**"当前市场在炒什么"，也**不判断**"某只票属不属于某条主线"。
  这两件事都需要新闻面/情绪面语料，本地一概没有，硬猜只会产出看起来合理、
  实则编造的归类。主线清单由用户给定，成员归属由用户本地的外部判定器
  （kimi code + swarm）产出后经 :func:`import_members` 导入。
- 系统**只负责**一件事：给定若干条主线及其成员，用本地行情数据把它们的
  **强弱排出顺序**。

强度口径由两路信号合成，两路用的都是已有数据、零 API 成本：

1. **行情面**（原"方法A 行业强度"）——组内累计涨幅中位数、上涨家数占比、
   人均成交额。衡量"这批票整体在不在涨、资金愿不愿意给量"。
2. **情绪面**（原"方法B 涨停板/龙虎榜"）——组内涨停次数、放量攻击次数。
   衡量"有没有出现赚钱效应的极值"。

   注意：涨停板接口 ``limit_list_d`` 与龙虎榜接口 ``top_list`` 在当前 Tushare
   账号下均返回"没有接口访问权限"，故：
   - 涨停改用 ``daily_kline.is_limit_up``（同步器按 pct_chg ≥ 9.9 现算，全库无空值）；
   - 龙虎榜无替代数据，改用"放量攻击次数"（量比>3 且涨幅>2%，即
     ``detect_volume_attack`` 的日线判据）作为资金关注度代理。

``daily_kline.vol_ratio`` 列全库为 NULL（建了但从未写入），因此量比在查询里
用窗口函数现算，不读该列。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .database import get_connection

logger = logging.getLogger(__name__)


# ==================== 强度常量 ====================
#
# **为什么强度用百分位而不是绝对分**：
# 最初把强度写成 `上涨家数占比 + 涨停率×2 + 攻击率`，实测在 20260807 这样的强势周
# （全市场 77.9% 的票 5 日累计上涨）直接崩掉——排名前 12 的行业强度全是 100.00，
# 完全分不出强弱。上涨占比本身就是 0~100 的有界量，再往上叠加必然撞天花板；
# 而"50 分等于中性"这个前提在 5 日累计口径下也不成立。
#
# 现在改为**以行业分布为参照系做百分位映射**：
#   - 行情面、情绪面各算一个原始值，各自映射成"强于百分之多少的行业"；
#   - 两者等权合成 0~100 的强度。
# 好处：天然相对（水涨船高不会让所有主线一起变强）、永不饱和、无需拍权重阈值，
# 且 50 分有明确含义——与中位数行业一样强。

# 涨停率在情绪面里的放大系数。涨停是情绪极值，一个组里 5% 的票涨停，
# 比"多涨 5% 家数"更说明资金在这里打板，故给两倍于放量攻击的权重。
_LIMIT_WEIGHT = 2.0
_ATTACK_WEIGHT = 1.0
# 放量攻击判据（与 indicators.detect_volume_attack 一致，日线口径）
_ATTACK_VOL_RATIO = 3.0
_ATTACK_PCT_CHG = 2.0
# 行情面与情绪面的合成权重（等权：一个说"在不在涨"，一个说"有没有极值"，
# 缺任何一边都会误判——只涨不打板是慢牛不是主线，只打板不普涨是个股异动）
_MOMENTUM_WEIGHT = 0.5
_SENTIMENT_WEIGHT = 0.5
# 情绪面的收缩强度，单位是"股票×交易日"（slots）。
#
# **为什么必须收缩**：涨停是稀有事件。20260807 全市场 5 日涨停率约 1.81%/股票日，
# 一条 6 只票的主线在 5 日窗口里只有 30 个 slots，期望涨停数 0.54——
# 拿到 0 是**最可能**的结果（P≈58%），根本不是"这条主线弱"的证据。
# 实测印证：10~20 成员的行业里 29% 涨停数为零，50+ 成员的行业 0%。
# 不收缩就会系统性地把小主线打到情绪面百分位的地板上。
#
# 做法是经验贝叶斯：给每个组补上 _SENTIMENT_PRIOR_SLOTS 个"按全市场平均表现"
# 的虚拟 slot。取 200（≈40 只票 × 5 天）——6 只票的主线自身数据只占 13% 权重，
# 结论贴近市场基准；311 只票的行业占 89%，几乎全按自身数据说话。
_SENTIMENT_PRIOR_SLOTS = 200.0
# 成员数下限：少于这个数的组统计噪音过大，算出的强度不可信
_MIN_MEMBERS = 3
# 进入"参照系"的行业最小成员数。4 只票的行业动不动就 100% 上涨，
# 让这类极值参与定标会把整条标尺压扁，故只用足够大的行业当尺子。
_REFERENCE_MIN_MEMBERS = 10
# 默认统计窗口。主线是持续数日到数周的现象，单日涨跌噪音太大，
# 5 个交易日（一周）是能反映持续性又不至于太滞后的折中。
DEFAULT_LOOKBACK = 5

# 全市场基准行在 theme_strength 表里的固定名字
MARKET_BASELINE = "__market__"


@dataclass
class GroupStrength:
    """一个股票分组在某个统计窗口内的强度画像。"""

    name: str
    kind: str  # theme / industry / market
    member_count: int = 0
    median_pct_chg: float = 0.0
    up_ratio: float = 0.0
    limit_up_count: int = 0
    attack_count: int = 0
    amount_per_stock: float = 0.0
    # 统计口径：股票×交易日的样本量，情绪面收缩要用
    slots: int = 0
    # 原始面板值（定标前）：行情面取累计涨幅中位数；
    # 情绪面取加权的涨停/攻击率，并已按 _SENTIMENT_PRIOR_SLOTS 向全市场收缩
    momentum_raw: float = 0.0
    sentiment_raw: float = 0.0
    # 定标后的百分位（0-100，"强于百分之多少的参照行业"）
    momentum_pct: float = 50.0
    sentiment_pct: float = 50.0
    strength: float = 50.0
    excess: float = 0.0
    rank: int = 0
    members: list[str] = field(default_factory=list)

    def as_row(self, trade_date: str, lookback: int) -> tuple:
        return (
            trade_date,
            self.name,
            self.kind,
            lookback,
            self.member_count,
            round(self.median_pct_chg, 4),
            round(self.up_ratio, 2),
            self.limit_up_count,
            self.attack_count,
            round(self.amount_per_stock, 2),
            round(self.strength, 2),
            round(self.excess, 2),
            self.rank,
        )


# ==================== 主线定义与成员维护 ====================


def upsert_theme(name: str, description: str = "", active: bool = True) -> None:
    """新增或更新一条主线定义（幂等）。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("主线名称不能为空")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO themes (name, description, active)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description = excluded.description,
                active = excluded.active,
                updated_at = CURRENT_TIMESTAMP
            """,
            (name, description or "", 1 if active else 0),
        )


def set_theme_active(name: str, active: bool) -> bool:
    """主线退潮时置为 inactive（保留历史成员与强度快照，只是不再参与排名）。"""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE themes SET active = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
            (1 if active else 0, name),
        )
        return cur.rowcount > 0


def remove_theme(name: str) -> bool:
    """彻底删除一条主线及其成员关系。"""
    with get_connection() as conn:
        conn.execute("DELETE FROM theme_members WHERE theme = ?", (name,))
        cur = conn.execute("DELETE FROM themes WHERE name = ?", (name,))
        return cur.rowcount > 0


def list_themes(active_only: bool = False) -> list[dict[str, Any]]:
    """列出主线及其成员数。"""
    sql = """
        SELECT t.name, t.description, t.active, t.updated_at,
               (SELECT COUNT(*) FROM theme_members m WHERE m.theme = t.name) AS member_count
        FROM themes t
        {where}
        ORDER BY t.active DESC, t.name
    """.format(where="WHERE t.active = 1" if active_only else "")
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def import_members(
    records: Iterable[dict[str, Any]],
    *,
    source: str = "external",
    replace: bool = False,
    auto_create_theme: bool = True,
) -> dict[str, Any]:
    """导入外部判定器产出的「股票 ↔ 主线」归属结果。

    Args:
        records: 每条形如
            ``{"theme": "商业航天", "ts_code": "600879.SH", "confidence": 0.9, "reason": "..."}``
        source: 判定来源标记，写入 theme_members.source
        replace: True 时先清空本次涉及的每条主线的旧成员再写入
            （外部判定器整体重跑时用，避免退出主线的票残留）
        auto_create_theme: 记录里出现的未知主线是否自动建档

    Returns:
        {"imported": int, "themes": [...], "skipped": [...]}
    """
    rows: list[tuple] = []
    themes_seen: list[str] = []
    skipped: list[str] = []

    for rec in records:
        theme = str(rec.get("theme") or "").strip()
        ts_code = str(rec.get("ts_code") or "").strip().upper()
        if not theme or not ts_code:
            skipped.append(f"缺少 theme 或 ts_code: {rec!r}")
            continue
        try:
            confidence = float(rec.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        # 置信度越界说明外部判定器口径不对，夹住而不是拒绝——宁可保守也别丢数据
        confidence = max(0.0, min(1.0, confidence))
        if theme not in themes_seen:
            themes_seen.append(theme)
        rows.append((theme, ts_code, confidence, str(rec.get("reason") or ""), source))

    if not rows:
        return {"imported": 0, "themes": [], "skipped": skipped}

    with get_connection() as conn:
        if auto_create_theme:
            conn.executemany(
                "INSERT OR IGNORE INTO themes (name, description, active) VALUES (?, '', 1)",
                [(t,) for t in themes_seen],
            )
        else:
            known = {r[0] for r in conn.execute("SELECT name FROM themes").fetchall()}
            unknown = [t for t in themes_seen if t not in known]
            if unknown:
                raise ValueError(f"未知主线（auto_create_theme=False）: {', '.join(unknown)}")

        if replace:
            conn.executemany("DELETE FROM theme_members WHERE theme = ?", [(t,) for t in themes_seen])

        conn.executemany(
            """
            INSERT INTO theme_members (theme, ts_code, confidence, reason, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(theme, ts_code) DO UPDATE SET
                confidence = excluded.confidence,
                reason     = excluded.reason,
                source     = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            rows,
        )

    return {"imported": len(rows), "themes": themes_seen, "skipped": skipped}


def get_theme_members(theme: str) -> list[str]:
    with get_connection() as conn:
        return [r[0] for r in conn.execute("SELECT ts_code FROM theme_members WHERE theme = ?", (theme,)).fetchall()]


def get_stock_themes(ts_code: str, active_only: bool = True) -> list[str]:
    """某只票归属的主线列表。"""
    sql = """
        SELECT m.theme FROM theme_members m
        JOIN themes t ON t.name = m.theme
        WHERE m.ts_code = ? {extra}
        ORDER BY m.confidence DESC
    """.format(extra="AND t.active = 1" if active_only else "")
    with get_connection() as conn:
        return [r[0] for r in conn.execute(sql, (ts_code,)).fetchall()]


# ==================== 强度计算 ====================


def _recent_trade_dates(conn, trade_date: str, count: int) -> list[str]:
    """取截至 trade_date（含）的最近 count 个有行情的交易日，由旧到新。"""
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM daily_kline
        WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?
        """,
        (trade_date, count),
    ).fetchall()
    return sorted(str(r[0]) for r in rows)


def _load_window_stats(trade_date: str, lookback: int) -> tuple[list[str], dict[str, dict[str, float]]]:
    """一次性拉出窗口内每只票的聚合统计。

    量比要用各票自己的 5 日均量，所以 SQL 得多读 5 个交易日的前置数据才能让
    窗口函数在窗口首日也算得出均量；外层再过滤回统计窗口。

    Returns:
        (窗口交易日列表, {ts_code: {cum_pct, limit_up, attack, amount, bars}})
    """
    with get_connection() as conn:
        window = _recent_trade_dates(conn, trade_date, lookback)
        if not window:
            return [], {}
        # 前置 5 根用于算均量；再多要 1 根是因为窗口是 [5 PRECEDING, 1 PRECEDING]
        padded = _recent_trade_dates(conn, trade_date, lookback + 6)
        floor_date = padded[0]
        win_start = window[0]

        rows = conn.execute(
            f"""
            WITH w AS (
                SELECT ts_code, trade_date, vol, pct_chg, amount,
                       COALESCE(is_limit_up, 0) AS lu,
                       AVG(vol) OVER (
                           PARTITION BY ts_code ORDER BY trade_date
                           ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                       ) AS vma5
                FROM daily_kline
                WHERE trade_date >= ? AND trade_date <= ?
            )
            SELECT ts_code,
                   COUNT(*)                                     AS bars,
                   SUM(COALESCE(pct_chg, 0))                    AS cum_pct,
                   SUM(lu)                                      AS limit_up,
                   SUM(CASE WHEN vma5 > 0 AND vol / vma5 > {_ATTACK_VOL_RATIO}
                             AND pct_chg > {_ATTACK_PCT_CHG} THEN 1 ELSE 0 END) AS attack,
                   SUM(COALESCE(amount, 0))                     AS amount
            FROM w
            WHERE trade_date >= ?
            GROUP BY ts_code
            """,
            (floor_date, trade_date, win_start),
        ).fetchall()

    stats = {
        str(r[0]): {
            "bars": int(r[1] or 0),
            "cum_pct": float(r[2] or 0.0),
            "limit_up": int(r[3] or 0),
            "attack": int(r[4] or 0),
            "amount": float(r[5] or 0.0),
        }
        for r in rows
    }
    return window, stats


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def compute_group_strength(
    name: str,
    kind: str,
    members: Sequence[str],
    stats: dict[str, dict[str, float]],
    lookback: int,
) -> GroupStrength | None:
    """算一个股票分组的原始面板值（定标前）。

    strength 字段在这一步还没有意义——它要等 :func:`_calibrate` 拿到参照系
    分布后才填。members 里没有行情数据的票直接忽略。
    """
    present = [c for c in members if c in stats]
    if len(present) < _MIN_MEMBERS:
        return None

    cum = [stats[c]["cum_pct"] for c in present]
    n = len(present)
    up_ratio = sum(1 for v in cum if v > 0) / n * 100
    limit_up = sum(int(stats[c]["limit_up"]) for c in present)
    attack = sum(int(stats[c]["attack"]) for c in present)
    amount = sum(stats[c]["amount"] for c in present)

    # 涨停率/攻击率的分母是"股票 × 交易日"，这样不同窗口长度之间可比
    slots = n * max(1, lookback)

    return GroupStrength(
        name=name,
        kind=kind,
        member_count=n,
        slots=slots,
        median_pct_chg=_median(cum),
        up_ratio=up_ratio,
        limit_up_count=limit_up,
        attack_count=attack,
        amount_per_stock=amount / n,
        # 行情面用累计涨幅中位数：中位数抗极端值，一两只妖股拉不动整条主线的读数。
        # 行情面不做收缩——中位数是无偏的，小样本只是噪音大，没有系统性方向。
        momentum_raw=_median(cum),
        # sentiment_raw 留待 _shrink_sentiment 用全市场基准填（此处填不了，
        # 那时才知道市场平均涨停率是多少）
        members=present,
    )


def _shrink_sentiment(groups: Iterable[GroupStrength], market: GroupStrength | None) -> None:
    """用全市场基准把各组的情绪面指标向均值收缩（就地修改 groups）。

    涨停/放量攻击都是稀有事件，小样本组"零命中"是常态而非弱势证据。
    这里给每组补 _SENTIMENT_PRIOR_SLOTS 个按全市场平均表现的虚拟样本，
    样本量越小、结论越贴近市场基准，样本量越大、越按自身数据说话。
    """
    if market and market.slots > 0:
        prior_limit = market.limit_up_count / market.slots
        prior_attack = market.attack_count / market.slots
    else:
        prior_limit = prior_attack = 0.0

    for g in groups:
        denom = g.slots + _SENTIMENT_PRIOR_SLOTS
        if denom <= 0:
            g.sentiment_raw = 0.0
            continue
        limit_rate = (g.limit_up_count + prior_limit * _SENTIMENT_PRIOR_SLOTS) / denom * 100
        attack_rate = (g.attack_count + prior_attack * _SENTIMENT_PRIOR_SLOTS) / denom * 100
        g.sentiment_raw = limit_rate * _LIMIT_WEIGHT + attack_rate * _ATTACK_WEIGHT


def _percentile_of(value: float, reference: Sequence[float]) -> float:
    """value 在 reference 分布中的百分位（0-100）。

    用"小于 value 的个数 + 等于的一半"的中点法，避免并列值全部被判成 0 或 100。
    参照系为空时退回 50（无从比较即视为中位水平）。
    """
    n = len(reference)
    if n == 0:
        return 50.0
    below = sum(1 for v in reference if v < value)
    equal = sum(1 for v in reference if v == value)
    return (below + equal / 2) / n * 100


def _calibrate(groups: Iterable[GroupStrength], reference: Sequence[GroupStrength]) -> None:
    """用参照系分布把原始面板值映射成 0-100 强度（就地修改 groups）。"""
    mom_ref = [g.momentum_raw for g in reference]
    sen_ref = [g.sentiment_raw for g in reference]
    for g in groups:
        g.momentum_pct = _percentile_of(g.momentum_raw, mom_ref)
        g.sentiment_pct = _percentile_of(g.sentiment_raw, sen_ref)
        g.strength = g.momentum_pct * _MOMENTUM_WEIGHT + g.sentiment_pct * _SENTIMENT_WEIGHT


def rank_themes(
    trade_date: str,
    lookback: int = DEFAULT_LOOKBACK,
    *,
    include_industry: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    """计算并排序所有主线（及行业）的强度，落库 theme_strength。

    Args:
        trade_date: 目标交易日 YYYYMMDD
        lookback: 统计窗口交易日数
        include_industry: 是否同时给 stock_basic.industry 行业分类排名。
            行业排名的用处是给没有任何主线归属的票兜底，也作为主线强度的参照系。
        persist: False 时只算不写库（测试/预览用）

    Returns:
        {"trade_date", "lookback", "window", "market": {...},
         "themes": [...], "industries": [...], "written": int}
    """
    window, stats = _load_window_stats(trade_date, lookback)
    if not window:
        return {
            "trade_date": trade_date,
            "lookback": lookback,
            "window": [],
            "market": None,
            "themes": [],
            "industries": [],
            "dropped_themes": [],
            "written": 0,
            "reason": f"{trade_date} 及之前没有任何日线数据",
        }

    market = compute_group_strength(MARKET_BASELINE, "market", list(stats.keys()), stats, lookback)

    with get_connection() as conn:
        theme_rows = conn.execute(
            """
            SELECT m.theme, m.ts_code FROM theme_members m
            JOIN themes t ON t.name = m.theme
            WHERE t.active = 1
            """
        ).fetchall()
        industry_rows = (
            conn.execute(
                "SELECT industry, ts_code FROM stock_basic WHERE industry IS NOT NULL AND industry != ''"
            ).fetchall()
            if include_industry
            else []
        )

    def _group(rows) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key, code in rows:
            out.setdefault(str(key), []).append(str(code))
        return out

    # 成员太少的组会被 compute_group_strength 判为不可信而丢弃。主线是用户
    # 手工维护的，被丢掉却不吭声，用户会以为"导入了就在算"——必须报出来。
    dropped: list[str] = []

    def _measure(groups: dict[str, list[str]], kind: str, report_dropped: bool) -> list[GroupStrength]:
        out: list[GroupStrength] = []
        for name, members in groups.items():
            g = compute_group_strength(name, kind, members, stats, lookback)
            if g is None:
                if report_dropped:
                    present = sum(1 for c in members if c in stats)
                    dropped.append(f"{name}（有行情的成员 {present} 只，少于 {_MIN_MEMBERS} 只，无法可靠统计）")
                continue
            out.append(g)
        return out

    themes = _measure(_group(theme_rows), "theme", report_dropped=True)
    industries = _measure(_group(industry_rows), "industry", report_dropped=False)

    # ── 情绪面收缩（必须在定标之前：定标要拿收缩后的值排百分位）──
    _shrink_sentiment(themes + industries + ([market] if market else []), market)

    # ── 定标 ──
    # 参照系固定为"成员数足够多的行业分类"：它覆盖全市场、每天都在、组数稳定在
    # 90 个上下，是一把不随主线增删而伸缩的尺子。若行业数据缺失（例如测试库里
    # 没有 stock_basic），退回用主线自身做参照——此时强度只反映主线之间的相对
    # 位次，不再有"对比行业中位数"的含义。
    reference = [g for g in industries if g.member_count >= _REFERENCE_MIN_MEMBERS] or industries or themes
    _calibrate(themes + industries + ([market] if market else []), reference)

    market_strength = market.strength if market else 50.0

    def _rank_in_place(groups: list[GroupStrength]) -> list[GroupStrength]:
        groups.sort(key=lambda g: g.strength, reverse=True)
        for i, g in enumerate(groups, start=1):
            g.rank = i
            g.excess = g.strength - market_strength
        return groups

    _rank_in_place(themes)
    _rank_in_place(industries)
    if market:
        market.excess = 0.0
        market.rank = 0

    written = 0
    if persist:
        records = [g.as_row(trade_date, lookback) for g in ([market] if market else []) + themes + industries]
        with get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO theme_strength
                (trade_date, theme, kind, lookback, member_count, median_pct_chg, up_ratio,
                 limit_up_count, attack_count, amount_per_stock, strength, excess, rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
        written = len(records)

    return {
        "trade_date": trade_date,
        "lookback": lookback,
        "window": window,
        "market": market,
        "themes": themes,
        "industries": industries,
        "dropped_themes": dropped,
        "written": written,
    }


def get_stock_theme_strength(
    ts_code: str,
    trade_date: str,
    lookback: int = DEFAULT_LOOKBACK,
) -> dict[str, Any] | None:
    """取某只票所属分组里**最强的那条主线**的强度快照。

    优先用用户定义的主线；该票没有任何主线归属时，退回它的行业分类
    ——行业不是"炒作主线"，但至少反映所在板块的冷热，聊胜于无，
    返回值里的 kind 字段会如实标明是哪一种，调用方可据此决定权重。

    快照日期同样**不要求精确匹配**，取不晚于 trade_date 的最近一期。
    个股的数据日会因同步失败而落后于编排器的目标日，精确匹配会查不到、
    把主线分静默清零——那比用一期稍旧的主线强度更糟，因为它看起来像
    "这条主线中性"，而不是"没查到"。返回值里的 snapshot_date 标明实际用了哪一期。
    """
    with get_connection() as conn:
        snapshot = conn.execute(
            "SELECT MAX(trade_date) FROM theme_strength WHERE trade_date <= ? AND lookback = ?",
            (trade_date, lookback),
        ).fetchone()[0]
        if not snapshot:
            return None

        row = conn.execute(
            """
            SELECT s.theme, s.kind, s.strength, s.excess, s.rank, s.member_count,
                   s.median_pct_chg, s.limit_up_count, s.attack_count
            FROM theme_strength s
            JOIN theme_members m ON m.theme = s.theme AND m.ts_code = ?
            WHERE s.trade_date = ? AND s.lookback = ? AND s.kind = 'theme'
            ORDER BY s.strength DESC LIMIT 1
            """,
            (ts_code, snapshot, lookback),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT s.theme, s.kind, s.strength, s.excess, s.rank, s.member_count,
                       s.median_pct_chg, s.limit_up_count, s.attack_count
                FROM theme_strength s
                JOIN stock_basic b ON b.industry = s.theme AND b.ts_code = ?
                WHERE s.trade_date = ? AND s.lookback = ? AND s.kind = 'industry'
                LIMIT 1
                """,
                (ts_code, snapshot, lookback),
            ).fetchone()
        if row is None:
            return None

        total = conn.execute(
            "SELECT COUNT(*) FROM theme_strength WHERE trade_date = ? AND lookback = ? AND kind = ?",
            (snapshot, lookback, row[1]),
        ).fetchone()[0]

    return {
        "theme": row[0],
        "kind": row[1],
        "snapshot_date": str(snapshot),
        "strength": float(row[2] or 0),
        "excess": float(row[3] or 0),
        "rank": int(row[4] or 0),
        "total": int(total or 0),
        "member_count": int(row[5] or 0),
        "median_pct_chg": float(row[6] or 0),
        "limit_up_count": int(row[7] or 0),
        "attack_count": int(row[8] or 0),
    }


def format_theme_ranking(result: dict[str, Any], limit: int = 15) -> str:
    """把 rank_themes 的返回值渲染成人类可读排行榜。"""
    lines: list[str] = []
    window = result.get("window") or []
    lines.append("=" * 78)
    lines.append(
        f"主线强度排行  {result.get('trade_date', '')}  "
        f"窗口={result.get('lookback', 0)}个交易日"
        + (f" ({window[0]}~{window[-1]})" if window else "")
    )
    lines.append("=" * 78)

    market = result.get("market")
    if market is not None:
        lines.append(
            f"全市场基准: 强度={market.strength:.2f}  上涨占比={market.up_ratio:.1f}%  "
            f"涨停={market.limit_up_count}  攻击={market.attack_count}  样本={market.member_count}"
        )
    if result.get("reason"):
        lines.append(f"说明: {result['reason']}")

    header = f"{'#':>3} {'名称':<16} {'强度':>7} {'超额':>7} {'涨幅中位':>9} {'上涨%':>7} {'涨停':>5} {'攻击':>5} {'成员':>5}"

    def _block(title: str, groups: list) -> None:
        if not groups:
            return
        lines.append(f"\n【{title}】")
        lines.append(header)
        for g in groups[:limit]:
            lines.append(
                f"{g.rank:>3} {g.name:<16} {g.strength:>7.2f} {g.excess:>+7.2f} "
                f"{g.median_pct_chg:>+9.2f} {g.up_ratio:>7.1f} {g.limit_up_count:>5} "
                f"{g.attack_count:>5} {g.member_count:>5}"
            )

    _block("用户定义主线", result.get("themes") or [])
    _block(f"行业分类（参照系，TOP {limit}）", result.get("industries") or [])

    dropped = result.get("dropped_themes") or []
    if dropped:
        lines.append("\n【已跳过的主线】")
        lines.extend(f"  ! {d}" for d in dropped)

    if not (result.get("themes") or []):
        lines.append("\n! 尚未导入任何主线成员。用 `zt theme import <json>` 导入外部判定器的产出。")

    return "\n".join(lines)
