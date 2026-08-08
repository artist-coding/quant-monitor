"""
自选股观察池模块
支持批量监控、每日报告、信号提醒、破位预警
"""

import logging
import re
from typing import Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

# dotenv 加载已移至 modules/__init__.py（包级别一次性加载）

from .database import add_watchlist_item, remove_watchlist_item, get_watchlist, get_connection
from .indicators import analyze_stock
from .strategies import detect_all_strategies, StrategyType

logger = logging.getLogger(__name__)

_CODE_PATTERN = re.compile(r"^(?:(SH|SZ|BJ)[.\s-]*)?(\d{6})(?:[.\s-]*(SH|SZ|BJ))?$", re.IGNORECASE)


def normalize_ts_code(value: str) -> str:
    """把 ``600487``、``SH600487`` 等输入统一为 ``600487.SH``。"""
    text = (value or "").strip().upper()
    match = _CODE_PATTERN.fullmatch(text)
    if not match:
        raise ValueError("请输入有效的 6 位 A 股代码，如 600487 或 600487.SH")
    prefix, code, suffix = match.groups()
    exchange = (prefix or suffix or "").upper()
    inferred = "SH" if code.startswith("6") else "BJ" if code.startswith(("4", "8", "9")) else "SZ"
    if exchange and exchange != inferred:
        raise ValueError(f"股票代码与交易所不匹配，建议使用 {code}.{inferred}")
    return f"{code}.{inferred}"


def _lookup_stock_name(ts_code: str) -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT name FROM stock_basic WHERE ts_code = ?", (ts_code,)).fetchone()
    return str(row["name"] or "") if row else ""


# 只有落在最近 N 根 K 线内的战法信号才值得当成"今天要处理的事"。
# 用 K 线根数而不是自然日：停牌、长假都不会误伤。
_SIGNAL_FRESH_BARS = 5


def _recent_trade_dates(ts_code: str, bars: int = _SIGNAL_FRESH_BARS) -> set[str]:
    """取该票最近 bars 个交易日的日期集合；库里没有 K 线时返回空集。"""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT trade_date FROM daily_kline WHERE ts_code = ? ORDER BY trade_date DESC LIMIT ?",
                (ts_code, bars),
            ).fetchall()
        return {str(r["trade_date"]) for r in rows}
    except Exception:
        return set()


@dataclass
class WatchAlert:
    """观察警报"""

    ts_code: str
    name: str
    alert_type: str  # B1/B2/BREAK/EXIT/ABNORMAL
    level: str  # INFO/WARNING/CRITICAL
    message: str
    data: dict[str, Any] = field(default_factory=dict)


def add_watch(ts_code: str, name: str = "", tags: str = "", notes: str = "") -> int:
    """添加自选股"""
    canonical = normalize_ts_code(ts_code)
    resolved_name = name.strip() or _lookup_stock_name(canonical)
    return add_watchlist_item(canonical, name=resolved_name, tags=tags, notes=notes)


def remove_watch(ts_code: str) -> bool:
    """移除自选股"""
    return remove_watchlist_item(normalize_ts_code(ts_code))


def list_watch(tags: str | None = None) -> list[dict]:
    """列出自选股"""
    watches = get_watchlist(tags=tags)
    migrated = False
    for item in watches:
        raw_code = item.get("ts_code", "")
        try:
            canonical = normalize_ts_code(raw_code)
        except ValueError:
            continue
        if canonical == raw_code:
            continue
        add_watchlist_item(
            canonical,
            name=item.get("name", "") or _lookup_stock_name(canonical),
            tags=item.get("tags", ""),
            notes=item.get("notes", ""),
        )
        remove_watchlist_item(raw_code)
        migrated = True
    return get_watchlist(tags=tags) if migrated else watches


def scan_watchlist(tags: str | None = None) -> dict[str, Any]:
    """
    批量扫描自选股池

    Returns:
        {"alerts": [...], "summary": {...}}
    """
    watches = list_watch(tags=tags)
    alerts = []
    summary = {
        "total": len(watches),
        "b1_count": 0,
        "b2_count": 0,
        "exit_count": 0,
        "break_count": 0,
        "abnormal_count": 0,
        # 被新鲜度闸门过滤掉的陈旧信号数：不进告警，但要让人看得见它存在
        # （大量陈旧信号通常意味着该票的 K 线有断档，需要回补数据）
        "stale_count": 0,
    }

    for w in watches:
        ts_code = w["ts_code"]
        name = w.get("name", ts_code)

        # 指标分析
        try:
            ind = analyze_stock(ts_code, days=60)
        except Exception as e:
            logger.warning(f"指标分析失败 {ts_code}: {e}")
            continue

        # 战法信号
        try:
            signals = detect_all_strategies(ts_code, days=60)
        except Exception as e:
            logger.warning(f"战法信号检测失败 {ts_code}: {e}")
            signals = []

        # 信号新鲜度闸门：detect_all_strategies 返回的是整段历史里的信号，
        # 一只 K 线有断档的票可能把几年前的信号排在最前面。把 2019 年的"逃顶"
        # 当成 CRITICAL 推送出去，比不报还糟。
        # 库里查不到 K 线时（测试环境 / 新票）返回空集，此时不设闸门，避免误杀全部信号。
        fresh_dates = _recent_trade_dates(ts_code)
        if fresh_dates:
            fresh_signals = [s for s in signals if s.trade_date in fresh_dates]
            stale_count = len(signals) - len(fresh_signals)
            if stale_count:
                summary["stale_count"] += stale_count
                logger.debug("%s 过滤掉 %s 条超过 %s 根K线的陈旧信号", ts_code, stale_count, _SIGNAL_FRESH_BARS)
            signals = fresh_signals

        # 1. 买点信号提醒（B1/B2）：只取最近 3 条，避免刷屏
        buy_signals = [s for s in signals if s.strategy in (StrategyType.B1, StrategyType.B2) and s.action == "BUY"]
        for s in buy_signals[:3]:
            if s.strategy == StrategyType.B1:
                alerts.append(
                    WatchAlert(
                        ts_code=ts_code,
                        name=name,
                        alert_type="B1",
                        level="INFO",
                        message=f"{s.trade_date} 出现B1买点 J={s.details.get('j', 0):.1f}",
                        data={"signal": s},
                    )
                )
                summary["b1_count"] += 1
            else:
                alerts.append(
                    WatchAlert(
                        ts_code=ts_code,
                        name=name,
                        alert_type="B2",
                        level="INFO",
                        message=f"{s.trade_date} 出现B2确认 涨{s.details.get('pct_chg', 0):.1f}%",
                        data={"signal": s},
                    )
                )
                summary["b2_count"] += 1

        # 1b. 逃顶信号（S1/S2/S3）：CRITICAL 级别，全量扫描不截断
        # detect_all_strategies 返回的是按日期倒序的最多 30 条混合信号，
        # 逃顶信号完全可能排在第 4 条之后；漏报逃顶的代价远高于多报几条。
        # 消息里必须带上信号日期：同一战法在不同交易日各触发一次是常态，
        # 不带日期的话多条预警长得一模一样，看起来像重复刷屏。
        seen_exits: set[tuple[str, str]] = set()
        for s in signals:
            if s.strategy in (StrategyType.S1, StrategyType.S2, StrategyType.S3):
                key = (s.strategy.value, s.trade_date)
                if key in seen_exits:
                    continue
                seen_exits.add(key)
                alerts.append(
                    WatchAlert(
                        ts_code=ts_code,
                        name=name,
                        alert_type="EXIT",
                        level="CRITICAL",
                        message=f"{s.trade_date} {s.strategy.value}逃顶信号",
                        data={"signal": s},
                    )
                )
                summary["exit_count"] += 1

        # 2. 破位预警（破白线/黄线/BBI）
        if ind.is_dead_cross:
            alerts.append(
                WatchAlert(
                    ts_code=ts_code,
                    name=name,
                    alert_type="BREAK",
                    level="WARNING",
                    message="白线死叉黄线，趋势走坏",
                    data={"white": ind.zg_white, "yellow": ind.dg_yellow},
                )
            )
            summary["break_count"] += 1
        # ind.close 为 0 说明 analyze_stock 无数据返回了空结果，此时不能判破位（0 < bbi*0.95 恒成立）
        elif ind.bbi > 0 and ind.close > 0 and ind.close < ind.bbi * 0.95:
            alerts.append(
                WatchAlert(
                    ts_code=ts_code,
                    name=name,
                    alert_type="BREAK",
                    level="WARNING",
                    message="跌破BBI",
                    data={"bbi": ind.bbi},
                )
            )
            summary["break_count"] += 1

        # 3. 异动检测（量比 > 3 或涨跌幅 > 5%），message 区分触发原因
        vol_abnormal = ind.vol_ratio > 3
        pct_abnormal = abs(ind.pct_chg) > 5
        if vol_abnormal or pct_abnormal:
            reasons = []
            if vol_abnormal:
                reasons.append(f"量比{ind.vol_ratio:.1f}")
            if pct_abnormal:
                reasons.append(f"涨跌幅{ind.pct_chg:+.2f}%")
            alerts.append(
                WatchAlert(
                    ts_code=ts_code,
                    name=name,
                    alert_type="ABNORMAL",
                    level="INFO",
                    message="异动 " + " ".join(reasons),
                    data={"vol_ratio": ind.vol_ratio, "pct_chg": ind.pct_chg},
                )
            )
            summary["abnormal_count"] += 1

    return {
        "alerts": alerts,
        "summary": summary,
    }


def generate_daily_report(tags: str | None = None) -> str:
    """
    生成每日观察报告（文本格式）
    """
    result = scan_watchlist(tags=tags)
    alerts = result["alerts"]
    summary = result["summary"]

    today = datetime.now().strftime("%Y-%m-%d")
    lines = []
    lines.append(f"{'=' * 60}")
    lines.append(f"自选股每日观察报告  {today}")
    lines.append(f"{'=' * 60}")
    lines.append(f"监控总数: {summary['total']}只")
    lines.append(f"B1信号: {summary['b1_count']}只 | B2信号: {summary['b2_count']}只")
    lines.append(f"逃顶信号: {summary['exit_count']}只 | 破位预警: {summary['break_count']}只")
    lines.append(f"异动: {summary['abnormal_count']}只")
    lines.append("")

    level_emoji = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"}
    type_labels = {"B1": "【买点】", "B2": "【买点】", "EXIT": "【逃顶】", "BREAK": "【破位】", "ABNORMAL": "【异动】"}

    for a in alerts:
        emoji = level_emoji.get(a.level, "")
        label = type_labels.get(a.alert_type, "")
        lines.append(f"{emoji} {label} {a.ts_code} {a.name}")
        lines.append(f"   {a.message}")

    if not alerts:
        lines.append("今日无特别信号，继续观察。")

    lines.append(f"{'=' * 60}")
    return "\n".join(lines)


# ==================== 命令行工具 ====================


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="Z哥 自选股观察池")
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="添加自选股")
    p_add.add_argument("ts_code", help="股票代码")
    p_add.add_argument("--name", default="", help="股票名称")
    p_add.add_argument("--tags", default="", help="标签，如 波段/短线")
    p_add.add_argument("--notes", default="", help="备注")

    # remove
    p_remove = sub.add_parser("remove", help="移除自选股")
    p_remove.add_argument("ts_code", help="股票代码")

    # list
    p_list = sub.add_parser("list", help="列出自选股")
    p_list.add_argument("--tags", default=None, help="按标签筛选")

    # scan
    p_scan = sub.add_parser("scan", help="扫描自选股池")
    p_scan.add_argument("--tags", default=None, help="按标签筛选")

    # report
    p_report = sub.add_parser("report", help="生成每日报告")
    p_report.add_argument("--tags", default=None, help="按标签筛选")

    args = parser.parse_args()

    if args.command == "add":
        wid = add_watch(args.ts_code, name=args.name, tags=args.tags, notes=args.notes)
        print(f"已添加: {args.ts_code} (ID={wid})")

    elif args.command == "remove":
        if remove_watch(args.ts_code):
            print(f"已移除: {args.ts_code}")
        else:
            print(f"未找到: {args.ts_code}")

    elif args.command == "list":
        watches = list_watch(tags=args.tags)
        print(f"{'=' * 60}")
        print(f"自选股列表 (共{len(watches)}只)")
        print(f"{'=' * 60}")
        for w in watches:
            tags_str = f" [{w['tags']}]" if w["tags"] else ""
            print(f"  {w['ts_code']:<12} {w.get('name', ''):<8}{tags_str}")

    elif args.command == "scan":
        result = scan_watchlist(tags=args.tags)
        print(f"扫描完成: {result['summary']}")
        for a in result["alerts"][:20]:
            print(f"  [{a.alert_type}] {a.ts_code} {a.message}")

    elif args.command == "report":
        print(generate_daily_report(tags=args.tags))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
