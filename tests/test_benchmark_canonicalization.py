from copy import deepcopy

import pytest

from refinvest.benchmarking.canonical import (
    CanonicalizationError,
    canonical_result_bytes,
    canonical_result_hash,
    is_result_hash,
)


def test_key_order_whitespace_and_top_level_run_id_do_not_change_hash() -> None:
    first = _payload("run-1")
    second = dict(reversed(list(_payload("run-2").items())))

    assert canonical_result_bytes(first) == canonical_result_bytes(second)
    assert canonical_result_hash(first) == canonical_result_hash(second)


def test_nested_run_id_is_not_removed_but_arrays_remain_ordered() -> None:
    first = _payload("run-1")
    first["metrics"]["runId"] = "nested-1"  # type: ignore[index]
    changed_nested = deepcopy(first)
    changed_nested["metrics"]["runId"] = "nested-2"  # type: ignore[index]
    reversed_trades = deepcopy(first)
    reversed_trades["trades"].reverse()  # type: ignore[union-attr]

    assert canonical_result_hash(first) != canonical_result_hash(changed_nested)
    assert canonical_result_hash(first) != canonical_result_hash(reversed_trades)


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        (("metrics", "tradeCount"), 3),
        (("benchmark", "primary", "equityCurve"), [{"date": "2025-01-01", "value": "2"}]),
        (("dataIntegrityStatus", "datasetSnapshotId"), "snapshot-2"),
        (("dataIntegrityStatus", "pointInTimeValidationPassed"), False),
    ),
)
def test_calculation_bearing_changes_change_hash(
    path: tuple[str, ...], replacement: object
) -> None:
    original = _payload("run-1")
    changed = deepcopy(original)
    target = changed
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment,index]
    target[path[-1]] = replacement  # type: ignore[index]

    assert canonical_result_hash(original) != canonical_result_hash(changed)


def test_decimal_text_null_and_lowercase_sha256_are_stable() -> None:
    payload = _payload("run-1")
    content = canonical_result_bytes(payload)
    digest = canonical_result_hash(payload)

    assert b'"totalReturn":"0.10000000000000000001"' in content
    assert b'"sharpe":null' in content
    assert is_result_hash(digest)
    assert digest == digest.lower()


def test_unsupported_canonicalization_version_is_rejected() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_result_bytes(_payload("run-1"), version=2)


def _payload(run_id: str) -> dict[str, object]:
    return {
        "backtestRunId": run_id,
        "metrics": {"totalReturn": "0.10000000000000000001", "sharpe": None, "tradeCount": 2},
        "equityCurve": [{"date": "2025-01-01", "value": "1"}],
        "trades": [{"id": 1}, {"id": 2}],
        "benchmark": {
            "primary": {"equityCurve": [{"date": "2025-01-01", "value": "1"}]},
            "secondaryReference": None,
        },
        "signalExecutionMarketRelation": "SAME_MARKET",
        "signalExecutionDelay": {"median": "0", "max": "0", "distribution": []},
        "sampleSizeWarning": "LOW",
        "dataIntegrityStatus": {
            "datasetSnapshotId": "snapshot-1",
            "corporateActionsApplied": True,
            "pointInTimeValidationPassed": True,
        },
    }
