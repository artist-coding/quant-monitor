"""Safe CLI boundary for the daily portfolio engine.

The adapter intentionally requires explicit market snapshots and, for replay,
an explicit exchange calendar.  It never falls back to the legacy simulator,
the legacy backtests, the watchlist scanner, a neutral market score, or stock
bar dates as a proxy for an exchange calendar.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
from typing import Any, NoReturn

from ..datasource import DataSource, get_datasource
from ..indicators import DailyData
from .config import DailyPortfolioConfig
from .buy_backtest import BuyBacktestConfig, BuyBacktestResult, backtest_buy_points
from .contracts import validate_bars_as_of
from .dates import normalize_trade_date
from .evidence_adapter import MarketSnapshot
from .execution_model import ExecutionConfig
from .models import LifecycleState, PositionState
from .replay import DailyReplayResult, PairedExitReplayResult, replay_exit_mode_pair
from .service import DailyScoreEvaluation, evaluate_daily_bar, score_daily_bar


_MARKET_SCHEMA = "market-snapshots-v1"
_CALENDAR_SCHEMA = "exchange-calendar-v1"
_MINIMUM_HISTORY_BARS = 120


@dataclass(frozen=True)
class MarketSnapshotBundle:
    version: str
    source: str
    source_hash: str
    snapshots: Mapping[str, MarketSnapshot]


@dataclass(frozen=True)
class ExchangeCalendarBundle:
    exchange: str
    source: str
    source_hash: str
    dates: tuple[str, ...]


def _read_json_file(path_value: str, *, label: str) -> tuple[Mapping[str, Any], str]:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise ValueError(f"{label}文件不存在: {path}")
    raw = path.read_bytes()
    try:
        decoded = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}必须是有效的 UTF-8 JSON: {path}") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError(f"{label}顶层必须是 JSON 对象")
    return decoded, sha256(raw).hexdigest()


def _finite_number(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有限数值")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是有限数值") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label}必须是有限数值")
    return result


def _nonnegative_integer(value: Any, *, label: str) -> int:
    """Reject JSON booleans/floats instead of silently truncating share counts."""

    if type(value) is not int or value < 0:
        raise ValueError(f"{label}必须是非负整数，不能是 bool 或浮点数")
    return value


def load_market_snapshots(path_value: str) -> MarketSnapshotBundle:
    """Load versioned, explicit daily market snapshots from JSON."""

    payload, file_hash = _read_json_file(path_value, label="市场快照")
    if payload.get("schema_version") != _MARKET_SCHEMA:
        raise ValueError(f"市场快照 schema_version 必须是 {_MARKET_SCHEMA}")
    version = payload.get("version")
    source = payload.get("source")
    raw_snapshots = payload.get("snapshots")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("市场快照 version 不能为空")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("市场快照 source 不能为空")
    if not isinstance(raw_snapshots, list) or not raw_snapshots:
        raise ValueError("市场快照 snapshots 必须是非空数组")

    snapshots: dict[str, MarketSnapshot] = {}
    for index, item in enumerate(raw_snapshots):
        if not isinstance(item, Mapping):
            raise ValueError(f"市场快照 snapshots[{index}] 必须是对象")
        trade_date = normalize_trade_date(item.get("trade_date"))
        if trade_date in snapshots:
            raise ValueError(f"市场快照存在重复日期: {trade_date}")
        score = _finite_number(item.get("score"), label=f"snapshots[{index}].score")
        snapshots[trade_date] = MarketSnapshot(
            trade_date=trade_date,
            score=score,
            version=version.strip(),
            source_hash=file_hash,
        )
    return MarketSnapshotBundle(
        version=version.strip(),
        source=source.strip(),
        source_hash=file_hash,
        snapshots=snapshots,
    )


def load_exchange_calendar(
    path_value: str, *, expected_exchange: str
) -> ExchangeCalendarBundle:
    """Load a strictly ordered, explicit exchange calendar from JSON."""

    payload, file_hash = _read_json_file(path_value, label="交易日历")
    if payload.get("schema_version") != _CALENDAR_SCHEMA:
        raise ValueError(f"交易日历 schema_version 必须是 {_CALENDAR_SCHEMA}")
    exchange = payload.get("exchange")
    source = payload.get("source")
    raw_dates = payload.get("dates")
    if not isinstance(exchange, str) or not exchange.strip():
        raise ValueError("交易日历 exchange 不能为空")
    exchange = exchange.strip().upper()
    if exchange != expected_exchange.strip().upper():
        raise ValueError(
            f"交易日历 exchange={exchange} 与 --exchange={expected_exchange} 不一致"
        )
    if not isinstance(source, str) or not source.strip():
        raise ValueError("交易日历 source 不能为空")
    if not isinstance(raw_dates, list) or not raw_dates:
        raise ValueError("交易日历 dates 必须是非空数组")

    dates = tuple(normalize_trade_date(item) for item in raw_dates)
    if len(set(dates)) != len(dates):
        raise ValueError("交易日历包含重复日期")
    if dates != tuple(sorted(dates)):
        raise ValueError("交易日历必须严格按日期升序")
    return ExchangeCalendarBundle(
        exchange=exchange,
        source=source.strip(),
        source_hash=file_hash,
        dates=dates,
    )


def _load_position(
    path_value: str | None, *, ts_code: str
) -> tuple[PositionState, dict[str, Any]]:
    if not path_value:
        return PositionState(ts_code=ts_code), {"source": "CLI_DEFAULT_FLAT"}

    payload, file_hash = _read_json_file(path_value, label="持仓")
    raw_position: Mapping[str, Any]
    if "position" in payload:
        if set(payload) - {"schema_version", "position"}:
            raise ValueError("持仓文件 wrapper 含有未知字段")
        candidate = payload.get("position")
        if not isinstance(candidate, Mapping):
            raise ValueError("持仓文件 position 必须是对象")
        raw_position = candidate
    else:
        raw_position = payload

    allowed = {
        "ts_code",
        "lifecycle_state",
        "shares",
        "available_shares",
        "avg_cost",
        "current_position_pct",
        "stop_loss",
        "can_sell_date",
    }
    unknown = set(raw_position) - allowed
    if unknown:
        raise ValueError(f"持仓文件包含未知字段: {sorted(unknown)}")

    position_code = str(raw_position.get("ts_code", ts_code))
    if position_code != ts_code:
        raise ValueError("持仓文件 ts_code 与命令股票代码不一致")
    shares = _nonnegative_integer(raw_position.get("shares", 0), label="shares")
    available_shares = _nonnegative_integer(
        raw_position.get("available_shares", shares), label="available_shares"
    )
    avg_cost = _finite_number(raw_position.get("avg_cost", 0), label="avg_cost")
    if shares > 0 and "current_position_pct" not in raw_position:
        raise ValueError("已有持仓时必须显式提供 current_position_pct")
    current_position_pct = _finite_number(
        raw_position.get("current_position_pct", 0), label="current_position_pct"
    )
    if shares > 0 and avg_cost <= 0:
        raise ValueError("已有持仓时 avg_cost 必须大于 0")
    raw_stop_loss = raw_position.get("stop_loss")
    if shares > 0 and raw_stop_loss is None:
        raise ValueError("已有持仓时必须显式提供 stop_loss")
    stop_loss = (
        None
        if raw_stop_loss is None
        else _finite_number(raw_stop_loss, label="stop_loss")
    )
    lifecycle_default = LifecycleState.HOLDING if shares > 0 else LifecycleState.FLAT
    try:
        lifecycle = LifecycleState(
            raw_position.get("lifecycle_state", lifecycle_default.value)
        )
    except ValueError as exc:
        raise ValueError("持仓 lifecycle_state 无效") from exc

    position = PositionState(
        ts_code=ts_code,
        lifecycle_state=lifecycle,
        shares=shares,
        available_shares=available_shares,
        avg_cost=avg_cost,
        current_position_pct=current_position_pct,
        stop_loss=stop_loss,
        can_sell_date=str(raw_position.get("can_sell_date", "")),
    )
    return position, {"source": "EXPLICIT_POSITION_FILE", "source_hash": file_hash}


def _rows_to_daily_bars(
    rows: Sequence[Mapping[str, Any]], *, expected_ts_code: str
) -> tuple[DailyData, ...]:
    if not rows:
        raise ValueError(f"没有找到 {expected_ts_code} 的日线数据")

    result: list[DailyData] = []
    normalized_dates: list[str] = []
    previous_close: float | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"K线第 {index} 行不是字段映射")
        row_code = str(row.get("ts_code", expected_ts_code))
        if row_code != expected_ts_code:
            raise ValueError(f"K线包含其他股票: {row_code}")
        trade_date = normalize_trade_date(row.get("trade_date"))
        normalized_dates.append(trade_date)
        open_price = _finite_number(row.get("open"), label=f"K线[{index}].open")
        high = _finite_number(row.get("high"), label=f"K线[{index}].high")
        low = _finite_number(row.get("low"), label=f"K线[{index}].low")
        close = _finite_number(row.get("close"), label=f"K线[{index}].close")
        volume = _finite_number(row.get("vol", 0), label=f"K线[{index}].vol")
        amount_value = row.get("amount")
        amount = (
            close * volume
            if amount_value is None
            else _finite_number(amount_value, label=f"K线[{index}].amount")
        )
        explicit_previous = row.get("prev_close", row.get("pre_close"))
        prev_close = (
            _finite_number(explicit_previous, label=f"K线[{index}].prev_close")
            if explicit_previous is not None
            else previous_close or close
        )
        pct_value = row.get("pct_chg")
        pct_chg = (
            (close / prev_close - 1) * 100 if pct_value is None and prev_close > 0 else 0.0
        )
        if pct_value is not None:
            pct_chg = _finite_number(pct_value, label=f"K线[{index}].pct_chg")
        if min(open_price, high, low, close) <= 0:
            raise ValueError(f"K线[{index}] OHLC 必须大于 0")
        if high < max(open_price, close) or low > min(open_price, close):
            raise ValueError(f"K线[{index}] high/low 与 open/close 不一致")
        if volume < 0 or amount < 0:
            raise ValueError(f"K线[{index}] 成交量和成交额不能为负")
        result.append(
            DailyData(
                ts_code=expected_ts_code,
                trade_date=trade_date,
                open=open_price,
                high=high,
                low=low,
                close=close,
                vol=volume,
                amount=amount,
                pct_chg=pct_chg,
                prev_close=prev_close,
            )
        )
        previous_close = close

    if len(set(normalized_dates)) != len(normalized_dates):
        raise ValueError("K线包含重复日期")
    if normalized_dates != sorted(normalized_dates):
        raise ValueError("数据源必须按 trade_date 严格升序返回K线")
    return tuple(result)


def _validate_common_numeric_args(*, max_position_pct: float, history_bars: int) -> None:
    if not 0 < max_position_pct <= 1:
        raise ValueError("--max-position-pct 必须在 (0, 1] 区间")
    if history_bars < _MINIMUM_HISTORY_BARS:
        raise ValueError(f"历史K线数量不能少于 {_MINIMUM_HISTORY_BARS}")


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON 输出不能包含 NaN 或 Infinity")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"不支持的 JSON 值类型: {type(value).__name__}")


def _print_json(payload: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            _json_value(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )


def _score_payload(
    evaluation: DailyScoreEvaluation,
    *,
    requested_source: str,
    resolved_source: str,
    bars: Sequence[DailyData],
    market_bundle: MarketSnapshotBundle,
    market_snapshot: MarketSnapshot,
    position: PositionState,
    position_provenance: Mapping[str, Any],
    config: DailyPortfolioConfig,
) -> dict[str, Any]:
    return {
        "schema_version": "daily-stock-score-v1",
        "engine": "daily_portfolio",
        "command": "score",
        "ts_code": evaluation.score.ts_code,
        "as_of_date": evaluation.score.signal_date,
        "data_provenance": {
            "requested_source": requested_source,
            "resolved_source": resolved_source,
            "bar_count": len(bars),
            "first_bar_date": bars[0].trade_date,
            "last_bar_date": bars[-1].trade_date,
        },
        "market_context": {
            **_json_value(market_snapshot),
            "source": market_bundle.source,
        },
        "position": _json_value(position),
        "position_provenance": dict(position_provenance),
        "score": evaluation.score.as_dict(),
        "buy_point": _json_value(evaluation.buy_point),
        "buy_score_contract": {
            "raw_components": evaluation.adapted_evidence.score_evidence.buy.as_mapping(),
            "weights": dict(config.score_weights.buy),
            "weighted_contributions": dict(
                evaluation.aggregated_scores.buy_contributions
            ),
            "risk_penalty_points": (
                evaluation.adapted_evidence.score_evidence.risk_penalty_points
            ),
            "final_buy_score": evaluation.aggregated_scores.buy_score,
            "open_threshold": config.thresholds.open_buy_score,
            "add_threshold": config.thresholds.add_buy_score,
            "parameter_fingerprint": config.fingerprint(),
        },
        "policy": {
            "decision_code": evaluation.policy.decision_code,
            "current_ladder_ratio": evaluation.policy.current_ladder_ratio,
            "target_ladder_ratio": evaluation.policy.target_ladder_ratio,
            "max_position_pct": evaluation.policy.max_position_pct,
            "has_score_conflict": evaluation.policy.has_score_conflict,
        },
        "features": {
            "feature_version": evaluation.features.feature_version,
            "required_bars": evaluation.features.required_bars,
            "missing_fields": list(evaluation.features.missing_fields),
            "hard_vetoes": list(evaluation.features.hard_vetoes),
            "eligibility_gates": dict(evaluation.features.eligibility_gates),
            "variant_evidence": _json_value(
                evaluation.features.variant_evidence
            ),
        },
        "diagnostics": evaluation.adapted_evidence.diagnostics,
    }


def _replay_payload(
    result: DailyReplayResult,
    *,
    execution_mode: str,
    lookahead: bool,
    include_daily_records: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "execution_mode": execution_mode,
        "lookahead": lookahead,
        "calendar_source": result.calendar_source,
        "final_cash": result.final_cash,
        "final_equity": result.final_equity,
        "final_position": _json_value(result.final_position),
        "fill_count": len(result.fills),
        "rejection_count": len(result.rejections),
        "fills": _json_value(result.fills),
        "rejections": _json_value(result.rejections),
        "pending_orders": _json_value(result.pending_orders),
    }
    if include_daily_records:
        payload["daily_records"] = _json_value(result.daily_records)
    return payload


def _paired_payload(
    paired: PairedExitReplayResult,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
    requested_source: str,
    resolved_source: str,
    warmup_count: int,
    replay_bar_count: int,
    period_dates: Sequence[str],
    following_date: str,
    market_bundle: MarketSnapshotBundle,
    calendar_bundle: ExchangeCalendarBundle,
    position: PositionState,
    position_provenance: Mapping[str, Any],
    initial_cash: float,
    max_position_pct: float,
    config: DailyPortfolioConfig,
    execution_config: ExecutionConfig,
    include_daily_records: bool,
) -> dict[str, Any]:
    return {
        "schema_version": "daily-portfolio-paired-replay-v1",
        "engine": "daily_portfolio",
        "command": "replay-pair",
        "ts_code": ts_code,
        "requested_range": {"start_date": start_date, "end_date": end_date},
        "inputs": {
            "bar_source": {
                "requested_source": requested_source,
                "resolved_source": resolved_source,
                "warmup_bar_count": warmup_count,
                "replay_bar_count": replay_bar_count,
            },
            "calendar": {
                "exchange": calendar_bundle.exchange,
                "source": calendar_bundle.source,
                "source_hash": calendar_bundle.source_hash,
                "period_session_count": len(period_dates),
                "following_trading_date": following_date,
            },
            "market_snapshots": {
                "version": market_bundle.version,
                "source": market_bundle.source,
                "source_hash": market_bundle.source_hash,
            },
            "position": _json_value(position),
            "position_provenance": dict(position_provenance),
            "initial_cash": initial_cash,
            "max_position_pct": max_position_pct,
            "strategy_version": config.strategy_version,
            "parameter_version": config.parameter_version,
            "parameter_fingerprint": config.fingerprint(),
            "execution_config": _json_value(execution_config),
        },
        "same_close_research": _replay_payload(
            paired.same_close_research,
            execution_mode="SAME_CLOSE_RESEARCH",
            lookahead=True,
            include_daily_records=include_daily_records,
        ),
        "next_open_strict": _replay_payload(
            paired.next_open_strict,
            execution_mode="NEXT_OPEN_STRICT",
            lookahead=False,
            include_daily_records=include_daily_records,
        ),
        "comparison": {
            "final_equity_difference": paired.final_equity_difference,
            "fill_count_difference": paired.fill_count_difference,
        },
    }


def _buy_backtest_payload(
    result: BuyBacktestResult,
    *,
    requested_source: str,
    resolved_source: str,
    market_bundle: MarketSnapshotBundle,
    calendar_bundle: ExchangeCalendarBundle,
    warmup_count: int,
    outcome_end_date: str,
    include_events: bool,
) -> dict[str, Any]:
    event_counts: dict[str, int] = {}
    for event in result.events:
        key = event.status.value
        event_counts[key] = event_counts.get(key, 0) + 1
    payload: dict[str, Any] = {
        "schema_version": "daily-buy-point-backtest-v1",
        "engine": "daily_portfolio",
        "command": "backtest-buy",
        "ts_code": result.ts_code,
        "analysis_range": {
            "start_date": result.analysis_start,
            "end_date": result.analysis_end,
            "outcome_data_end_date": outcome_end_date,
        },
        "inputs": {
            "bar_source": {
                "requested_source": requested_source,
                "resolved_source": resolved_source,
                "warmup_bar_count": warmup_count,
            },
            "calendar": {
                "exchange": calendar_bundle.exchange,
                "source": calendar_bundle.source,
                "source_hash": calendar_bundle.source_hash,
            },
            "market_snapshots": {
                "version": market_bundle.version,
                "source": market_bundle.source,
                "source_hash": market_bundle.source_hash,
            },
            "strategy_version": result.strategy_version,
            "parameter_version": result.parameter_version,
            "parameter_fingerprint": result.parameter_fingerprint,
            "execution_config_fingerprint": result.execution_config_fingerprint,
            "research_config": dict(result.research_config),
            "research_config_fingerprint": result.research_config_fingerprint,
            "confirmation_policy_version": result.confirmation_policy_version,
            "confirmation_policy_fingerprint": (
                result.confirmation_policy_fingerprint
            ),
            "feature_versions": list(result.feature_versions),
            "bar_data_fingerprint": result.bar_data_fingerprint,
            "calendar_fingerprint": result.calendar_fingerprint,
            "market_context_fingerprint": result.market_context_fingerprint,
            "horizon_unit": result.horizon_unit,
        },
        "event_counts": event_counts,
        "setup_candidate_metrics": _json_value(result.setup_candidate_metrics),
        "selected_metrics": _json_value(result.selected_metrics),
        "independent_metrics": _json_value(result.independent_metrics),
        "score_buckets": _json_value(result.score_buckets),
        "variant_metrics": _json_value(result.variant_metrics),
        "execution_assumptions": list(result.execution_assumptions),
    }
    if include_events:
        payload["events"] = _json_value(result.events)
    return payload


def _resolve_datasource(name: str) -> DataSource:
    if name not in {"sqlite", "tushare"}:
        raise ValueError("daily-portfolio 首版仅允许显式 sqlite 或 tushare 数据源")
    return get_datasource(name)


def _cmd_score(args: Any) -> None:
    as_of_date = normalize_trade_date(args.as_of)
    _validate_common_numeric_args(
        max_position_pct=args.max_position_pct,
        history_bars=args.lookback,
    )
    market_bundle = load_market_snapshots(args.market_file)
    market_snapshot = market_bundle.snapshots.get(as_of_date)
    if market_snapshot is None:
        raise ValueError(f"市场快照缺少评分日 {as_of_date}")
    position, position_provenance = _load_position(
        args.position_file, ts_code=args.ts_code
    )
    datasource = _resolve_datasource(args.data_source)
    rows = datasource.get_kline_dicts(
        args.ts_code,
        days=args.lookback,
        end_date=as_of_date,
    )
    bars = _rows_to_daily_bars(rows or (), expected_ts_code=args.ts_code)
    confirmed = validate_bars_as_of(bars, as_of_date)
    config = DailyPortfolioConfig()
    evaluation = evaluate_daily_bar(
        args.ts_code,
        as_of_date,
        confirmed,
        position,
        market_snapshot,
        max_position_pct=args.max_position_pct,
        config=config,
    )
    payload = _score_payload(
        evaluation,
        requested_source=args.data_source,
        resolved_source=datasource.name,
        bars=confirmed,
        market_bundle=market_bundle,
        market_snapshot=market_snapshot,
        position=position,
        position_provenance=position_provenance,
        config=config,
    )
    if args.json:
        _print_json(payload)
        return
    score = evaluation.score
    buy_point = evaluation.buy_point
    primary_variant = (
        buy_point.primary_confirming_variant
        or buy_point.primary_variant
        or "-"
    )
    print(
        f"{score.ts_code} {score.signal_date} "
        f"买入分={score.buy_score:.1f} 卖出分={score.sell_score:.1f} "
        f"动作={score.desired_action.value} 目标仓位={score.target_position_pct:.1%}\n"
        f"  买点状态={buy_point.status.value} 主结构={primary_variant} "
        f"确认阈值={buy_point.confirmation_threshold:.1f} "
        f"止损={buy_point.planned_stop_loss or 'N/A'}\n"
        f"  策略={score.strategy_version} 参数={score.parameter_version} "
        f"指纹={score.parameter_fingerprint[:12]}"
    )


def _parse_horizons(value: str) -> tuple[int, ...]:
    try:
        horizons = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError("--horizons 必须是逗号分隔的正整数") from exc
    if not horizons or any(item <= 0 for item in horizons):
        raise ValueError("--horizons 必须是逗号分隔的正整数")
    if tuple(sorted(set(horizons))) != horizons:
        raise ValueError("--horizons 必须唯一且严格递增")
    return horizons


def _cmd_backtest_buy(args: Any) -> None:
    start_date = normalize_trade_date(args.start)
    end_date = normalize_trade_date(args.end)
    if start_date > end_date:
        raise ValueError("--start 不能晚于 --end")
    if args.data_source != "sqlite":
        raise ValueError(
            "backtest-buy 在双价格接入前仅允许 sqlite RAW 数据源"
        )
    _validate_common_numeric_args(
        max_position_pct=1.0,
        history_bars=args.warmup_bars,
    )
    horizons = _parse_horizons(args.horizons)
    if not math.isfinite(args.standardized_equity) or args.standardized_equity <= 0:
        raise ValueError("--standardized-equity 必须为正数")

    calendar_bundle = load_exchange_calendar(
        args.calendar_file, expected_exchange=args.exchange
    )
    period_dates = tuple(
        item for item in calendar_bundle.dates if start_date <= item <= end_date
    )
    if not period_dates:
        raise ValueError("交易日历在买点回测区间内没有开放交易日")
    future_dates = tuple(item for item in calendar_bundle.dates if item > end_date)
    required_future_sessions = max(horizons)
    # Outcome horizons count stock bars, not exchange sessions.  Fetch a
    # conservative exchange-session buffer, then verify actual stock-bar
    # coverage below.  Two sessions are also required for D+1 plus T+1 state.
    fetched_future_sessions = max(2, required_future_sessions * 2)
    if len(future_dates) < fetched_future_sessions:
        raise ValueError(
            "交易日历必须在 --end 后至少包含 "
            f"{fetched_future_sessions} 个交易日，用于覆盖个股停牌和结果观察"
        )
    outcome_end_date = future_dates[fetched_future_sessions - 1]

    market_bundle = load_market_snapshots(args.market_file)
    datasource = _resolve_datasource(args.data_source)
    requested_bars = args.warmup_bars + len(period_dates) + fetched_future_sessions
    rows = datasource.get_kline_dicts(
        args.ts_code,
        days=requested_bars,
        end_date=outcome_end_date,
    )
    all_bars = _rows_to_daily_bars(rows or (), expected_ts_code=args.ts_code)
    validate_bars_as_of(all_bars, outcome_end_date)
    warmup_count = sum(bar.trade_date < start_date for bar in all_bars)
    if warmup_count < _MINIMUM_HISTORY_BARS:
        raise ValueError(
            "实际预热K线不足 "
            f"{_MINIMUM_HISTORY_BARS} 根（仅 {warmup_count} 根），拒绝买点回测"
        )
    signal_bar_dates = {
        bar.trade_date for bar in all_bars if start_date <= bar.trade_date <= end_date
    }
    if not signal_bar_dates:
        raise ValueError("买点回测区间内没有个股日线数据")
    last_signal_bar_date = max(signal_bar_dates)
    future_stock_bar_count = sum(
        bar.trade_date > last_signal_bar_date for bar in all_bars
    )
    if future_stock_bar_count < required_future_sessions:
        raise ValueError(
            "末端买点的未来个股K线不足 "
            f"{required_future_sessions} 根（仅 {future_stock_bar_count} 根），"
            "拒绝以截尾样本生成CLI回测报告"
        )
    missing_market_dates = sorted(signal_bar_dates - set(market_bundle.snapshots))
    if missing_market_dates:
        raise ValueError(f"市场快照缺少买点评分日: {missing_market_dates}")

    def market_provider(trade_date: str) -> MarketSnapshot:
        normalized = normalize_trade_date(trade_date)
        snapshot = market_bundle.snapshots.get(normalized)
        if snapshot is None:
            raise ValueError(f"市场快照缺少买点评分日 {normalized}")
        return snapshot

    score_config = DailyPortfolioConfig()
    execution_config = ExecutionConfig()
    research_config = BuyBacktestConfig(
        horizons=horizons,
        standardized_equity=args.standardized_equity,
        independent_horizon=max(horizons),
    )
    result = backtest_buy_points(
        all_bars,
        analysis_start=start_date,
        analysis_end=end_date,
        trading_dates=calendar_bundle.dates,
        market_provider=market_provider,
        score_config=score_config,
        execution_config=execution_config,
        backtest_config=research_config,
    )
    payload = _buy_backtest_payload(
        result,
        requested_source=args.data_source,
        resolved_source=datasource.name,
        market_bundle=market_bundle,
        calendar_bundle=calendar_bundle,
        warmup_count=warmup_count,
        outcome_end_date=outcome_end_date,
        include_events=args.include_events,
    )
    if args.json:
        _print_json(payload)
        return

    counts = payload["event_counts"]
    print(
        f"{args.ts_code} {start_date}-{end_date} 买点事件回测\n"
        f"  已确认并执行: {counts.get('EXECUTED_SELECTED', 0)}\n"
        f"  候选事件执行: {counts.get('EXECUTED_CANDIDATE', 0)}\n"
        f"  非买点日: {counts.get('NOT_A_SETUP', 0)}"
    )
    candidate_primary = result.setup_candidate_metrics[-1]
    print(
        f"  全部结构候选{candidate_primary.horizon_bars}日样本: "
        f"n={candidate_primary.sample_count} "
        f"证据={candidate_primary.evidence_status.value}"
    )
    for metrics in result.selected_metrics:
        win_rate = (
            f"{metrics.win_rate:.1%}" if metrics.win_rate is not None else "N/A"
        )
        expectancy = (
            f"{metrics.expectancy_r:.3f}R"
            if metrics.expectancy_r is not None
            else "N/A"
        )
        print(
            f"  {metrics.horizon_bars}日: n={metrics.sample_count} "
            f"胜率={win_rate} 期望={expectancy} "
            f"证据={metrics.evidence_status.value}"
        )
    print(
        "  研究限制: 双价格/公司行动尚未接入，涨跌停规则尚未按历史日期版本化；"
        "当前结果不能解释为正式历史成交收益。"
    )


def _cmd_replay_pair(args: Any) -> None:
    start_date = normalize_trade_date(args.start)
    end_date = normalize_trade_date(args.end)
    if start_date > end_date:
        raise ValueError("--start 不能晚于 --end")
    if args.initial_cash < 0:
        raise ValueError("--initial-cash 不能为负")
    _validate_common_numeric_args(
        max_position_pct=args.max_position_pct,
        history_bars=args.warmup_bars,
    )

    calendar_bundle = load_exchange_calendar(
        args.calendar_file, expected_exchange=args.exchange
    )
    period_dates = tuple(
        item for item in calendar_bundle.dates if start_date <= item <= end_date
    )
    if not period_dates:
        raise ValueError("交易日历在回放区间内没有开放交易日")
    following_date = next(
        (item for item in calendar_bundle.dates if item > end_date), ""
    )
    if not following_date:
        raise ValueError("交易日历必须显式包含 --end 之后的下一交易日")

    market_bundle = load_market_snapshots(args.market_file)
    position, position_provenance = _load_position(
        args.position_file, ts_code=args.ts_code
    )
    datasource = _resolve_datasource(args.data_source)
    requested_bars = args.warmup_bars + len(period_dates)
    rows = datasource.get_kline_dicts(
        args.ts_code,
        days=requested_bars,
        end_date=end_date,
    )
    all_bars = _rows_to_daily_bars(rows or (), expected_ts_code=args.ts_code)
    validate_bars_as_of(all_bars, end_date)
    warmup_bars = tuple(bar for bar in all_bars if bar.trade_date < start_date)[
        -args.warmup_bars :
    ]
    replay_bars = tuple(
        bar for bar in all_bars if start_date <= bar.trade_date <= end_date
    )
    if not replay_bars:
        raise ValueError("回放区间内没有个股日线数据")
    if len(warmup_bars) < _MINIMUM_HISTORY_BARS:
        raise ValueError(
            "实际预热K线不足 "
            f"{_MINIMUM_HISTORY_BARS} 根（仅 {len(warmup_bars)} 根），拒绝回放"
        )
    missing_market_dates = sorted(
        {bar.trade_date for bar in replay_bars} - set(market_bundle.snapshots)
    )
    if missing_market_dates:
        raise ValueError(f"市场快照缺少回放评分日: {missing_market_dates}")

    history = warmup_bars
    config = DailyPortfolioConfig()
    execution_config = ExecutionConfig()

    def score_provider_factory():
        def provide(prefix, current_position, market):
            if not isinstance(market, MarketSnapshot):
                raise TypeError("回放市场提供器必须返回 MarketSnapshot")
            confirmed = history + tuple(prefix)
            return score_daily_bar(
                args.ts_code,
                prefix[-1].trade_date,
                confirmed,
                current_position,
                market,
                max_position_pct=args.max_position_pct,
                config=config,
            )

        return provide

    def market_provider(trade_date: str) -> MarketSnapshot:
        normalized = normalize_trade_date(trade_date)
        snapshot = market_bundle.snapshots.get(normalized)
        if snapshot is None:
            raise ValueError(f"市场快照缺少回放评分日 {normalized}")
        return snapshot

    paired = replay_exit_mode_pair(
        replay_bars,
        score_provider_factory=score_provider_factory,
        market_provider=market_provider,
        initial_position=position,
        initial_cash=args.initial_cash,
        execution_config=execution_config,
        trading_dates=period_dates,
        following_trading_date=following_date,
    )
    payload = _paired_payload(
        paired,
        ts_code=args.ts_code,
        start_date=start_date,
        end_date=end_date,
        requested_source=args.data_source,
        resolved_source=datasource.name,
        warmup_count=len(warmup_bars),
        replay_bar_count=len(replay_bars),
        period_dates=period_dates,
        following_date=following_date,
        market_bundle=market_bundle,
        calendar_bundle=calendar_bundle,
        position=position,
        position_provenance=position_provenance,
        initial_cash=args.initial_cash,
        max_position_pct=args.max_position_pct,
        config=config,
        execution_config=execution_config,
        include_daily_records=args.include_daily_records,
    )
    if args.json:
        _print_json(payload)
        return
    research = paired.same_close_research
    strict = paired.next_open_strict
    print(
        f"{args.ts_code} {start_date}-{end_date} paired replay\n"
        f"  SAME_CLOSE_RESEARCH（含前视）: {research.final_equity:.2f}\n"
        f"  NEXT_OPEN_STRICT（严格口径）: {strict.final_equity:.2f}\n"
        f"  差额: {paired.final_equity_difference:.2f}"
    )


def _fail(message: str) -> NoReturn:
    print(f"错误: daily-portfolio: {message}", file=sys.stderr)
    raise SystemExit(2)


def cmd_daily_portfolio(args: Any) -> None:
    """Dispatch the isolated daily-portfolio CLI command group."""

    try:
        if args.daily_portfolio_action == "score":
            _cmd_score(args)
            return
        if args.daily_portfolio_action == "backtest-buy":
            _cmd_backtest_buy(args)
            return
        if args.daily_portfolio_action == "replay-pair":
            _cmd_replay_pair(args)
            return
        raise ValueError("未知 daily-portfolio 子命令")
    except (OSError, TypeError, ValueError) as exc:
        _fail(str(exc))


__all__ = [
    "cmd_daily_portfolio",
    "load_exchange_calendar",
    "load_market_snapshots",
]
