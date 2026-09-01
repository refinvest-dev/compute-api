from datetime import date
from decimal import Decimal

import pytest

from refinvest.application.backtests import (
    BacktestIdempotencyConflict,
    BacktestJob,
    BacktestJobStatusView,
    BacktestSubmission,
    submit_backtest,
)
from refinvest.engine.validation.models import (
    AssetSymbol,
    BacktestRequest,
    ComparisonOperator,
    Condition,
    FeeModel,
    MetricReference,
    MetricType,
    Period,
    StrategyVersion,
)


class FakeBacktestJobRepository:
    def __init__(self) -> None:
        self.jobs: list[BacktestJob] = []
        self.idempotency: dict[str, tuple[str, BacktestSubmission]] = {}

    def create(self, job: BacktestJob) -> None:
        self.jobs.append(job)

    def create_idempotently(
        self, job: BacktestJob, idempotency_key_hash: str, request_fingerprint: str
    ) -> BacktestSubmission:
        existing = self.idempotency.get(idempotency_key_hash)
        if existing is not None:
            fingerprint, submission = existing
            if fingerprint != request_fingerprint:
                raise BacktestIdempotencyConflict("different request")
            return submission
        submission = BacktestSubmission(job.run_id, job.strategy_version_id)
        self.idempotency[idempotency_key_hash] = (request_fingerprint, submission)
        self.create(job)
        return submission

    def get(self, run_id: str) -> BacktestJobStatusView | None:
        del run_id
        return None


def test_reuses_the_original_run_id_for_the_same_key_and_canonical_request() -> None:
    repository = FakeBacktestJobRepository()
    key = "d9428888-122b-4f8a-8a95-2c5b31eb3380"

    first = submit_backtest(_request("snapshot-1"), key, repository)
    replay = submit_backtest(_request("snapshot-1"), key, repository)

    assert replay == first
    assert len(repository.jobs) == 1


def test_rejects_the_same_key_when_the_canonical_request_changes() -> None:
    repository = FakeBacktestJobRepository()
    key = "d9428888-122b-4f8a-8a95-2c5b31eb3380"
    submit_backtest(_request("snapshot-1"), key, repository)

    with pytest.raises(BacktestIdempotencyConflict):
        submit_backtest(_request("snapshot-2"), key, repository)


def _request(snapshot_id: str) -> BacktestRequest:
    return BacktestRequest(
        strategy_version_id="strategy-v1",
        strategy_version=StrategyVersion(
            primary_signal_asset=AssetSymbol.QQQ,
            conditions=(
                Condition(
                    ComparisonOperator.GT,
                    None,
                    MetricReference(AssetSymbol.QQQ, MetricType.SIMPLE, None),
                    Decimal("0"),
                ),
            ),
            execution_asset=AssetSymbol.TQQQ,
            lag=0,
            holding_signal_sessions=1,
            position_policy=None,
        ),
        fee_model=FeeModel(Decimal("0"), Decimal("0")),
        period=Period(date(2025, 1, 1), date(2025, 1, 31)),
        dataset_snapshot_id=snapshot_id,
    )
