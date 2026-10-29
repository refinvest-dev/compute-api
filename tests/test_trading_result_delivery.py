"""V1 transport policy validation and exact byte boundaries."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from refinvest.api.schemas.trading_review_jobs import CreateTradingNormalizationJobTransport
from refinvest.application.trading_review_jobs import normalization_request_fingerprint
from refinvest.contracts.normalization.result_delivery import effective_payload_limit
from tests.test_trading_normalization_input_identity import WIRE


def test_policy_is_required_and_pinned_outside_logical_identity() -> None:
    request = CreateTradingNormalizationJobTransport.model_validate(WIRE)
    assert request.result_delivery_policy.max_accepted_payload_bytes > 0
    without = copy.deepcopy(WIRE)
    del without["resultDeliveryPolicy"]
    with pytest.raises(ValidationError):
        CreateTradingNormalizationJobTransport.model_validate(without)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("policyVersion", "RESULT_DELIVERY_V2"),
        ("mode", "PAGED"),
        ("maxAcceptedPayloadBytes", 0),
        ("maxAcceptedPayloadBytes", -1),
        ("maxAcceptedPayloadBytes", "100"),
        ("maxAcceptedPayloadBytes", 2**63),
        ("acceptedContentEncodings", ["gzip"]),
        ("acceptedContentEncodings", ["identity", "gzip"]),
        ("mediaType", "application/json"),
    ],
)
def test_unsupported_policy_rejected(field: str, value: object) -> None:
    body = copy.deepcopy(WIRE)
    body["resultDeliveryPolicy"][field] = value
    with pytest.raises(ValidationError):
        CreateTradingNormalizationJobTransport.model_validate(body)


def test_policy_changes_request_fingerprint() -> None:
    changed = copy.deepcopy(WIRE)
    changed["resultDeliveryPolicy"]["maxAcceptedPayloadBytes"] += 1
    assert normalization_request_fingerprint(WIRE) != normalization_request_fingerprint(changed)


@pytest.mark.parametrize(
    ("advertised", "configured", "limit", "limited_by"),
    [(5, 8, 5, "CORE"), (8, 5, 5, "COMPUTE"), (5, 5, 5, "CORE")],
)
def test_effective_limit_is_minimum(
    advertised: int, configured: int, limit: int, limited_by: str
) -> None:
    policy = {**WIRE["resultDeliveryPolicy"], "maxAcceptedPayloadBytes": advertised}
    assert effective_payload_limit(policy, configured) == (limit, limited_by)
    entity = "한".encode() + b"ab"
    assert len(entity) == limit
    assert len(entity[:-1]) < limit
    assert len(entity + b"x") > limit


def test_invalid_deployment_limit_rejected() -> None:
    with pytest.raises(ValueError):
        effective_payload_limit(WIRE["resultDeliveryPolicy"], 0)
