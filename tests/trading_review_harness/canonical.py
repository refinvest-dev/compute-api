from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


def decimal_node(value: Decimal | None) -> dict[str, int | str] | None:
    if value is None:
        return None
    sign, digits, exponent = value.as_tuple()
    scale = max(-exponent, 0)
    coefficient = "".join(str(digit) for digit in digits) or "0"
    unscaled = int(coefficient) * (-1 if sign else 1)
    if exponent > 0:
        unscaled *= 10**exponent
    return {"unscaled": str(unscaled), "scale": scale}


def timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_hex(canonical_json_bytes(value))


def decimal_from_node(value: dict[str, int | str]) -> Decimal:
    return Decimal(int(str(value["unscaled"]))).scaleb(-int(value["scale"]))
