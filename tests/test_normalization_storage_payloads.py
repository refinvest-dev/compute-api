"""At-rest authentication and stored integrity checks across the extracted boundary."""

import copy
import hashlib
import importlib
import json
from dataclasses import replace

import pytest
from cryptography.exceptions import InvalidTag

from refinvest.contracts.normalization.result_integrity import ResultIntegrityError
from refinvest.persistence.postgres.normalization_payloads import (
    create_payload_cipher,
    decode_terminal_payload,
    encrypt_grants,
    encrypt_terminal_payload,
    require_terminal_cipher,
    with_grant_locators,
)
from refinvest.persistence.postgres.trading_review_jobs import PostgresTradingReviewJobRepository
from tests.integration.test_postgres_trading_review_jobs import _job, _terminal_payload


def _stored():
    job = replace(_job("storage-test"), job_id="storage-job")
    payload = _terminal_payload(job)
    document = json.loads(payload)
    cipher = require_terminal_cipher(create_payload_cipher(b"K" * 32))
    return (
        cipher,
        payload,
        [
            encrypt_terminal_payload(cipher, "payload-id", payload),
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            document["counts"],
            document["sectionDigests"],
            document["resultDigestManifest"],
            document["reconciliationManifest"]["manifestHash"],
            document["resultHash"],
        ],
    )


def test_existing_storage_format_round_trips_exact_utf8_and_random_nonce():
    cipher, payload, row = _stored()
    assert decode_terminal_payload(cipher, "payload-id", row) == payload
    repeated = encrypt_terminal_payload(cipher, "payload-id", payload)
    assert repeated != row[0]
    assert len(repeated) == len(payload) + 12 + 16


@pytest.mark.parametrize("fault", ["key", "identity", "ciphertext", "short"])
def test_terminal_authentication_errors_keep_safe_transport_code(fault):
    cipher, _, row = _stored()
    payload_id = "payload-id"
    if fault == "key":
        cipher = require_terminal_cipher(create_payload_cipher(b"X" * 32))
    elif fault == "identity":
        payload_id = "other-payload"
    elif fault == "ciphertext":
        row[0] = row[0][:-1] + bytes([row[0][-1] ^ 1])
    else:
        row[0] = b"short"
    with pytest.raises(ResultIntegrityError, match="RESULT_TRANSPORT_HASH_MISMATCH"):
        decode_terminal_payload(cipher, payload_id, row)


@pytest.mark.parametrize(
    "column,value,code",
    [
        (1, "0" * 64, "RESULT_TRANSPORT_HASH_MISMATCH"),
        (2, 0, "RESULT_TRANSPORT_HASH_MISMATCH"),
        (3, {}, "RESULT_COUNT_MISMATCH"),
        (4, [], "RESULT_SECTION_HASH_MISMATCH"),
        (5, {}, "RESULT_HASH_MISMATCH"),
        (6, "0" * 64, "RECONCILIATION_MANIFEST_HASH_MISMATCH"),
        (7, "0" * 64, "RESULT_HASH_MISMATCH"),
    ],
)
def test_authenticated_bytes_must_match_persisted_integrity_metadata(column, value, code):
    cipher, _, row = _stored()
    row[column] = value
    with pytest.raises(ResultIntegrityError, match=code):
        decode_terminal_payload(cipher, "payload-id", row)


def test_grant_cipher_uses_job_identity_and_keeps_legacy_unencrypted_inputs():
    cipher = create_payload_cipher(b"K" * 32)
    grants = [{"role": "TRADE_HISTORY", "accessUri": "https://synthetic.invalid/read"}]
    payload = {"artifactAccessGrants": [], "identity": "fixed"}
    original = copy.deepcopy(payload)
    encrypted = encrypt_grants(cipher, "job", grants)
    assert with_grant_locators(cipher, "job", payload, encrypted) == {
        **payload,
        "artifactAccessGrants": grants,
    }
    assert payload == original
    with pytest.raises(InvalidTag):
        with_grant_locators(cipher, "other-job", payload, encrypted)
    assert with_grant_locators(None, "job", payload, None) is payload


def test_missing_cipher_keeps_existing_failure_before_database_access(monkeypatch):
    module = importlib.import_module("refinvest.persistence.postgres.trading_review_jobs")

    def unexpected_connection(*_):
        pytest.fail("Missing key must fail before opening a terminal payload connection.")

    monkeypatch.setattr(module, "connect", unexpected_connection)
    with pytest.raises(RuntimeError, match="Terminal payload encryption is not configured"):
        PostgresTradingReviewJobRepository("unused").get_terminal_payload("payload")
    with pytest.raises(ValueError, match="32 bytes"):
        PostgresTradingReviewJobRepository("unused", b"short", active_job_limit=0)
