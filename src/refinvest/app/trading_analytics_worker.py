"""Independent, lease-fenced Analytics worker for the three Behavior Metrics."""

from __future__ import annotations

import json
import logging
import signal
from datetime import UTC, datetime, timedelta
from os import getpid
from socket import gethostname
from threading import Event, Thread
from typing import cast

from refinvest.app.config import Settings, get_settings, trading_review_payload_key
from refinvest.app.trading_analytics_input import (
    AnalyticsInputFailure,
    adapt_inputs,
    download_inputs,
)
from refinvest.app.trading_analytics_result import build_sections
from refinvest.contracts.analytics.identity import (
    AnalyticsContractError,
    require_supported_version_set,
)
from refinvest.contracts.analytics.result_integrity import SECTION_FIELDS, finish_sections
from refinvest.persistence.postgres.trading_analytics_jobs import (
    AnalyticsJobConflict,
    PostgresAnalyticsJobRepository,
)
from refinvest.trading_review.analytics import calculate_behavior_metrics
from refinvest.trading_review.model import CanonicalRecord, FuturesPositionEpisode

_LOG = logging.getLogger(__name__)


def _bounded_json(payload: dict[str, object], maximum: int) -> bytes:
    """Measure each UTF-8 chunk before retaining it; never create an oversized result."""
    if maximum < 1:
        raise ValueError("A positive payload limit is required.")
    output = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for text in encoder.iterencode(payload):
        chunk = text.encode("utf-8")
        if len(output) + len(chunk) > maximum:
            raise AnalyticsContractError("RESULT_PAYLOAD_TOO_LARGE")
        output.extend(chunk)
    return bytes(output)


class TradingAnalyticsWorkerHost:
    def __init__(
        self,
        repository: PostgresAnalyticsJobRepository,
        settings: Settings,
        *,
        worker_id: str,
    ) -> None:
        if not settings.trading_review_artifact_hosts:
            raise ValueError("REFINVEST_TRADING_REVIEW_ARTIFACT_HOSTS is required.")
        if settings.trading_analytics_implementation_digest is None:
            raise ValueError("REFINVEST_TRADING_ANALYTICS_IMPLEMENTATION_DIGEST is required.")
        self.repository = repository
        self.settings = settings
        self.worker_id = worker_id

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
            ledger, manifest = download_inputs(
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
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            created = (
                cast(datetime, claim["createdAt"])
                .astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
            payload: dict[str, object] = {
                "jobId": job_id,
                "coreLogicalJobId": request["coreLogicalJobId"],
                "coreAnalysisRunId": request["coreAnalysisRunId"],
                "attemptToken": claim["attemptToken"],
                "deletionGeneration": claim["deletionGeneration"],
                "status": "COMPLETED",
                "targetVersionSet": request["targetVersionSet"],
                "analysisConfigHash": request["analysisConfigHash"],
                "inputHash": request["expectedInputHash"],
                "inputHashSpecificationVersion": request["inputHashSpecificationVersion"],
                "resultSchemaVersion": request["resultSchemaVersion"],
                "resultHashSpecificationVersion": request["resultHashSpecificationVersion"],
                **digests,
                **{field: sections[name] for name, (field, _, _) in SECTION_FIELDS.items()},
                "createdAt": created,
                "terminalAt": now,
            }
            policy = cast(dict[str, object], request["resultDeliveryPolicy"])
            maximum = min(
                int(cast(int, policy["maxPayloadBytes"])), self.repository.max_payload_bytes
            )
            body = _bounded_json(payload, maximum)
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


def create_trading_analytics_worker(settings: Settings) -> TradingAnalyticsWorkerHost:
    if settings.database_url is None or settings.trading_review_active_job_limit is None:
        raise ValueError("Analytics database and capacity settings are required.")
    repository = PostgresAnalyticsJobRepository(
        settings.database_url,
        trading_review_payload_key(settings),
        active_job_limit=settings.trading_review_active_job_limit,
        max_payload_bytes=settings.trading_review_max_payload_bytes,
    )
    return TradingAnalyticsWorkerHost(
        repository, settings, worker_id=settings.worker_id or f"{gethostname()}-{getpid()}"
    )


def trading_analytics_worker_main() -> None:
    shutdown = Event()

    def stop(_: int, __: object) -> None:
        shutdown.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    settings = get_settings()
    create_trading_analytics_worker(settings).run_forever(
        shutdown, timedelta(seconds=settings.worker_poll_seconds)
    )
