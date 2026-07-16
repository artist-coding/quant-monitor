"""Auditable end-of-day refresh for a daily stock candidate pool.

The refresh deliberately consumes already-confirmed SQLite daily bars.  It
does not fetch market data and it never substitutes a neutral market score.
Callers must sync data first, then invoke this module with the expected signal
date.  By default, one stale stock rejects the whole batch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import re
import sqlite3
from typing import Any
from uuid import uuid4

from ..database import (
    get_connection,
    init_daily_candidate_tables,
)
from ..indicators import DailyData
from .buy_points import BuyPointStatus
from .config import DailyPortfolioConfig
from .dates import normalize_trade_date
from .evidence_adapter import MarketSnapshot
from .models import PositionState
from .service import DailyScoreEvaluation, evaluate_daily_bar


_TS_CODE_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_ALLOWED_CANDIDATE_STATUSES = frozenset({BuyPointStatus.CANDIDATE.value, BuyPointStatus.CONFIRMED.value})


class CandidatePoolError(ValueError):
    """Base error for a rejected candidate-pool refresh."""


class CandidatePoolDataError(CandidatePoolError):
    """Raised when point-in-time market or stock data is incomplete."""


class CandidateUniverse(str, Enum):
    WATCHLIST = "WATCHLIST"
    EXPLICIT = "EXPLICIT"


class CandidateRunStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ExplicitMarketSnapshot:
    score: float
    version: str = "explicit-market-context-v1"
    source_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.score, (int, float)) or not math.isfinite(self.score):
            raise CandidatePoolError("market score must be a finite number")
        if not 0 <= self.score <= 100:
            raise CandidatePoolError("market score must be between 0 and 100")
        if not self.version.strip():
            raise CandidatePoolError("market version cannot be empty")


@dataclass(frozen=True)
class CandidatePoolRefreshConfig:
    as_of_date: str
    universe: CandidateUniverse = CandidateUniverse.WATCHLIST
    ts_codes: tuple[str, ...] = ()
    lookback_bars: int = 180
    max_position_pct: float = 0.10
    minimum_buy_score: float = 60.0
    candidate_statuses: tuple[str, ...] = (
        BuyPointStatus.CANDIDATE.value,
        BuyPointStatus.CONFIRMED.value,
    )
    top_n: int = 100
    minimum_market_coverage: int = 1000
    allow_partial: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of_date", normalize_trade_date(self.as_of_date))
        if not isinstance(self.universe, CandidateUniverse):
            raise CandidatePoolError("universe must be WATCHLIST or EXPLICIT")
        normalized_codes = tuple(_normalize_codes(self.ts_codes))
        object.__setattr__(self, "ts_codes", normalized_codes)
        if self.universe == CandidateUniverse.EXPLICIT and not normalized_codes:
            raise CandidatePoolError("EXPLICIT universe requires ts_codes")
        if self.universe == CandidateUniverse.WATCHLIST and normalized_codes:
            raise CandidatePoolError("WATCHLIST universe cannot include ts_codes")
        if not isinstance(self.lookback_bars, int) or self.lookback_bars < 120:
            raise CandidatePoolError("lookback_bars must be an integer of at least 120")
        if not 0 < self.max_position_pct <= 1:
            raise CandidatePoolError("max_position_pct must be in (0, 1]")
        if not 0 <= self.minimum_buy_score <= 100:
            raise CandidatePoolError("minimum_buy_score must be between 0 and 100")
        statuses = tuple(dict.fromkeys(self.candidate_statuses))
        if not statuses or not set(statuses).issubset(_ALLOWED_CANDIDATE_STATUSES):
            raise CandidatePoolError("candidate_statuses may only contain CANDIDATE and CONFIRMED")
        object.__setattr__(self, "candidate_statuses", statuses)
        if not isinstance(self.top_n, int) or self.top_n <= 0:
            raise CandidatePoolError("top_n must be a positive integer")
        if not isinstance(self.minimum_market_coverage, int) or self.minimum_market_coverage <= 0:
            raise CandidatePoolError("minimum_market_coverage must be a positive integer")

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of_date": self.as_of_date,
            "universe": self.universe.value,
            "ts_codes": list(self.ts_codes),
            "lookback_bars": self.lookback_bars,
            "max_position_pct": self.max_position_pct,
            "minimum_buy_score": self.minimum_buy_score,
            "candidate_statuses": list(self.candidate_statuses),
            "top_n": self.top_n,
            "minimum_market_coverage": self.minimum_market_coverage,
            "allow_partial": self.allow_partial,
        }


@dataclass(frozen=True)
class CandidatePoolIssue:
    ts_code: str
    issue_type: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "ts_code": self.ts_code,
            "issue_type": self.issue_type,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CandidatePoolItem:
    rank: int
    trade_date: str
    ts_code: str
    name: str
    buy_score: float
    sell_score: float
    buy_point_status: str
    desired_action: str
    primary_variant: str
    reference_close: float
    planned_stop_loss: float | None
    estimated_risk_pct: float | None
    buy_contributions: dict[str, float]
    rule_qualification: str
    strategy_version: str
    parameter_version: str
    parameter_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "trade_date": self.trade_date,
            "ts_code": self.ts_code,
            "name": self.name,
            "buy_score": self.buy_score,
            "sell_score": self.sell_score,
            "buy_point_status": self.buy_point_status,
            "desired_action": self.desired_action,
            "primary_variant": self.primary_variant,
            "reference_close": self.reference_close,
            "planned_stop_loss": self.planned_stop_loss,
            "estimated_risk_pct": self.estimated_risk_pct,
            "buy_contributions": dict(self.buy_contributions),
            "rule_qualification": self.rule_qualification,
            "strategy_version": self.strategy_version,
            "parameter_version": self.parameter_version,
            "parameter_fingerprint": self.parameter_fingerprint,
        }


@dataclass(frozen=True)
class CandidatePoolRefreshResult:
    run_id: str
    status: CandidateRunStatus
    trade_date: str
    universe: CandidateUniverse
    requested_count: int
    scored_count: int
    candidate_count: int
    skipped_count: int
    failed_count: int
    market_snapshot: MarketSnapshot
    strategy_version: str
    parameter_version: str
    parameter_fingerprint: str
    candidates: tuple[CandidatePoolItem, ...] = ()
    issues: tuple[CandidatePoolIssue, ...] = ()
    schema_version: str = "daily-candidate-pool-v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status.value,
            "trade_date": self.trade_date,
            "universe": self.universe.value,
            "requested_count": self.requested_count,
            "scored_count": self.scored_count,
            "candidate_count": self.candidate_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "market_context": {
                "trade_date": self.market_snapshot.trade_date,
                "score": self.market_snapshot.score,
                "version": self.market_snapshot.version,
                "source_hash": self.market_snapshot.source_hash,
            },
            "strategy_version": self.strategy_version,
            "parameter_version": self.parameter_version,
            "parameter_fingerprint": self.parameter_fingerprint,
            "candidates": [item.as_dict() for item in self.candidates],
            "issues": [item.as_dict() for item in self.issues],
        }


@dataclass
class _ScoredRecord:
    values: dict[str, Any]
    is_candidate: bool
    score_id: int | None = None


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _normalize_codes(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        code = str(value).strip().upper()
        if not _TS_CODE_RE.fullmatch(code):
            raise CandidatePoolError(f"invalid ts_code: {value!r}")
        if code not in seen:
            seen.add(code)
            result.append(code)
    return result


def ensure_candidate_pool_tables() -> None:
    """Upgrade an existing database without requiring a separate migration."""

    with get_connection() as conn:
        init_daily_candidate_tables(conn)


def _resolve_universe(conn: sqlite3.Connection, config: CandidatePoolRefreshConfig) -> tuple[str, ...]:
    if config.universe == CandidateUniverse.EXPLICIT:
        return config.ts_codes
    rows = conn.execute(
        """
        SELECT ts_code
        FROM watchlist
        WHERE COALESCE(alert_enabled, 1) = 1
        ORDER BY ts_code
        """
    ).fetchall()
    codes = tuple(_normalize_codes([str(row["ts_code"]) for row in rows]))
    if not codes:
        raise CandidatePoolError("watchlist has no enabled stocks")
    return codes


def _resolve_market_snapshot(
    conn: sqlite3.Connection,
    config: CandidatePoolRefreshConfig,
    explicit: ExplicitMarketSnapshot | None,
) -> MarketSnapshot:
    if explicit is not None:
        source_hash = explicit.source_hash.strip() or _sha256_json(
            {
                "trade_date": config.as_of_date,
                "score": explicit.score,
                "version": explicit.version,
            }
        )
        return MarketSnapshot(
            config.as_of_date,
            float(explicit.score),
            version=explicit.version.strip(),
            source_hash=source_hash,
        )

    rows = conn.execute(
        """
        SELECT ts_code, pct_chg
        FROM daily_kline
        WHERE trade_date = ?
          AND pct_chg IS NOT NULL
          AND open > 0 AND high > 0 AND low > 0 AND close > 0
        ORDER BY ts_code
        """,
        (config.as_of_date,),
    ).fetchall()
    if len(rows) < config.minimum_market_coverage:
        raise CandidatePoolDataError(
            f"market coverage is incomplete for {config.as_of_date}: {len(rows)} < {config.minimum_market_coverage}"
        )
    advances = sum(float(row["pct_chg"]) > 0 for row in rows)
    declines = sum(float(row["pct_chg"]) < 0 for row in rows)
    flats = len(rows) - advances - declines
    score = (advances + 0.5 * flats) / len(rows) * 100.0
    source_hash = _sha256_json(
        {
            "trade_date": config.as_of_date,
            "rows": [[str(row["ts_code"]), round(float(row["pct_chg"]), 8)] for row in rows],
        }
    )
    return MarketSnapshot(
        config.as_of_date,
        round(score, 6),
        version="market-breadth-advance-decline-v1",
        source_hash=source_hash,
    )


def _load_bars(
    conn: sqlite3.Connection,
    ts_code: str,
    *,
    as_of_date: str,
    lookback_bars: int,
) -> tuple[DailyData, ...]:
    rows = conn.execute(
        """
        SELECT ts_code, trade_date, open, high, low, close, vol, amount, pct_chg
        FROM daily_kline
        WHERE ts_code = ? AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (ts_code, as_of_date, lookback_bars),
    ).fetchall()
    ordered = list(reversed(rows))
    result: list[DailyData] = []
    previous_close: float | None = None
    for row in ordered:
        close = float(row["close"])
        prev_close = previous_close or close
        pct_chg = (
            float(row["pct_chg"])
            if row["pct_chg"] is not None
            else ((close / prev_close - 1) * 100 if prev_close > 0 else 0.0)
        )
        result.append(
            DailyData(
                ts_code=ts_code,
                trade_date=normalize_trade_date(str(row["trade_date"])),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=close,
                vol=float(row["vol"] or 0),
                amount=float(row["amount"] or 0),
                pct_chg=pct_chg,
                prev_close=prev_close,
            )
        )
        previous_close = close
    return tuple(result)


def _stock_names(conn: sqlite3.Connection, ts_codes: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for code in ts_codes:
        row = conn.execute("SELECT name FROM stock_basic WHERE ts_code = ?", (code,)).fetchone()
        result[code] = str(row["name"] or "") if row else ""
    return result


def _to_scored_record(
    evaluation: DailyScoreEvaluation,
    *,
    name: str,
    market: MarketSnapshot,
    config: CandidatePoolRefreshConfig,
) -> _ScoredRecord:
    score = evaluation.score
    buy_point = evaluation.buy_point
    components = evaluation.adapted_evidence.score_evidence.buy.as_mapping()
    primary_variant = buy_point.primary_confirming_variant or buy_point.primary_variant
    is_candidate = score.buy_score >= config.minimum_buy_score and buy_point.status.value in config.candidate_statuses
    values = {
        "trade_date": score.signal_date,
        "ts_code": score.ts_code,
        "name": name,
        "buy_score": score.buy_score,
        "sell_score": score.sell_score,
        "position_score": score.position_score,
        "target_position_pct": score.target_position_pct,
        "desired_action": score.desired_action.value,
        "buy_point_status": buy_point.status.value,
        "buy_point_confirmed": 1 if buy_point.confirmed else 0,
        "primary_variant": primary_variant,
        "reference_close": buy_point.reference_close,
        "planned_stop_loss": buy_point.planned_stop_loss,
        "estimated_risk_pct": buy_point.estimated_risk_pct,
        "entry_structure_score": components["entry_structure"],
        "trend_score": components["trend"],
        "volume_score": components["volume"],
        "pattern_quality_score": components["pattern_quality"],
        "stage_score": components["stage"],
        "market_score": components["market"],
        "resonance_score": components["resonance"],
        "risk_penalty": (evaluation.adapted_evidence.score_evidence.risk_penalty_points),
        "raw_components_json": _json(components),
        "score_contributions_json": _json(
            {
                "buy": score.buy_contributions,
                "sell": score.sell_contributions,
            }
        ),
        "reasons_json": _json(list(score.reasons)),
        "hard_vetoes_json": _json(list(score.vetoes)),
        "hard_exit_reasons_json": _json(list(score.hard_exit_reasons)),
        "market_version": market.version,
        "market_source_hash": market.source_hash,
        "rule_qualification": buy_point.rule_qualification,
        "strategy_version": score.strategy_version,
        "parameter_version": score.parameter_version,
        "parameter_fingerprint": score.parameter_fingerprint,
    }
    return _ScoredRecord(values=values, is_candidate=is_candidate)


_SCORE_COLUMNS = (
    "trade_date",
    "ts_code",
    "name",
    "buy_score",
    "sell_score",
    "position_score",
    "target_position_pct",
    "desired_action",
    "buy_point_status",
    "buy_point_confirmed",
    "primary_variant",
    "reference_close",
    "planned_stop_loss",
    "estimated_risk_pct",
    "entry_structure_score",
    "trend_score",
    "volume_score",
    "pattern_quality_score",
    "stage_score",
    "market_score",
    "resonance_score",
    "risk_penalty",
    "raw_components_json",
    "score_contributions_json",
    "reasons_json",
    "hard_vetoes_json",
    "hard_exit_reasons_json",
    "market_version",
    "market_source_hash",
    "rule_qualification",
    "strategy_version",
    "parameter_version",
    "parameter_fingerprint",
)


def _insert_run(
    run_id: str,
    config: CandidatePoolRefreshConfig,
    market: MarketSnapshot,
    score_config: DailyPortfolioConfig,
    requested_count: int,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO daily_candidate_pool_runs (
                run_id, trade_date, universe, status, requested_count,
                minimum_buy_score, candidate_statuses_json, top_n,
                lookback_bars, max_position_pct, market_score,
                market_version, market_source_hash, strategy_version,
                parameter_version, parameter_fingerprint, request_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                config.as_of_date,
                config.universe.value,
                CandidateRunStatus.RUNNING.value,
                requested_count,
                config.minimum_buy_score,
                _json(list(config.candidate_statuses)),
                config.top_n,
                config.lookback_bars,
                config.max_position_pct,
                market.score,
                market.version,
                market.source_hash,
                score_config.strategy_version,
                score_config.parameter_version,
                score_config.fingerprint(),
                _json(config.as_dict()),
            ),
        )


def _mark_run_failed(run_id: str, message: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE daily_candidate_pool_runs
            SET status = ?, error_message = ?, completed_at = CURRENT_TIMESTAMP
            WHERE run_id = ?
            """,
            (CandidateRunStatus.FAILED.value, message[:2000], run_id),
        )


def _persist_run(
    run_id: str,
    status: CandidateRunStatus,
    records: Sequence[_ScoredRecord],
    selected: Sequence[_ScoredRecord],
    issues: Sequence[CandidatePoolIssue],
) -> None:
    placeholders = ",".join("?" for _ in _SCORE_COLUMNS)
    sql = f"""
        INSERT INTO daily_stock_scores ({",".join(_SCORE_COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT(
            trade_date, ts_code, strategy_version,
            parameter_version, parameter_fingerprint
        ) DO NOTHING
    """
    with get_connection() as conn:
        for record in records:
            values = record.values
            conn.execute(sql, tuple(values[column] for column in _SCORE_COLUMNS))
            row = conn.execute(
                """
                SELECT * FROM daily_stock_scores
                WHERE trade_date = ? AND ts_code = ?
                  AND strategy_version = ? AND parameter_version = ?
                  AND parameter_fingerprint = ?
                """,
                (
                    values["trade_date"],
                    values["ts_code"],
                    values["strategy_version"],
                    values["parameter_version"],
                    values["parameter_fingerprint"],
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("persisted daily score could not be reloaded")
            mismatched = [column for column in _SCORE_COLUMNS if row[column] != values[column]]
            if mismatched:
                raise CandidatePoolDataError(
                    f"immutable daily score conflict for {values['ts_code']} {values['trade_date']}: {mismatched}"
                )
            record.score_id = int(row["id"])

        for rank, record in enumerate(selected, 1):
            if record.score_id is None:
                raise RuntimeError("candidate item is missing its score id")
            conn.execute(
                """
                INSERT INTO daily_candidate_pool_items (
                    run_id, score_id, trade_date, ts_code, candidate_rank
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    record.score_id,
                    record.values["trade_date"],
                    record.values["ts_code"],
                    rank,
                ),
            )
        for issue in issues:
            conn.execute(
                """
                INSERT INTO daily_candidate_pool_issues (
                    run_id, ts_code, issue_type, reason
                ) VALUES (?, ?, ?, ?)
                """,
                (run_id, issue.ts_code, issue.issue_type, issue.reason),
            )
        conn.execute(
            """
            UPDATE daily_candidate_pool_runs
            SET status = ?, scored_count = ?, candidate_count = ?,
                skipped_count = ?, failed_count = ?, completed_at = CURRENT_TIMESTAMP
            WHERE run_id = ?
            """,
            (
                status.value,
                len(records),
                len(selected),
                sum(item.issue_type == "SKIPPED" for item in issues),
                sum(item.issue_type == "FAILED" for item in issues),
                run_id,
            ),
        )


def _item_from_record(record: _ScoredRecord, rank: int) -> CandidatePoolItem:
    value = record.values
    contributions = json.loads(value["score_contributions_json"])["buy"]
    return CandidatePoolItem(
        rank=rank,
        trade_date=value["trade_date"],
        ts_code=value["ts_code"],
        name=value["name"],
        buy_score=float(value["buy_score"]),
        sell_score=float(value["sell_score"]),
        buy_point_status=value["buy_point_status"],
        desired_action=value["desired_action"],
        primary_variant=value["primary_variant"],
        reference_close=float(value["reference_close"]),
        planned_stop_loss=value["planned_stop_loss"],
        estimated_risk_pct=value["estimated_risk_pct"],
        buy_contributions={key: float(item) for key, item in contributions.items()},
        rule_qualification=value["rule_qualification"],
        strategy_version=value["strategy_version"],
        parameter_version=value["parameter_version"],
        parameter_fingerprint=value["parameter_fingerprint"],
    )


def refresh_candidate_pool(
    config: CandidatePoolRefreshConfig,
    *,
    explicit_market: ExplicitMarketSnapshot | None = None,
    score_config: DailyPortfolioConfig | None = None,
) -> CandidatePoolRefreshResult:
    """Score the requested universe and atomically publish one pool snapshot."""

    resolved_score_config = score_config or DailyPortfolioConfig()
    ensure_candidate_pool_tables()
    with get_connection() as conn:
        ts_codes = _resolve_universe(conn, config)
        market = _resolve_market_snapshot(conn, config, explicit_market)
        names = _stock_names(conn, ts_codes)
        bars_by_code = {
            code: _load_bars(
                conn,
                code,
                as_of_date=config.as_of_date,
                lookback_bars=config.lookback_bars,
            )
            for code in ts_codes
        }

    stale_issues = tuple(
        CandidatePoolIssue(
            ts_code=code,
            issue_type="SKIPPED",
            reason=(
                "MISSING_DAILY_BARS" if not bars_by_code[code] else f"STALE_BAR:{bars_by_code[code][-1].trade_date}"
            ),
        )
        for code in ts_codes
        if not bars_by_code[code] or normalize_trade_date(bars_by_code[code][-1].trade_date) != config.as_of_date
    )
    if stale_issues and not config.allow_partial:
        examples = ", ".join(f"{item.ts_code}={item.reason}" for item in stale_issues[:10])
        raise CandidatePoolDataError(
            f"candidate refresh rejected: {len(stale_issues)} stale/missing stocks; {examples}"
        )

    run_id = uuid4().hex
    _insert_run(
        run_id,
        config,
        market,
        resolved_score_config,
        requested_count=len(ts_codes),
    )
    records: list[_ScoredRecord] = []
    issues: list[CandidatePoolIssue] = list(stale_issues)
    stale_codes = {item.ts_code for item in stale_issues}
    try:
        for code in ts_codes:
            if code in stale_codes:
                continue
            try:
                evaluation = evaluate_daily_bar(
                    code,
                    config.as_of_date,
                    bars_by_code[code],
                    PositionState(ts_code=code),
                    market,
                    max_position_pct=config.max_position_pct,
                    config=resolved_score_config,
                )
                records.append(
                    _to_scored_record(
                        evaluation,
                        name=names.get(code, ""),
                        market=market,
                        config=config,
                    )
                )
            except Exception as exc:
                issues.append(
                    CandidatePoolIssue(
                        ts_code=code,
                        issue_type="FAILED",
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                )

        selected = sorted(
            (record for record in records if record.is_candidate),
            key=lambda record: (
                -float(record.values["buy_score"]),
                float(record.values["sell_score"]),
                str(record.values["ts_code"]),
            ),
        )[: config.top_n]
        if not records:
            status = CandidateRunStatus.FAILED
        elif issues:
            status = CandidateRunStatus.PARTIAL
        else:
            status = CandidateRunStatus.SUCCEEDED
        _persist_run(run_id, status, records, selected, issues)
    except Exception as exc:
        _mark_run_failed(run_id, f"{type(exc).__name__}: {exc}")
        raise

    candidates = tuple(_item_from_record(record, rank) for rank, record in enumerate(selected, 1))
    return CandidatePoolRefreshResult(
        run_id=run_id,
        status=status,
        trade_date=config.as_of_date,
        universe=config.universe,
        requested_count=len(ts_codes),
        scored_count=len(records),
        candidate_count=len(candidates),
        skipped_count=sum(item.issue_type == "SKIPPED" for item in issues),
        failed_count=sum(item.issue_type == "FAILED" for item in issues),
        market_snapshot=market,
        strategy_version=resolved_score_config.strategy_version,
        parameter_version=resolved_score_config.parameter_version,
        parameter_fingerprint=resolved_score_config.fingerprint(),
        candidates=candidates,
        issues=tuple(issues),
    )


def _row_to_item(row: Mapping[str, Any]) -> CandidatePoolItem:
    contributions = json.loads(str(row["score_contributions_json"]))["buy"]
    return CandidatePoolItem(
        rank=int(row["candidate_rank"]),
        trade_date=str(row["trade_date"]),
        ts_code=str(row["ts_code"]),
        name=str(row["name"] or ""),
        buy_score=float(row["buy_score"]),
        sell_score=float(row["sell_score"]),
        buy_point_status=str(row["buy_point_status"]),
        desired_action=str(row["desired_action"]),
        primary_variant=str(row["primary_variant"] or ""),
        reference_close=float(row["reference_close"]),
        planned_stop_loss=(float(row["planned_stop_loss"]) if row["planned_stop_loss"] is not None else None),
        estimated_risk_pct=(float(row["estimated_risk_pct"]) if row["estimated_risk_pct"] is not None else None),
        buy_contributions={key: float(item) for key, item in contributions.items()},
        rule_qualification=str(row["rule_qualification"]),
        strategy_version=str(row["strategy_version"]),
        parameter_version=str(row["parameter_version"]),
        parameter_fingerprint=str(row["parameter_fingerprint"]),
    )


def get_candidate_pool(*, trade_date: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Return the most recent completed pool, optionally for one signal date."""

    if not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise CandidatePoolError("limit must be between 1 and 1000")
    normalized_date = normalize_trade_date(trade_date) if trade_date else None
    ensure_candidate_pool_tables()
    with get_connection() as conn:
        params: list[Any] = [
            CandidateRunStatus.SUCCEEDED.value,
            CandidateRunStatus.PARTIAL.value,
        ]
        date_clause = ""
        if normalized_date:
            date_clause = " AND trade_date = ?"
            params.append(normalized_date)
        run = conn.execute(
            f"""
            SELECT * FROM daily_candidate_pool_runs
            WHERE status IN (?, ?){date_clause}
            ORDER BY trade_date DESC, completed_at DESC, started_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if run is None:
            raise LookupError("no completed daily candidate pool was found")
        rows = conn.execute(
            """
            SELECT i.candidate_rank, s.*
            FROM daily_candidate_pool_items AS i
            JOIN daily_stock_scores AS s ON s.id = i.score_id
            WHERE i.run_id = ?
            ORDER BY i.candidate_rank ASC
            LIMIT ?
            """,
            (run["run_id"], limit),
        ).fetchall()
    items = tuple(_row_to_item(dict(row)) for row in rows)
    return {
        "schema_version": "daily-candidate-pool-v1",
        "run_id": str(run["run_id"]),
        "status": str(run["status"]),
        "trade_date": str(run["trade_date"]),
        "universe": str(run["universe"]),
        "count": len(items),
        "total_candidates": int(run["candidate_count"]),
        "market_context": {
            "trade_date": str(run["trade_date"]),
            "score": float(run["market_score"]),
            "version": str(run["market_version"]),
            "source_hash": str(run["market_source_hash"]),
        },
        "candidates": [item.as_dict() for item in items],
    }


def get_candidate_run(run_id: str) -> dict[str, Any]:
    """Return persisted status and issues for one refresh run."""

    ensure_candidate_pool_tables()
    with get_connection() as conn:
        run = conn.execute("SELECT * FROM daily_candidate_pool_runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise LookupError(f"candidate pool run not found: {run_id}")
        issues = conn.execute(
            """
            SELECT ts_code, issue_type, reason
            FROM daily_candidate_pool_issues
            WHERE run_id = ?
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
    return {
        "run_id": str(run["run_id"]),
        "status": str(run["status"]),
        "trade_date": str(run["trade_date"]),
        "universe": str(run["universe"]),
        "requested_count": int(run["requested_count"]),
        "scored_count": int(run["scored_count"]),
        "candidate_count": int(run["candidate_count"]),
        "skipped_count": int(run["skipped_count"]),
        "failed_count": int(run["failed_count"]),
        "error_message": str(run["error_message"] or ""),
        "started_at": str(run["started_at"] or ""),
        "completed_at": str(run["completed_at"] or ""),
        "issues": [dict(item) for item in issues],
    }


__all__ = [
    "CandidatePoolDataError",
    "CandidatePoolError",
    "CandidatePoolIssue",
    "CandidatePoolItem",
    "CandidatePoolRefreshConfig",
    "CandidatePoolRefreshResult",
    "CandidateRunStatus",
    "CandidateUniverse",
    "ExplicitMarketSnapshot",
    "ensure_candidate_pool_tables",
    "get_candidate_pool",
    "get_candidate_run",
    "refresh_candidate_pool",
]
