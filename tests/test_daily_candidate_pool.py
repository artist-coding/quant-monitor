"""Tests for the end-of-day candidate-pool persistence and API."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from modules.daily_portfolio.buy_points import BuyPointStatus
from modules.daily_portfolio.candidate_pool import (
    CandidatePoolDataError,
    CandidatePoolRefreshConfig,
    CandidateRunStatus,
    CandidateUniverse,
    ExplicitMarketSnapshot,
    get_candidate_pool,
    refresh_candidate_pool,
)
from modules.daily_portfolio.models import TradeAction


def _rows(ts_code: str, count: int = 130, *, start: str = "20260101") -> list[dict]:
    day = datetime.strptime(start, "%Y%m%d")
    close = 10.0
    result = []
    for index in range(count):
        previous = close
        close = previous * (1.001 + ((index % 5) - 2) * 0.0001)
        volume = 1_000_000 + index * 1_000
        result.append(
            {
                "ts_code": ts_code,
                "trade_date": (day + timedelta(days=index)).strftime("%Y%m%d"),
                "open": previous,
                "high": max(previous, close) * 1.01,
                "low": min(previous, close) * 0.99,
                "close": close,
                "vol": volume,
                "amount": volume * close,
                "pct_chg": (close / previous - 1) * 100,
            }
        )
    return result


def _write_rows(conn, rows: list[dict], *, name: str = "Test Stock") -> None:
    ts_code = rows[0]["ts_code"]
    conn.execute(
        """
        INSERT OR REPLACE INTO stock_basic
        (ts_code, name, area, industry, market, list_date, is_hs)
        VALUES (?, ?, '', '', '主板', '20000101', '')
        """,
        (ts_code, name),
    )
    conn.executemany(
        """
        INSERT INTO daily_kline
        (ts_code, trade_date, open, high, low, close, vol, amount, pct_chg)
        VALUES (:ts_code, :trade_date, :open, :high, :low, :close, :vol, :amount, :pct_chg)
        """,
        rows,
    )
    conn.commit()


def _explicit_config(as_of_date: str, *codes: str, **overrides) -> CandidatePoolRefreshConfig:
    values = {
        "as_of_date": as_of_date,
        "universe": CandidateUniverse.EXPLICIT,
        "ts_codes": tuple(codes),
        "minimum_buy_score": 0,
        "minimum_market_coverage": 1,
    }
    values.update(overrides)
    return CandidatePoolRefreshConfig(**values)


def test_refresh_persists_canonical_daily_score(temp_db, db_conn) -> None:
    rows = _rows("000001.SZ")
    _write_rows(db_conn, rows, name="Alpha")
    as_of = rows[-1]["trade_date"]

    result = refresh_candidate_pool(
        _explicit_config(as_of, "000001.SZ"),
        explicit_market=ExplicitMarketSnapshot(55, source_hash="test-market"),
    )

    assert result.status == CandidateRunStatus.SUCCEEDED
    assert result.requested_count == 1
    assert result.scored_count == 1
    assert result.skipped_count == 0
    with db_conn:
        score = db_conn.execute("SELECT * FROM daily_stock_scores").fetchone()
        run = db_conn.execute(
            "SELECT * FROM daily_candidate_pool_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    assert score["trade_date"] == as_of
    assert score["ts_code"] == "000001.SZ"
    assert score["market_source_hash"] == "test-market"
    assert run["status"] == "SUCCEEDED"
    assert run["scored_count"] == 1

    repeated = refresh_candidate_pool(
        _explicit_config(as_of, "000001.SZ"),
        explicit_market=ExplicitMarketSnapshot(55, source_hash="test-market"),
    )
    assert repeated.run_id != result.run_id
    assert db_conn.execute("SELECT COUNT(*) FROM daily_stock_scores").fetchone()[0] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM daily_candidate_pool_runs").fetchone()[0] == 2


def test_stale_stock_rejects_entire_refresh_by_default(temp_db, db_conn) -> None:
    rows = _rows("000001.SZ")
    _write_rows(db_conn, rows)
    stale_as_of = (datetime.strptime(rows[-1]["trade_date"], "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")

    with pytest.raises(CandidatePoolDataError, match="stale/missing"):
        refresh_candidate_pool(
            _explicit_config(stale_as_of, "000001.SZ"),
            explicit_market=ExplicitMarketSnapshot(50),
        )

    assert db_conn.execute("SELECT COUNT(*) FROM daily_candidate_pool_runs").fetchone()[0] == 0


def test_market_context_can_be_derived_from_complete_daily_breadth(temp_db, db_conn) -> None:
    first = _rows("000001.SZ")
    second = _rows("000002.SZ")
    first[-1]["pct_chg"] = 1.0
    second[-1]["pct_chg"] = -1.0
    _write_rows(db_conn, first)
    _write_rows(db_conn, second)

    result = refresh_candidate_pool(
        _explicit_config(
            first[-1]["trade_date"],
            "000001.SZ",
            "000002.SZ",
            minimum_market_coverage=2,
        )
    )

    assert result.market_snapshot.score == 50
    assert result.market_snapshot.version == "market-breadth-advance-decline-v1"
    assert len(result.market_snapshot.source_hash) == 64


def test_candidate_ranking_and_latest_pool_query(temp_db, db_conn, monkeypatch) -> None:
    first = _rows("000001.SZ")
    second = _rows("000002.SZ")
    _write_rows(db_conn, first, name="Alpha")
    _write_rows(db_conn, second, name="Beta")
    as_of = first[-1]["trade_date"]

    def fake_evaluate(ts_code, as_of_date, bars, position, market, **kwargs):
        buy_score = 88.0 if ts_code == "000002.SZ" else 76.0
        score = SimpleNamespace(
            ts_code=ts_code,
            signal_date=as_of_date,
            buy_score=buy_score,
            sell_score=12.0,
            position_score=75.0,
            target_position_pct=0.0,
            desired_action=TradeAction.WATCH,
            buy_contributions={"entry_structure": 20.0},
            sell_contributions={},
            reasons=("test",),
            vetoes=(),
            hard_exit_reasons=(),
            strategy_version="test-strategy",
            parameter_version="test-params",
            parameter_fingerprint="test-fingerprint",
        )
        buy_point = SimpleNamespace(
            status=BuyPointStatus.CANDIDATE,
            confirmed=False,
            primary_confirming_variant="",
            primary_variant="b1.loose_3of4",
            reference_close=bars[-1].close,
            planned_stop_loss=bars[-1].close * 0.95,
            estimated_risk_pct=0.05,
            rule_qualification="UNVALIDATED_RESEARCH_RULE",
        )
        buy = SimpleNamespace(
            as_mapping=lambda: {
                "entry_structure": 80.0,
                "trend": 70.0,
                "volume": 60.0,
                "pattern_quality": 50.0,
                "stage": 50.0,
                "market": market.score,
                "resonance": 25.0,
            }
        )
        score_evidence = SimpleNamespace(buy=buy, risk_penalty_points=0.0)
        adapted = SimpleNamespace(score_evidence=score_evidence)
        return SimpleNamespace(score=score, buy_point=buy_point, adapted_evidence=adapted)

    monkeypatch.setattr("modules.daily_portfolio.candidate_pool.evaluate_daily_bar", fake_evaluate)
    result = refresh_candidate_pool(
        _explicit_config(as_of, "000001.SZ", "000002.SZ", minimum_buy_score=70),
        explicit_market=ExplicitMarketSnapshot(60),
    )

    assert [item.ts_code for item in result.candidates] == [
        "000002.SZ",
        "000001.SZ",
    ]
    latest = get_candidate_pool(trade_date=as_of)
    assert latest["run_id"] == result.run_id
    assert [item["rank"] for item in latest["candidates"]] == [1, 2]
    assert latest["candidates"][0]["name"] == "Beta"


def test_api_refresh_and_query_empty_candidate_pool(temp_db, db_conn) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from api.main import app

    rows = _rows("000001.SZ")
    _write_rows(db_conn, rows)
    client = TestClient(app)
    payload = {
        "as_of_date": rows[-1]["trade_date"],
        "universe": "EXPLICIT",
        "ts_codes": ["000001.SZ"],
        "minimum_market_coverage": 1,
        "market_context": {"score": 50, "source_hash": "api-test"},
    }

    response = client.post("/api/v1/daily-candidates/refresh", json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["scored_count"] == 1
    query = client.get(
        "/api/v1/daily-candidates/",
        params={"trade_date": rows[-1]["trade_date"]},
    )
    assert query.status_code == 200, query.text
    assert query.json()["run_id"] == body["run_id"]


def test_api_returns_conflict_for_stale_data(temp_db, db_conn) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from api.main import app

    rows = _rows("000001.SZ")
    _write_rows(db_conn, rows)
    stale_as_of = (datetime.strptime(rows[-1]["trade_date"], "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
    client = TestClient(app)

    response = client.post(
        "/api/v1/daily-candidates/refresh",
        json={
            "as_of_date": stale_as_of,
            "universe": "EXPLICIT",
            "ts_codes": ["000001.SZ"],
            "market_context": {"score": 50},
        },
    )

    assert response.status_code == 409
    assert "stale/missing" in response.json()["detail"]
