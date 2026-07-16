"""防止日线评分读取未来数据的时间契约。"""

from __future__ import annotations

from collections.abc import Sequence

from ..indicators import DailyData
from .dates import TradeDateError, normalize_trade_date


class AsOfContractError(ValueError):
    """评分输入违反 as-of 时间约束。"""


def _normalize_date(value: str) -> str:
    try:
        return normalize_trade_date(value)
    except TradeDateError as exc:
        raise AsOfContractError(str(exc)) from exc


def validate_bars_as_of(bars: Sequence[DailyData], as_of_date: str) -> tuple[DailyData, ...]:
    """验证K线非空、严格升序、无重复且不超过信号日。

    本函数选择拒绝未来数据，而不是静默截断。静默截断会掩盖调用方的
    前视错误，使回测看起来正常但生产逻辑不可信。
    """

    if not bars:
        raise AsOfContractError("bars cannot be empty")

    cutoff = _normalize_date(as_of_date)
    dates = [_normalize_date(bar.trade_date) for bar in bars]

    if len(set(dates)) != len(dates):
        raise AsOfContractError("bars contain duplicate trade dates")
    if dates != sorted(dates):
        raise AsOfContractError("bars must be ordered by trade_date ascending")
    if dates[-1] > cutoff:
        raise AsOfContractError(
            f"future bar {dates[-1]} exceeds as_of_date {cutoff}"
        )

    return tuple(bars)
