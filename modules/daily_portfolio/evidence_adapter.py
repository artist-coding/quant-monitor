"""Pure mapping from strategy features to normalized buy/sell evidence."""

from __future__ import annotations

from dataclasses import dataclass

from ..indicators import (
    DailyData,
    calculate_bbi,
    calculate_ma,
    calculate_sandglass_score,
    calculate_zg_white,
    detect_bull_rope,
    detect_chuhuo_wushi,
    detect_kirin_stage,
    detect_three_waves,
    precompute_macd_sequence,
)
from .bar_features import enrich_daily_bars
from .buy_points import ENTRY_VARIANT_NAMES, matched_entry_variants
from .dates import normalize_trade_date
from .models import PositionState
from .score_engine import BuyComponents, ScoreEvidence, SellComponents
from .strategy_features import DailyStrategyFeatures


@dataclass(frozen=True)
class MarketSnapshot:
    trade_date: str
    score: float
    version: str = "market-context-v0.1"
    source_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_date", normalize_trade_date(self.trade_date))
        if not 0 <= self.score <= 100:
            raise ValueError("market score must be between 0 and 100")
        if not self.version:
            raise ValueError("market snapshot version cannot be empty")


@dataclass(frozen=True)
class EvidenceAdapterResult:
    score_evidence: ScoreEvidence
    reasons: tuple[str, ...]
    diagnostics: dict[str, object]


def _variant_matched(features: DailyStrategyFeatures, name: str) -> bool:
    evidence = features.variant_evidence.get(name)
    return bool(evidence and evidence.matched)


def _entry_structure_score(
    features: DailyStrategyFeatures,
) -> tuple[float, dict[str, float], tuple[str, ...]]:
    variant_scores: dict[str, float] = {}
    loose = features.variant_evidence.get("b1.loose_3of4")
    if loose and loose.strength is not None:
        # Deliberately retain partial setup quality as a continuous score.  It
        # can rank candidates, but buy_points.py will not confirm an order
        # unless the variant itself is matched.
        variant_scores["b1.loose_3of4"] = loose.strength
    if _variant_matched(features, "b1.strict_oversold"):
        variant_scores["b1.strict_oversold"] = 85.0
    if _variant_matched(features, "b1.quality_confirmed"):
        variant_scores["b1.quality_confirmed"] = 100.0
    if _variant_matched(features, "b2.knowledge_5bar"):
        variant_scores["b2.knowledge_5bar"] = 90.0
    if _variant_matched(features, "b2.legacy_5_14bar"):
        variant_scores["b2.legacy_5_14bar"] = 80.0
    if _variant_matched(features, "b3.pullback_reentry"):
        variant_scores["b3.pullback_reentry"] = 85.0
    if _variant_matched(features, "b3.consensus_continuation"):
        variant_scores["b3.consensus_continuation"] = 75.0
    if _variant_matched(features, "super_b1.washout"):
        variant_scores["super_b1.washout"] = 100.0
    if _variant_matched(features, "sb1.reclaim_confirmation"):
        variant_scores["sb1.reclaim_confirmation"] = 95.0
    score = max(variant_scores.values(), default=0.0)
    winners = tuple(
        name for name, value in variant_scores.items() if value == score and score > 0
    )
    return score, variant_scores, winners


def _trend_score(bars: list[DailyData]) -> tuple[float, dict[str, object]]:
    closes = [bar.close for bar in bars]
    ma5 = calculate_ma(closes, 5)
    ma10 = calculate_ma(closes, 10)
    ma20 = calculate_ma(closes, 20)
    if ma5 > ma10 > ma20:
        ma_score = 100.0
    elif ma5 > ma10 or ma10 > ma20:
        ma_score = 70.0
    elif bars[-1].close >= ma20:
        ma_score = 50.0
    else:
        ma_score = 0.0

    if len(bars) >= 25:
        current_bbi = calculate_bbi(bars)
        previous_bbi = calculate_bbi(bars[:-1])
        above_bbi = current_bbi > 0 and bars[-1].close >= current_bbi
        bbi_rising = current_bbi > previous_bbi > 0
        if above_bbi and bbi_rising:
            bbi_score = 100.0
        elif above_bbi or bbi_rising:
            bbi_score = 70.0
        else:
            bbi_score = 0.0
    else:
        current_bbi = 0.0
        bbi_score = 0.0

    rope = detect_bull_rope(bars) if len(bars) >= 120 else {"status": "数据不足"}
    rope_score = {
        "金叉": 100.0,
        "牵牛": 80.0,
        "牛绳断": 20.0,
        "死叉": 0.0,
    }.get(str(rope.get("status")), 0.0)
    score = ma_score * 0.40 + bbi_score * 0.30 + rope_score * 0.30
    return score, {
        "ma_score": ma_score,
        "bbi_score": bbi_score,
        "bull_rope_score": rope_score,
        "bull_rope_status": rope.get("status"),
        "bbi": current_bbi,
    }


def _volume_score(features: DailyStrategyFeatures) -> float:
    ratio_previous = features.continuous_features.get("volume_ratio_prev")
    ratio_five = features.continuous_features.get("volume_ratio_5d")
    pct_chg = float(features.continuous_features.get("pct_chg") or 0.0)
    is_pullback_entry = any(
        _variant_matched(features, name)
        for name in (
            "b1.loose_3of4",
            "b1.strict_oversold",
            "b1.quality_confirmed",
            "super_b1.washout",
            "sb1.reclaim_confirmation",
        )
    )
    is_b2 = _variant_matched(features, "b2.knowledge_5bar") or _variant_matched(
        features, "b2.legacy_5_14bar"
    )
    if (
        features.boolean_features.get("close_below_prev_close")
        and isinstance(ratio_previous, (int, float))
        and ratio_previous > 1.5
    ):
        return 0.0
    if pct_chg < -2 and isinstance(ratio_previous, (int, float)) and ratio_previous > 1.2:
        return 10.0
    if pct_chg > 2 and isinstance(ratio_previous, (int, float)) and ratio_previous > 3:
        return 100.0
    if is_pullback_entry and isinstance(ratio_five, (int, float)):
        if ratio_five <= 0.6:
            return 100.0
        if ratio_five <= 0.8:
            return 90.0
    if is_b2 and isinstance(ratio_previous, (int, float)) and 1.2 <= ratio_previous <= 3:
        return 90.0
    return 50.0


def _confidence_adjusted(base: float, confidence: float) -> float:
    return 50.0 + (base - 50.0) * max(0.0, min(1.0, confidence))


def _stage_score(bars: list[DailyData]) -> tuple[float, dict[str, object]]:
    waves = detect_three_waves(bars)
    wave_base = {
        "建仓波": 90.0,
        "拉升波": 60.0,
        "冲刺波": 0.0,
        "未知": 50.0,
    }.get(str(waves.get("wave")), 50.0)
    wave_score = _confidence_adjusted(wave_base, float(waves.get("confidence", 0)))

    kirin = detect_kirin_stage(bars)
    kirin_base = {
        "吸筹": 90.0,
        "拉升": 60.0,
        "派发": 0.0,
        "回落": 10.0,
        "未知": 50.0,
    }.get(str(kirin.get("stage")), 50.0)
    kirin_score = _confidence_adjusted(kirin_base, float(kirin.get("confidence", 0)))
    return (wave_score + kirin_score) / 2, {
        "wave": waves.get("wave"),
        "wave_confidence": waves.get("confidence"),
        "kirin_stage": kirin.get("stage"),
        "kirin_confidence": kirin.get("confidence"),
    }


def _resonance_score(features: DailyStrategyFeatures) -> tuple[float, tuple[str, ...]]:
    matched_categories = {
        name.split(".", 1)[0]
        for name, evidence in features.variant_evidence.items()
        if name in ENTRY_VARIANT_NAMES and evidence.matched
    }
    return min(100.0, len(matched_categories) * 25.0), tuple(sorted(matched_categories))


def _stop_score(position: PositionState, close: float) -> tuple[float, tuple[str, ...]]:
    if position.shares <= 0 or position.stop_loss is None or position.stop_loss <= 0:
        return 0.0, ()
    distance = (close - position.stop_loss) / position.stop_loss
    if distance <= 0:
        return 100.0, ("EXIT_STOP_LOSS",)
    if distance <= 0.01:
        return 90.0, ()
    if distance <= 0.02:
        return 75.0, ()
    if distance <= 0.03:
        return 60.0, ()
    if distance <= 0.05:
        return 35.0, ()
    if distance <= 0.08:
        return 15.0, ()
    return 0.0, ()


def _exit_signal_score(features: DailyStrategyFeatures) -> tuple[float, tuple[str, ...]]:
    matched: list[tuple[str, float]] = []
    for name in ("s1.ugly_hat", "s2.anchor_divergence", "s3.failed_rebound"):
        evidence = features.variant_evidence.get(name)
        if evidence and evidence.matched:
            matched.append((name, float(evidence.strength or 0)))
    score = max((strength for _, strength in matched), default=0.0)
    if len(matched) >= 2:
        score = min(100.0, score + 15.0)
    return score, tuple(name for name, _ in matched)


def _trend_break_score(
    bars: list[DailyData], trend_diagnostics: dict[str, object]
) -> float:
    candidates: list[float] = []
    status = trend_diagnostics.get("bull_rope_status")
    if status == "死叉":
        candidates.append(100.0)
    elif status == "牛绳断":
        candidates.append(80.0)

    if len(bars) >= 11:
        current_white = calculate_zg_white(bars)
        previous_white = calculate_zg_white(bars[:-1])
        if bars[-1].close < current_white and bars[-2].close < previous_white:
            candidates.append(85.0)
    if len(bars) >= 26:
        current_bbi = calculate_bbi(bars)
        previous_bbi = calculate_bbi(bars[:-1])
        if bars[-1].close < current_bbi and bars[-2].close < previous_bbi:
            candidates.append(75.0)

    ma20 = calculate_ma([bar.close for bar in bars], 20)
    if bars[-1].close < ma20:
        candidates.append(55.0)
    dif, _, _ = precompute_macd_sequence(bars)
    if len(dif) >= 2 and dif[-2] >= 0 > dif[-1]:
        candidates.append(65.0)
    return max(candidates, default=0.0)


def _distribution_score(
    bars: list[DailyData], features: DailyStrategyFeatures
) -> tuple[float, int]:
    result = detect_chuhuo_wushi(bars)
    total = int(result.get("total_score", 0) or 0)
    pattern_score = min(100.0, total / 5 * 100.0)
    s1 = features.variant_evidence.get("s1.ugly_hat")
    return max(pattern_score, float(s1.strength or 0) if s1 and s1.matched else 0.0), total


def _position_heat_score(position: PositionState, max_position_pct: float) -> float:
    utilization = position.current_position_pct / max_position_pct
    if utilization <= 0.75:
        return 0.0
    if utilization <= 1.0:
        return (utilization - 0.75) / 0.25 * 60.0
    if utilization <= 1.25:
        return 60.0 + (utilization - 1.0) / 0.25 * 40.0
    return 100.0


def build_score_evidence(
    bars: list[DailyData] | tuple[DailyData, ...],
    features: DailyStrategyFeatures,
    position: PositionState,
    market: MarketSnapshot,
    *,
    max_position_pct: float,
) -> EvidenceAdapterResult:
    """Map one confirmed prefix into all v0.1 normalized score dimensions."""

    if not 0 < max_position_pct <= 1:
        raise ValueError("max_position_pct must be in (0, 1]")
    confirmed = list(enrich_daily_bars(bars, features.signal_date))
    if confirmed[-1].trade_date.replace("-", "") != features.bars_end_date:
        raise ValueError("feature snapshot does not match the supplied bar prefix")
    if confirmed[-1].ts_code != features.ts_code or position.ts_code != features.ts_code:
        raise ValueError("bars, features and position must refer to the same stock")
    if market.trade_date != normalize_trade_date(features.signal_date):
        raise ValueError("market snapshot date must equal the signal date")

    entry_structure, entry_variant_scores, entry_winners = _entry_structure_score(
        features
    )
    trend, trend_diagnostics = _trend_score(confirmed)
    volume = _volume_score(features)
    sandglass = calculate_sandglass_score(confirmed)
    pattern_quality = float(sandglass.get("score", 0))
    stage, stage_diagnostics = _stage_score(confirmed)
    resonance, matched_categories = _resonance_score(features)
    matched_entries = matched_entry_variants(features)

    risk_penalty = 0.0
    if features.boolean_features.get("close_below_prev_close") and features.boolean_features.get(
        "volume_above_prev_15"
    ):
        risk_penalty += 15.0
    if stage_diagnostics.get("wave") == "冲刺波":
        risk_penalty += 20.0
    if stage_diagnostics.get("kirin_stage") == "派发":
        risk_penalty += 20.0
    if trend_diagnostics.get("bull_rope_status") in ("牛绳断", "死叉"):
        risk_penalty += 10.0
    risk_penalty = min(30.0, risk_penalty)

    stop, hard_exit_reasons = _stop_score(position, confirmed[-1].close)
    exit_signal, matched_exits = _exit_signal_score(features)
    trend_break = _trend_break_score(confirmed, trend_diagnostics)
    distribution, distribution_count = _distribution_score(confirmed, features)
    position_heat = _position_heat_score(position, max_position_pct)

    evidence = ScoreEvidence(
        buy=BuyComponents(
            entry_structure=entry_structure,
            trend=trend,
            volume=volume,
            pattern_quality=pattern_quality,
            stage=stage,
            market=market.score,
            resonance=resonance,
        ),
        sell=SellComponents(
            stop=stop,
            exit_signal=exit_signal,
            trend_break=trend_break,
            distribution=distribution,
            market_risk=100.0 - market.score,
            position_heat=position_heat,
            profit_protection=0.0,
        ),
        risk_penalty_points=risk_penalty,
        hard_vetoes=features.hard_vetoes,
        hard_exit_reasons=hard_exit_reasons,
    )
    reasons = tuple(
        [f"entry_variant:{name}" for name in matched_entries]
        + [f"entry_family:{category}" for category in matched_categories]
        + [f"exit:{name}" for name in matched_exits]
        + list(hard_exit_reasons)
    )
    return EvidenceAdapterResult(
        score_evidence=evidence,
        reasons=reasons,
        diagnostics={
            "trend": trend_diagnostics,
            "stage": stage_diagnostics,
            "sandglass": sandglass,
            "entry_structure": {
                "component_score": entry_structure,
                "variant_scores": entry_variant_scores,
                "winning_variants": entry_winners,
                "matched_variants": matched_entries,
            },
            "matched_categories": matched_categories,
            "matched_exits": matched_exits,
            "distribution_signal_count": distribution_count,
            "market_version": market.version,
            "market_source_hash": market.source_hash,
        },
    )
