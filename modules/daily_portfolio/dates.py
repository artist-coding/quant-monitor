"""Canonical trade-date helpers for daily portfolio contracts."""

from __future__ import annotations

from datetime import datetime


class TradeDateError(ValueError):
    """Raised when a trade date is missing or not a real calendar date."""


def normalize_trade_date(value: str) -> str:
    """Return ``YYYYMMDD`` for supported date strings.

    Daily bars in the existing repository use both ``YYYYMMDD`` and
    ``YYYY-MM-DD``.  Comparing the raw strings would order those formats
    incorrectly, so every portfolio-domain boundary normalizes first.
    """

    if not isinstance(value, str) or not value:
        raise TradeDateError("trade date must be a non-empty string")

    for date_format in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).strftime("%Y%m%d")
        except ValueError:
            continue
    raise TradeDateError(f"unsupported trade date: {value!r}")


def require_later_trade_date(later: str, earlier: str, *, label: str) -> str:
    """Normalize ``later`` and require it to be strictly after ``earlier``."""

    normalized_later = normalize_trade_date(later)
    normalized_earlier = normalize_trade_date(earlier)
    if normalized_later <= normalized_earlier:
        raise TradeDateError(f"{label} must be after {normalized_earlier}")
    return normalized_later
