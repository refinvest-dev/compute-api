"""Exact executable version registry for the Trading Review V1 engine."""

from __future__ import annotations

from dataclasses import replace

from refinvest.trading_review.errors import FailureCode, FailureStage, fail
from refinvest.trading_review.hashing import content_hash, sha256_hex
from refinvest.trading_review.model import NormalizationVersionSet, SourceArtifactRole

TRADE_DIALECT_V1 = "BINANCE_USDS_TRADE_HISTORY_V1"
POSITION_DIALECT_V1 = "BINANCE_USDS_POSITION_HISTORY_V1"
RECONSTRUCTION_POLICY_V1 = "BINANCE_USDS_LINEAR_PERPETUAL_ONE_WAY_V1"
RECONCILIATION_V1 = "BINANCE_USDS_LINEAR_PERPETUAL_ONE_WAY_V1"
ADAPTER_V1 = "BINANCE_USDS_CSV_ADAPTER_V1"
NORMALIZER_V1 = "BINANCE_USDS_TRADING_NORMALIZER_V1"
CANONICAL_SCHEMA_V1 = "CANONICAL_TRADING_RECORD_V1"
REVIEW_UNIT_SCHEMA_V1 = "FUTURES_POSITION_EPISODE_V1"
EVIDENCE_SCHEMA_V1 = "TRADING_REVIEW_EVIDENCE_V1"
MANIFEST_SCHEMA_V1 = "TRADING_RECONCILIATION_MANIFEST_V1"

# An immutable implementation identity for this semantic implementation. Deployments may replace
# the registry entry with an immutable build/image digest, but never with a mutable tag.
IMPLEMENTATION_DIGEST_V1 = "sha256:" + sha256_hex(
    b"refinvest-trading-review-normalization-reconstruction-v1"
)


def _without_hash(version_set: NormalizationVersionSet) -> dict[str, object]:
    return {
        "sourceDialectVersionsByArtifactRole": {
            role.value: version
            for role, version in version_set.source_dialect_versions_by_artifact_role
        },
        "adapterVersion": version_set.adapter_version,
        "normalizerVersion": version_set.normalizer_version,
        "canonicalSchemaVersion": version_set.canonical_schema_version,
        "reconstructionPolicyVersion": version_set.reconstruction_policy_version,
        "reconciliationVersion": version_set.reconciliation_version,
        "reviewUnitSchemaVersion": version_set.review_unit_schema_version,
        "evidenceSchemaVersion": version_set.evidence_schema_version,
        "implementationDigest": version_set.implementation_digest,
    }


def build_v1_version_set() -> NormalizationVersionSet:
    base = NormalizationVersionSet(
        source_dialect_versions_by_artifact_role=(
            (SourceArtifactRole.POSITION_HISTORY, POSITION_DIALECT_V1),
            (SourceArtifactRole.TRADE_HISTORY, TRADE_DIALECT_V1),
        ),
        adapter_version=ADAPTER_V1,
        normalizer_version=NORMALIZER_V1,
        canonical_schema_version=CANONICAL_SCHEMA_V1,
        reconstruction_policy_version=RECONSTRUCTION_POLICY_V1,
        reconciliation_version=RECONCILIATION_V1,
        review_unit_schema_version=REVIEW_UNIT_SCHEMA_V1,
        evidence_schema_version=EVIDENCE_SCHEMA_V1,
        implementation_digest=IMPLEMENTATION_DIGEST_V1,
        version_set_hash="",
    )
    return replace(base, version_set_hash=content_hash(_without_hash(base)))


SUPPORTED_VERSION_SET = build_v1_version_set()


def require_supported(version_set: NormalizationVersionSet) -> None:
    if version_set != SUPPORTED_VERSION_SET:
        raise fail(
            FailureCode.VERSION_INCOMPATIBLE,
            FailureStage.VALIDATION,
            field="targetVersionSet",
            reason="EXACT_VERSION_SET_REQUIRED",
        )
