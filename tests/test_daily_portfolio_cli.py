"""Focused tests for the isolated daily-portfolio CLI boundary."""

from __future__ import annotations

import ast
from datetime import date, timedelta
import inspect
import json
from pathlib import Path

import pytest

from modules.cli import build_parser
from modules.daily_portfolio.cli_adapter import (
    _load_position,
    cmd_daily_portfolio,
    load_exchange_calendar,
    load_market_snapshots,
)
from modules.daily_portfolio.models import DailyStockScore, TradeAction


TS_CODE = "000001.SZ"


class FakeDataSource:
    name = "fake-explicit-source"

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls: list[dict] = []

    def get_kline_dicts(
        self,
        ts_code: str,
        days: int = 60,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        self.calls.append(
            {
                "ts_code": ts_code,
                "days": days,
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        selected = [
            row
            for row in self.rows
            if (start_date is None or row["trade_date"] >= start_date)
            and (end_date is None or row["trade_date"] <= end_date)
        ]
        return selected[-days:] if days > 0 else selected


def _rows(count: int = 130) -> list[dict]:
    result = []
    close = 10.0
    for index in range(count):
        previous = close
        close = previous * (1 + 0.001 + (index % 3 - 1) * 0.0002)
        volume = 1_000_000 + index * 100
        result.append(
            {
                "ts_code": TS_CODE,
                "trade_date": (date(2025, 1, 1) + timedelta(days=index)).strftime(
                    "%Y%m%d"
                ),
                "open": previous,
                "high": max(previous, close) * 1.01,
                "low": min(previous, close) * 0.99,
                "close": close,
                "vol": volume,
                "amount": volume * close,
                "pct_chg": (close / previous - 1) * 100,
                "prev_close": previous,
            }
        )
    return result


def _buy_backtest_rows(count: int = 170, signal_index: int = 129) -> list[dict]:
    result = []
    close = 10.0
    for index in range(count):
        previous = close
        pct = -0.015 if index in (signal_index - 1, signal_index) else 0.003
        close = previous * (1 + pct)
        volume = 400_000 if index == signal_index else 1_000_000 + index * 1_000
        result.append(
            {
                "ts_code": TS_CODE,
                "trade_date": (
                    date(2025, 1, 1) + timedelta(days=index)
                ).strftime("%Y%m%d"),
                "open": previous,
                "high": max(previous, close) * 1.005,
                "low": min(previous, close) * 0.995,
                "close": close,
                "vol": volume,
                "amount": volume * close,
                "pct_chg": pct * 100,
                "prev_close": previous,
            }
        )
    return result


def _write_market_file(path: Path, dates: list[str]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "market-snapshots-v1",
                "version": "market-test-v1",
                "source": "TEST_EXPLICIT_MARKET",
                "snapshots": [
                    {"trade_date": trade_date, "score": 55 + index}
                    for index, trade_date in enumerate(dates)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_calendar_file(
    path: Path, dates: list[str], *, exchange: str = "SZSE"
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "exchange-calendar-v1",
                "exchange": exchange,
                "source": "TEST_EXPLICIT_EXCHANGE_CALENDAR",
                "dates": dates,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_parser_registers_isolated_daily_portfolio_commands(tmp_path: Path) -> None:
    parser = build_parser()
    market_file = tmp_path / "market.json"
    calendar_file = tmp_path / "calendar.json"

    score = parser.parse_args(
        [
            "daily-portfolio",
            "score",
            TS_CODE,
            "--as-of",
            "20260105",
            "--market-file",
            str(market_file),
        ]
    )
    assert score.command == "daily-portfolio"
    assert score.daily_portfolio_action == "score"
    assert score.data_source == "sqlite"
    assert score.lookback == 180

    replay = parser.parse_args(
        [
            "daily-portfolio",
            "replay-pair",
            TS_CODE,
            "--start",
            "20260101",
            "--end",
            "20260105",
            "--calendar-file",
            str(calendar_file),
            "--exchange",
            "SZSE",
            "--market-file",
            str(market_file),
        ]
    )
    assert replay.daily_portfolio_action == "replay-pair"
    assert replay.warmup_bars == 150
    assert replay.initial_cash == 100_000

    buy_backtest = parser.parse_args(
        [
            "daily-portfolio",
            "backtest-buy",
            TS_CODE,
            "--start",
            "20260101",
            "--end",
            "20260105",
            "--calendar-file",
            str(calendar_file),
            "--exchange",
            "SZSE",
            "--market-file",
            str(market_file),
        ]
    )
    assert buy_backtest.daily_portfolio_action == "backtest-buy"
    assert buy_backtest.horizons == "1,3,5,10,20"
    assert buy_backtest.standardized_equity == 1_000_000


def test_parser_requires_explicit_market_and_calendar_files() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["daily-portfolio", "score", TS_CODE, "--as-of", "20260105"]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "daily-portfolio",
                "replay-pair",
                TS_CODE,
                "--start",
                "20260101",
                "--end",
                "20260105",
                "--exchange",
                "SZSE",
                "--market-file",
                "market.json",
            ]
        )


def test_explicit_evidence_files_reject_duplicate_dates(tmp_path: Path) -> None:
    market_file = _write_market_file(
        tmp_path / "market.json", ["20260105", "20260105"]
    )
    calendar_file = _write_calendar_file(
        tmp_path / "calendar.json", ["20260105", "20260105"]
    )
    with pytest.raises(ValueError, match="重复日期"):
        load_market_snapshots(str(market_file))
    with pytest.raises(ValueError, match="重复日期"):
        load_exchange_calendar(str(calendar_file), expected_exchange="SZSE")


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("shares", True),
        ("shares", 100.0),
        ("available_shares", False),
        ("available_shares", 100.0),
    ],
)
def test_position_file_rejects_non_integer_share_counts(
    tmp_path: Path, field: str, bad_value: object
) -> None:
    position = {
        "ts_code": TS_CODE,
        "shares": 100,
        "available_shares": 100,
        "avg_cost": 10.0,
        "current_position_pct": 0.1,
        "stop_loss": 9.0,
    }
    position[field] = bad_value
    path = tmp_path / f"position-{field}.json"
    path.write_text(json.dumps(position), encoding="utf-8")
    with pytest.raises(ValueError, match="非负整数"):
        _load_position(str(path), ts_code=TS_CODE)


def test_held_position_file_requires_explicit_stop_loss(tmp_path: Path) -> None:
    path = tmp_path / "position.json"
    path.write_text(
        json.dumps(
            {
                "ts_code": TS_CODE,
                "shares": 100,
                "available_shares": 100,
                "avg_cost": 10.0,
                "current_position_pct": 0.1,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="stop_loss"):
        _load_position(str(path), ts_code=TS_CODE)


def test_score_cli_uses_as_of_bound_and_outputs_stable_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    rows = _rows(125)
    as_of = rows[-1]["trade_date"]
    market_file = _write_market_file(tmp_path / "market.json", [as_of])
    datasource = FakeDataSource(rows)
    monkeypatch.setattr(
        "modules.daily_portfolio.cli_adapter.get_datasource",
        lambda name: datasource,
    )
    args = build_parser().parse_args(
        [
            "daily-portfolio",
            "score",
            TS_CODE,
            "--as-of",
            as_of,
            "--market-file",
            str(market_file),
            "--lookback",
            "125",
            "--json",
        ]
    )

    cmd_daily_portfolio(args)

    payload = json.loads(capsys.readouterr().out)
    assert datasource.calls == [
        {
            "ts_code": TS_CODE,
            "days": 125,
            "start_date": None,
            "end_date": as_of,
        }
    ]
    assert payload["schema_version"] == "daily-stock-score-v1"
    assert payload["engine"] == "daily_portfolio"
    assert payload["as_of_date"] == as_of
    assert payload["market_context"]["trade_date"] == as_of
    assert payload["market_context"]["source_hash"]
    assert payload["buy_point"]["status"] in {
        "BLOCKED",
        "NO_SETUP",
        "CANDIDATE",
        "CONFLICT",
        "CONFIRMED",
    }
    assert set(payload["buy_score_contract"]["raw_components"]) == {
        "entry_structure",
        "trend",
        "volume",
        "pattern_quality",
        "stage",
        "market",
        "resonance",
    }
    assert payload["buy_score_contract"]["weights"]["entry_structure"] == 25
    assert "b1.quality_confirmed" in payload["features"]["variant_evidence"]
    assert payload["score"]["desired_action"] in {
        "OPEN",
        "WATCH",
        "BLOCK",
    }
    assert not payload["score"]["desired_action"].startswith("TradeAction.")


def test_score_cli_fails_closed_when_market_date_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    market_file = _write_market_file(tmp_path / "market.json", ["20260101"])
    monkeypatch.setattr(
        "modules.daily_portfolio.cli_adapter.get_datasource",
        lambda name: pytest.fail("data source must not be queried before market validation"),
    )
    args = build_parser().parse_args(
        [
            "daily-portfolio",
            "score",
            TS_CODE,
            "--as-of",
            "20260102",
            "--market-file",
            str(market_file),
        ]
    )
    with pytest.raises(SystemExit) as exc_info:
        cmd_daily_portfolio(args)
    assert exc_info.value.code == 2
    assert "市场快照缺少评分日" in capsys.readouterr().err


def test_replay_pair_uses_explicit_calendar_and_marks_research_lookahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    rows = _rows(127)
    replay_dates = [row["trade_date"] for row in rows[123:126]]
    following_date = rows[126]["trade_date"]
    market_file = _write_market_file(tmp_path / "market.json", replay_dates)
    calendar_file = _write_calendar_file(
        tmp_path / "calendar.json", replay_dates + [following_date]
    )
    datasource = FakeDataSource(rows)
    monkeypatch.setattr(
        "modules.daily_portfolio.cli_adapter.get_datasource",
        lambda name: datasource,
    )

    def hold_score(
        ts_code,
        as_of_date,
        bars,
        position,
        market_context,
        *,
        max_position_pct,
        config,
    ):
        return DailyStockScore(
            ts_code=ts_code,
            signal_date=as_of_date,
            last_bar_date=bars[-1].trade_date,
            buy_score=10,
            sell_score=10,
            position_score=0,
            current_position_pct=position.current_position_pct,
            target_position_pct=position.current_position_pct,
            desired_action=(
                TradeAction.HOLD if position.shares else TradeAction.WATCH
            ),
            stop_loss=position.stop_loss,
        )

    monkeypatch.setattr(
        "modules.daily_portfolio.cli_adapter.score_daily_bar", hold_score
    )
    args = build_parser().parse_args(
        [
            "daily-portfolio",
            "replay-pair",
            TS_CODE,
            "--start",
            replay_dates[0],
            "--end",
            replay_dates[-1],
            "--calendar-file",
            str(calendar_file),
            "--exchange",
            "SZSE",
            "--market-file",
            str(market_file),
            "--warmup-bars",
            "120",
            "--json",
        ]
    )

    cmd_daily_portfolio(args)

    payload = json.loads(capsys.readouterr().out)
    assert datasource.calls[0]["end_date"] == replay_dates[-1]
    assert payload["schema_version"] == "daily-portfolio-paired-replay-v1"
    assert payload["inputs"]["calendar"]["following_trading_date"] == following_date
    assert payload["inputs"]["calendar"]["source_hash"]
    assert payload["same_close_research"]["lookahead"] is True
    assert payload["same_close_research"]["execution_mode"] == "SAME_CLOSE_RESEARCH"
    assert payload["next_open_strict"]["lookahead"] is False
    assert payload["next_open_strict"]["execution_mode"] == "NEXT_OPEN_STRICT"
    assert "daily_records" not in payload["next_open_strict"]


def test_buy_backtest_cli_runs_real_score_to_d_plus_1_open_and_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    rows = _buy_backtest_rows()
    signal_date = rows[129]["trade_date"]
    market_file = _write_market_file(tmp_path / "market.json", [signal_date])
    calendar_file = _write_calendar_file(
        tmp_path / "calendar.json", [row["trade_date"] for row in rows]
    )
    datasource = FakeDataSource(rows)
    monkeypatch.setattr(
        "modules.daily_portfolio.cli_adapter.get_datasource",
        lambda name: datasource,
    )
    args = build_parser().parse_args(
        [
            "daily-portfolio",
            "backtest-buy",
            TS_CODE,
            "--start",
            signal_date,
            "--end",
            signal_date,
            "--calendar-file",
            str(calendar_file),
            "--exchange",
            "SZSE",
            "--market-file",
            str(market_file),
            "--warmup-bars",
            "120",
            "--include-events",
            "--json",
        ]
    )

    cmd_daily_portfolio(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "daily-buy-point-backtest-v1"
    assert payload["command"] == "backtest-buy"
    assert payload["event_counts"]["EXECUTED_SELECTED"] == 1
    event = payload["events"][0]
    assert event["signal_date"] == signal_date
    assert event["fill"]["execution_date"] == rows[130]["trade_date"]
    assert event["fill"]["raw_price"] == rows[130]["open"]
    assert event["fill"]["lookahead_flag"] is False
    assert event["outcomes"][-1]["horizon_bars"] == 20
    assert event["outcomes"][-1]["complete"] is True
    assert payload["setup_candidate_metrics"][-1]["sample_count"] == 1
    assert payload["inputs"]["parameter_fingerprint"]
    assert payload["inputs"]["bar_data_fingerprint"]
    assert payload["inputs"]["calendar_fingerprint"]
    assert payload["inputs"]["market_context_fingerprint"]
    assert payload["inputs"]["research_config"]["horizons"] == [1, 3, 5, 10, 20]
    assert payload["inputs"]["research_config_fingerprint"]
    assert payload["inputs"]["confirmation_policy_version"] == (
        "buy-confirmation-policy-v0.2"
    )
    assert payload["inputs"]["confirmation_policy_fingerprint"]
    assert payload["inputs"]["feature_versions"] == [
        "daily-strategy-features-v0.1"
    ]


def test_replay_pair_rejects_calendar_without_following_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    market_file = _write_market_file(tmp_path / "market.json", ["20260105"])
    calendar_file = _write_calendar_file(
        tmp_path / "calendar.json", ["20260105"]
    )
    monkeypatch.setattr(
        "modules.daily_portfolio.cli_adapter.get_datasource",
        lambda name: pytest.fail("calendar must fail before loading bars"),
    )
    args = build_parser().parse_args(
        [
            "daily-portfolio",
            "replay-pair",
            TS_CODE,
            "--start",
            "20260105",
            "--end",
            "20260105",
            "--calendar-file",
            str(calendar_file),
            "--exchange",
            "SZSE",
            "--market-file",
            str(market_file),
        ]
    )
    with pytest.raises(SystemExit) as exc_info:
        cmd_daily_portfolio(args)
    assert exc_info.value.code == 2
    assert "下一交易日" in capsys.readouterr().err


def test_buy_backtest_horizon_one_still_requires_d_plus_2_for_default_t1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    market_file = _write_market_file(tmp_path / "market.json", ["20260105"])
    calendar_file = _write_calendar_file(
        tmp_path / "calendar.json", ["20260105", "20260106"]
    )
    monkeypatch.setattr(
        "modules.daily_portfolio.cli_adapter.get_datasource",
        lambda name: pytest.fail("calendar coverage must fail before loading bars"),
    )
    args = build_parser().parse_args(
        [
            "daily-portfolio",
            "backtest-buy",
            TS_CODE,
            "--start",
            "20260105",
            "--end",
            "20260105",
            "--calendar-file",
            str(calendar_file),
            "--exchange",
            "SZSE",
            "--market-file",
            str(market_file),
            "--horizons",
            "1",
        ]
    )

    with pytest.raises(SystemExit) as exc_info:
        cmd_daily_portfolio(args)

    assert exc_info.value.code == 2
    assert "至少包含 2 个交易日" in capsys.readouterr().err


def test_replay_pair_rejects_actual_warmup_shorter_than_120(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    rows = _rows(122)
    replay_dates = [row["trade_date"] for row in rows[119:122]]
    following_date = (
        date.fromisoformat(
            f"{replay_dates[-1][:4]}-{replay_dates[-1][4:6]}-{replay_dates[-1][6:]}"
        )
        + timedelta(days=1)
    ).strftime("%Y%m%d")
    market_file = _write_market_file(tmp_path / "market.json", replay_dates)
    calendar_file = _write_calendar_file(
        tmp_path / "calendar.json", replay_dates + [following_date]
    )
    datasource = FakeDataSource(rows)
    monkeypatch.setattr(
        "modules.daily_portfolio.cli_adapter.get_datasource",
        lambda name: datasource,
    )
    args = build_parser().parse_args(
        [
            "daily-portfolio",
            "replay-pair",
            TS_CODE,
            "--start",
            replay_dates[0],
            "--end",
            replay_dates[-1],
            "--calendar-file",
            str(calendar_file),
            "--exchange",
            "SZSE",
            "--market-file",
            str(market_file),
            "--warmup-bars",
            "120",
        ]
    )
    with pytest.raises(SystemExit) as exc_info:
        cmd_daily_portfolio(args)
    assert exc_info.value.code == 2
    assert "实际预热K线不足 120 根" in capsys.readouterr().err


def test_adapter_has_no_legacy_command_or_scanner_dependency() -> None:
    import modules.daily_portfolio.cli_adapter as adapter

    tree = ast.parse(inspect.getsource(adapter))
    imported_modules: set[str] = set()
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)

    def is_legacy_dependency(module: str) -> bool:
        parts = module.split(".")
        return any(
            part in {"cli_commands", "simulator", "watchlist", "backtest"}
            or part.startswith("backtest_")
            for part in parts
        )

    assert not any(is_legacy_dependency(module) for module in imported_modules)
    assert {
        "cmd_backtest",
        "cmd_simulate",
        "run_simulation",
        "scan_watchlist",
    }.isdisjoint(called_names)
