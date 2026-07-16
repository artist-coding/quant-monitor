"""Canonical, prefix-stable feature enrichment for legacy strategy adapters."""

from __future__ import annotations

from dataclasses import replace

from ..indicators import DailyData
from .contracts import validate_bars_as_of


def enrich_daily_bars(
    bars: list[DailyData] | tuple[DailyData, ...],
    as_of_date: str,
) -> tuple[DailyData, ...]:
    """Derive legacy volume/price flags from one confirmed OHLCV prefix.

    Several legacy B2/B3 functions consume flags that the standard datasource
    never populated.  This adapter computes those flags exactly once using the
    definitions shared by ``modules.strategies.core``.  It returns new objects
    and clears any copied indicator cache so inputs and future prefixes cannot
    contaminate one another.
    """

    confirmed = validate_bars_as_of(bars, as_of_date)
    enriched: list[DailyData] = []
    for index, bar in enumerate(confirmed):
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            raise ValueError(f"OHLC prices must be positive on {bar.trade_date}")
        if bar.vol < 0 or bar.amount < 0:
            raise ValueError(f"volume and amount cannot be negative on {bar.trade_date}")

        if index == 0:
            previous_close = bar.prev_close if bar.prev_close > 0 else bar.close
            pct_chg = bar.pct_chg
            is_rise = False
            is_beidou = False
            is_suoliang = False
            is_jiayin = False
            is_yinxian = False
            is_fangliang_yinxian = False
        else:
            previous = confirmed[index - 1]
            previous_close = previous.close
            previous_volume = previous.vol
            pct_chg = (bar.close / previous_close - 1) * 100
            is_rise = bar.close > previous_close
            is_beidou = previous_volume > 0 and bar.vol >= previous_volume * 2
            is_suoliang = previous_volume > 0 and bar.vol <= previous_volume * 0.5
            is_jiayin = bar.close < bar.open and bar.close > previous_close
            is_yinxian = bar.close < previous_close
            is_fangliang_yinxian = (
                previous_volume > 0
                and bar.close < previous_close
                and bar.vol > previous_volume * 1.5
            )

        enriched.append(
            replace(
                bar,
                prev_close=previous_close,
                pct_chg=pct_chg,
                is_rise=is_rise,
                is_beidou=is_beidou,
                is_suoliang=is_suoliang,
                is_jiayin=is_jiayin,
                is_yinxian=is_yinxian,
                is_fangliang_yinxian=is_fangliang_yinxian,
                kdj_k=None,
                kdj_d=None,
                kdj_j=None,
                bbi=None,
                macd_dif=None,
                macd_dea=None,
                macd_hist=None,
            )
        )
    return tuple(enriched)
