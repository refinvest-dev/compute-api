"""Versioned canonical Backtest result representation."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import cast

CANONICALIZATION_VERSION = 1
_RESULT_FIELDS = (
    "metrics",
    "equityCurve",
    "trades",
    "benchmark",
    "signalExecutionMarketRelation",
    "signalExecutionDelay",
    "sampleSizeWarning",
    "dataIntegrityStatus",
)
_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


class CanonicalizationError(ValueError):
    """A result cannot be represented under the requested canonical contract."""


def canonical_result_bytes(
    payload: dict[str, object], version: int = CANONICALIZATION_VERSION
) -> bytes:
    """Select calculation-bearing fields and produce compact, sorted UTF-8 JSON."""
    if version != CANONICALIZATION_VERSION:
        raise CanonicalizationError(f"Unsupported canonicalization version: {version}")
    missing = tuple(field for field in _RESULT_FIELDS if field not in payload)
    if missing:
        raise CanonicalizationError("Backtest result is missing calculation-bearing fields.")
    selected = {field: payload[field] for field in _RESULT_FIELDS}
    try:
        return json.dumps(
            _json_value(selected),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CanonicalizationError("Backtest result contains a non-canonical value.") from error


def canonical_result_hash(
    payload: dict[str, object], version: int = CANONICALIZATION_VERSION
) -> str:
    digest = hashlib.sha256(canonical_result_bytes(payload, version)).hexdigest()
    return f"sha256:{digest}"


def is_result_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH_PATTERN.fullmatch(value) is not None


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        if any(not isinstance(key, str) for key in mapping):
            raise TypeError("Canonical JSON object keys must be strings.")
        return {cast(str, key): _json_value(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in cast(list[object] | tuple[object, ...], value)]
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")
