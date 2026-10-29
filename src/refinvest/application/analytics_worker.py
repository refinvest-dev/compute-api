"""Analytics lease renewal and calculation/publication coordination through ports."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import cast

from refinvest.application.analytics_input import AnalyticsInputFailure, adapt_inputs
from refinvest.application.analytics_jobs import AnalyticsJobConflict, AnalyticsWorkerRepository
from refinvest.application.analytics_result import (
    bounded_analytics_json,
    build_analytics_payload,
    build_sections,
)
from refinvest.contracts.analytics.identity import (
    AnalyticsContractError,
    require_supported_version_set,
)
from refinvest.contracts.analytics.result_integrity import finish_sections
from refinvest.trading_review.analytics import calculate_behavior_metrics
from refinvest.trading_review.model import CanonicalRecord, FuturesPositionEpisode

_LOG = logging.getLogger("refinvest.app.trading_analytics_worker")
AnalyticsInputDownloader = Callable[
    [dict[str, object], dict[str, object], tuple[str, ...], Callable[[], bool], int],
    tuple[dict[str, object], dict[str, object]],
]


@dataclass(frozen=True, slots=True)
class AnalyticsWorkerSettings:
    trading_review_artifact_hosts: tuple[str, ...]
    trading_analytics_implementation_digest: str | None
    trading_analytics_max_input_bytes: int


class TradingAnalyticsWorkerHost:
    def __init__(
        self,
        repository: AnalyticsWorkerRepository,
        settings: AnalyticsWorkerSettings,
        *,
        worker_id: str,
        download_inputs: AnalyticsInputDownloader,
    ) -> None:
        if not settings.trading_review_artifact_hosts:
            raise ValueError("REFINVEST_TRADING_REVIEW_ARTIFACT_HOSTS is required.")
        if settings.trading_analytics_implementation_digest is None:
            raise ValueError("REFINVEST_TRADING_ANALYTICS_IMPLEMENTATION_DIGEST is required.")
        self.repository = repository
        self.settings = settings
        self.worker_id = worker_id
        self.download_inputs = download_inputs

    def run_once(self, shutdown: Event | None = None) -> bool:
        if shutdown is not None and shutdown.is_set():
            return False
        claim = self.repository.claim_next(self.worker_id, timedelta(seconds=60))
        if claim is None:
            return False
        job_id = cast(str, claim["jobId"])
        finished = Event()
        lost = Event()

        def heartbeat() -> None:
            while not finished.wait(20):
                try:
                    if not self.repository.heartbeat(claim, timedelta(seconds=60)):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    return

        thread = Thread(target=heartbeat, name="analytics-lease-heartbeat", daemon=True)
        thread.start()

        def should_stop() -> bool:
            return lost.is_set() or (shutdown is not None and shutdown.is_set())

        try:
            request = cast(dict[str, object], claim["request"])
            require_supported_version_set(
                request["targetVersionSet"],
                cast(str, self.settings.trading_analytics_implementation_digest),
            )
            ledger, manifest = self.download_inputs(
                request,
                cast(dict[str, object], claim["grants"]),
                self.settings.trading_review_artifact_hosts,
                should_stop,
                self.settings.trading_analytics_max_input_bytes,
            )
            if should_stop():
                return True
            try:
                episodes, records, instruments, config = adapt_inputs(request, ledger, manifest)
            except AnalyticsInputFailure:
                raise
            except (KeyError, TypeError, ValueError) as error:
                raise AnalyticsInputFailure("INPUT_SCHEMA_UNSUPPORTED") from error
            metrics = calculate_behavior_metrics(
                episodes=cast(tuple[FuturesPositionEpisode, ...], episodes),
                canonical_records=cast(tuple[CanonicalRecord, ...], records),
                instruments=instruments,
                analysis_config=config,
            )
            if should_stop():
                return True
            try:
                sections = build_sections(metrics, ledger, manifest)
            except AnalyticsContractError:
                raise
            except (KeyError, TypeError, ValueError) as error:
                raise AnalyticsContractError("RESULT_INTEGRITY_FAILURE") from error
            digests = finish_sections(
                sections,
                input_hash=cast(str, request["expectedInputHash"]),
                config_hash=cast(str, request["analysisConfigHash"]),
                version_set=cast(dict[str, object], request["targetVersionSet"]),
            )
            payload = build_analytics_payload(
                claim, request, sections, digests, terminal_at=datetime.now(UTC)
            )
            policy = cast(dict[str, object], request["resultDeliveryPolicy"])
            maximum = min(
                int(cast(int, policy["maxPayloadBytes"])), self.repository.max_payload_bytes
            )
            body = bounded_analytics_json(payload, maximum)
            if not should_stop():
                self.repository.publish_completed(claim, payload, body)
        except AnalyticsInputFailure as error:
            if error.code != "LEASE_LOST" and not should_stop():
                self.repository.fail_attempt(claim, error.code, retryable=error.retryable)
        except AnalyticsContractError as error:
            if not should_stop():
                self.repository.fail_attempt(claim, error.code, retryable=False)
        except AnalyticsJobConflict as error:
            if str(error) == "RESULT_PAYLOAD_TOO_LARGE" and not should_stop():
                self.repository.fail_attempt(claim, str(error), retryable=False)
        except ValueError:
            if not should_stop():
                self.repository.fail_attempt(claim, "ENGINE_INVARIANT_VIOLATION", retryable=False)
        except Exception:
            _LOG.warning("trading_analytics_attempt_failed job_id=%s", job_id)
            if not should_stop():
                self.repository.fail_attempt(claim, "TEMPORARY_DEPENDENCY_FAILURE", retryable=True)
        finally:
            finished.set()
            thread.join(timeout=1)
        return True

    def run_forever(self, shutdown: Event, poll_interval: timedelta) -> None:
        while not shutdown.is_set():
            try:
                worked = self.run_once(shutdown)
            except Exception:
                _LOG.warning("trading_analytics_worker_poll_failed")
                worked = False
            if not worked:
                shutdown.wait(poll_interval.total_seconds())
