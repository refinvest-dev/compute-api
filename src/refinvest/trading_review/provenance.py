"""Privacy-preserving source identity and provenance helpers."""

from __future__ import annotations

import hashlib
import hmac

from refinvest.trading_review.hashing import canonical_tuple, sha256_hex


def account_fingerprint(stable_identity_key: bytes, uid_lexeme: str) -> str:
    if not stable_identity_key:
        raise ValueError("stable identity key must not be empty")
    return hmac.new(
        stable_identity_key,
        canonical_tuple("BINANCE", uid_lexeme),
        hashlib.sha256,
    ).hexdigest()


def source_execution_key(account_fingerprint_value: str, symbol: str, trade_id: str) -> str:
    return "sek_" + sha256_hex(canonical_tuple(account_fingerprint_value, symbol, trade_id))


def position_source_identity(artifact_content_hash: str, source_row_number: int) -> str:
    return "psi_" + sha256_hex(canonical_tuple(artifact_content_hash, str(source_row_number)))


def mask_identifier(value: str) -> str:
    visible = value[-4:] if len(value) > 4 else ""
    return f"***{visible}:len={len(value)}"
