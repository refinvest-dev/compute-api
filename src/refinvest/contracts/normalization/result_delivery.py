"""Pinned V1 transport policy and exact entity-body limit."""

from __future__ import annotations

MEDIA_TYPE = "application/vnd.refinvest.trading-normalization+json"


def effective_payload_limit(policy: dict[str, object], configured_maximum: int) -> tuple[int, str]:
    """Return the byte limit and the party whose limit is effective."""
    advertised = policy.get("maxAcceptedPayloadBytes")
    if (
        policy.get("policyVersion") != "RESULT_DELIVERY_V1"
        or policy.get("mode") != "SINGLE_RESOURCE"
        or policy.get("acceptedContentEncodings") != ["identity"]
        or not isinstance(advertised, int)
        or isinstance(advertised, bool)
        or not 1 <= advertised <= 2**63 - 1
        or configured_maximum <= 0
    ):
        raise ValueError("Invalid pinned result delivery policy or deployment limit.")
    core_maximum = advertised
    return min(core_maximum, configured_maximum), (
        "CORE" if core_maximum <= configured_maximum else "COMPUTE"
    )
