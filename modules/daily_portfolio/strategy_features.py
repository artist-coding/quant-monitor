"""As-of-safe strategy evidence and named legacy-compatible variants."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..indicators import (
    DailyData,
    calculate_bbi,
    calculate_dg_yellow,
    calculate_zg_white,
    precompute_kdj_sequence,
)
from ..strategies.sell_signals import detect_s1, detect_s2
from .bar_features import enrich_daily_bars
from .dates import normalize_trade_date


@dataclass(frozen=True)
class AnchorEvidence:
    anchor_date: str
    age_bars: int
    anchor_variant: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "anchor_date", normalize_trade_date(self.anchor_date))
        if self.age_bars < 0:
            raise ValueError("anchor age cannot be negative")
        if not self.anchor_variant:
            raise ValueError("anchor variant cannot be empty")


@dataclass(frozen=True)
class VariantEvidence:
    name: str
    matched: bool
    strength: float | None
    anchor: AnchorEvidence | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.strength is not None and not 0 <= self.strength <= 100:
            raise ValueError("variant strength must be between 0 and 100")


@dataclass(frozen=True)
class DailyStrategyFeatures:
    ts_code: str
    signal_date: str
    bars_end_date: str
    feature_version: str
    required_bars: int
    missing_fields: tuple[str, ...]
    continuous_features: dict[str, float | int | None]
    boolean_features: dict[str, bool | None]
    sequence_anchors: dict[str, AnchorEvidence]
    variant_evidence: dict[str, VariantEvidence]
    eligibility_gates: dict[str, bool | None]
    hard_vetoes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.ts_code or not self.feature_version:
            raise ValueError("feature stock code and version cannot be empty")
        signal_date = normalize_trade_date(self.signal_date)
        bars_end_date = normalize_trade_date(self.bars_end_date)
        if bars_end_date > signal_date:
            raise ValueError("bars_end_date cannot be after signal_date")
        if self.required_bars <= 0:
            raise ValueError("required_bars must be positive")
        object.__setattr__(self, "signal_date", signal_date)
        object.__setattr__(self, "bars_end_date", bars_end_date)


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _amplitude_pct(bar: DailyData) -> float | None:
    return (
        (bar.high - bar.low) / bar.prev_close * 100
        if bar.prev_close > 0
        else None
    )


def _n_structure_ok(bars: Sequence[DailyData], index: int) -> bool | None:
    lookback = min(index, 20)
    if lookback < 10:
        return None
    window = bars[index - lookback : index + 1]
    midpoint = len(window) // 2
    first_low = min(bar.low for bar in window[:midpoint])
    second_low = min(bar.low for bar in window[midpoint:])
    return second_low >= first_low * 0.98


def _four_down_closes(bars: Sequence[DailyData], index: int) -> bool | None:
    if index < 3:
        return None
    return all(bars[item].close < bars[item].prev_close for item in range(index - 3, index + 1))


def _b1_variants(
    bars: Sequence[DailyData],
    kdj: Sequence[tuple[float, float, float]],
    index: int,
) -> dict[str, VariantEvidence]:
    today = bars[index]
    j = kdj[index][2] if index >= 8 else None
    amplitude = _amplitude_pct(today)
    volume_ratio = (
        _ratio(today.vol, bars[index - 1].vol) if index > 0 else None
    )
    pct_in_range = -2 <= today.pct_chg <= 1.8
    loose_flags: tuple[bool | None, ...] = (
        j < 13 if j is not None else None,
        amplitude < 4 if amplitude is not None else None,
        pct_in_range,
        volume_ratio < 1 if volume_ratio is not None else None,
    )
    observed_loose = [flag for flag in loose_flags if flag is not None]
    loose_count = sum(flag is True for flag in observed_loose)
    loose_strength = (
        loose_count / len(observed_loose) * 100 if observed_loose else None
    )
    loose = VariantEvidence(
        name="b1.loose_3of4",
        matched=len(observed_loose) == 4 and loose_count >= 3,
        strength=loose_strength,
        details={"matched_conditions": loose_count, "condition_count": 4},
    )

    four_down = _four_down_closes(bars, index)
    strict_flags = (
        j < -10 if j is not None else None,
        not four_down if four_down is not None else None,
    )
    observed_strict = [flag for flag in strict_flags if flag is not None]
    strict = VariantEvidence(
        name="b1.strict_oversold",
        matched=len(observed_strict) == 2 and all(observed_strict),
        strength=(
            sum(flag is True for flag in observed_strict)
            / len(observed_strict)
            * 100
            if observed_strict
            else None
        ),
        details={"four_down_closes": four_down},
    )

    n_structure = _n_structure_ok(bars, index)
    white_above_yellow: bool | None = None
    if index + 1 >= 114:
        prefix = list(bars[: index + 1])
        white_above_yellow = calculate_zg_white(prefix) >= calculate_dg_yellow(prefix)
    quality_flags: tuple[bool | None, ...] = (
        loose.matched,
        j <= 12 if j is not None else None,
        white_above_yellow,
        volume_ratio < 0.8 if volume_ratio is not None else None,
        n_structure,
    )
    observed_quality = [flag for flag in quality_flags if flag is not None]
    quality = VariantEvidence(
        name="b1.quality_confirmed",
        matched=len(observed_quality) == 5 and all(observed_quality),
        strength=(
            sum(flag is True for flag in observed_quality)
            / len(observed_quality)
            * 100
            if observed_quality
            else None
        ),
        details={
            "white_above_yellow": white_above_yellow,
            "n_structure_ok": n_structure,
            "volume_ratio_prev": volume_ratio,
        },
    )
    return {item.name: item for item in (loose, strict, quality)}


def _latest_anchor(
    histories: Sequence[dict[str, VariantEvidence]],
    current_index: int,
    variant: str,
    minimum_age: int,
    maximum_age: int,
) -> tuple[int, int, str] | None:
    """Find a matched prior variant; the current bar is always excluded."""

    for anchor_index in range(current_index - minimum_age, current_index - maximum_age - 1, -1):
        if anchor_index < 0:
            continue
        evidence = histories[anchor_index].get(variant)
        if evidence and evidence.matched:
            return anchor_index, current_index - anchor_index, variant
    return None


def _with_anchor_date(
    found: tuple[int, int, str] | None,
    bars: Sequence[DailyData],
) -> tuple[int, AnchorEvidence] | None:
    if found is None:
        return None
    index, age_bars, variant = found
    return index, AnchorEvidence(
        anchor_date=bars[index].trade_date,
        age_bars=age_bars,
        anchor_variant=variant,
    )


def _b2_variants(
    bars: Sequence[DailyData],
    kdj: Sequence[tuple[float, float, float]],
    b1_history: Sequence[dict[str, VariantEvidence]],
    index: int,
) -> dict[str, VariantEvidence]:
    today = bars[index]
    j = kdj[index][2] if index >= 8 else None
    volume_ratio = _ratio(today.vol, bars[index - 1].vol) if index > 0 else None

    knowledge_anchor = _with_anchor_date(
        _latest_anchor(b1_history, index, "b1.quality_confirmed", 1, 5), bars
    )
    knowledge_conditions: tuple[bool | None, ...] = (
        knowledge_anchor is not None,
        today.pct_chg >= 4,
        volume_ratio >= 1.2 if volume_ratio is not None else None,
        j < 55 if j is not None else None,
    )
    observed = [condition for condition in knowledge_conditions if condition is not None]
    knowledge = VariantEvidence(
        name="b2.knowledge_5bar",
        matched=len(observed) == 4 and all(observed),
        strength=sum(condition is True for condition in observed) / len(observed) * 100,
        anchor=knowledge_anchor[1] if knowledge_anchor else None,
        details={"volume_ratio_prev": volume_ratio, "kdj_j": j},
    )

    legacy_anchor = _with_anchor_date(
        _latest_anchor(b1_history, index, "b1.strict_oversold", 5, 14), bars
    )
    legacy_conditions: tuple[bool | None, ...] = (
        legacy_anchor is not None,
        today.pct_chg >= 4,
        volume_ratio >= 2 if volume_ratio is not None else None,
    )
    legacy_observed = [condition for condition in legacy_conditions if condition is not None]
    legacy = VariantEvidence(
        name="b2.legacy_5_14bar",
        matched=len(legacy_observed) == 3 and all(legacy_observed),
        strength=(
            sum(condition is True for condition in legacy_observed)
            / len(legacy_observed)
            * 100
        ),
        anchor=legacy_anchor[1] if legacy_anchor else None,
        details={"volume_ratio_prev": volume_ratio, "kdj_j": j},
    )
    return {item.name: item for item in (knowledge, legacy)}


def _b3_variants(
    bars: Sequence[DailyData],
    b2_history: Sequence[dict[str, VariantEvidence]],
    index: int,
) -> dict[str, VariantEvidence]:
    today = bars[index]
    amplitude = _amplitude_pct(today)

    pullback_anchor = _with_anchor_date(
        _latest_anchor(b2_history, index, "b2.knowledge_5bar", 2, 5), bars
    )
    pullback_volume_ratio: float | None = None
    pullback_not_break: bool | None = None
    if pullback_anchor:
        anchor_bar = bars[pullback_anchor[0]]
        pullback_volume_ratio = _ratio(today.vol, anchor_bar.vol)
        pullback_not_break = today.low >= anchor_bar.low * 0.98
    pullback_conditions: tuple[bool | None, ...] = (
        pullback_anchor is not None,
        pullback_volume_ratio < 0.8 if pullback_volume_ratio is not None else None,
        pullback_not_break,
        abs(today.pct_chg) < 3,
    )
    observed = [condition for condition in pullback_conditions if condition is not None]
    pullback = VariantEvidence(
        name="b3.pullback_reentry",
        matched=len(observed) == 4 and all(observed),
        strength=sum(condition is True for condition in observed) / len(observed) * 100,
        anchor=pullback_anchor[1] if pullback_anchor else None,
        details={
            "volume_ratio_anchor": pullback_volume_ratio,
            "not_break_anchor_low": pullback_not_break,
        },
    )

    continuation_anchor = _with_anchor_date(
        _latest_anchor(b2_history, index, "b2.knowledge_5bar", 3, 9), bars
    )
    continuation_conditions: tuple[bool | None, ...] = (
        continuation_anchor is not None,
        0 < today.pct_chg < 2,
        amplitude < 7 if amplitude is not None else None,
    )
    continuation_observed = [
        condition for condition in continuation_conditions if condition is not None
    ]
    continuation = VariantEvidence(
        name="b3.consensus_continuation",
        matched=len(continuation_observed) == 3 and all(continuation_observed),
        strength=(
            sum(condition is True for condition in continuation_observed)
            / len(continuation_observed)
            * 100
        ),
        anchor=continuation_anchor[1] if continuation_anchor else None,
        details={"amplitude_prev_close_pct": amplitude},
    )
    return {item.name: item for item in (pullback, continuation)}


def _sb1_variants(
    bars: Sequence[DailyData],
    kdj: Sequence[tuple[float, float, float]],
    index: int,
) -> dict[str, VariantEvidence]:
    today = bars[index]
    j = kdj[index][2] if index >= 8 else None

    false_break = False
    prior_low: float | None = None
    if index >= 5:
        prior_low = min(bar.low for bar in bars[index - 5 : index])
        false_break = today.low < prior_low
    false_break_evidence = VariantEvidence(
        name="sb1.false_break",
        matched=false_break,
        strength=100.0 if false_break else 0.0,
        details={"prior_low": prior_low},
    )

    reclaim = False
    reclaim_anchor: AnchorEvidence | None = None
    reclaim_prior_low: float | None = None
    if index >= 6:
        fake_drop = bars[index - 1]
        reclaim_prior_low = min(bar.low for bar in bars[index - 6 : index - 1])
        broken = fake_drop.low < reclaim_prior_low
        reclaimed = today.close > reclaim_prior_low and today.pct_chg > 2
        volume_confirmed = today.vol > fake_drop.vol * 1.2
        reclaim = broken and reclaimed and volume_confirmed
        if broken:
            reclaim_anchor = AnchorEvidence(
                anchor_date=fake_drop.trade_date,
                age_bars=1,
                anchor_variant="sb1.false_break",
            )
    reclaim_evidence = VariantEvidence(
        name="sb1.reclaim_confirmation",
        matched=reclaim,
        strength=100.0 if reclaim else 0.0,
        anchor=reclaim_anchor,
        details={"prior_low": reclaim_prior_low},
    )

    washout = False
    shrink = False
    washout_anchor: AnchorEvidence | None = None
    if index >= 2:
        washout_bar = bars[index - 2]
        shrink = today.vol < bars[index - 1].vol
        washout = washout_bar.is_fangliang_yinxian and shrink and j is not None and j < -5
        if washout_bar.is_fangliang_yinxian:
            washout_anchor = AnchorEvidence(
                anchor_date=washout_bar.trade_date,
                age_bars=2,
                anchor_variant="super_b1.washout_source",
            )
    washout_evidence = VariantEvidence(
        name="super_b1.washout",
        matched=washout,
        strength=100.0 if washout else 0.0,
        anchor=washout_anchor,
        details={"volume_shrinking": shrink, "kdj_j": j},
    )
    return {
        item.name: item
        for item in (false_break_evidence, reclaim_evidence, washout_evidence)
    }


def _exit_variants(bars: list[DailyData], index: int) -> dict[str, VariantEvidence]:
    variants: dict[str, VariantEvidence] = {}
    s1 = detect_s1(bars, index)
    variants["s1.ugly_hat"] = VariantEvidence(
        name="s1.ugly_hat",
        matched=s1 is not None,
        strength=s1.confidence * 100 if s1 else 0.0,
        details=dict(s1.details) if s1 else {},
    )

    s2 = detect_s2(bars, index)
    s2_anchor = None
    if s2 and s2.details.get("prev_high_date"):
        anchor_date = str(s2.details["prev_high_date"])
        anchor_index = next(
            item for item, bar in enumerate(bars) if bar.trade_date == anchor_date
        )
        s2_anchor = AnchorEvidence(
            anchor_date=anchor_date,
            age_bars=index - anchor_index,
            anchor_variant="price_high",
        )
    variants["s2.anchor_divergence"] = VariantEvidence(
        name="s2.anchor_divergence",
        matched=s2 is not None,
        strength=s2.confidence * 100 if s2 else 0.0,
        anchor=s2_anchor,
        details=dict(s2.details) if s2 else {},
    )

    s3_anchor_index: int | None = None
    if index >= 15:
        for candidate in range(index - 1, max(-1, index - 15), -1):
            candidate_bar = bars[candidate]
            if candidate_bar.is_fangliang_yinxian and candidate_bar.close < candidate_bar.open:
                s3_anchor_index = candidate
                break
    s3_matched = False
    s3_details: dict[str, Any] = {}
    s3_anchor = None
    if s3_anchor_index is not None:
        anchor_bar = bars[s3_anchor_index]
        s3_matched = (
            anchor_bar.open * 0.95 <= bars[index].close <= anchor_bar.high * 1.02
            and bars[index].vol <= anchor_bar.vol * 0.7
            and bars[index].pct_chg <= 2
        )
        s3_anchor = AnchorEvidence(
            anchor_date=anchor_bar.trade_date,
            age_bars=index - s3_anchor_index,
            anchor_variant="s1.high_volume_bearish_bar",
        )
        s3_details = {
            "anchor_high": anchor_bar.high,
            "volume_ratio_anchor": _ratio(bars[index].vol, anchor_bar.vol),
        }
    variants["s3.failed_rebound"] = VariantEvidence(
        name="s3.failed_rebound",
        matched=s3_matched,
        strength=70.0 if s3_matched else 0.0,
        anchor=s3_anchor,
        details=s3_details,
    )
    return variants


def build_strategy_features(
    bars: list[DailyData] | tuple[DailyData, ...],
    as_of_date: str,
    *,
    required_bars: int = 120,
    feature_version: str = "daily-strategy-features-v0.1",
) -> DailyStrategyFeatures:
    """Build one auditable feature snapshot from an explicitly bounded prefix."""

    enriched = list(enrich_daily_bars(bars, as_of_date))
    index = len(enriched) - 1
    today = enriched[index]
    kdj = precompute_kdj_sequence(enriched)

    b1_history = [_b1_variants(enriched, kdj, item) for item in range(len(enriched))]
    b2_history = [
        _b2_variants(enriched, kdj, b1_history, item)
        for item in range(len(enriched))
    ]
    variants = dict(b1_history[index])
    variants.update(b2_history[index])
    variants.update(_b3_variants(enriched, b2_history, index))
    variants.update(_sb1_variants(enriched, kdj, index))
    variants.update(_exit_variants(enriched, index))

    previous = enriched[index - 1] if index > 0 else None
    amplitude = _amplitude_pct(today)
    day_range = today.high - today.low
    high_20 = max(bar.high for bar in enriched[max(0, index - 19) : index + 1])
    low_20 = min(bar.low for bar in enriched[max(0, index - 19) : index + 1])
    bbi = calculate_bbi(enriched[: index + 1]) if index + 1 >= 24 else None
    white = calculate_zg_white(enriched[: index + 1]) if index + 1 >= 10 else None
    yellow = calculate_dg_yellow(enriched[: index + 1]) if index + 1 >= 114 else None
    j = kdj[index][2] if index >= 8 else None
    volume_ratio_prev = _ratio(today.vol, previous.vol) if previous else None
    recent_volumes = [bar.vol for bar in enriched[max(0, index - 5) : index]]
    volume_ratio_5d = (
        _ratio(today.vol, sum(recent_volumes) / len(recent_volumes))
        if recent_volumes
        else None
    )

    continuous: dict[str, float | int | None] = {
        "kdj_k": kdj[index][0] if index >= 8 else None,
        "kdj_d": kdj[index][1] if index >= 8 else None,
        "kdj_j": j,
        "pct_chg": today.pct_chg,
        "amplitude_prev_close_pct": amplitude,
        "body_pct": (today.close - today.open) / today.open * 100,
        "open_gap_pct": (
            (today.open - today.prev_close) / today.prev_close * 100
            if today.prev_close > 0
            else None
        ),
        "close_position": (
            (today.close - today.low) / day_range if day_range > 0 else 0.5
        ),
        "volume_ratio_prev": volume_ratio_prev,
        "volume_ratio_5d": volume_ratio_5d,
        "runup_20_pct": (high_20 - low_20) / low_20 * 100,
        "distance_to_high_20_pct": (today.close / high_20 - 1) * 100,
        "distance_to_bbi_pct": (
            (today.close / bbi - 1) * 100 if bbi and bbi > 0 else None
        ),
        "white_yellow_spread_pct": (
            (white / yellow - 1) * 100 if white and yellow and yellow > 0 else None
        ),
    }
    four_down = _four_down_closes(enriched, index)
    n_structure = _n_structure_ok(enriched, index)
    booleans: dict[str, bool | None] = {
        "j_lt_13": j < 13 if j is not None else None,
        "j_le_12": j <= 12 if j is not None else None,
        "j_lt_minus_10": j < -10 if j is not None else None,
        "pct_in_b1_range": -2 <= today.pct_chg <= 1.8,
        "amplitude_lt_4": amplitude < 4 if amplitude is not None else None,
        "amplitude_lt_7": amplitude < 7 if amplitude is not None else None,
        "volume_below_prev": volume_ratio_prev < 1 if volume_ratio_prev is not None else None,
        "volume_below_prev_08": volume_ratio_prev < 0.8 if volume_ratio_prev is not None else None,
        "volume_below_prev_05": volume_ratio_prev <= 0.5 if volume_ratio_prev is not None else None,
        "volume_above_prev_12": volume_ratio_prev >= 1.2 if volume_ratio_prev is not None else None,
        "volume_above_prev_15": volume_ratio_prev >= 1.5 if volume_ratio_prev is not None else None,
        "volume_above_prev_20": volume_ratio_prev >= 2 if volume_ratio_prev is not None else None,
        "four_down_closes": four_down,
        "n_structure_ok": n_structure,
        "white_above_yellow": white >= yellow if white is not None and yellow is not None else None,
        "close_above_bbi": today.close >= bbi if bbi is not None else None,
        "close_below_open": today.close < today.open,
        "close_below_prev_close": today.close < today.prev_close,
        "near_20d_high": today.close >= high_20 * 0.90,
    }

    anchors = {
        name: evidence.anchor
        for name, evidence in variants.items()
        if evidence.anchor is not None
    }
    missing: list[str] = []
    if index + 1 < 9:
        missing.append("kdj")
    if index + 1 < 24:
        missing.append("bbi")
    if index + 1 < 114:
        missing.append("yellow_line")
    if index + 1 < required_bars:
        missing.append("required_history")
    hard_vetoes: list[str] = []
    if index + 1 < required_bars:
        hard_vetoes.append("INSUFFICIENT_HISTORY")
    if today.trade_date.replace("-", "") < as_of_date.replace("-", ""):
        hard_vetoes.append("STALE_BAR")

    return DailyStrategyFeatures(
        ts_code=today.ts_code,
        signal_date=as_of_date.replace("-", ""),
        bars_end_date=today.trade_date.replace("-", ""),
        feature_version=feature_version,
        required_bars=required_bars,
        missing_fields=tuple(missing),
        continuous_features=continuous,
        boolean_features=booleans,
        sequence_anchors=anchors,
        variant_evidence=variants,
        eligibility_gates={
            "has_required_history": index + 1 >= required_bars,
            "has_yellow_line": index + 1 >= 114,
            "data_prefix_confirmed": True,
        },
        hard_vetoes=tuple(hard_vetoes),
    )
