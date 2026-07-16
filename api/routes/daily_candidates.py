"""Daily stock candidate-pool endpoints."""

import logging

from fastapi import APIRouter, HTTPException, Query

from api.models.daily_candidates import (
    CandidatePoolListResponse,
    CandidatePoolRefreshRequest,
    CandidatePoolRefreshResponse,
    CandidatePoolRunResponse,
)
from api.services import daily_candidates_service
from modules.daily_portfolio.candidate_pool import (
    CandidatePoolDataError,
    CandidatePoolError,
)


logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/refresh", response_model=CandidatePoolRefreshResponse)
def refresh_candidate_pool(req: CandidatePoolRefreshRequest):
    """Rebuild one end-of-day candidate snapshot from confirmed daily bars."""

    try:
        return daily_candidates_service.refresh(req)
    except CandidatePoolDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CandidatePoolError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("daily candidate refresh failed")
        raise HTTPException(status_code=500, detail=f"candidate refresh failed: {exc}") from exc


@router.get("/", response_model=CandidatePoolListResponse)
def list_candidate_pool(
    trade_date: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
):
    """Return the latest completed pool, or the latest pool for one date."""

    try:
        return daily_candidates_service.list_candidates(trade_date=trade_date, limit=limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidatePoolError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("daily candidate query failed")
        raise HTTPException(status_code=500, detail=f"candidate query failed: {exc}") from exc


@router.get("/runs/{run_id}", response_model=CandidatePoolRunResponse)
def get_candidate_pool_run(run_id: str):
    """Return audit status and per-stock issues for one refresh."""

    try:
        return daily_candidates_service.get_run(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("daily candidate run query failed")
        raise HTTPException(status_code=500, detail=f"candidate run query failed: {exc}") from exc


__all__ = ["router"]
