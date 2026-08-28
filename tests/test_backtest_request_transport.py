from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from refinvest.api.schemas.backtests import CreateBacktestRequestTransport, MetricReferenceTransport
from refinvest.api.schemas.errors import map_transport_validation_error


def test_request_transport_maps_to_immutable_domain_values() -> None:
    request = CreateBacktestRequestTransport.model_validate(
        {
            "strategyVersionId": "strategy-v1",
            "strategyVersion": {
                "primarySignalAsset": "QQQ",
                "conditions": [
                    {
                        "operator": "GT",
                        "operandA": {"asset": "QQQ", "metric": "RETURN", "window": 5},
                        "operandB": 0.01,
                    }
                ],
                "executionAsset": "TQQQ",
                "lag": 1,
                "exit": {"holdingSignalSessions": 5},
                "positionPolicy": {
                    "longOnly": True,
                    "singlePosition": True,
                    "duplicateEntry": "IGNORE",
                },
            },
            "feeModel": {"commission": 0.001, "slippage": 0.002},
            "period": {"start": "2024-01-01", "end": "2024-12-31"},
            "datasetSnapshotId": "snapshot-1",
        }
    ).to_domain()

    assert request.strategy_version_id == "strategy-v1"
    assert request.strategy_version.execution_asset.value == "TQQQ"
    assert request.strategy_version.conditions[0].operand_a.window == 5
    assert request.strategy_version.conditions[0].operand_b == Decimal("0.01")
    assert request.fee_model.commission == Decimal("0.001")
    assert request.fee_model.slippage == Decimal("0.002")
    assert request.period.start == date(2024, 1, 1)
    assert request.dataset_snapshot_id == "snapshot-1"


def test_transport_validation_maps_to_the_openapi_error_shape() -> None:
    with pytest.raises(ValidationError) as raised:
        CreateBacktestRequestTransport.model_validate(
            {
                "strategyVersion": {
                    "primarySignalAsset": "QQQ",
                    "conditions": [],
                    "executionAsset": "TQQQ",
                    "lag": 0,
                    "exit": {"holdingSignalSessions": 1},
                },
                "feeModel": {"commission": 0.0, "slippage": 0.0},
                "period": {"start": "2024-01-01", "end": "2024-12-31"},
            }
        )

    response = map_transport_validation_error(raised.value)

    assert response.model_dump(by_alias=True) == {
        "message": "Backtest request validation failed.",
        "errorCode": "DSL_INVALID",
        "details": [
            "strategyVersion.conditions: List should have at least 1 item after validation, not 0"
        ],
    }


def test_transport_rejects_vix_as_an_execution_asset() -> None:
    with pytest.raises(ValidationError) as raised:
        CreateBacktestRequestTransport.model_validate(
            {
                "strategyVersion": {
                    "primarySignalAsset": "QQQ",
                    "conditions": [
                        {
                            "operator": "GT",
                            "operandA": {"asset": "QQQ", "metric": "SIMPLE"},
                            "operandB": 0.0,
                        }
                    ],
                    "executionAsset": "VIX",
                    "lag": 0,
                    "exit": {"holdingSignalSessions": 1},
                },
                "feeModel": {"commission": 0.0, "slippage": 0.0},
                "period": {"start": "2024-01-01", "end": "2024-12-31"},
            }
        )

    assert raised.value.errors()[0]["loc"] == ("strategyVersion", "executionAsset")


def test_transport_rejects_non_positive_metric_windows() -> None:
    with pytest.raises(ValidationError) as raised:
        MetricReferenceTransport.model_validate({"asset": "QQQ", "metric": "RETURN", "window": 0})

    assert raised.value.errors()[0]["loc"] == ("window",)
