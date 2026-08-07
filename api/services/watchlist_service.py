"""自选股管理服务"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _quant_snapshot(item: dict) -> dict:
    """为自选股补充本地行情、指标与四维系统评分。"""
    payload = {
        "trade_date": item.get("trade_date", "") or "",
        "price": item.get("price"),
        "pct_chg": item.get("pct_chg"),
        "vol": item.get("vol"),
        "amount": item.get("amount"),
        "kline_count": int(item.get("kline_count", 0) or 0),
        "data_ready": int(item.get("kline_count", 0) or 0) >= 30,
        "score": 0.0,
        "b1_score": 0.0,
        "trend_score": 0.0,
        "volume_score": 0.0,
        "risk_score": 0.0,
        "rating": "数据不足",
        "j": None,
        "vol_ratio": None,
        "macd_status": "--",
        "trend_status": "待补历史",
        "signal": "--",
    }
    if not payload["data_ready"]:
        return payload

    try:
        from modules.indicators import analyze_stock
        from modules.screener import analyze_stock as score_stock

        ts_code = item.get("ts_code", "")
        indicator = analyze_stock(ts_code, days=150)
        score = score_stock(ts_code)
        signal = getattr(indicator, "signal", "WATCH")
        signal_text = signal.value if hasattr(signal, "value") else str(signal)

        if getattr(indicator, "macd_gold_cross", False):
            macd_status = "金叉"
        elif getattr(indicator, "macd_dead_cross", False):
            macd_status = "死叉"
        elif indicator.dif > indicator.dea:
            macd_status = "偏多"
        else:
            macd_status = "偏空"

        trend_status = "强势" if score.trend_score >= 65 else "弱势" if score.trend_score <= 35 else "震荡"
        payload.update(
            score=score.score,
            b1_score=score.b1_score,
            trend_score=score.trend_score,
            volume_score=score.volume_score,
            risk_score=score.risk_score,
            rating=score.rating,
            j=round(indicator.j, 2),
            vol_ratio=round(indicator.vol_ratio, 2),
            macd_status=macd_status,
            trend_status=trend_status,
            signal=signal_text,
        )
    except Exception as exc:
        logger.warning("自选股量化快照失败 %s: %s", item.get("ts_code", ""), exc)
    return payload


def _watchlist_item(item: dict) -> dict:
    """把数据库召回结果整理成前端所需的完整自选股条目。"""
    result = {
        "id": item.get("id", 0),
        "ts_code": item.get("ts_code", ""),
        "name": item.get("name", ""),
        "tags": item.get("tags", ""),
        "notes": item.get("notes", ""),
        "added_date": item.get("added_date", ""),
        "alert_enabled": item.get("alert_enabled", True),
    }
    result.update(_quant_snapshot(item))
    return result


def list_watchlist(tags: str | None = None) -> dict:
    """列出自选股"""
    from modules.watchlist import list_watch

    items = list_watch(tags=tags)
    result_items = [_watchlist_item(item) for item in items]
    return {"count": len(result_items), "items": result_items}


def add_to_watchlist(ts_code: str, tags: str = "", notes: str = "") -> dict:
    """添加自选股，并立即从本地数据库召回核心信息。"""
    from modules.database import get_stock_core_info
    from modules.watchlist import add_watch, normalize_ts_code

    canonical = normalize_ts_code(ts_code)
    core = get_stock_core_info(canonical)
    if core is None:
        raise ValueError(f"本地数据库中未找到 {canonical}，请先同步股票基础信息")

    row_id = add_watch(canonical, name=core.get("name", ""), tags=tags, notes=notes)
    recalled = {
        **core,
        "id": row_id,
        "tags": tags,
        "notes": notes,
        "alert_enabled": True,
    }
    item = _watchlist_item(recalled)
    return {
        "status": "ok",
        "message": f"已从本地数据库载入 {item['name'] or canonical}",
        "item": item,
    }


def remove_from_watchlist(ts_code: str) -> dict:
    """从自选股移除"""
    from modules.watchlist import remove_watch

    success = remove_watch(ts_code)
    return {"status": "ok" if success else "not_found"}


def refresh_watchlist(days: int = 250) -> dict:
    """补齐自选股未复权历史日线并重算指标缓存。"""
    from modules.data_sync import DataSyncer
    from modules.watchlist import list_watch

    watches = list_watch()
    if not watches:
        return {"status": "empty", "stocks": 0, "kline_rows": 0, "indicator_rows": 0, "failures": []}

    syncer = DataSyncer()
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=max(365, days * 2))).strftime("%Y%m%d")
    kline_rows = 0
    indicator_rows = 0
    failures: list[str] = []
    for item in watches:
        ts_code = item["ts_code"]
        rows = syncer.sync_daily_kline(ts_code, start_date=start_date, end_date=end_date)
        if rows <= 0:
            failures.append(ts_code)
            continue
        kline_rows += rows
        indicator_rows += syncer.sync_indicator_cache(ts_code, days=days)

    return {
        "status": "success" if not failures else "partial",
        "stocks": len(watches),
        "kline_rows": kline_rows,
        "indicator_rows": indicator_rows,
        "failures": failures,
    }


def scan_watchlist() -> dict:
    """扫描自选股信号"""
    from modules.watchlist import scan_watchlist

    result = scan_watchlist()
    alerts = []
    for a in result.get("alerts", []):
        if hasattr(a, "ts_code"):
            alerts.append({
                "ts_code": a.ts_code,
                "name": a.name,
                "alert_type": a.alert_type,
                "level": a.level,
                "message": a.message,
            })
        elif isinstance(a, dict):
            alerts.append({
                "ts_code": a.get("ts_code", ""),
                "name": a.get("name", ""),
                "alert_type": a.get("alert_type", ""),
                "level": a.get("level", ""),
                "message": a.get("message", ""),
            })

    summary = result.get("summary", {})
    return {
        "total": summary.get("total", 0),
        "b1_count": summary.get("b1_count", 0),
        "b2_count": summary.get("b2_count", 0),
        "exit_count": summary.get("exit_count", 0),
        "break_count": summary.get("break_count", 0),
        "abnormal_count": summary.get("abnormal_count", 0),
        "alerts": alerts,
    }


def generate_report() -> dict:
    """生成日报"""
    from modules.watchlist import generate_daily_report

    report_text = generate_daily_report()
    return {"report": report_text}
