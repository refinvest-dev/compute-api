"""Analytics stored document and ciphertext compatibility boundaries."""

import copy
import importlib
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag

from refinvest.application.analytics_jobs import AnalyticsJobConflict
from refinvest.persistence.postgres.analytics_documents import (
    expiry,
    grants_from_request,
    stable_request,
)
from refinvest.persistence.postgres.analytics_payloads import (
    create_payload_cipher,
    decrypt_payload,
    encrypt_payload,
)
from refinvest.persistence.postgres.trading_analytics_jobs import PostgresAnalyticsJobRepository
from tests.test_trading_analytics_runtime import _request


def test_cipher_keeps_nonce_prefix_aad_and_exact_plaintext(monkeypatch):
    module = importlib.import_module("refinvest.persistence.postgres.analytics_payloads")
    nonce = bytes(range(12))
    monkeypatch.setattr(module, "urandom", lambda _: nonce)
    cipher = create_payload_cipher(b"K" * 32)
    data = '{"value":"한글","amount":"0.0100"}'.encode()
    encrypted = encrypt_payload(cipher, "payload", data)
    assert encrypted == nonce + cipher.encrypt(nonce, data, b"payload")
    assert decrypt_payload(cipher, "payload", encrypted) == data


@pytest.mark.parametrize("fault", ["identity", "key", "tag"])
def test_authentication_failure_retains_original_invalid_tag(fault):
    cipher = create_payload_cipher(b"K" * 32)
    encrypted = encrypt_payload(cipher, "payload", b"fixed")
    identity = "payload"
    if fault == "identity":
        identity = "other"
    elif fault == "key":
        cipher = create_payload_cipher(b"X" * 32)
    else:
        encrypted = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    with pytest.raises(InvalidTag):
        decrypt_payload(cipher, identity, encrypted)


def test_persisted_request_excludes_locators_and_preserves_semantic_fields():
    request = _request()
    original = copy.deepcopy(request)
    stable = stable_request(request)
    grants = grants_from_request(request)
    for role in ("ledgerRevisionContent", "reconciliationManifest"):
        assert set(stable[role]) == set(request[role]) - {"accessUri", "expiresAt"}
        assert grants[role] == {key: request[role][key] for key in ("accessUri", "expiresAt")}
    assert stable["inputIdentity"] == request["inputIdentity"]
    assert request == original


@pytest.mark.parametrize("seconds", [-1, 0, 1, 300, 301])
def test_expiry_uses_supplied_database_time_and_five_minute_boundary(seconds):
    now = datetime(2025, 1, 1, tzinfo=UTC)
    grants = {
        role: {"expiresAt": (now + timedelta(seconds=seconds)).isoformat()}
        for role in ("ledgerRevisionContent", "reconciliationManifest")
    }
    if seconds <= 0 or seconds > 300:
        with pytest.raises(AnalyticsJobConflict, match="INPUT_REFERENCE_EXPIRED"):
            expiry(grants, now)
    else:
        assert expiry(grants, now) == now + timedelta(seconds=seconds)


def test_expiry_selects_earlier_role_without_current_wallclock():
    now = datetime(2025, 1, 1, tzinfo=UTC)
    grants = {
        "ledgerRevisionContent": {"expiresAt": (now + timedelta(seconds=180)).isoformat()},
        "reconciliationManifest": {"expiresAt": (now + timedelta(seconds=60)).isoformat()},
    }
    assert expiry(grants, now) == now + timedelta(seconds=60)


@pytest.mark.parametrize(
    "key,capacity,maximum", [(b"short", 20, 100), (b"K" * 32, 0, 100), (b"K" * 32, 20, 0)]
)
def test_repository_keeps_configuration_validation(key, capacity, maximum):
    with pytest.raises(ValueError, match="Analytics job store configuration is invalid"):
        PostgresAnalyticsJobRepository(
            "unused", key, active_job_limit=capacity, max_payload_bytes=maximum
        )
