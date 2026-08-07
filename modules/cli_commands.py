#!/usr/bin/env python3
"""
CLI 扩展命令模块（配合 cli.py 使用）

提供的命令：
  - backtest  : 多策略融合 / 组合回测（支持 JSON 输出）
  - trade     : 交易记录的增删查改
  - daily     : 每日五步工作流（观察池 + 选股 + 持仓检查 + 信号汇总 + 报告）
  - monitor   : 自选股主动预警与扫描推送

用法示例：
    python -m modules.cli backtest multi 600487.SH --days 250 --json
    python -m modules.cli trade add "4月25号买了100股茅台，1800块"
    python -m modules.cli daily --json
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from typing import Any, NoReturn

logger = logging.getLogger(__name__)


# ==================== 工具函数 ====================


def _json_output(data: Any) -> None:
    """将数据序列化为 JSON 并打印到 stdout"""
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _error(msg: str) -> NoReturn:
    """打印错误信息到 stderr 并退出"""
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(1)


def _warn(msg: str) -> None:
    """打印警告信息到 stderr"""
    print(f"警告: {msg}", file=sys.stderr)


# ==================== 1. cmd_backtest ====================


def _portfolio_result_to_dict(result: Any) -> dict:
    """
    将 PortfolioBacktestResult 转换为可序列化的字典

    包含资金曲线摘要（不输出完整 equity_curve 以控制体积）
    """
    trades = []
    for t in result.trades:
        trades.append(
            {
                "ts_code": t.ts_code,
                "entry_date": t.entry_date,
                "entry_price": round(t.entry_price, 2),
                "exit_date": t.exit_date,
                "exit_price": round(t.exit_price, 2) if t.exit_price else None,
                "exit_reason": t.exit_reason,
                "pnl_pct": round(t.pnl_pct * 100, 2),
            }
        )

    return {
        "initial_capital": result.initial_capital,
        "final_value": round(result.final_value, 2),
        "total_return": round(result.total_return * 100, 2),
        "annualized_return": round(result.annualized_return * 100, 2),
        "sharpe_ratio": round(result.sharpe_ratio, 2),
        "max_drawdown": round(result.max_drawdown * 100, 2),
        "win_rate": round(result.win_rate, 3),
        "profit_factor": round(result.profit_factor, 2),
        "total_trades": result.total_trades,
        "trades": trades,
    }


def cmd_backtest(args) -> None:
    """
    回测命令

    子命令：
        multi    <ts_code>  [--days N] [--json]          多策略融合回测
        portfolio <c1,c2,..> [--days N] [--json]         组合回测

    示例：
        zt backtest multi 600487.SH --days 120 --json
        zt backtest portfolio 600487.SH,601318.SH --days 120 --json
    """
    sub = getattr(args, "backtest_sub", None)
    use_json = getattr(args, "json", False)
    days = getattr(args, "days", 250)

    if not sub:
        _error("请指定回测子命令: multi / portfolio")

    ts_code = getattr(args, "ts_code", None)

    # ── multi: 多策略融合回测 ──
    if sub == "multi":
        if not ts_code:
            _error("请指定股票代码，如: backtest multi 600487.SH")

        from .backtest import backtest_multi_strategy

        # --strategy 参数暂不传给底层（底层用全部策略融合）
        # 未来可扩展为按策略过滤
        result_multi = backtest_multi_strategy(ts_code, days=days)

        if result_multi.total_trades == 0:
            _warn(f"{ts_code} 在 {days} 天内无交易记录")

        if use_json:
            _json_output(_portfolio_result_to_dict(result_multi))
        else:
            print(result_multi.summary())

    # ── portfolio: 组合回测 ──
    elif sub == "portfolio":
        codes_str = getattr(args, "codes", None)
        if not codes_str:
            _error("请指定股票代码列表（逗号分隔），如: backtest portfolio 600487.SH,601318.SH")

        ts_codes = [c.strip() for c in codes_str.split(",") if c.strip()]
        if not ts_codes:
            _error("股票代码列表为空")

        from .backtest import backtest_portfolio

        stock_configs = [{"ts_code": code} for code in ts_codes]
        result_port = backtest_portfolio(stock_configs, days=days)

        if use_json:
            _json_output(_portfolio_result_to_dict(result_port))
        else:
            print(result_port.summary())

    else:
        _error(f"未知回测子命令: {sub}")


# ==================== 2. cmd_trade ====================


def cmd_trade(args) -> None:
    """
    交易记录管理命令

    子命令：
        add   "口语化交易描述"           解析并保存交易记录
        list  [--json]                   列出最近交易记录
        stats [--json]                   交易统计摘要

    示例：
        zt trade add "4月25号买了100股茅台，1800块"
        zt trade list --json
        zt trade stats --json
    """
    sub = getattr(args, "trade_sub", None)
    use_json = getattr(args, "json", False)

    if not sub:
        _error("请指定交易子命令: add / list / stats")

    # ── add: 解析并保存交易 ──
    if sub == "add":
        text = getattr(args, "text", None)
        if not text:
            _error('请输入交易描述，如: trade add "4月25号买了100股茅台，1800块"')

        from .trade_parser import TradeParser
        from .trade_manager import TradeManager

        parser = TradeParser()
        result = parser.parse(text)

        if not result.success:
            _error(f"解析失败: {result.error_message}")

        data = result.data
        if not data:
            _error("解析结果为空")

        # 展示解析结果
        if use_json:
            _json_output(
                {
                    "parsed": data,
                    "confidence": result.confidence,
                    "missing_fields": result.missing_fields,
                }
            )
            return

        # 文本模式：显示解析确认
        confirm_msg = parser.generate_confirm_message(data)
        print(confirm_msg)
        print(f"  置信度: {result.confidence:.0%}")

        if result.missing_fields:
            print(f"  缺失字段: {', '.join(result.missing_fields)}")

        # 检查必填字段
        required = ["ts_code", "action", "price", "quantity"]
        missing_required = [f for f in required if f not in data or not data.get(f)]
        if missing_required:
            _warn(f"缺少必填字段 {missing_required}，无法保存。请补充后重试。")
            return

        # 自动补充金额
        if "amount" not in data and data.get("price") and data.get("quantity"):
            data["amount"] = round(float(data["price"]) * int(data["quantity"]), 2)

        # 保存到数据库
        manager = TradeManager()
        trade_id = manager.add_trade(data)
        print(f"\n已保存交易记录 (ID={trade_id})")

    # ── list: 列出交易记录 ──
    elif sub == "list":
        from .trade_manager import TradeManager

        manager = TradeManager()
        limit = getattr(args, "limit", 20)
        trades = manager.get_recent_trades(limit=limit)

        if use_json:
            _json_output(trades)
        else:
            if not trades:
                print("暂无交易记录")
                return
            print(f"\n最近 {len(trades)} 条交易记录:")
            print(f"{'=' * 70}")
            for t in trades:
                action_text = "买入" if t.get("action") == "BUY" else "卖出"
                print(
                    f"  [{t.get('id', '?'):>3}] {t.get('trade_date', '?')}"
                    f"  {action_text}  {t.get('ts_code', '?')}"
                    f"  {t.get('quantity', 0)}股 @ {t.get('price', 0)}元"
                )
            print(f"{'=' * 70}")

    # ── stats: 交易统计 ──
    elif sub == "stats":
        from .trade_manager import TradeManager

        manager = TradeManager()
        summary = manager.get_summary()
        pnl = manager.calculate_pnl()

        stats = {
            "summary": summary,
            "pnl": pnl,
        }

        if use_json:
            _json_output(stats)
        else:
            print(f"\n{'=' * 60}")
            print("交易统计摘要")
            print(f"{'=' * 60}")
            print(f"  买入总额:   {pnl.get('buy_total', 0):,.2f} 元")
            print(f"  卖出总额:   {pnl.get('sell_total', 0):,.2f} 元")
            print(f"  净投入:     {pnl.get('net_invested', 0):,.2f} 元")
            print(f"  买入股数:   {pnl.get('buy_qty', 0)}")
            print(f"  卖出股数:   {pnl.get('sell_qty', 0)}")
            print(f"  当前持仓:   {pnl.get('current_qty', 0)}")
            print(f"  已实现盈亏: {pnl.get('realized_pnl', 0):,.2f} 元")
            print(f"{'=' * 60}")

    else:
        _error(f"未知交易子命令: {sub}")


# ==================== 3. cmd_daily ====================


def cmd_daily(args) -> None:
    """每日五步工作流：观察池扫描 → 选股 → 持仓诊断 → 信号汇总 → 日报

    拆分为 5 个独立步骤函数，每步独立 try/except，互不阻塞。
    """
    use_json = getattr(args, "json", False)
    today = datetime.now().strftime("%Y-%m-%d")

    report: dict[str, Any] = {
        "date": today,
        "watchlist_scan": [],
        "top_picks": [],
        "portfolio_status": [],
        "signals": [],
        "summary": "",
    }

    watches = _daily_step_watchlist(report)
    _daily_step_screener(report)
    _daily_step_portfolio(report, watches)
    _daily_step_signals(report)
    _daily_step_summary(report)

    # ── 输出 ──
    if use_json:
        _json_output(report)
    else:
        _print_daily_report(report, today)


def _daily_step_watchlist(report: dict) -> list:
    """Step 1: 扫描观察池，返回 watchlist 列表供后续步骤使用"""
    try:
        from .watchlist import scan_watchlist, list_watch

        watches = list_watch()
        if not watches:
            report["watchlist_scan"] = {"total": 0, "alerts": []}
            return watches

        scan_result = scan_watchlist()
        alerts = scan_result.get("alerts", [])
        summary = scan_result.get("summary", {})

        watchlist_scan = {
            "total": summary.get("total", 0),
            "b1_count": summary.get("b1_count", 0),
            "b2_count": summary.get("b2_count", 0),
            "exit_count": summary.get("exit_count", 0),
            "break_count": summary.get("break_count", 0),
            "abnormal_count": summary.get("abnormal_count", 0),
            "alerts": [
                {
                    "ts_code": a.ts_code,
                    "name": a.name,
                    "alert_type": a.alert_type,
                    "level": a.level,
                    "message": a.message,
                }
                for a in alerts
            ],
        }
        report["watchlist_scan"] = watchlist_scan

        for a in alerts:
            if a.alert_type in ("B1", "B2", "EXIT"):
                report["signals"].append(
                    {
                        "ts_code": a.ts_code,
                        "name": a.name,
                        "signal": a.alert_type,
                        "message": a.message,
                        "source": "watchlist",
                    }
                )
        return watches
    except Exception as e:
        _warn(f"观察池扫描失败: {e}")
        report["watchlist_scan"] = {"error": str(e)}
        return []


def _daily_step_screener(report: dict) -> None:
    """Step 2: 全市场 B1 选股，取前 10"""
    try:
        from .screener import screen_stocks

        top_picks_raw = screen_stocks(criteria="b1", max_stocks=20)
        top_picks = []
        for s in top_picks_raw[:10]:
            pick = {
                "ts_code": s.ts_code,
                "name": s.name,
                "score": round(s.score, 1),
                "b1_score": round(s.b1_score, 1),
                "trend_score": round(s.trend_score, 1),
                "rating": s.rating,
            }
            top_picks.append(pick)
            if s.b1_score >= 50:
                report["signals"].append(
                    {
                        "ts_code": s.ts_code,
                        "name": s.name,
                        "signal": "B1",
                        "message": f"综合评分 {s.score:.0f}，B1评分 {s.b1_score:.0f}",
                        "source": "screener",
                    }
                )
        report["top_picks"] = top_picks
    except Exception as e:
        _warn(f"全市场选股失败: {e}")
        report["top_picks"] = {"error": str(e)}


def _daily_step_portfolio(report: dict, watches: list) -> None:
    """Step 3: 持仓快速诊断（前 5 只）"""
    try:
        from .portfolio_diagnosis import diagnose_stock

        check_codes: list[str] = []
        wl = report["watchlist_scan"]
        if isinstance(wl, dict):
            for a in wl.get("alerts", [])[:5]:
                if a["ts_code"] not in check_codes:
                    check_codes.append(a["ts_code"])
        if not check_codes and watches:
            check_codes = [w["ts_code"] for w in watches[:5]]

        portfolio_status = []
        for code in check_codes:
            try:
                diag = diagnose_stock(code, days=60)
                portfolio_status.append(
                    {
                        "ts_code": code,
                        "diagnosis": diag[:200] if isinstance(diag, str) else str(diag)[:200],
                    }
                )
            except Exception as e:
                portfolio_status.append({"ts_code": code, "error": str(e)})
        report["portfolio_status"] = portfolio_status
    except Exception as e:
        _warn(f"持仓检查失败: {e}")
        report["portfolio_status"] = {"error": str(e)}


def _daily_step_signals(report: dict) -> None:
    """Step 4: 信号去重"""
    seen: set[tuple] = set()
    unique: list = []
    for sig in report["signals"]:
        key = (sig["ts_code"], sig["signal"])
        if key not in seen:
            seen.add(key)
            unique.append(sig)
    report["signals"] = unique


def _daily_step_summary(report: dict) -> None:
    """Step 5: 生成摘要文本"""
    wl = report["watchlist_scan"]
    is_dict = isinstance(wl, dict)
    b1_count = wl.get("b1_count", 0) if is_dict else 0
    exit_count = wl.get("exit_count", 0) if is_dict else 0
    picks_count = len(report["top_picks"]) if isinstance(report["top_picks"], list) else 0
    sig_count = len(report["signals"])

    parts = [f"今日观察池 {wl.get('total', 0) if is_dict else 0} 只"]
    if b1_count:
        parts.append(f"出现 B1 信号 {b1_count} 只")
    if exit_count:
        parts.append(f"逃顶预警 {exit_count} 只")
    if picks_count:
        parts.append(f"全市场选出 {picks_count} 只潜力股")
    if sig_count:
        parts.append(f"共 {sig_count} 条信号待关注")
    if not any([b1_count, exit_count, picks_count]):
        parts.append("今日无特别信号，继续观察")

    report["summary"] = "，".join(parts) + "。"


def _print_daily_report(report: dict, today: str) -> None:
    """格式化打印每日报告"""
    wl = report["watchlist_scan"]
    print(f"\n{'=' * 60}")
    print(f"Z哥每日工作流报告  {today}")
    print(f"{'=' * 60}")
    print(f"\n{report['summary']}")

    if isinstance(wl, dict) and wl.get("alerts"):
        print(f"\n【观察池信号】({wl.get('total', 0)}只)")
        for a in wl["alerts"][:10]:
            print(f"  [{a['alert_type']}] {a['ts_code']} {a['name']}: {a['message']}")

    if isinstance(report["top_picks"], list) and report["top_picks"]:
        print("\n【B1 潜力股 TOP 10】")
        for i, p in enumerate(report["top_picks"], 1):
            print(
                f"  {i:2}. {p['ts_code']} {p['name']:<8} 评分:{p['score']:5.1f}  B1:{p['b1_score']:5.1f}  {p['rating']}"
            )

    if report["portfolio_status"]:
        print("\n【持仓诊断】")
        for p in report["portfolio_status"]:
            if "error" in p:
                print(f"  {p['ts_code']}: 诊断失败 - {p['error']}")
            else:
                print(f"  {p['ts_code']}: {p['diagnosis']}")

    if report["signals"]:
        print(f"\n【信号汇总】({len(report['signals'])}条)")
        for sig in report["signals"]:
            print(f"  [{sig['signal']}] {sig['ts_code']} {sig['name']}: {sig['message']}")

    print(f"\n{'=' * 60}")


def cmd_monitor(args):
    """自选股监控扫描命令行处理入口"""
    from modules.monitor import run_watchlist_monitor

    use_json = getattr(args, "json", False)
    enable_push = not getattr(args, "no_push", False)
    days = getattr(args, "days", 30)

    # 运行监控扫描
    res = run_watchlist_monitor(sync_days=days, enable_push=enable_push)

    if use_json:
        _json_output(res)
    else:
        # 非 JSON 输出时已经在 run_watchlist_monitor 内部写入了 Markdown 报告，打印简易提示
        print(f"自选股主动扫描监控完成。状态: {res['status']}, 警报总数: {res.get('alerts_count', 0)}")
        print("详细警报分析已输出至 data/reports/monitor_alert.md")
