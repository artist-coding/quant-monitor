#!/usr/bin/env python3
"""Z哥量化工具 CLI（v2.10.0 统一入口）

用法：
    python -m modules.cli analyze 600487.SH
    python -m modules.cli screen --strategy B1
    python -m modules.cli score 600487.SH
    python -m modules.cli workflow
    python -m modules.cli watchlist add 600487.SH --tags 通信设备
    python -m modules.cli diagnose 600487.SH
    python -m modules.cli sync init
    python -m modules.cli sync sync 600487.SH
    python -m modules.cli sync market-daily
    python -m modules.cli sync trade-cal --start 20260101 --end 20261231
    python -m modules.cli sync index --ts-code 000300.SH
    python -m modules.cli sync status
    python -m modules.cli sync stk-factor 600487.SH
    python -m modules.cli scan

设计：所有命令通过 `zt` entry point（已在 pyproject.toml 注册）暴露。
本文件取代 v2.9.0 散落在 5 个模块的独立 main()（screener / data_sync /
portfolio_diagnosis / watchlist / indicators.data_layer）。
"""

from __future__ import annotations

import argparse
import json
import sys
import os

# dotenv 加载已移至 modules/__init__.py（包级别一次性加载）


def _json_output(data):
    """Print data as JSON and exit."""
    print(json.dumps(data, ensure_ascii=False, indent=2))


# CLI 中文别名 → screener 英文 criteria 的统一映射
STRATEGY_ALIAS = {
    "B1": "b1",
    "B2": "b2_breakout",
    "B3": "b3_consensus",
    "完美图形": "perfect",
    "超级B1": "super_b1",
    "长安战法": "changan",
    "建仓波": "build_wave",
    "吸筹": "xishou",
    "安全": "safe",
    "超跌": "oversold",
    "突破": "breakout",
    "牵牛": "bull_rope",
    "牛绳": "bull_rope",
    "沙漏": "sandglass_perfect",
    "沙漏评分": "sandglass_perfect",
    "量比战法": "volume_ratio_super",
}

STRATEGY_CHOICES = list(STRATEGY_ALIAS.keys())


def _analyze_core(ts_code: str, days: int = 120) -> dict:
    """
    核心分析逻辑，返回所有分析结果的字典。
    cmd_analyze 和 cmd_score 共用此函数，避免重复计算。
    """
    from modules.indicators import analyze_stock
    from modules.indicators.data_layer import get_kline_data, DailyData
    from modules.strategies import detect_all_strategies
    from modules.portfolio_diagnosis import diagnose_stock
    from modules.screener import analyze_stock as screener_analyze

    # 1. 指标分析
    result = analyze_stock(ts_code, days=days)

    # 2. 主力阶段
    wave_data = None
    kirin_data = None
    try:
        from modules.indicators import detect_three_waves, detect_kirin_stage

        klines = get_kline_data(ts_code, days=days)
        if klines:
            daily_klines = []
            for i, k in enumerate(klines):
                prev_close = klines[i - 1].close if i > 0 else k.close
                daily_klines.append(
                    DailyData(
                        ts_code=k.ts_code,
                        trade_date=k.trade_date,
                        open=k.open,
                        high=k.high,
                        low=k.low,
                        close=k.close,
                        vol=k.vol,
                        amount=k.amount,
                        pct_chg=k.pct_chg,
                        prev_close=prev_close,
                    )
                )
            wave_data = detect_three_waves(daily_klines)
            kirin_data = detect_kirin_stage(daily_klines)
    except Exception:
        pass

    # 3. 策略信号
    signals = detect_all_strategies(ts_code, days=days)

    # 4. 诊断
    diagnosis = diagnose_stock(ts_code, days=days)

    # 5. screener 评分（复用已有数据，不再重复拉取）
    score = screener_analyze(ts_code)

    return {
        "ts_code": ts_code,
        "days": days,
        "result": result,
        "wave_data": wave_data,
        "kirin_data": kirin_data,
        "signals": signals,
        "diagnosis": diagnosis,
        "score": score,
    }


def cmd_analyze(args):
    """分析单只股票（指标 + 主力 + 战法 + 诊断 + 评分）"""
    core = _analyze_core(args.ts_code, args.days)

    ts_code = core["ts_code"]
    result = core["result"]
    wave_data = core["wave_data"]
    kirin_data = core["kirin_data"]
    signals = core["signals"]
    diagnosis = core["diagnosis"]
    score = core["score"]

    # ── JSON 输出 ──
    if args.json:
        json_result = {
            "ts_code": ts_code,
            "name": getattr(diagnosis, "name", ts_code),
            "price": getattr(diagnosis, "price", 0),
            "indicators": {
                "kdj": {"k": result.k, "d": result.d, "j": result.j},
                "macd": {
                    "dif": result.dif,
                    "dea": result.dea,
                    "hist": result.macd_hist,
                    "veto": getattr(diagnosis, "macd_veto", False),
                },
                "bbi": result.bbi,
                "white_line": getattr(diagnosis, "white_line", 0),
                "yellow_line": getattr(diagnosis, "yellow_line", 0),
                "rsi": {"rsi6": result.rsi6, "rsi12": result.rsi12, "rsi24": result.rsi24},
            },
            "waves": {
                "type": wave_data["wave"] if wave_data else "未知",
                "confidence": wave_data["confidence"] if wave_data else 0,
            },
            "kirin": {
                "phase": kirin_data["stage"] if kirin_data else "未知",
                "confidence": kirin_data["confidence"] if kirin_data else 0,
            },
            "strategies": [
                {
                    "strategy": s.strategy.value,
                    "date": s.trade_date,
                    "confidence": s.confidence,
                    "action": s.action,
                    "description": s.description,
                }
                for s in signals[:10]
            ],
            "diagnosis": {
                "price_position": getattr(diagnosis, "price_position", ""),
                "trend_status": getattr(diagnosis, "trend_status", ""),
                "sell_score": getattr(diagnosis, "sell_score", 0),
                "sell_score_desc": getattr(diagnosis, "sell_score_desc", ""),
                "kirin_phase": getattr(diagnosis, "kirin_phase", ""),
                "bull_rope": getattr(diagnosis, "bull_rope_status", ""),
                "sandglass_score": getattr(diagnosis, "sandglass_score", 0),
                "is_centipede": getattr(diagnosis, "is_centipede", False),
                "risk_level": getattr(diagnosis, "risk_level", ""),
                "recommendation": getattr(diagnosis, "recommendation", ""),
            },
            "score": {
                "total": score.score,
                "b1_score": score.b1_score,
                "trend_score": score.trend_score,
                "volume_score": score.volume_score,
                "risk_score": score.risk_score,
                "rating": score.rating,
                "reasons": score.reasons,
                "warnings": score.warnings,
            },
        }
        _json_output(json_result)
        return

    # ── 人类可读输出（保持原样） ──
    print(f"\n{'=' * 60}")
    print(f"股票分析: {ts_code}")
    print(f"{'=' * 60}")

    print("\n【技术指标】")
    print(f"  日期: {result.trade_date}")
    print(f"  KDJ:  K={result.k:.2f}  D={result.d:.2f}  J={result.j:.2f}")
    print(f"  MACD: DIF={result.dif:.4f}  DEA={result.dea:.4f}  柱={result.macd_hist:.4f}")
    print(f"  BBI:  {result.bbi:.2f}")
    print(f"  均线: MA5={result.ma5:.2f}  MA10={result.ma10:.2f}  MA20={result.ma20:.2f}")
    print(f"  RSI:  {result.rsi6:.2f}/{result.rsi12:.2f}/{result.rsi24:.2f}")

    print("\n【主力阶段】")
    if wave_data:
        print(f"  三波理论: {wave_data['wave']} (conf={wave_data['confidence']}) → {wave_data['b1_suggestion']}")
        if wave_data["stats"]:
            s = wave_data["stats"]
            print(f"    低点→当前: {s['low_price']:.1f}→{s['high_price']:.1f} 涨幅{s['gain_pct']:.1f}%")
            print(f"    涨停{s['limit_up_count']}次 阳线占比{s['red_ratio'] * 100:.0f}% 日均{s['avg_daily_gain']:.2f}%")
    if kirin_data:
        print(f"  麒麟会: {kirin_data['stage']} (conf={kirin_data['confidence']}) → {kirin_data['operation']}")
        if kirin_data["sub_type"] != "未知":
            print(f"    子类型: {kirin_data['sub_type']}")
        if kirin_data.get("scores"):
            sc = kirin_data["scores"]
            print(f"    评分: 吸{sc['xishou']} 拉{sc['lasheng']} 派{sc['paifa']} 落{sc['luoluo']}")
    if not wave_data and not kirin_data:
        print("  无 K 线数据，跳过主力阶段分析")

    print("\n【战法信号】")
    if not signals:
        print("  无信号")
    else:
        critical = [s for s in signals if s.priority.value == 3]
        opportunity = [s for s in signals if s.priority.value == 2]
        observe = [s for s in signals if s.priority.value == 1]

        if critical:
            print(f"  🔴 紧急 ({len(critical)}个):")
            for s in critical[:3]:
                print(f"     {s.trade_date} {s.strategy.value}: {s.description}")
        if opportunity:
            print(f"  🟢 机会 ({len(opportunity)}个):")
            for s in opportunity[:3]:
                print(f"     {s.trade_date} {s.strategy.value}: {s.description}")
        if observe:
            print(f"  ⚪ 观察 ({len(observe)}个):")
            for s in observe[:3]:
                print(f"     {s.trade_date} {s.strategy.value}: {s.description}")

    print("\n【综合评分】")
    print(f"  总分: {score.score:.1f}  {score.rating}")
    print(
        f"  B1评分: {score.b1_score:.1f}  趋势: {score.trend_score:.1f}  量价: {score.volume_score:.1f}  风险: {score.risk_score:.1f}"
    )
    if score.reasons:
        print(f"  理由: {', '.join(score.reasons[:5])}")
    if score.warnings:
        print(f"  警告: {', '.join(score.warnings[:3])}")

    print("\n【持仓诊断】")
    from modules.portfolio_diagnosis import format_report

    print(format_report(diagnosis))


def cmd_screen(args):
    """筛选股票（调 screener.screen_stocks）"""
    from modules.screener import screen_stocks

    criteria = STRATEGY_ALIAS.get(args.strategy, args.strategy)

    results = screen_stocks(
        criteria=criteria,
        max_stocks=args.limit if args.limit > 0 else 0,
        use_parallel=not args.no_parallel,
    )

    # 输出前 limit 只（limit=0 时输出全部 500 上限内的命中）
    output_limit = args.limit if args.limit > 0 else len(results)

    # ── JSON 输出 ──
    if args.json:
        json_result = {
            "criteria": criteria,
            "count": len(results[:output_limit]),
            "stocks": [
                {
                    "ts_code": r.ts_code,
                    "name": r.name,
                    "score": r.score,
                    "rating": r.rating,
                    "reasons": getattr(r, "reasons", []) or [],
                    "warnings": getattr(r, "warnings", []) or [],
                }
                for r in results[:output_limit]
            ],
        }
        _json_output(json_result)
        return

    # ── 人类可读输出（保持原样） ──
    print(f"\n{'=' * 60}")
    print(f"股票筛选 (criteria={criteria}, 上限={args.limit or '全市场'})")
    print(f"{'=' * 60}")
    print(f"\n扫描完成，命中: {len(results)} 只\n")

    for r in results[:output_limit]:
        print(f"  {r.ts_code:<12} {r.name:<8} score={r.score:.1f}  {r.rating}")
        reasons = getattr(r, "reasons", []) or []
        warnings = getattr(r, "warnings", []) or []
        if reasons:
            print(f"    reasons: {','.join(reasons[:3])}")
        if warnings:
            print(f"    warnings: {','.join(warnings[:3])}")


def cmd_score(args):
    """单只股票综合评分（复用 _analyze_core，不重复计算）"""
    from modules.screener import format_stock_score

    if not args.ts_code:
        print("请指定股票代码: zt score <ts_code>")
        sys.exit(1)

    core = _analyze_core(args.ts_code, days=60)
    score = core["score"]

    # ── JSON 输出 ──
    if args.json:
        json_result = {
            "ts_code": score.ts_code,
            "name": score.name,
            "score": score.score,
            "b1_score": score.b1_score,
            "trend_score": score.trend_score,
            "volume_score": score.volume_score,
            "risk_score": score.risk_score,
            "rating": score.rating,
            "reasons": score.reasons,
            "warnings": score.warnings,
        }
        _json_output(json_result)
        return

    # ── 人类可读输出 ──
    print(format_stock_score(score))


def cmd_workflow(args):
    """每日五步工作流（来自 screener.py workflow action）"""
    from modules.screener import daily_workflow

    daily_workflow()


def cmd_watchlist(args):
    """自选股管理"""
    from modules.watchlist import (
        add_watch,
        remove_watch,
        list_watch,
        scan_watchlist,
        generate_daily_report,
    )

    action = args.action

    if action == "add":
        tags = args.tags if hasattr(args, "tags") and args.tags else ""
        add_watch(args.ts_code, tags=tags)
        print(f"已添加: {args.ts_code}")

    elif action == "remove":
        remove_watch(args.ts_code)
        print(f"已移除: {args.ts_code}")

    elif action == "list":
        stocks = list_watch()
        print(f"\n自选股列表 ({len(stocks)}只):")
        for s in stocks:
            tags = s.get("tags", "") or "无"
            added = s.get("added_date", s.get("updated_at", "未知"))
            print(f"  {s['ts_code']}  标签:{tags}  添加:{added}")

    elif action == "scan":
        result = scan_watchlist()
        alerts = result.get("alerts", [])
        summary = result.get("summary", {})

        # ── JSON 输出 ──
        if hasattr(args, "json") and args.json:
            # 按 ts_code 聚合 alerts
            stock_map = {}
            for a in alerts:
                if a.ts_code not in stock_map:
                    stock_map[a.ts_code] = {"ts_code": a.ts_code, "name": a.name, "signals": [], "alerts": []}
                stock_map[a.ts_code]["alerts"].append(
                    {
                        "alert_type": a.alert_type,
                        "level": a.level,
                        "message": a.message,
                    }
                )
            json_result = {
                "count": len(stock_map),
                "stocks": list(stock_map.values()),
            }
            _json_output(json_result)
            return

        # ── 人类可读输出（保持原样） ──
        print(f"\n扫描自选股 ({summary.get('total', 0)}只):")
        print(
            f"  B1={summary.get('b1_count', 0)}  B2={summary.get('b2_count', 0)}  "
            f"逃顶={summary.get('exit_count', 0)}  破位={summary.get('break_count', 0)}  "
            f"异动={summary.get('abnormal_count', 0)}"
        )
        for a in alerts[:20]:
            print(f"  [{a.level}] {a.ts_code} {a.name}  {a.alert_type}: {a.message}")

    elif action == "report":
        print(generate_daily_report())


def cmd_diagnose(args):
    """持仓诊断（含逐步放飞阶梯）"""
    from dataclasses import asdict

    from modules.portfolio_diagnosis import diagnose_stock, format_report
    from modules.sell_decision import evaluate_today, format_sell_decision

    ts_code = args.ts_code
    diagnosis = diagnose_stock(ts_code, days=args.days)
    # --cost 显式给成本价；不给则尝试从交易记录反推持仓均价
    sell_plan = evaluate_today(ts_code, entry_price=getattr(args, "cost", None))

    # ── JSON 输出 ──
    if args.json:
        payload = asdict(diagnosis)
        payload["sell_plan"] = asdict(sell_plan)
        _json_output(payload)
        return

    # ── 人类可读输出 ──
    print(format_report(diagnosis))
    print(format_sell_decision(sell_plan))


def cmd_sync(args):
    """数据同步（init / sync / market-daily / trade-cal / index / status / stk-factor）"""
    import logging
    from datetime import datetime, timedelta
    from modules.data_sync import DataSyncer
    from modules.database import init_database
    from modules.datasource import get_datasource

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    action = args.sync_action

    if action == "init":
        init_database()
        print("数据库初始化完成")

    elif action == "sync":
        syncer = DataSyncer(datasource=get_datasource("tushare"))
        if args.ts_code:
            # 同步单只股票
            syncer.sync_daily_kline(args.ts_code)
            if not args.skip_indicators:
                print(f"正在同步指标缓存: {args.ts_code} ...")
                syncer.sync_indicator_cache(args.ts_code, days=args.days)
        else:
            # 批量同步所有股票
            syncer.sync_stock_basic()
            syncer.sync_all_daily_kline(days=args.days)
            if args.indicators and not args.skip_indicators:
                print("正在批量同步指标缓存...")
                syncer.sync_all_indicators()
        print("同步完成")
        print(syncer.get_sync_status())

    elif action == "stk-factor":
        syncer = DataSyncer(datasource=get_datasource("tushare"))
        if args.ts_code:
            print(f"正在同步 Tushare 官方指标: {args.ts_code} ...")
            start_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
            end_date = datetime.now().strftime("%Y%m%d")
            count = syncer.sync_stk_factor(args.ts_code, start_date=start_date, end_date=end_date)
            print(f"同步完成，{count} 条")
        else:
            print("正在批量同步 Tushare 官方指标...")
            results = syncer.sync_all_stk_factor(days=args.days)
            success = sum(1 for v in results.values() if v > 0)
            print(f"批量同步完成，成功 {success}/{len(results)}")

    elif action == "market-daily":
        syncer = DataSyncer(datasource=get_datasource("tushare"))
        result = syncer.sync_market_daily(
            trade_date=args.date,
            refresh_stock_basic=not args.no_refresh_stock_basic,
            check_trade_calendar=not args.skip_calendar_check,
        )
        if args.json:
            _json_output(result)
        else:
            status_icon = "✓" if result["status"] == "success" else "-" if result["status"] == "skipped" else "✗"
            print(f"{status_icon} {result['message']}")
            print(f"  交易日: {result['trade_date']}")
            print(f"  股票列表: {result['stock_basic_rows']} 条")
            print(f"  当日日线: {result['market_rows']} 条")
            print(f"  数据口径: {result['price_mode']}（未复权）")
        if result["status"] == "failed":
            raise SystemExit(1)

    elif action == "trade-cal":
        # 注意：Tushare trade_cal 接口被限流到 1 次/分钟，建议按整年拉取
        syncer = DataSyncer(datasource=get_datasource("tushare"))
        year = datetime.now().strftime("%Y")
        start_date = args.start or f"{year}0101"
        end_date = args.end or f"{year}1231"
        rows = syncer.sync_trade_cal(start_date, end_date, exchange=args.exchange)
        result = {
            "status": "success" if rows else "failed",
            "exchange": args.exchange,
            "start_date": start_date,
            "end_date": end_date,
            "rows": rows,
        }
        if args.json:
            _json_output(result)
        else:
            status_icon = "✓" if rows else "✗"
            print(f"{status_icon} 交易日历同步 {args.exchange} {start_date}~{end_date}: {rows} 条")
        if not rows:
            raise SystemExit(1)

    elif action == "index":
        syncer = DataSyncer(datasource=get_datasource("tushare"))
        if args.ts_code:
            rows = syncer.sync_index_daily(args.ts_code, start_date=args.start, end_date=args.end)
            per_index = {args.ts_code: rows}
        else:
            per_index = syncer.sync_all_index_daily(start_date=args.start, end_date=args.end)
        total = sum(int(v or 0) for v in per_index.values())
        # rows=0 表示"已是最新"，属正常情况，不视为失败
        result = {
            "status": "success",
            "total_rows": total,
            "index_count": len(per_index),
            "per_index": per_index,
        }
        if args.json:
            _json_output(result)
        else:
            print(f"✓ 指数日线同步完成，{len(per_index)} 个指数，共 {total} 条")
            for code, count in per_index.items():
                print(f"  {code:<12} {count} 条")
            if total == 0:
                print("  提示：0 条既可能是本地已最新，也可能是接口限流全部失败，请看上方日志")

    elif action == "status":
        # 状态查询只读本地数据库，不应因 Tushare 配置缺失而失败。
        syncer = DataSyncer(datasource=get_datasource("sqlite"))
        status = syncer.get_sync_status()
        print("=" * 50)
        print(f"  数据库: {status.get('db_path', 'N/A')}")
        print(f"  股票: {status.get('stock_count', 0)}")
        print(f"  K线: {status.get('kline_count', 0)}")
        print("=" * 50)
        if status.get("sync_status"):
            print("同步状态:")
            for s in status["sync_status"]:
                print(f"  {s['data_type']}: {s.get('last_date', 'N/A')} ({s.get('status', 'N/A')})")


def cmd_theme(args):
    """主线（炒作题材）管理：成员由外部判定器导入，强弱由本系统排序"""
    import json as _json

    from modules import themes as th

    action = args.theme_action

    if action == "add":
        th.upsert_theme(args.name, args.description or "", active=not args.inactive)
        print(f"主线已保存: {args.name}")
        return

    if action == "remove":
        ok = th.remove_theme(args.name)
        print(f"主线 {args.name} {'已删除' if ok else '不存在'}")
        return

    if action == "activate" or action == "deactivate":
        ok = th.set_theme_active(args.name, action == "activate")
        print(f"主线 {args.name} {'已' + ('启用' if action == 'activate' else '停用') if ok else '不存在'}")
        return

    if action == "list":
        rows = th.list_themes(active_only=args.active_only)
        if args.json:
            _json_output(rows)
            return
        if not rows:
            print("尚无主线。用 `zt theme add <名称>` 新建，或 `zt theme import <json>` 直接导入成员。")
            return
        print(f"{'主线':<18} {'状态':<6} {'成员':>5}  说明")
        print("-" * 76)
        for r in rows:
            print(
                f"{r['name']:<18} {'启用' if r['active'] else '停用':<6} {r['member_count']:>5}  {r['description'][:40]}"
            )
        return

    if action == "members":
        codes = th.get_theme_members(args.name)
        if args.json:
            _json_output({"theme": args.name, "members": codes})
        else:
            print(f"主线「{args.name}」成员 {len(codes)} 只:")
            for c in codes:
                print(f"  {c}")
        return

    if action == "import":
        # 外部判定器（kimi code + swarm）产出的 JSON：
        #   [{"theme": "商业航天", "ts_code": "600879.SH", "confidence": 0.9, "reason": "..."}]
        # 也接受 {"records": [...]} 包一层的形式
        with open(args.file, encoding="utf-8") as f:
            payload = _json.load(f)
        records = payload.get("records", payload) if isinstance(payload, dict) else payload
        res = th.import_members(records, source=args.source, replace=args.replace)
        if args.json:
            _json_output(res)
        else:
            print(f"导入 {res['imported']} 条归属，涉及主线: {', '.join(res['themes']) or '无'}")
            for s in res["skipped"]:
                print(f"  ! 跳过: {s}")
        return

    if action == "rank":
        trade_date = args.date
        if not trade_date:
            from modules.database import get_connection

            with get_connection() as conn:
                row = conn.execute("SELECT MAX(trade_date) FROM daily_kline").fetchone()
            trade_date = str(row[0]) if row and row[0] else ""
        res = th.rank_themes(trade_date, lookback=args.lookback, persist=not args.dry_run)
        if args.json:
            _json_output(
                {
                    "trade_date": res["trade_date"],
                    "lookback": res["lookback"],
                    "window": res["window"],
                    "written": res["written"],
                    "themes": [vars(g) | {"members": len(g.members)} for g in res["themes"]],
                    "industries": [vars(g) | {"members": len(g.members)} for g in res["industries"]],
                }
            )
        else:
            print(th.format_theme_ranking(res, limit=args.limit))
        return


def cmd_buy(args):
    """买点确认：大盘环境 + 日线买点战法 + MACD + 成交量 + 主线"""
    from modules.buy_decision import (
        apply_picks,
        confirm_buy_batch,
        format_buy_decision,
        format_buy_summary,
        format_final_picks,
        save_buy_decisions,
        select_final_picks,
    )

    codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()] if args.codes else []
    if not codes:
        from modules.watchlist import list_watch

        codes = [w["ts_code"] for w in list_watch() if w.get("ts_code")]
    if not codes:
        print("没有可确认的股票：未指定代码且票池为空")
        return

    decisions, blocked = confirm_buy_batch(
        codes, args.date, theme_lookback=args.theme_lookback, market_gate=args.market_gate
    )
    if blocked:
        print(f"活跃市值门槛未通过：{blocked}")
        print("本次未判定任何个股。要忽略区间请加 --market-gate off。")
        return
    selection = select_final_picks(
        decisions,
        top_n=args.top_n,
        min_group_strength=args.min_strength,
        max_per_group=args.max_per_group,
        include_watch=args.include_watch,
    )
    apply_picks(decisions, selection)
    if args.save:
        written = save_buy_decisions(decisions)
        print(f"已落库 buy_decisions {written} 行\n")

    if args.json:
        _json_output(
            [
                {
                    "ts_code": d.ts_code,
                    "name": d.name,
                    "trade_date": d.trade_date,
                    "action": d.action,
                    "score": round(d.score, 2),
                    "confidence": d.confidence,
                    "base_strategy": d.base_strategy,
                    "triggers": d.triggers,
                    "confirms": d.confirms,
                    "vetoes": d.vetoes,
                    "market": d.market,
                    "theme": d.theme,
                    "pick_rank": d.pick_rank,
                    "pick_reason": d.pick_reason,
                    "detail": d.detail,
                }
                for d in decisions
            ]
        )
        return

    if len(decisions) == 1 or args.detail:
        for d in decisions:
            print(format_buy_decision(d))
            print()
    else:
        print(format_buy_summary(decisions))

    if len(decisions) > 1:
        print()
        print(format_final_picks(selection))


def cmd_amv(args):
    """活跃市值：选股的总开关（多空区间）"""
    from modules import amv

    action = args.amv_action

    if action == "import":
        res = amv.import_history(args.file)
        print(f"导入 {res['imported']} 行，区间 {res['start']} ~ {res['end']}")
        v = amv.verify_against_imported()
        if v["total"]:
            print(f"与文件自带标注比对: {v['matched']}/{v['total']}  吻合 {v['accuracy']}%")
            for m in v["mismatches"][:10]:
                print(f"  ! {m['trade_date']}  重算={m['computed']}  标注={m['imported']}")
        print()
        print(amv.format_amv_status(amv.get_regime(), amv.regime_segments(5)))
        return

    if action == "add":
        day = amv.add_daily(args.date, close=args.close, pct_chg=args.pct)
        print(amv.format_amv_status(day, amv.regime_segments(5)))
        if args.close is None:
            print("\n! 只提供了涨幅。涨幅若是四舍五入到两位小数的值，在 -2.3% 边界附近可能")
            print("  判出相反的区间（实测 -2.295% 与 -2.303% 都显示 -2.30%，结论相反）。")
            print("  下次尽量给 --close 收盘价。")
        return

    if action == "status":
        day = amv.get_regime(args.date)
        if args.json:
            _json_output(
                {}
                if day is None
                else {
                    "trade_date": day.trade_date,
                    "close": day.close,
                    "pct_chg": day.pct_chg,
                    "regime": day.regime,
                    "can_select": day.can_select,
                }
            )
        else:
            print(amv.format_amv_status(day, amv.regime_segments(args.segments)))
        return

    if action == "list":
        days = amv.recent(args.limit, end_date=args.date)
        if args.json:
            _json_output(
                [{"trade_date": d.trade_date, "close": d.close, "pct_chg": d.pct_chg, "regime": d.regime} for d in days]
            )
            return
        print(f"{'日期':<10} {'收盘':>14} {'涨幅':>10}  区间")
        print("-" * 52)
        for d in days:
            pct = f"{d.pct_chg:+.2f}%" if d.pct_chg is not None else "-"
            print(f"{d.trade_date:<10} {d.close:>14,.2f} {pct:>10}  {d.regime}")
        return

    if action == "verify":
        v = amv.verify_against_imported()
        if args.json:
            _json_output(v)
            return
        print(f"重算区间 vs 文件标注: {v['matched']}/{v['total']}  吻合 {v['accuracy']}%")
        for m in v["mismatches"][:20]:
            print(f"  ! {m['trade_date']}  重算={m['computed']}  标注={m['imported']}")
        if not v["mismatches"]:
            print("  逐日完全一致。")
        return


def cmd_replay(args):
    """买点框架历史回放回测"""
    import csv as _csv
    import logging

    from modules import framework_backtest as fb

    if not args.json:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

    conn = fb._connect()
    cal = fb.load_calendar(conn, args.start, args.end or "99999999")
    full_cal = [str(r[0]) for r in conn.execute("SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date")]
    cal_pos = {d: i for i, d in enumerate(full_cal)}
    dates = [d for d in cal if cal_pos[d] + fb.HOLD_DAYS < len(full_cal)]
    regimes = fb.load_regimes(conn, full_cal)
    # gate=on 时只有多头区间的日子会出信号，快照也只需要算这些日子；
    # gate=off 要跑全部决策日，快照就得铺满
    need = dates if args.gate == "off" else [d for d in dates if regimes.get(d) == "多头区间"]

    if not args.skip_precompute:
        todo = fb.missing_theme_dates(conn, need, args.theme_lookback)
        if todo:
            print(f"预计算分组强度快照 {len(todo)} 天（缺了会导致第二阶段全员落选）...")
            fb.precompute_theme_strength(todo, args.theme_lookback)
    conn.close()

    result = fb.run_backtest(
        args.start,
        args.end or full_cal[-1],
        workers=args.workers,
        gate=(args.gate == "on"),
        top_n=args.top_n,
        min_group_strength=args.min_strength,
        max_per_group=args.max_per_group,
        include_watch=args.include_watch,
        theme_lookback=args.theme_lookback,
        limit_codes=args.limit,
    )

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = _csv.writer(fh)
            w.writerow(
                [
                    "决策日",
                    "代码",
                    "名称",
                    "结论",
                    "确认分",
                    "入选名次",
                    "分组",
                    "组强度",
                    "区间",
                    "买入日",
                    "买入价",
                    "卖出日",
                    "卖出价",
                    "期间最低",
                    "期间最高",
                    "买不进",
                    "止损先于最高点",
                    "收益率_未截断",
                    "收益率_路径止损",
                    "收益率_只封底",
                    "收益率_卖最高点",
                    "收益率_卖最高点无止损",
                ]
            )
            for t in result["trades"]:
                w.writerow(
                    [
                        t.decision_date,
                        t.ts_code,
                        t.name,
                        t.action,
                        round(t.score, 2),
                        t.pick_rank,
                        t.group,
                        t.group_strength,
                        t.regime,
                        t.entry_date,
                        t.entry_price,
                        t.exit_date,
                        t.exit_price,
                        t.lowest,
                        t.highest,
                        int(t.unbuyable),
                        int(t.stopped_before_peak),
                        round(t.ret_raw, 6),
                        round(t.ret_stop, 6),
                        round(t.ret_floor, 6),
                        round(t.ret_peak, 6),
                        round(t.ret_peak_nostop, 6),
                    ]
                )
        print(f"逐笔明细已写入 {args.csv}（{len(result['trades'])} 行）\n")

    if args.json:
        _json_output(
            {
                "params": result["params"],
                "universe": result["universe"],
                "decision_dates": result["decision_dates"],
                "picks": fb.summarize([t for t in result["trades"] if t.pick_rank], label="picks"),
                "all_buys": fb.summarize([t for t in result["trades"] if t.action == "BUY"], label="all_buys"),
            }
        )
    else:
        print(fb.format_summary(result))


def cmd_review(args):
    """复盘案例库：人工录入 → 框架归因回放 → 前瞻收益结算"""
    from modules import review_memory as rm

    action = args.review_action
    if action == "add":
        case = rm.add_case(
            args.ts_code,
            args.date,
            note=args.note,
            tags=args.tags,
            source=args.source,
            theme_lookback=args.theme_lookback,
            precompute_theme=not args.no_theme,
        )
        if args.json:
            _json_output(case)
            return
        print(rm.format_case(case))
        return

    if action == "list":
        cases = rm.list_cases(limit=args.limit, source=args.source, status=args.status)
        if args.json:
            _json_output(cases)
            return
        print(rm.format_case_list(cases))
        return

    if action == "show":
        case = rm.get_case(args.id)
        if case is None:
            print(f"案例 #{args.id} 不存在")
            sys.exit(1)
        if args.json:
            _json_output(case)
            return
        print(rm.format_case(case))
        return

    if action == "settle":
        updated = rm.settle_open_cases()
        if args.json:
            _json_output(updated)
            return
        if not updated:
            print("没有待结算的案例。")
            return
        print(f"补结算 {len(updated)} 个案例：")
        for c in updated:
            state = "已结清" if c["settled"] else "仍未满窗口"
            print(f"  #{c['id']} {c['ts_code']} @{c['case_date']}  +30日 {rm._pct(c.get('ret_30'))}  {state}")
        return


def cmd_scan(args):
    """全市场扫描：大盘门槛 → 逐票 B1 买点确认 → 主线/行业筛选"""
    import logging

    from modules.buy_decision import format_scan_result, save_buy_decisions, scan_market

    if not args.json:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

    result = scan_market(
        trade_date=args.date,
        market_gate=args.market_gate,
        top_n=args.top_n,
        min_group_strength=args.min_strength,
        max_per_group=args.max_per_group,
        include_watch=args.include_watch,
        limit=args.limit,
        theme_lookback=args.theme_lookback,
    )

    if args.save and not result["blocked"]:
        written = save_buy_decisions(result["decisions"], only_actionable=not args.save_all)
        print(f"已落库 buy_decisions {written} 行\n")

    if args.json:
        selection = result.get("selection") or {}
        _json_output(
            {
                "trade_date": result["trade_date"],
                "market": result["market"],
                "blocked": result["blocked"],
                "scanned": result["scanned"],
                "picks": [
                    {
                        "rank": e["rank"],
                        "ts_code": e["decision"].ts_code,
                        "name": e["decision"].name,
                        "score": round(e["decision"].score, 2),
                        "base_strategy": e["decision"].base_strategy,
                        "group": e["group"],
                        "group_kind": e["group_kind"],
                        "group_strength": e["group_strength"],
                        "triggers": e["decision"].triggers,
                        "confirms": e["decision"].confirms,
                    }
                    for e in (selection.get("picks") or [])
                ],
                "rejected": [
                    {
                        "ts_code": e["decision"].ts_code,
                        "name": e["decision"].name,
                        "score": round(e["decision"].score, 2),
                        "reason": e["reason"],
                    }
                    for e in (selection.get("rejected") or [])
                ],
            }
        )
        return

    print(format_scan_result(result, show_rejected=args.show_rejected))

    if result["blocked"]:
        raise SystemExit(0)


def cmd_advanced(args):
    """高阶行情数据层：目录 / 取数 / 自检（东财 / 同花顺 / 财联社公开接口）"""
    # 延迟 import：照 cmd_amv 的惯例，命令处理函数用到的模块都在函数体内 import。
    # （原注释说这能省下 pandas 的启动开销——实测不成立：import modules.cli 时 pandas
    #   已经被拉进来了。真正值得省的是 akshare 那 2 秒，那句已在 advanced_data.fetch 里注明。）
    from modules import advanced_data as adv

    action = args.advanced_action

    def _fmt_ttl(seconds: int) -> str:
        """TTL 秒数转人话——目录里摆一个 43200，没人一眼认得出那是 12 小时。"""
        if seconds >= 86400 and seconds % 86400 == 0:
            return f"{seconds // 86400}天"
        if seconds >= 3600 and seconds % 3600 == 0:
            return f"{seconds // 3600}小时"
        if seconds >= 60 and seconds % 60 == 0:
            return f"{seconds // 60}分钟"
        return f"{seconds}秒"

    def _wrap(text: str, indent: str, width: int = 44) -> list[str]:
        """按中文折行。textwrap 按字符数算宽度，中文占两格，所以 width 取终端宽度的一半。"""
        import textwrap

        return [indent + line for line in textwrap.wrap(text, width=width)]

    if action == "catalog":
        grouped = adv.catalog()
        if args.category:
            if args.category not in grouped:
                print(f"! 未知分组: {args.category}")
                print("  可用分组: " + " / ".join(grouped))
                raise SystemExit(1)
            grouped = {args.category: grouped[args.category]}

        # 目录是给人翻的，`zt advanced catalog | head -30` 是最常见的用法，而 Python 默认
        # 忽略 SIGPIPE：head 一关管子，后面的 print 就抛 BrokenPipeError，退出时再吐一句
        # "Exception ignored"，看着像出了故障。这里恢复成 Unix 默认（管子关了就安静退出）。
        # 放在 --json **之前**：`catalog --json | head` 一样会被截断，只覆盖文本那一支等于漏一半。
        # **只在 catalog 这一支**做——它整支都纯本地、不碰网络；get 那边要写网络 socket，
        # 把 SIGPIPE 放回 SIG_DFL 会让本该抛 BrokenPipeError 的写操作直接杀掉进程，
        # 违反"fetch 只返回 None、不炸"的契约。
        import signal
        import threading

        # signal.signal() 在非主线程直接抛 ValueError（"signal only works in main thread"）。
        # api/ 下有线程池，谁把 cmd_advanced 接进去，catalog 就会栽在这句纯装饰性的信号复位上；
        # 而在子线程里本来也没有"复位进程信号处置"这回事，跳过即可。
        if hasattr(signal, "SIGPIPE") and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)

        if args.json:
            _json_output(grouped)
            return

        total = sum(len(v) for v in grouped.values())
        print(f"高阶行情数据层：{total} 条接口 / {len(grouped)} 个分组")
        print("key 是对外稳定名，上游改函数名只动 func —— 调用方一律只认 key。")
        for category, items in grouped.items():
            print()
            print(f"━━ {category}（{len(items)} 条）" + "━" * 26)
            for it in items:
                print(f"  {it['key']:<18} TTL {_fmt_ttl(it['ttl']):<6} 源 {it['source']:<4} {it['func']}")
                for line in _wrap(it["desc"], "     "):
                    print(line)
                if it["params"]:
                    for name, note in it["params"].items():
                        # 参数说明和 desc 一样要折行：注册表里有的条目会把"参数取值的代价"
                        # 写进说明（jgdy 那条讲的是页数随日期指数增长），一行摆不下，
                        # 不折行就直接跑出终端右边，等于没写
                        head, *rest = _wrap(f"参数 {name}: {note}", "     ")
                        print(head)
                        for line in rest:
                            print("       " + line.lstrip())
                else:
                    print("     参数 无（无参接口）")
        return

    if action == "get":
        params: dict[str, str] = {}
        for item in args.param or []:
            # 只切第一个 =：参数值本身可能带 =（如 symbol=A=B），切多了会把值截断
            if "=" not in item:
                print(f"! --param 要写成 k=v，收到的是: {item!r}")
                raise SystemExit(2)
            name, value = item.split("=", 1)
            name = name.strip()
            if not name:
                print(f"! --param 的参数名是空的: {item!r}")
                raise SystemExit(2)
            params[name] = value

        source = adv.get_advanced_source()
        df = source.fetch(args.key, params, force=args.force)
        warnings = source.warnings

        if df is None:
            # last_error 的文案本来就是照着"一眼看出该去改哪"写的（未知 key 会列出全部可用 key、
            # 参数名打错会列出接受的参数名），原样打出来，别自己再包一层把线索盖掉
            error = source.last_error or "未知错误"
            if args.json:
                _json_output(
                    {
                        "key": args.key,
                        "params": params,
                        "ok": False,
                        "error": error,
                        "warnings": warnings,
                        "rows": 0,
                        "data": [],
                    }
                )
            else:
                print(f"! 取数失败: {args.key}")
                print(f"  {error}")
                for w in warnings:
                    print(f"  ! {w}")
            raise SystemExit(1)

        # ths_industry_index 是 31 条里**唯一**把行标识放在行索引里的（DatetimeIndex(name='日期')），
        # 而下面两条渲染路径都会把 index 整个丢掉：文本走 to_string(index=False)，
        # --json 走 orient="records"。日期一丢，输出只剩"收盘/成交量"两列，
        # 这几行到底是哪几天完全看不出来。所以渲染前先把非默认行索引还原成普通列——
        # 放在这里而不是各自的分支里，是为了让文本、--json 的 columns、data 三处口径一致。
        import pandas as pd

        # "行标识在不在 index 里"**不能**拿 isinstance(df.index, pd.RangeIndex) 判：
        # 走缓存回来的普通行号是 Index([0,1,...], dtype=int64) 而不是 RangeIndex
        # （split json 里存的就是一串整数，读回来只能是普通 Index）。判成"有行标识"的话，
        # 那 30 条本来就是 RangeIndex 的接口在**命中缓存时**会凭空 reset 出一列叫 index 的行号：
        # 同一条 `zt advanced get zt_pool`，冷缓存打 2 列、热缓存打 3 列，
        # --json 的每条记录还多一个 index 键——下游按列名取数会直接错位。
        # 所以问的是语义而不是类型：index 有没有名字（ths_industry_index 的是 name='日期'），
        # 或者它压根就不是 0..n-1 那串行号。
        index_carries_labels = any(name is not None for name in df.index.names) or not df.index.equals(
            pd.RangeIndex(len(df))
        )
        if index_carries_labels:
            try:
                df = df.reset_index()
            except ValueError:
                # index 名和现有列名撞了（reset_index 会抛 ValueError）。少显示一列日期，
                # 也好过让整条命令炸在渲染上——数据本身照常打出来。
                pass

        shown = df if args.limit <= 0 else df.head(args.limit)

        if args.json:
            try:
                records = json.loads(shown.to_json(orient="records", force_ascii=False, date_format="iso"))
            except (ValueError, TypeError, OverflowError):
                # 上游偶尔塞进 to_json 认不得的对象；宁可退化成字符串，也不能让 --json 整个炸掉
                records = shown.astype(str).to_dict(orient="records")
            _json_output(
                {
                    "key": args.key,
                    "params": params,
                    "ok": True,
                    "rows": int(len(df)),
                    "shown": int(len(shown)),
                    "columns": [str(c) for c in df.columns],
                    "warnings": warnings,
                    "data": records,
                }
            )
            return

        tail = f"（只显示前 {len(shown)} 行）" if len(shown) < len(df) else ""
        print(f"{args.key}  {len(df)} 行 × {len(df.columns)} 列{tail}")
        # 空表和缺列都不是错误，但调用方必须知道这次不完整——warnings 一条都不能吞
        for w in warnings:
            print(f"  ! {w}")
        if df.empty:
            print("（空表）")
            return
        print(shown.to_string(index=False))
        return

    if action == "selfcheck":
        result = adv.selfcheck(probe=not args.no_probe)
        if args.json:
            _json_output(result)
            return

        print(f"akshare 版本: {result['akshare 版本']}")
        print(f"接口总数: {result['接口总数']}")

        gone = result["函数已不存在"]
        if gone:
            print(f"! 函数已不存在 {len(gone)} 条（akshare 改过名，请更新 INTERFACES 的 func 字段）:")
            for g in gone:
                print(f"    {g['key']:<18} {g['func']}")
        else:
            print("函数名: 全部对得上。")

        if args.no_probe:
            print("（--no-probe：只做离线 hasattr 检查，没发任何网络请求）")
            return

        skipped = result["跳过_需要参数"]
        print(f"跳过（需要参数，随手编的参数取回空表说明不了问题）: {len(skipped)} 条")

        passed = result["探活通过"]
        print(f"探活通过: {len(passed)} 条")
        for p in passed:
            note = ("  告警: " + "; ".join(p["告警"])) if p.get("告警") else ""
            print(f"    {p['key']:<18} {p['行数']} 行  {p['列']}{note}")

        failed = result["探活失败"]
        if failed:
            print(f"! 探活失败: {len(failed)} 条")
            for f in failed:
                print(f"    {f['key']:<18} {f['错误']}")
        return


def build_parser():
    """构建并返回 zt CLI 的 ArgumentParser（支持独立导入测试）"""
    parser = argparse.ArgumentParser(
        prog="zt",
        description="Z哥量化工具 CLI（v2.10.0 统一入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  zt analyze 600487.SH
  zt analyze 600487.SH --json
  zt screen --strategy B1 --limit 20
  zt score 600487.SH
  zt diagnose 600487.SH
  zt watchlist add 600487.SH --tags 通信设备,5G
  zt watchlist scan
  zt backtest multi 600487.SH
  zt backtest portfolio 600487.SH,601318.SH
  zt trade add "4月25号买了100股茅台1800块"
  zt trade list
  zt daily
  zt monitor
  zt sync init
  zt sync sync 600487.SH
  zt sync trade-cal --start 20260101 --end 20261231
  zt sync index --ts-code 000300.SH
  zt theme add 商业航天 --description "卫星互联网/火箭发射产业链"
  zt theme import themes.json --replace
  zt theme rank --lookback 5
  zt buy 601360.SH --detail
  zt amv add 20260810 --close 215000
  zt amv status
  zt advanced catalog
  zt advanced get zt_pool --param date=20260814 --limit 5
  zt scan
  zt scan --top-n 3
  zt review add 600487.SH 20260715 --note "缩量回踩20日线后放量长阳" --tags 缩量回踩
  zt review list
  zt review settle
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令", required=True)

    # ── analyze ──
    p_analyze = subparsers.add_parser("analyze", help="分析单只股票（指标 + 主力阶段 + 战法信号 + 诊断）")
    p_analyze.add_argument("ts_code", help="股票代码，如 600487.SH")
    p_analyze.add_argument("--days", type=int, default=120, help="分析天数")
    p_analyze.add_argument("--json", action="store_true", help="JSON输出")

    # ── screen ──
    p_screen = subparsers.add_parser("screen", help="批量选股（11 种策略）")
    p_screen.add_argument("--strategy", choices=STRATEGY_CHOICES, default="B1", help="筛选策略（11 种别名）")
    p_screen.add_argument("--limit", type=int, default=20, help="输出数量（0=全市场 500 上限）")
    p_screen.add_argument("--no-parallel", action="store_true", help="禁用多进程并行")
    p_screen.add_argument("--json", action="store_true", help="JSON输出")

    # ── score（来自 screener.py score）──
    p_score = subparsers.add_parser("score", help="单只股票综合评分")
    p_score.add_argument("ts_code", nargs="?", help="股票代码，如 600487.SH")
    p_score.add_argument("--json", action="store_true", help="JSON输出")

    # ── workflow（来自 screener.py workflow）──
    subparsers.add_parser("workflow", help="每日五步工作流")

    # ── diagnose ──
    p_diag = subparsers.add_parser("diagnose", help="持仓诊断（含逐步放飞阶梯）")
    p_diag.add_argument("ts_code", help="股票代码")
    p_diag.add_argument("--days", type=int, default=120, help="分析天数")
    p_diag.add_argument("--cost", type=float, default=None, help="持仓成本价（不传则从交易记录反推均价）")
    p_diag.add_argument("--json", action="store_true", help="JSON输出")

    # ── watchlist（add/remove/list/scan/report）──
    p_wl = subparsers.add_parser("watchlist", help="自选股管理")
    p_wl.add_argument("action", choices=["add", "remove", "list", "scan", "report"], help="操作")
    p_wl.add_argument("ts_code", nargs="?", help="股票代码（add/remove 必填）")
    p_wl.add_argument("--tags", help="标签，逗号分隔")
    p_wl.add_argument("--json", action="store_true", help="JSON输出（仅 scan 操作）")

    # ── sync（init/sync/market-daily/status/stk-factor）──
    p_sync = subparsers.add_parser("sync", help="数据同步（含收盘后全市场日线）")
    p_sync_sub = p_sync.add_subparsers(dest="sync_action", required=True)

    p_sync_sub.add_parser("init", help="初始化数据库")
    p_sync_run = p_sync_sub.add_parser("sync", help="同步日线 K 线（+ 可选指标缓存）")
    p_sync_run.add_argument("ts_code", nargs="?", help="股票代码（不传 = 全市场批量）")
    p_sync_run.add_argument("--days", type=int, default=730, help="同步天数")
    p_sync_run.add_argument("--indicators", action="store_true", help="批量同步完成后计算并缓存技术指标")
    p_sync_run.add_argument(
        "--skip-indicators", action="store_true", help="跳过指标缓存（单只默认同步，批量需 --indicators）"
    )
    p_sync_market = p_sync_sub.add_parser("market-daily", help="收盘后按交易日一次同步全 A 股日线")
    p_sync_market.add_argument("--date", help="交易日期 YYYYMMDD，默认今天")
    p_sync_market.add_argument("--no-refresh-stock-basic", action="store_true", help="不刷新上市股票基本信息")
    p_sync_market.add_argument("--skip-calendar-check", action="store_true", help="跳过交易日历检查（仅排障使用）")
    p_sync_market.add_argument("--json", action="store_true", help="JSON 输出")
    p_sync_cal = p_sync_sub.add_parser("trade-cal", help="同步交易日历（接口限流 1 次/分钟，建议按整年拉）")
    p_sync_cal.add_argument("--start", help="起始日期 YYYYMMDD，默认今年 0101")
    p_sync_cal.add_argument("--end", help="结束日期 YYYYMMDD，默认今年 1231")
    p_sync_cal.add_argument("--exchange", default="SSE", help="交易所代码，默认 SSE")
    p_sync_cal.add_argument("--json", action="store_true", help="JSON 输出")
    p_sync_index = p_sync_sub.add_parser("index", help="同步宽基指数日线（默认 7 个指数）")
    p_sync_index.add_argument("--ts-code", dest="ts_code", help="指数代码，如 000300.SH（不传 = 全部默认指数）")
    p_sync_index.add_argument("--start", help="起始日期 YYYYMMDD，默认增量续传")
    p_sync_index.add_argument("--end", help="结束日期 YYYYMMDD，默认今天")
    p_sync_index.add_argument("--json", action="store_true", help="JSON 输出")
    p_sync_sub.add_parser("status", help="查看同步状态")
    p_sync_factor = p_sync_sub.add_parser("stk-factor", help="同步 Tushare 官方指标（diff 验证用）")
    p_sync_factor.add_argument("ts_code", nargs="?", help="股票代码（不传 = 全市场）")
    p_sync_factor.add_argument("--days", type=int, default=365, help="同步天数")

    # ── backtest（multi / portfolio）──
    # dest 字段名必须与 cli_commands.cmd_backtest 里 getattr(args, "backtest_sub", ...) 一致
    p_bt = subparsers.add_parser("backtest", help="策略回测")
    p_bt_sub = p_bt.add_subparsers(dest="backtest_sub", required=True)

    p_bt_multi = p_bt_sub.add_parser("multi", help="多策略融合回测")
    p_bt_multi.add_argument("ts_code", help="股票代码")
    p_bt_multi.add_argument("--strategy", default="b1,b2", help="策略列表，逗号分隔")
    p_bt_multi.add_argument("--days", type=int, default=120, help="回测天数")
    p_bt_multi.add_argument("--json", action="store_true", help="JSON输出")

    p_bt_portfolio = p_bt_sub.add_parser("portfolio", help="多股票组合回测")
    # 字段名 codes 与 cli_commands.cmd_backtest 中 getattr(args, "codes", ...) 对齐
    p_bt_portfolio.add_argument("codes", help="股票代码，逗号分隔")
    p_bt_portfolio.add_argument("--days", type=int, default=120, help="回测天数")
    p_bt_portfolio.add_argument("--json", action="store_true", help="JSON输出")

    # ── trade（add / list / stats）──
    # 改为 subparser 模式：dest="trade_sub" 与 cli_commands.cmd_trade 里 getattr(args, "trade_sub", ...) 对齐
    p_trade = subparsers.add_parser("trade", help="交易记录管理")
    p_trade_sub = p_trade.add_subparsers(dest="trade_sub", required=True)

    p_trade_add = p_trade_sub.add_parser("add", help="添加交易记录")
    # 字段名 text 与 cli_commands.cmd_trade 中 getattr(args, "text", ...) 对齐
    p_trade_add.add_argument("text", help="交易描述（口语化）")
    p_trade_add.add_argument("--json", action="store_true", help="JSON输出")

    p_trade_list = p_trade_sub.add_parser("list", help="列出最近交易记录")
    p_trade_list.add_argument("--limit", type=int, default=20, help="列出条数")
    p_trade_list.add_argument("--json", action="store_true", help="JSON输出")

    p_trade_stats = p_trade_sub.add_parser("stats", help="交易统计摘要")
    p_trade_stats.add_argument("--json", action="store_true", help="JSON输出")

    # ── daily ──
    p_daily = subparsers.add_parser("daily", help="每日五步工作流")
    p_daily.add_argument("--json", action="store_true", help="JSON输出")

    # ── theme（主线管理）──
    p_theme = subparsers.add_parser("theme", help="主线（炒作题材）管理：成员外部导入，强弱本地排序")
    p_theme_sub = p_theme.add_subparsers(dest="theme_action", required=True)

    p_theme_add = p_theme_sub.add_parser("add", help="新建/更新一条主线")
    p_theme_add.add_argument("name", help="主线名称，如 商业航天")
    p_theme_add.add_argument("--description", default="", help="主线说明（给外部判定器看的口径）")
    p_theme_add.add_argument("--inactive", action="store_true", help="建档但不参与排名")

    p_theme_rm = p_theme_sub.add_parser("remove", help="删除主线及其成员关系")
    p_theme_rm.add_argument("name")

    p_theme_on = p_theme_sub.add_parser("activate", help="启用主线")
    p_theme_on.add_argument("name")
    p_theme_off = p_theme_sub.add_parser("deactivate", help="停用主线（退潮，保留历史）")
    p_theme_off.add_argument("name")

    p_theme_ls = p_theme_sub.add_parser("list", help="列出主线及成员数")
    p_theme_ls.add_argument("--active-only", action="store_true", help="只看启用中的")
    p_theme_ls.add_argument("--json", action="store_true", help="JSON输出")

    p_theme_mem = p_theme_sub.add_parser("members", help="列出某条主线的成员")
    p_theme_mem.add_argument("name")
    p_theme_mem.add_argument("--json", action="store_true", help="JSON输出")

    p_theme_imp = p_theme_sub.add_parser(
        "import",
        help="导入外部判定器（kimi code + swarm）产出的股票↔主线归属 JSON",
    )
    p_theme_imp.add_argument(
        "file", help='JSON 文件：[{"theme":"...","ts_code":"...","confidence":0.9,"reason":"..."}]'
    )
    p_theme_imp.add_argument("--source", default="kimi-swarm", help="判定来源标记，默认 kimi-swarm")
    p_theme_imp.add_argument("--replace", action="store_true", help="先清空涉及主线的旧成员再导入（整体重跑时用）")
    p_theme_imp.add_argument("--json", action="store_true", help="JSON输出")

    p_theme_rank = p_theme_sub.add_parser("rank", help="计算并排序主线/行业强度")
    p_theme_rank.add_argument("--date", help="交易日 YYYYMMDD，默认库内最新")
    p_theme_rank.add_argument("--lookback", type=int, default=5, help="统计窗口交易日数，默认 5")
    p_theme_rank.add_argument("--limit", type=int, default=15, help="每类最多显示几条")
    p_theme_rank.add_argument("--dry-run", action="store_true", help="只算不落库")
    p_theme_rank.add_argument("--json", action="store_true", help="JSON输出")

    # ── buy（买点确认）──
    p_buy = subparsers.add_parser("buy", help="买点确认：大盘环境 + 日线买点战法 + MACD + 成交量 + 主线")
    p_buy.add_argument("codes", nargs="?", help="股票代码，逗号分隔；省略则用票池")
    p_buy.add_argument("--date", help="交易日 YYYYMMDD，默认该票最新数据日")
    p_buy.add_argument("--detail", action="store_true", help="多只票时也逐只展开详情")
    p_buy.add_argument("--save", action="store_true", help="结果落库 buy_decisions")
    p_buy.add_argument("--theme-lookback", type=int, default=5, help="主线强度统计窗口（交易日，默认 5）")
    p_buy.add_argument("--top-n", type=int, default=5, help="第二阶段最终选股数上限（默认 5）")
    p_buy.add_argument("--min-strength", type=float, default=50.0, help="第二阶段主线/行业强度门槛（默认 50=中位行业）")
    p_buy.add_argument(
        "--max-per-group", type=int, default=None, help="第二阶段每个主线/行业最多选几只（默认不限，允许集中）"
    )
    p_buy.add_argument("--include-watch", action="store_true", help="第二阶段把 WATCH 也纳入候选")
    p_buy.add_argument(
        "--market-gate",
        choices=["on", "off"],
        default="on",
        help="活跃市值区间门槛：on=空头区间不选股（默认）/ off=忽略区间（调试用）",
    )
    p_buy.add_argument("--json", action="store_true", help="JSON输出")

    # ── amv（活跃市值：选股总开关）──
    p_amv = subparsers.add_parser("amv", help="活跃市值多空区间：选股的总开关")
    p_amv_sub = p_amv.add_subparsers(dest="amv_action", required=True)

    p_amv_imp = p_amv_sub.add_parser("import", help="导入历史（0AMV-YYMMDD-增强.csv）")
    p_amv_imp.add_argument("file", help="CSV 路径")

    p_amv_add = p_amv_sub.add_parser("add", help="录入单日活跃市值（收盘后）")
    p_amv_add.add_argument("date", help="交易日 YYYYMMDD 或 YYYY-MM-DD")
    p_amv_add.add_argument("--close", type=float, help="收盘价（首选：区间判定用它现算全精度涨幅）")
    p_amv_add.add_argument("--pct", type=float, help="日涨幅%%（备选：四舍五入值在 -2.3%% 边界附近不可靠）")

    p_amv_st = p_amv_sub.add_parser("status", help="当前多空区间")
    p_amv_st.add_argument("--date", help="截至某日，默认最新")
    p_amv_st.add_argument("--segments", type=int, default=8, help="显示最近几段区间")
    p_amv_st.add_argument("--json", action="store_true", help="JSON输出")

    p_amv_ls = p_amv_sub.add_parser("list", help="列出最近若干日")
    p_amv_ls.add_argument("--limit", type=int, default=20)
    p_amv_ls.add_argument("--date", help="截至某日")
    p_amv_ls.add_argument("--json", action="store_true", help="JSON输出")

    p_amv_v = p_amv_sub.add_parser("verify", help="重算区间与文件标注逐日比对")
    p_amv_v.add_argument("--json", action="store_true", help="JSON输出")

    # ── advanced（高阶行情数据层：东财/同花顺/财联社公开接口）──
    p_adv = subparsers.add_parser("advanced", help="高阶行情数据：资金面/情绪面/结构面/风险面/消息面/技术榜")
    p_adv_sub = p_adv.add_subparsers(dest="advanced_action", required=True)

    p_adv_cat = p_adv_sub.add_parser("catalog", help="列出全部接口（按分组），纯本地不联网")
    p_adv_cat.add_argument("--category", help="只看一个分组：资金面/情绪面/结构面/风险面/消息面/技术榜")
    p_adv_cat.add_argument("--json", action="store_true", help="JSON输出")

    p_adv_get = p_adv_sub.add_parser("get", help="取一条接口的数据（未命中缓存时会联网）")
    p_adv_get.add_argument("key", help="接口 key（见 zt advanced catalog），如 zt_pool")
    p_adv_get.add_argument(
        "--param",
        action="append",
        metavar="K=V",
        help="接口参数，可重复，如 --param date=20260814",
    )
    p_adv_get.add_argument("--limit", type=int, default=20, help="最多显示几行（0=全部），默认 20")
    p_adv_get.add_argument("--force", action="store_true", help="跳过缓存强制重取")
    p_adv_get.add_argument("--json", action="store_true", help="JSON输出")

    p_adv_sc = p_adv_sub.add_parser("selfcheck", help="体检：函数还在不在、无参接口还能不能取到数据")
    p_adv_sc.add_argument("--no-probe", action="store_true", help="只做离线检查，不发任何网络请求")
    p_adv_sc.add_argument("--json", action="store_true", help="JSON输出")

    # ── scan（全市场扫描）──
    p_scan = subparsers.add_parser("scan", help="全市场扫描：大盘门槛 → B1 买点确认 → 主线/行业筛选")
    p_scan.add_argument("--date", help="交易日 YYYYMMDD，默认库内最新")
    p_scan.add_argument(
        "--market-gate",
        choices=["on", "off"],
        default="on",
        help="活跃市值区间门槛：on=空头区间不选股（默认）/ off=忽略区间（调试用）",
    )
    p_scan.add_argument("--top-n", type=int, default=5, help="最终选股数上限（默认 5）")
    p_scan.add_argument("--min-strength", type=float, default=50.0, help="主线/行业强度门槛（默认 50=中位行业）")
    p_scan.add_argument("--max-per-group", type=int, default=None, help="每个主线/行业最多选几只（默认不限）")
    p_scan.add_argument("--include-watch", action="store_true", help="把 WATCH 也纳入最终候选")
    p_scan.add_argument("--theme-lookback", type=int, default=5, help="主线强度统计窗口（交易日，默认 5）")
    p_scan.add_argument("--limit", type=int, default=0, help="只扫前 N 只（调试用），0=全市场")
    p_scan.add_argument("--show-rejected", type=int, default=15, help="最多列出几只落选票")
    p_scan.add_argument("--save", action="store_true", help="结果落库 buy_decisions（默认只写 BUY/WATCH）")
    p_scan.add_argument("--save-all", action="store_true", help="与 --save 连用：把 NONE 也一并落库")
    p_scan.add_argument("--json", action="store_true", help="JSON输出")

    # ── replay（买点框架历史回放回测）──
    p_replay = subparsers.add_parser("replay", help="买点框架历史回放：逐日重跑选股，看 30 个交易日后的涨跌")
    p_replay.add_argument("--start", required=True, help="决策日区间起点 YYYYMMDD")
    p_replay.add_argument("--end", help="决策日区间终点，默认到库内最新（自动扣掉结算不了的尾部）")
    p_replay.add_argument("--workers", type=int, default=8, help="并行进程数（默认 8）")
    p_replay.add_argument(
        "--gate",
        choices=["on", "off"],
        default="on",
        help="活跃市值总开关：on=只在多头区间选股（框架现状，默认）/ off=空头区间也跑一遍作对照（约 2.4 倍耗时）",
    )
    p_replay.add_argument("--top-n", type=int, default=5, help="最终选股数上限（默认 5，与 scan 一致）")
    p_replay.add_argument("--min-strength", type=float, default=50.0, help="主线/行业强度门槛（默认 50）")
    p_replay.add_argument("--max-per-group", type=int, default=None, help="每个主线/行业最多选几只（默认不限）")
    p_replay.add_argument("--include-watch", action="store_true", help="把 WATCH 也纳入最终候选")
    p_replay.add_argument("--theme-lookback", type=int, default=5, help="分组强度统计窗口（交易日，默认 5）")
    p_replay.add_argument("--limit", type=int, default=0, help="只跑前 N 只票（调试用），0=全池")
    p_replay.add_argument("--skip-precompute", action="store_true", help="跳过分组强度快照预计算（已算过时用）")
    p_replay.add_argument("--csv", help="把逐笔明细导出到该 CSV 路径")
    p_replay.add_argument("--json", action="store_true", help="JSON输出")

    # ── review（复盘案例库：人工复盘记忆的案例层）──
    p_rev = subparsers.add_parser("review", help="复盘案例库：录入值得复盘的买点，自动做框架归因回放与前瞻收益结算")
    p_rev_sub = p_rev.add_subparsers(dest="review_action", required=True)

    p_rev_add = p_rev_sub.add_parser("add", help="录入一个复盘案例（录入即归因：框架当时为什么没选/选了它）")
    p_rev_add.add_argument("ts_code", help="股票代码，如 600487 或 600487.SH")
    p_rev_add.add_argument("date", help="买点所在交易日 YYYYMMDD")
    p_rev_add.add_argument("--note", default="", help="复盘记录：这个买点好在哪 / 框架错在哪")
    p_rev_add.add_argument("--tags", default="", help="标签，逗号分隔（如 缩量回踩,放量长阳），供日后聚类")
    p_rev_add.add_argument(
        "--source",
        choices=["manual", "missed", "failed"],
        default="manual",
        help="案例来源：manual=人工复盘（默认）/ missed=错过 / failed=失误",
    )
    p_rev_add.add_argument("--theme-lookback", type=int, default=None, help="主线强度统计窗口（默认与扫描一致）")
    p_rev_add.add_argument("--no-theme", action="store_true", help="跳过分组强度快照补算（快，但主线归属会缺失）")
    p_rev_add.add_argument("--json", action="store_true", help="JSON输出")

    p_rev_ls = p_rev_sub.add_parser("list", help="列出复盘案例")
    p_rev_ls.add_argument("--limit", type=int, default=20, help="最多列出几条")
    p_rev_ls.add_argument("--source", choices=["manual", "missed", "failed"], default=None, help="按来源过滤")
    p_rev_ls.add_argument("--status", default=None, help="按状态过滤（open/lesson_linked/closed）")
    p_rev_ls.add_argument("--json", action="store_true", help="JSON输出")

    p_rev_show = p_rev_sub.add_parser("show", help="查看单个案例的完整归因")
    p_rev_show.add_argument("id", type=int, help="案例 ID")
    p_rev_show.add_argument("--json", action="store_true", help="JSON输出")

    p_rev_settle = p_rev_sub.add_parser("settle", help="补算未满 30 个交易日的案例前瞻收益")
    p_rev_settle.add_argument("--json", action="store_true", help="JSON输出")

    # ── monitor ──
    p_monitor = subparsers.add_parser("monitor", help="自选股主动预警与扫描推送")
    p_monitor.add_argument("--days", type=int, default=30, help="同步 K 线回溯天数")
    p_monitor.add_argument("--no-push", action="store_true", help="关闭推送通知")
    p_monitor.add_argument("--json", action="store_true", help="JSON输出")

    return parser


def main():
    """zt CLI 主入口"""
    parser = build_parser()
    args = parser.parse_args()

    # 调度表
    from modules.cli_commands import cmd_backtest, cmd_trade, cmd_daily, cmd_monitor

    handlers = {
        "analyze": cmd_analyze,
        "screen": cmd_screen,
        "score": cmd_score,
        "workflow": cmd_workflow,
        "diagnose": cmd_diagnose,
        "watchlist": cmd_watchlist,
        "sync": cmd_sync,
        "theme": cmd_theme,
        "buy": cmd_buy,
        "scan": cmd_scan,
        "replay": cmd_replay,
        "review": cmd_review,
        "amv": cmd_amv,
        "advanced": cmd_advanced,
        "backtest": cmd_backtest,
        "trade": cmd_trade,
        "daily": cmd_daily,
        "monitor": cmd_monitor,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    # 取消代理，避免 Tushare 连接问题（仅脚本直调时，不影响库导入）
    os.environ["HTTP_PROXY"] = ""
    os.environ["HTTPS_PROXY"] = ""
    main()
