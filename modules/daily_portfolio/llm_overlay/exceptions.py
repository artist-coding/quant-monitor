"""Validation errors for the local LLM overlay contract."""

from __future__ import annotations


class OverlayValidationError(ValueError):
    """Raised when structured overlay data violates its frozen contract."""
