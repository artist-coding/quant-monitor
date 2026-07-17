"""Canonical JSON and content hashing for reproducible overlay decisions."""

from __future__ import annotations

import hashlib
import json
import math
from enum import Enum
from typing import Any
from collections.abc import Mapping

from .exceptions import OverlayValidationError


def _to_json_value(value: Any, *, path: str = "$") -> Any:
    """Return a JSON-compatible copy while rejecting lossy coercions."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OverlayValidationError(f"{path} must not contain NaN or Infinity")
        return value
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _to_json_value(value.to_dict(), path=path)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise OverlayValidationError(f"{path} object keys must be strings")
            result[key] = _to_json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise OverlayValidationError(f"{path} contains unsupported JSON value {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize with stable keys, UTF-8 text, and no insignificant spaces."""

    return json.dumps(
        _to_json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value: Any) -> str:
    """Return the lowercase SHA-256 digest of the canonical UTF-8 JSON."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
