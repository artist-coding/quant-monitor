"""API adapter for the daily candidate-pool domain service."""

from api.models.daily_candidates import CandidatePoolRefreshRequest
from modules.daily_portfolio.candidate_pool import (
    CandidatePoolRefreshConfig,
    CandidateUniverse,
    ExplicitMarketSnapshot,
    get_candidate_pool,
    get_candidate_run,
    refresh_candidate_pool,
)


def refresh(req: CandidatePoolRefreshRequest) -> dict:
    market = None
    if req.market_context is not None:
        market = ExplicitMarketSnapshot(
            score=req.market_context.score,
            version=req.market_context.version,
            source_hash=req.market_context.source_hash,
        )
    config = CandidatePoolRefreshConfig(
        as_of_date=req.as_of_date,
        universe=CandidateUniverse(req.universe),
        ts_codes=tuple(req.ts_codes),
        lookback_bars=req.lookback_bars,
        max_position_pct=req.max_position_pct,
        minimum_buy_score=req.minimum_buy_score,
        candidate_statuses=tuple(req.candidate_statuses),
        top_n=req.top_n,
        minimum_market_coverage=req.minimum_market_coverage,
        allow_partial=req.allow_partial,
    )
    return refresh_candidate_pool(config, explicit_market=market).as_dict()


def list_candidates(*, trade_date: str | None = None, limit: int = 100) -> dict:
    return get_candidate_pool(trade_date=trade_date, limit=limit)


def get_run(run_id: str) -> dict:
    return get_candidate_run(run_id)


__all__ = ["get_run", "list_candidates", "refresh"]
