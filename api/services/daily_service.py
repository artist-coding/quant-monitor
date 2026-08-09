"""每日选股：活跃市值录入 → 全市场扫描 → 交给 Kimi 复核。

扫描为什么必须做成异步任务
--------------------------

全市场扫描实测 4999 只票要 145 秒，而前端 axios 的超时是 120 秒——同步返回
必然超时。这里沿用 research_service 的模式：POST 立刻返回 task_id，
前端轮询进度。任务落盘到 ``data/daily_scans/``，刷新页面也能接回去。

三层职责
--------

- **活跃市值**（``modules.amv``）：选股总开关，用户收盘后录入。
- **量化扫描**（``modules.buy_decision.scan_market``）：技术面把关 + 板块强弱排序。
- **Kimi 复核**（``research_service.create_selection``）：龙虎榜、题材归属、
  公告消息面——这些本地都没有数据源（Tushare 的 top_list / limit_list_d
  在本账号下是"无接口访问权限"），只能联网查。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 同时只允许一个扫描在跑：它是 CPU 密集的（5000 只票逐只算指标），
# 并发两个只会互相拖慢，还会让 SQLite 写锁竞争。
_MAX_CONCURRENT_SCANS = 1
# 保留最近多少次扫描结果，超出的按时间清掉
_KEEP_SCANS = 50


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ScanBusyError(RuntimeError):
    """已有扫描在执行。"""


class DailyService:
    def __init__(self, data_dir: Path | None = None) -> None:
        base = data_dir or Path(os.getenv("DATA_DIR", "data")) / "daily_scans"
        if not base.is_absolute():
            base = PROJECT_ROOT / base
        self.data_dir = base.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_SCANS, thread_name_prefix="daily-scan")
        self._lock = threading.Lock()

    # ==================== 活跃市值 ====================

    def amv_status(self, trade_date: str | None = None, segments: int = 8) -> dict[str, Any]:
        from modules import amv

        day = amv.get_regime(trade_date)
        return {
            "available": day is not None,
            "trade_date": day.trade_date if day else "",
            "close": day.close if day else 0.0,
            "pct_chg": day.pct_chg if day else None,
            "regime": day.regime if day else "",
            "can_select": bool(day and day.can_select),
            "bull_threshold": amv.BULL_THRESHOLD,
            "bear_threshold": amv.BEAR_THRESHOLD,
            "segments": amv.regime_segments(segments),
            "recent": [
                {"trade_date": d.trade_date, "close": d.close, "pct_chg": d.pct_chg, "regime": d.regime}
                for d in amv.recent(15, end_date=trade_date)
            ],
        }

    def amv_add(self, trade_date: str, close: float | None, pct_chg: float | None) -> dict[str, Any]:
        from modules import amv

        amv.add_daily(trade_date, close=close, pct_chg=pct_chg)
        status = self.amv_status()
        # 只给涨幅时结论可能在 -2.3% 边界上翻转，如实告知而不是默默接受
        status["precision_warning"] = (
            "只提供了涨幅。若它是四舍五入到两位小数的值，在 -2.3% 边界附近可能判出相反的区间"
            "（实测 -2.295% 与 -2.303% 都显示 -2.30%，一个多头一个空头）。建议改用收盘价。"
            if close is None
            else ""
        )
        return status

    # ==================== 主线 ====================

    def list_themes(self) -> list[dict[str, Any]]:
        from modules.themes import list_themes

        return list_themes()

    def upsert_theme(self, name: str, description: str = "", active: bool = True) -> list[dict[str, Any]]:
        from modules.themes import upsert_theme

        upsert_theme(name, description, active=active)
        return self.list_themes()

    def remove_theme(self, name: str) -> list[dict[str, Any]]:
        from modules.themes import remove_theme

        remove_theme(name)
        return self.list_themes()

    def set_theme_members(self, name: str, codes: list[str], source: str = "manual") -> dict[str, Any]:
        """整体替换某条主线的成员。

        replace=True 是有意的：前端是"编辑这条主线的成员清单"的语义，
        不带 replace 会让删掉的票残留在库里。
        """
        from modules.themes import get_theme_members, import_members

        records = [{"theme": name, "ts_code": c} for c in codes if c and c.strip()]
        if records:
            import_members(records, source=source, replace=True)
        else:
            # 清空成员：import_members 对空列表什么都不做，这里显式删
            from modules.database import get_connection

            with get_connection() as conn:
                conn.execute("DELETE FROM theme_members WHERE theme = ?", (name,))
        return {"theme": name, "members": get_theme_members(name)}

    def theme_ranking(self, trade_date: str | None = None, lookback: int = 5) -> dict[str, Any]:
        from modules.buy_decision import _latest_trade_date
        from modules.themes import rank_themes

        target = trade_date or _latest_trade_date()
        if not target:
            return {"trade_date": "", "themes": [], "industries": [], "reason": "库内没有任何日线数据"}
        res = rank_themes(target, lookback=lookback)

        def _pack(groups):
            return [
                {
                    "theme": g.name,
                    "kind": g.kind,
                    "strength": round(g.strength, 2),
                    "excess": round(g.excess, 2),
                    "rank": g.rank,
                    "member_count": g.member_count,
                    "median_pct_chg": round(g.median_pct_chg, 2),
                    "limit_up_count": g.limit_up_count,
                }
                for g in groups
            ]

        return {
            "trade_date": res["trade_date"],
            "lookback": res["lookback"],
            "window": res.get("window") or [],
            "themes": _pack(res.get("themes") or []),
            "industries": _pack(res.get("industries") or [])[:20],
            "dropped_themes": res.get("dropped_themes") or [],
        }

    # ==================== 扫描任务 ====================

    def _path(self, scan_id: str) -> Path:
        return self.data_dir / f"{scan_id}.json"

    def _write(self, task: dict[str, Any]) -> None:
        path = self._path(task["scan_id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{32}", scan_id):
            return None
        path = self._path(scan_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("读取扫描任务失败: %s", scan_id)
            return None

    def latest_scan(self) -> dict[str, Any] | None:
        paths = sorted(self.data_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return self.get_scan(paths[0].stem) if paths else None

    def list_scans(self, limit: int = 20) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.data_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            task = self.get_scan(path.stem)
            if task:
                out.append(
                    {
                        "scan_id": task["scan_id"],
                        "trade_date": task.get("trade_date", ""),
                        "status": task.get("status", ""),
                        "created_at": task.get("created_at", ""),
                        "blocked": task.get("blocked", ""),
                        "pick_count": len(task.get("picks") or []),
                        "buy_count": task.get("counts", {}).get("BUY", 0),
                    }
                )
        return out

    def create_scan(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            running = [
                t
                for t in (self.get_scan(p.stem) for p in self.data_dir.glob("*.json"))
                if t and t.get("status") in ("queued", "running")
            ]
            if len(running) >= _MAX_CONCURRENT_SCANS:
                raise ScanBusyError("已有扫描在执行，请等它跑完（全市场约需 2~3 分钟）")

            scan_id = uuid4().hex
            task = {
                "scan_id": scan_id,
                "status": "queued",
                "progress": 3,
                "message": "扫描任务已排队",
                "created_at": _now(),
                "started_at": "",
                "completed_at": "",
                "params": params,
                "trade_date": params.get("trade_date") or "",
                "amv": None,
                "position_hint": {},
                "market": {},
                "warnings": [],
                "blocked": "",
                "scanned": 0,
                "counts": {},
                "picks": [],
                "rejected": [],
                "error": "",
                "review_task_id": "",
            }
            self._write(task)
            self._prune()
        self._executor.submit(self._run_scan, scan_id)
        return task

    def _prune(self) -> None:
        paths = sorted(self.data_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths[_KEEP_SCANS:]:
            try:
                path.unlink()
            except OSError:
                pass

    def _run_scan(self, scan_id: str) -> None:
        task = self.get_scan(scan_id)
        if not task:
            return
        params = task.get("params") or {}
        task.update(status="running", progress=10, message="正在检查活跃市值区间…", started_at=_now())
        self._write(task)

        try:
            from modules.buy_decision import save_buy_decisions, scan_market

            # 必须先刷主线强度再扫描：scan_market 的第二阶段读的是 theme_strength
            # 快照表，不会自己重算。前端"刚加完主线就点扫描"的路径下，新主线在表里
            # 根本不存在，个股会静默退回行业兜底——看起来像主线没起作用。
            # 排名只要 1~2 秒，相对 160 秒的扫描可以忽略，索性每次都刷。
            # 扫描前产生的告警要单独攒着：下面 task.update(warnings=...) 是整体赋值，
            # 直接往 task["warnings"] 里 append 会被它覆盖掉。
            pre_warnings: list[str] = []
            task.update(progress=8, message="正在刷新主线/行业强度…")
            self._write(task)
            try:
                ranking = self.theme_ranking(params.get("trade_date") or None, int(params.get("theme_lookback", 5)))
                task["theme_ranking"] = {
                    "trade_date": ranking.get("trade_date", ""),
                    "themes": ranking.get("themes", []),
                    "dropped_themes": ranking.get("dropped_themes", []),
                }
            except Exception as exc:
                logger.warning("主线强度刷新失败，扫描将使用上一次的快照: %s", exc)
                pre_warnings.append(f"主线强度刷新失败，本次沿用上一次快照: {exc}")

            task.update(progress=20, message="正在全市场扫描（约 2~3 分钟）…")
            self._write(task)

            result = scan_market(
                trade_date=params.get("trade_date") or None,
                market_gate=params.get("market_gate", "on"),
                top_n=int(params.get("top_n", 5)),
                min_group_strength=float(params.get("min_group_strength", 50.0)),
                max_per_group=params.get("max_per_group"),
                include_watch=bool(params.get("include_watch", False)),
                theme_lookback=int(params.get("theme_lookback", 5)),
            )

            selection = result.get("selection") or {}
            decisions = result.get("decisions") or []
            counts = {a: sum(1 for d in decisions if d.action == a) for a in ("BUY", "WATCH", "NONE")}
            stopped: dict[str, int] = {}
            for d in decisions:
                if d.action == "NONE":
                    key = str(d.detail.get("stopped_at") or "other")
                    stopped[key] = stopped.get(key, 0) + 1

            if decisions and params.get("save", True):
                save_buy_decisions(decisions, only_actionable=True)

            task.update(
                status="completed",
                progress=100,
                message=result.get("blocked") or f"扫描完成，选出 {len(selection.get('picks') or [])} 只",
                completed_at=_now(),
                trade_date=result.get("trade_date", ""),
                amv=result.get("amv"),
                position_hint=result.get("position_hint") or {},
                market=result.get("market") or {},
                warnings=pre_warnings + (result.get("warnings") or []),
                blocked=result.get("blocked", ""),
                scanned=result.get("scanned", 0),
                elapsed=result.get("elapsed", 0),
                counts=counts,
                stopped=stopped,
                picks=[
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
                rejected=[
                    {
                        "ts_code": e["decision"].ts_code,
                        "name": e["decision"].name,
                        "score": round(e["decision"].score, 2),
                        "reason": e["reason"],
                    }
                    for e in (selection.get("rejected") or [])[:30]
                ],
            )
            self._write(task)
        except Exception as exc:
            logger.exception("每日扫描失败: %s", scan_id)
            task.update(status="failed", progress=100, error=str(exc), message=f"扫描失败: {exc}", completed_at=_now())
            self._write(task)

    # ==================== 交给 Kimi 复核 ====================

    def create_review(self, scan_id: str) -> dict[str, Any]:
        """把扫描选出的标的交给 Kimi Swarm 做最终买入复核。"""
        from api.services.research_service import research_service

        task = self.get_scan(scan_id)
        if not task:
            raise ValueError("扫描任务不存在")
        if task.get("status") != "completed":
            raise ValueError(f"扫描尚未完成（当前 {task.get('status')}），无法发起复核")
        picks = task.get("picks") or []
        if not picks:
            raise ValueError(
                task.get("blocked") or "本次扫描没有选出任何标的，无需复核——今日不买入是一个合法结论"
            )

        review = research_service.create_selection(
            trade_date=task.get("trade_date", ""),
            candidates=picks,
            themes=self.list_themes(),
            market={"amv": task.get("amv") or {}, "position_hint": task.get("position_hint") or {}},
        )
        task["review_task_id"] = review["task_id"]
        self._write(task)
        return review


daily_service = DailyService()
