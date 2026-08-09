"""每日选股路由：活跃市值 → 全市场扫描 → Kimi 复核。"""

from fastapi import APIRouter, HTTPException, Query

from api.models.daily import (
    AmvAddRequest,
    AmvStatusResponse,
    ScanCreateRequest,
    ScanListResponse,
    ScanResponse,
    ThemeListResponse,
    ThemeMembersRequest,
    ThemeMembersResponse,
    ThemeRankingResponse,
    ThemeUpsertRequest,
)
from api.models.research import ResearchTaskResponse
from api.services.daily_service import ScanBusyError, daily_service
from api.services.research_service import ResearchBusyError

router = APIRouter()


# ==================== 活跃市值 ====================


@router.get("/amv", response_model=AmvStatusResponse)
def get_amv(trade_date: str = Query(default="", max_length=12)):
    return daily_service.amv_status(trade_date or None)


@router.post("/amv", response_model=AmvStatusResponse)
def add_amv(req: AmvAddRequest):
    try:
        return daily_service.amv_add(req.trade_date, req.close, req.pct_chg)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ==================== 主线 ====================


@router.get("/themes", response_model=ThemeListResponse)
def list_themes():
    return {"themes": daily_service.list_themes()}


@router.post("/themes", response_model=ThemeListResponse)
def upsert_theme(req: ThemeUpsertRequest):
    try:
        return {"themes": daily_service.upsert_theme(req.name, req.description, req.active)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.delete("/themes/{name}", response_model=ThemeListResponse)
def remove_theme(name: str):
    return {"themes": daily_service.remove_theme(name)}


@router.put("/themes/{name}/members", response_model=ThemeMembersResponse)
def set_theme_members(name: str, req: ThemeMembersRequest):
    return daily_service.set_theme_members(name, req.codes)


@router.get("/themes/ranking", response_model=ThemeRankingResponse)
def theme_ranking(
    trade_date: str = Query(default="", max_length=12),
    lookback: int = Query(default=5, ge=1, le=60),
):
    return daily_service.theme_ranking(trade_date or None, lookback)


# ==================== 扫描 ====================


@router.post("/scan", response_model=ScanResponse, status_code=202)
def create_scan(req: ScanCreateRequest):
    try:
        return daily_service.create_scan(req.model_dump())
    except ScanBusyError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"创建扫描任务失败: {exc}")


@router.get("/scan/latest", response_model=ScanResponse)
def latest_scan():
    task = daily_service.latest_scan()
    if not task:
        raise HTTPException(status_code=404, detail="还没有任何扫描记录")
    return task


@router.get("/scan/list", response_model=ScanListResponse)
def list_scans(limit: int = Query(default=20, ge=1, le=50)):
    return {"scans": daily_service.list_scans(limit)}


@router.get("/scan/{scan_id}", response_model=ScanResponse)
def get_scan(scan_id: str):
    task = daily_service.get_scan(scan_id)
    if not task:
        raise HTTPException(status_code=404, detail="扫描任务不存在")
    return task


# ==================== Kimi 复核 ====================


@router.post("/scan/{scan_id}/review", response_model=ResearchTaskResponse, status_code=202)
def create_review(scan_id: str):
    """把扫描选出的标的交给 Kimi Swarm 做最终买入复核（龙虎榜 / 题材 / 消息面）。"""
    try:
        return daily_service.create_review(scan_id)
    except ResearchBusyError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"创建复核任务失败: {exc}")
