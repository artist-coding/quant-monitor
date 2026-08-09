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
    python -m modules.cli daily-run --json

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
    print(f"  砖型图: {result.brick_trend}({result.brick_count}块)  值={result.brick_value:.2f}")

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
    """持仓诊断"""
    from modules.portfolio_diagnosis import diagnose_stock, format_report

    ts_code = args.ts_code
    diagnosis = diagnose_stock(ts_code, days=args.days)

    # ── JSON 输出 ──
    if args.json:
        from dataclasses import asdict

        _json_output(asdict(diagnosis))
        return

    # ── 人类可读输出（保持原样） ──
    print(format_report(diagnosis))


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


def cmd_daily_run(args):
    """每日收盘后全流程编排（同步 → 刷指标 → 评分落库）"""
    import logging
    from modules.daily_pipeline import format_pipeline_summary, run_daily_pipeline

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    result = run_daily_pipeline(
        trade_date=args.date,
        skip_market=args.skip_market,
        skip_index=args.skip_index,
        skip_indicators=args.skip_indicators,
        skip_scores=args.skip_scores,
        skip_themes=args.skip_themes,
        skip_buy=args.skip_buy,
        watchlist_days=args.watchlist_days,
        theme_lookback=args.theme_lookback,
    )

    if args.json:
        _json_output(result)
    else:
        print(format_pipeline_summary(result))

    if result["status"] == "failed":
        raise SystemExit(1)


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
            print(f"{r['name']:<18} {'启用' if r['active'] else '停用':<6} {r['member_count']:>5}  {r['description'][:40]}")
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
        confirm_buy_batch,
        format_buy_decision,
        format_buy_summary,
        save_buy_decisions,
    )

    codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()] if args.codes else []
    if not codes:
        from modules.watchlist import list_watch

        codes = [w["ts_code"] for w in list_watch() if w.get("ts_code")]
    if not codes:
        print("没有可确认的股票：未指定代码且票池为空")
        return

    decisions = confirm_buy_batch(codes, args.date, theme_lookback=args.theme_lookback)
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
  zt buy
  zt buy 601360.SH --detail
  zt daily-run
  zt daily-run --date 20260807 --json
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
    p_diag = subparsers.add_parser("diagnose", help="持仓诊断")
    p_diag.add_argument("ts_code", help="股票代码")
    p_diag.add_argument("--days", type=int, default=120, help="分析天数")
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

    # ── daily-run（每日收盘后全流程编排）──
    p_daily_run = subparsers.add_parser("daily-run", help="每日收盘后全流程：同步 → 刷指标 → 评分落库")
    p_daily_run.add_argument("--date", help="交易日期 YYYYMMDD，默认今天")
    p_daily_run.add_argument("--json", action="store_true", help="JSON输出")
    p_daily_run.add_argument("--skip-market", action="store_true", help="跳过全市场日线同步")
    p_daily_run.add_argument("--skip-index", action="store_true", help="跳过宽基指数日线同步")
    p_daily_run.add_argument("--skip-indicators", action="store_true", help="跳过票池 K 线补齐与指标缓存重算")
    p_daily_run.add_argument("--skip-scores", action="store_true", help="跳过票池评分落库")
    p_daily_run.add_argument("--skip-themes", action="store_true", help="跳过主线/行业强度排名")
    p_daily_run.add_argument("--skip-buy", action="store_true", help="跳过票池买点确认")
    p_daily_run.add_argument(
        "--watchlist-days", type=int, default=250, help="票池指标缓存回溯天数（双线战法需 ≥115，默认 250）"
    )
    p_daily_run.add_argument("--theme-lookback", type=int, default=5, help="主线强度统计窗口（交易日，默认 5）")

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
    p_theme_imp.add_argument("file", help='JSON 文件：[{"theme":"...","ts_code":"...","confidence":0.9,"reason":"..."}]')
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
    p_buy.add_argument("--json", action="store_true", help="JSON输出")

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
        "daily-run": cmd_daily_run,
        "theme": cmd_theme,
        "buy": cmd_buy,
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
