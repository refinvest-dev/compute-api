"""Canonical serialization and deterministic identity helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence, Set
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from refinvest.trading_review.decimal import ExactRational, ScaledDecimal

HASH_EXCLUDED_FIELDS = frozenset(
    {
        "artifact_id",
        "content",
        "generated_at",
        "created_at",
        "terminal_at",
        "manifest_hash",
        "result_hash",
    }
)


def timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def decimal_node(value: Decimal) -> dict[str, int | str]:
    return ScaledDecimal.from_decimal(value).canonical_node()


def canonical_value(value: Any) -> Any:
    if isinstance(value, ScaledDecimal):
        return value.canonical_node()
    if isinstance(value, ExactRational):
        return {"numerator": value.numerator, "denominator": value.denominator}
    if isinstance(value, Decimal):
        return decimal_node(value)
    if isinstance(value, datetime):
        return timestamp_text(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        result: dict[str, Any] = {}
        for field in fields(value):
            if (
                field.name.startswith("_")
                or field.name in HASH_EXCLUDED_FIELDS
                or field.metadata.get("hash") is False
            ):
                continue
            result[field.name] = canonical_value(getattr(value, field.name))
        return result
    if isinstance(value, dict):
        mapping = cast(Mapping[object, object], value)
        return {str(key): canonical_value(item) for key, item in mapping.items()}
    if isinstance(value, (tuple, list)):
        sequence = cast(Sequence[object], value)
        return [canonical_value(item) for item in sequence]
    if isinstance(value, (set, frozenset)):
        values = cast(Set[object], value)
        return sorted((canonical_value(item) for item in values), key=_canonical_sort_key)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical value type: {type(value).__name__}")


def _canonical_sort_key(value: Any) -> bytes:
    return canonical_json_bytes(value)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_hex(canonical_json_bytes(value))


def canonical_tuple(*components: str) -> bytes:
    output = bytearray()
    for component in components:
        encoded = component.encode("utf-8")
        output.extend(len(encoded).to_bytes(8, byteorder="big", signed=False))
        output.extend(encoded)
    return bytes(output)


def stable_id(namespace: str, payload: Any) -> str:
    return f"{namespace}_{content_hash(payload)}"
