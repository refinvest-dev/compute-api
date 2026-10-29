"""Normalization grant encryption and terminal payload storage integrity."""

from __future__ import annotations

import hashlib
import json
from os import urandom
from typing import cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from refinvest.contracts.normalization.result_integrity import (
    ResultIntegrityError,
    parse_result_json_bytes,
    verify_public_result,
)


def create_payload_cipher(key: bytes | None) -> AESGCM | None:
    if key is not None and len(key) != 32:
        raise ValueError("Trading Review payload encryption key must be 32 bytes.")
    return AESGCM(key) if key else None


def require_terminal_cipher(cipher: AESGCM | None) -> AESGCM:
    if cipher is None:
        raise RuntimeError("Terminal payload encryption is not configured.")
    return cipher


def encrypt_grants(cipher: AESGCM | None, job_id: str, grants: list[dict[str, object]]) -> bytes:
    if cipher is None:
        raise RuntimeError("Trading Review encryption is not configured.")
    nonce = urandom(12)
    raw = json.dumps(grants, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return nonce + cipher.encrypt(nonce, raw, job_id.encode())


def with_grant_locators(
    cipher: AESGCM | None, job_id: str, payload: dict[str, object], encrypted: bytes | None
) -> dict[str, object]:
    if encrypted is None:
        return payload  # Live jobs created before migration 0011 retain their original grants.
    if cipher is None:
        raise RuntimeError("Trading Review encryption is not configured.")
    raw = bytes(encrypted)
    grants = json.loads(cipher.decrypt(raw[:12], raw[12:], job_id.encode()))
    return {**payload, "artifactAccessGrants": grants}


def encrypt_terminal_payload(cipher: AESGCM, payload_id: str, payload: bytes) -> bytes:
    nonce = urandom(12)
    encrypted_payload = nonce + cipher.encrypt(nonce, payload, payload_id.encode())
    return encrypted_payload


def decode_terminal_payload(cipher: AESGCM, payload_id: str, row: object) -> bytes:
    row = cast(
        tuple[
            bytes, str, int, dict[str, int], list[dict[str, object]], dict[str, object], str, str
        ],
        row,
    )
    encrypted = bytes(row[0])
    if len(encrypted) < 28:
        raise ResultIntegrityError("RESULT_TRANSPORT_HASH_MISMATCH")
    try:
        payload = cipher.decrypt(encrypted[:12], encrypted[12:], payload_id.encode())
    except InvalidTag as error:
        raise ResultIntegrityError("RESULT_TRANSPORT_HASH_MISMATCH") from error
    if len(payload) != row[2] or hashlib.sha256(payload).hexdigest() != row[1]:
        raise ResultIntegrityError("RESULT_TRANSPORT_HASH_MISMATCH")
    document = parse_result_json_bytes(payload)
    verify_public_result(document)
    manifest = cast(dict[str, object], document["reconciliationManifest"])
    if document["counts"] != row[3]:
        raise ResultIntegrityError("RESULT_COUNT_MISMATCH")
    if document["sectionDigests"] != row[4]:
        raise ResultIntegrityError("RESULT_SECTION_HASH_MISMATCH")
    if document["resultDigestManifest"] != row[5] or document["resultHash"] != row[7]:
        raise ResultIntegrityError("RESULT_HASH_MISMATCH")
    if manifest["manifestHash"] != row[6]:
        raise ResultIntegrityError("RECONCILIATION_MANIFEST_HASH_MISMATCH")
    return payload
