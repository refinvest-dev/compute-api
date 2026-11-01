"""Opt-in measurement validation; requires a fresh ar08_pub_ integration schema."""

import json
import os

import pytest

from tools import analytics_publication_baseline as probe


@pytest.mark.skipif(
    not os.getenv("REFINVEST_PUBLICATION_TEST_DATABASE_URL"),
    reason="Owned probe schema URL absent.",
)
def test_real_publication_lock_visibility_roundtrip_and_ack(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "REFINVEST_TEST_DATABASE_URL", os.environ["REFINVEST_PUBLICATION_TEST_DATABASE_URL"]
    )
    directory = tmp_path / "prepared"
    probe.prepare(directory, 4)
    result = probe.measure(directory, lock_seconds=0.05)
    assert result["status"] == "PUBLICATION_VERIFIED"
    assert result["lock"]["wait_observed"]
    assert result["lock"]["precommit"] == "RUNNING"
    assert result["phases"]["publish_commit"]["seconds"] >= 0.05
    assert result["ciphertext_bytes"] == result["payload_bytes"] + 28
    assert set(result["phases"]) == {
        "publish_commit",
        "descriptor",
        "payload_read_decrypt",
        "ack_commit",
    }
    report = json.dumps(result)
    assert "postgresql://" not in report and "encryption_key" not in report
