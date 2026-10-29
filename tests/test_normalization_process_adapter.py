"""Real spawn/IPC and cancellation/deadline contracts for the moved adapter."""

import base64
import importlib
import multiprocessing
import pickle
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from refinvest.adapters.normalization.process_executor import ProcessIsolatedNormalizationExecutor
from refinvest.app.config import Settings
from refinvest.app.trading_review_worker import create_trading_review_worker
from refinvest.application.normalization_execution import (
    CompletedNormalization,
    FailedNormalization,
    NormalizationExecutionSettings,
)
from tests.test_trading_review_routes import _body
from tests.test_trading_review_worker_runtime import _claim


def _settings():
    return NormalizationExecutionSettings("test-tzdb", "i" * 32, "m" * 32, (), 1_000_000)


def _completed_child(settings, claim, sender):
    sender.send(
        CompletedNormalization(b"{}", "application/json", "a" * 64, "b" * 64, "c" * 64, "d" * 64)
    )
    sender.close()


def _empty_child(settings, claim, sender):
    sender.close()


def _hanging_child(settings, claim, sender):
    time.sleep(60)


def test_real_child_imports_new_modules_and_returns_validation_failure_without_network():
    payload = _body()
    payload["inputIdentity"]["hashSpecificationVersion"] = "FUTURE"
    claim = replace(_claim(), request_payload=payload)
    outcome = ProcessIsolatedNormalizationExecutor(_settings(), timedelta(seconds=20)).execute(
        claim, lambda: False
    )
    assert isinstance(outcome, FailedNormalization)
    assert outcome.failure.code == "INPUT_HASH_SPECIFICATION_UNSUPPORTED"
    assert not outcome.retryable


@pytest.mark.parametrize(
    "target,code", [(_completed_child, None), (_empty_child, "TEMPORARY_DEPENDENCY_FAILURE")]
)
def test_spawn_outcome_and_eof_keep_their_original_classification(monkeypatch, target, code):
    adapter = importlib.import_module("refinvest.adapters.normalization.process_executor")
    monkeypatch.setattr(adapter, "execute_normalization_child", target)
    outcome = ProcessIsolatedNormalizationExecutor(_settings(), timedelta(seconds=20)).execute(
        _claim(), lambda: False
    )
    if code is None:
        assert isinstance(outcome, CompletedNormalization) and outcome.payload == b"{}"
    else:
        assert isinstance(outcome, FailedNormalization) and outcome.failure.code == code
        assert outcome.retryable


@pytest.mark.parametrize(
    "cancel,code,stage",
    [(True, "JOB_CANCELLED", "CANCELLATION"), (False, "INTERNAL_ERROR", "TIMEOUT")],
)
def test_cancellation_and_hard_deadline_join_the_child(monkeypatch, cancel, code, stage):
    adapter = importlib.import_module("refinvest.adapters.normalization.process_executor")
    monkeypatch.setattr(adapter, "execute_normalization_child", _hanging_child)
    before = {child.pid for child in multiprocessing.active_children()}
    outcome = ProcessIsolatedNormalizationExecutor(_settings(), timedelta(seconds=0.2)).execute(
        _claim(), lambda: cancel
    )
    assert isinstance(outcome, FailedNormalization)
    assert outcome.failure.code == code and outcome.failure.stage == stage
    assert not outcome.retryable
    assert not ({child.pid for child in multiprocessing.active_children()} - before)


def test_composition_keeps_parent_credentials_out_of_spawn_configuration():
    settings = Settings(
        internal_api_key="parent-only-internal-key",
        database_url="postgresql://user:parent-only-db-secret@localhost/test",
        trading_review_active_job_limit=20,
        trading_review_timezone_database_version="test-tzdb",
        trading_review_identity_key="i" * 32,
        trading_review_masking_key="m" * 32,
        trading_review_artifact_hosts=("localhost",),
        trading_review_payload_encryption_key=base64.b64encode(b"P" * 32).decode(),
    )
    worker = create_trading_review_worker(settings)
    child_settings = worker.executor._settings
    assert isinstance(child_settings, NormalizationExecutionSettings)
    encoded = pickle.dumps(child_settings)
    assert pickle.loads(encoded) == child_settings
    assert b"parent-only" not in encoded
    assert settings.trading_review_payload_encryption_key.encode() not in encoded


def test_ipc_send_failure_is_safely_classified_once_and_sender_is_closed(monkeypatch):
    adapter = importlib.import_module("refinvest.adapters.normalization.process_executor")
    outcome = CompletedNormalization(
        b"{}", "application/json", "a" * 64, "b" * 64, "c" * 64, "d" * 64
    )
    monkeypatch.setattr(adapter, "execute_normalization", lambda *_: outcome)

    class Sender:
        calls = 0
        closed = False
        result = None

        def send(self, value):
            self.calls += 1
            if self.calls == 1:
                raise OSError("private transport detail")
            self.result = value

        def close(self):
            self.closed = True

    sender = Sender()
    adapter.execute_normalization_child(_settings(), _claim(), sender)
    assert sender.closed and sender.calls == 2
    assert isinstance(sender.result, FailedNormalization)
    assert sender.result.failure.code == "INTERNAL_ERROR" and sender.result.retryable
    assert "private transport" not in str(sender.result)
