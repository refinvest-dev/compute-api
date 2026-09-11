"""Safe, log-derived operational observations for terminal Backtest failures."""

import json
import logging

BACKTEST_RUNS_FAILED_TOTAL = "backtest_runs_failed_total"
UNSPECIFIED_ERROR_CODE = "UNSPECIFIED"
_FATAL_ERROR_CODES = frozenset(
    {
        "MISSING_REQUIRED_DATA",
        "DATASET_CORRUPTION",
        "CALENDAR_RESOLUTION_FAILED",
        "PRICE_DATA_MISSING",
        "EXECUTION_TIMEOUT",
        "DSL_INVALID",
    }
)

_LOGGER = logging.getLogger(__name__)


def record_terminal_backtest_failure(
    *,
    run_id: str,
    snapshot_id: str | None,
    error_code: str | None,
    worker_id: str | None = None,
    attempt: int | None = None,
    execution_duration_seconds: float | None = None,
) -> None:
    """Emit terminal failure signals after its durable state transition succeeds.

    Journal forwarding is this service's established metrics boundary.  The counter
    increment is intentionally a separate, low-cardinality JSON event so operators
    can derive ``backtest_runs_failed_total`` by ``error_code`` without an SDK or a
    new service dependency.
    """
    normalized_error_code = _metric_error_code(error_code)
    event_fields: dict[str, object] = {
        "event": "backtest_execution_failed",
        "run_status": "FAILED",
        "error_code": normalized_error_code,
        "run_id": run_id,
    }
    if snapshot_id is not None:
        event_fields["snapshot_id"] = snapshot_id
    if worker_id is not None:
        event_fields["worker_id"] = worker_id
    if attempt is not None:
        event_fields["attempt"] = attempt
    if execution_duration_seconds is not None:
        event_fields["execution_duration_seconds"] = execution_duration_seconds
    _LOGGER.warning(json.dumps(event_fields, sort_keys=True, default=str))
    _LOGGER.info(
        json.dumps(
            {
                "event": "metric_recorded",
                "metric_name": BACKTEST_RUNS_FAILED_TOTAL,
                "metric_type": "counter",
                "value": 1,
                "error_code": normalized_error_code,
            },
            sort_keys=True,
        )
    )


def _metric_error_code(error_code: str | None) -> str:
    """Keep the counter's only tag bounded to the public fatal-code vocabulary."""
    return error_code if error_code in _FATAL_ERROR_CODES else UNSPECIFIED_ERROR_CODE
