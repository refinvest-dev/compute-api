from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from refinvest.trading_review import TradingReviewError, normalize_and_reconstruct
from refinvest.trading_review.hashing import canonical_json_bytes, content_hash
from refinvest.trading_review.versions import SUPPORTED_VERSION_SET
from tests.trading_review_support import position_row, request_for, simple_long_request, trade_row

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src/refinvest/trading_review"


def test_repeated_invocation_and_runtime_ids_have_stable_hashes() -> None:
    first = normalize_and_reconstruct(simple_long_request())
    second = normalize_and_reconstruct(simple_long_request())
    different_runtime_ids = normalize_and_reconstruct(
        simple_long_request(artifact_ids=("different-trade-id", "different-position-id"))
    )
    assert first.input_hash == second.input_hash == different_runtime_ids.input_hash
    assert first.canonical_record_set_hash == different_runtime_ids.canonical_record_set_hash
    assert first == second
    assert content_hash(first.reconciliation_manifest) == content_hash(
        different_runtime_ids.reconciliation_manifest
    )


def test_source_row_permutation_changes_artifact_lineage_not_economic_order() -> None:
    ordered = [
        trade_row("1", "BUY", "1", "10", time="2026-01-02 10:00:01"),
        trade_row("2", "BUY", "2", "13", time="2026-01-02 10:01:01"),
        trade_row("3", "SELL", "3", "14", "6", time="2026-01-02 10:05:01"),
    ]
    position = [position_row("Long", "12", "14", "3", "3", "6")]
    first = normalize_and_reconstruct(request_for(ordered, position))
    permuted = normalize_and_reconstruct(request_for(list(reversed(ordered)), position))
    assert [item.action for item in first.episodes[0].allocations] == [
        item.action for item in permuted.episodes[0].allocations
    ]
    assert first.episodes[0].average_entry_price == permuted.episodes[0].average_entry_price
    assert first.episodes[0].gross_realized_pnl == permuted.episodes[0].gross_realized_pnl
    # Physical bytes and source-row provenance are part of artifact lineage by contract.
    assert first.input_hash != permuted.input_hash
    assert content_hash(first.reconciliation_manifest) != content_hash(
        permuted.reconciliation_manifest
    )


def test_canonical_json_uses_unicode_code_point_key_order_and_no_float_decimal() -> None:
    payload = {"Å": 1, "A": 2, "Å": 3}
    encoded = canonical_json_bytes(payload)
    assert encoded == '{"A":2,"Å":3,"Å":1}'.encode()
    result = normalize_and_reconstruct(simple_long_request())
    canonical = canonical_json_bytes(result)
    assert b'"unscaled"' in canonical
    assert b'"source_lexeme"' not in canonical


def test_version_set_content_change_changes_hash_and_is_not_silently_accepted() -> None:
    changed = replace(
        SUPPORTED_VERSION_SET,
        normalizer_version="BINANCE_USDS_TRADING_NORMALIZER_V2",
        version_set_hash="changed",
    )
    assert content_hash(SUPPORTED_VERSION_SET) != content_hash(changed)
    with pytest.raises(TradingReviewError):
        normalize_and_reconstruct(replace(simple_long_request(), version_set=changed))


def test_production_trading_review_has_no_test_integration_or_backtest_type_dependency() -> None:
    forbidden = (
        "tests",
        "integration",
        "refinvest.engine.trades",
        "refinvest.engine.position",
    )
    for path in SOURCE_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            assert all(
                not any(name == item or name.startswith(f"{item}.") for item in forbidden)
                for name in imports
            ), f"forbidden dependency in {path.name}: {imports}"


def test_financial_calculation_source_contains_no_float_construction() -> None:
    for path in SOURCE_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            assert not isinstance(node, ast.Constant) or not isinstance(node.value, float), (
                f"binary float literal in {path.name}:{node.lineno}"
            )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "float", f"float conversion in {path.name}:{node.lineno}"
