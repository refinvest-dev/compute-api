"""Public application entrypoint for pure normalization and reconstruction."""

from __future__ import annotations

from dataclasses import replace

from refinvest.trading_review.canonical import canonicalize
from refinvest.trading_review.capability import aggregate_capability
from refinvest.trading_review.coverage import compute_coverages
from refinvest.trading_review.errors import FailureCode, FailureStage, fail
from refinvest.trading_review.hashing import content_hash
from refinvest.trading_review.invariants import verify_conservation
from refinvest.trading_review.model import (
    ExecutionRecord,
    InstrumentDescriptor,
    NormalizationRequest,
    NormalizationResult,
    ReconciliationManifest,
    SourceArtifact,
    SourceArtifactRole,
)
from refinvest.trading_review.parser import parse_artifact
from refinvest.trading_review.quality import build_quality_report
from refinvest.trading_review.reconciliation import (
    has_unsupported_overlap,
    reconcile_positions,
)
from refinvest.trading_review.reconstruction import reconstruct_positions
from refinvest.trading_review.versions import MANIFEST_SCHEMA_V1, require_supported


def normalize_and_reconstruct(request: NormalizationRequest) -> NormalizationResult:
    """Normalize one Trade/Position CSV pair into a deterministic reconciliation result."""
    require_supported(request.version_set)
    if request.declared_position_mode != "ONE_WAY":
        raise fail(
            FailureCode.UNSUPPORTED_POSITION_MODE,
            FailureStage.VALIDATION,
            field="declaredPositionMode",
            reason="ONE_WAY_REQUIRED",
        )
    if not request.source_timezone or not request.timezone_database_version:
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.VALIDATION,
            field="sourceTimezone",
            reason="PINNED_TIMEZONE_AND_DATABASE_VERSION_REQUIRED",
        )
    if not request.stable_identity_key or not request.identity_key_version:
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.VALIDATION,
            field="identityKeyVersion",
            reason="PINNED_IDENTITY_KEY_REQUIRED",
        )
    if any(
        valuation.rate.value <= 0
        or not valuation.source_asset
        or not valuation.settlement_asset
        or not valuation.valuation_version
        for valuation in request.fee_valuations
    ):
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.VALIDATION,
            field="feeValuations",
            reason="INVALID_PINNED_FEE_VALUATION",
        )
    artifacts = _artifacts_by_role(request.artifacts)
    parsed = {role: parse_artifact(artifact) for role, artifact in artifacts.items()}
    canonical = canonicalize(
        parsed[SourceArtifactRole.TRADE_HISTORY],
        parsed[SourceArtifactRole.POSITION_HISTORY],
        source_timezone=request.source_timezone,
        instruments=request.instruments,
        stable_identity_key=request.stable_identity_key,
    )
    if has_unsupported_overlap(canonical.summaries, canonical.executions):
        raise fail(
            FailureCode.UNSUPPORTED_POSITION_MODE,
            FailureStage.RECONCILIATION,
            reason="HEDGE_OVERLAP_DETECTED",
        )
    _reject_uniform_grid_violation(canonical.executions, request.instruments)
    coverages = compute_coverages(
        canonical.executions,
        canonical.summaries,
        trade_row_count=len(parsed[SourceArtifactRole.TRADE_HISTORY].rows),
        position_row_count=len(parsed[SourceArtifactRole.POSITION_HISTORY].rows),
        source_timezone=request.source_timezone,
    )
    reconstructed = reconstruct_positions(
        canonical.executions,
        canonical.fees,
        canonical.pnl_records,
        request.instruments,
        reconstruction_policy_version=request.version_set.reconstruction_policy_version,
        fee_valuations=request.fee_valuations,
    )
    reconciled = reconcile_positions(
        reconstructed,
        canonical.summaries,
        request.instruments,
        canonical.executions,
    )
    quantity_ok, fee_ok, pnl_ok = verify_conservation(
        canonical.executions,
        canonical.fees,
        canonical.pnl_records,
        reconciled.episodes,
        canonical.summaries,
    )
    artifact_hashes = tuple(
        (role, artifacts[role].content_hash)
        for role in sorted(artifacts, key=lambda item: item.value)
    )
    terms_hash = content_hash(
        tuple(sorted(request.instruments, key=lambda item: item.instrument_id))
    )
    input_hash = content_hash(
        {
            "artifacts": tuple(
                {
                    "role": role,
                    "dialect": artifacts[role].dialect,
                    "contentHash": artifacts[role].content_hash,
                }
                for role in sorted(artifacts, key=lambda item: item.value)
            ),
            "sourceTimezone": request.source_timezone,
            "timezoneDatabaseVersion": request.timezone_database_version,
            "declaredPositionMode": request.declared_position_mode,
            "instrumentTermsHash": terms_hash,
            "versionSetHash": request.version_set.version_set_hash,
            "identityKeyVersion": request.identity_key_version,
            "feeValuations": tuple(
                sorted(
                    request.fee_valuations,
                    key=lambda item: (item.source_asset, item.settlement_asset),
                )
            ),
        }
    )
    canonical_hash = content_hash(canonical.records)
    manifest_base = ReconciliationManifest(
        manifest_schema_version=MANIFEST_SCHEMA_V1,
        input_hash=input_hash,
        canonical_record_set_hash=canonical_hash,
        instrument_terms_hash=terms_hash,
        version_set_hash=request.version_set.version_set_hash,
        reconstruction_policy_version=request.version_set.reconstruction_policy_version,
        reconciliation_version=request.version_set.reconciliation_version,
        source_artifact_content_hashes=artifact_hashes,
        artifact_coverages=coverages,
        episodes=reconciled.episodes,
        matches=reconciled.matches,
        issues=reconciled.issues,
        quantity_conservation_passed=quantity_ok,
        fee_conservation_passed=fee_ok,
        pnl_conservation_passed=pnl_ok,
        manifest_hash="",
    )
    manifest = replace(manifest_base, manifest_hash=content_hash(manifest_base))
    capability = aggregate_capability(reconciled.episodes)
    quality = build_quality_report(
        trade_source_rows=len(parsed[SourceArtifactRole.TRADE_HISTORY].rows),
        position_source_rows=len(parsed[SourceArtifactRole.POSITION_HISTORY].rows),
        canonical_record_count=len(canonical.records),
        duplicate_count=canonical.duplicate_count,
        episodes=reconciled.episodes,
        issues=reconciled.issues,
        coverages=coverages,
        validation_issues=canonical.validation_issues,
    )
    result_base = NormalizationResult(
        input_hash=input_hash,
        version_set=request.version_set,
        instrument_terms_hash=terms_hash,
        artifact_coverages=coverages,
        canonical_records=canonical.records,
        episodes=reconciled.episodes,
        reconciliation_manifest=manifest,
        capability=capability,
        quality_report=quality,
        canonical_record_set_hash=canonical_hash,
        result_hash="",
    )
    return replace(result_base, result_hash=content_hash(result_base))


def _artifacts_by_role(
    artifacts: tuple[SourceArtifact, ...],
) -> dict[SourceArtifactRole, SourceArtifact]:
    by_role: dict[SourceArtifactRole, SourceArtifact] = {}
    for artifact in artifacts:
        if artifact.role in by_role:
            raise fail(
                FailureCode.UNSUPPORTED_SOURCE_SCHEMA,
                FailureStage.VALIDATION,
                reason="DUPLICATE_SOURCE_ARTIFACT",
                artifact_role=artifact.role.value,
            )
        by_role[artifact.role] = artifact
    required = {SourceArtifactRole.TRADE_HISTORY, SourceArtifactRole.POSITION_HISTORY}
    if set(by_role) != required:
        missing = sorted(required - set(by_role), key=lambda item: item.value)
        raise fail(
            FailureCode.REQUIRED_COLUMN_MISSING,
            FailureStage.VALIDATION,
            field="artifacts",
            reason="REQUIRED_ARTIFACT_ROLE_MISSING",
            artifact_role=missing[0].value if missing else None,
        )
    return by_role


def _reject_uniform_grid_violation(
    executions: tuple[ExecutionRecord, ...],
    instruments: tuple[InstrumentDescriptor, ...],
) -> None:
    terms = {item.instrument_id: item for item in instruments}
    for instrument_id in sorted({item.instrument_id for item in executions}):
        rows = tuple(item for item in executions if item.instrument_id == instrument_id)
        instrument = terms[instrument_id]
        if rows and all(
            item.quantity.value % instrument.quantity_step.value != 0
            or item.price.value % instrument.price_tick.value != 0
            for item in rows
        ):
            raise fail(
                FailureCode.UNSUPPORTED_PRODUCT,
                FailureStage.RECONSTRUCTION,
                reason="PINNED_INSTRUMENT_TERMS_CONTRADICT_ALL_EXECUTIONS",
            )
