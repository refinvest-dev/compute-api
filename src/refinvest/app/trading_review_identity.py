"""ADR-065 V1 normalization input identity and RFC 8785 string-only canonicalization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from refinvest.trading_review.versions import SUPPORTED_VERSION_SET

HASH_SPECIFICATION_V1 = "TRADING_INPUT_SET_HASH_V1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
ROLES = frozenset({"TRADE_HISTORY", "POSITION_HISTORY"})


class IdentityModel(BaseModel):
    model_config = ConfigDict(populate_by_name=False, extra="forbid", frozen=True)


class ArtifactIdentity(IdentityModel):
    artifact_id: str = Field(alias="artifactId", min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)


class DeclaredProduct(IdentityModel):
    venue: Literal["BINANCE"]
    product_family: Literal["LINEAR_PERPETUAL"] = Field(alias="productFamily")
    settlement_asset: Literal["USDT"] = Field(alias="settlementAsset")


class NormalizerVersionSet(IdentityModel):
    source_dialect_versions_by_artifact_role: dict[
        Literal["TRADE_HISTORY", "POSITION_HISTORY"], str
    ] = Field(alias="sourceDialectVersionsByArtifactRole")
    adapter_version: str = Field(alias="adapterVersion", min_length=1, max_length=200)
    normalizer_version: str = Field(alias="normalizerVersion", min_length=1, max_length=200)
    canonical_schema_version: str = Field(
        alias="canonicalSchemaVersion", min_length=1, max_length=200
    )
    implementation_digest: str = Field(alias="implementationDigest", min_length=1)
    version_set_hash: str = Field(alias="versionSetHash", pattern=SHA256_PATTERN)

    @field_validator("source_dialect_versions_by_artifact_role")
    @classmethod
    def exact_roles(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != set(ROLES) or any(not item or len(item) > 200 for item in value.values()):
            raise ValueError("Both source dialect versions are required.")
        return value


class ReconstructionVersionSet(IdentityModel):
    instrument_terms_version_by_instrument: dict[str, str] = Field(
        alias="instrumentTermsVersionByInstrument"
    )
    reconstruction_policy_version: str = Field(
        alias="reconstructionPolicyVersion", min_length=1, max_length=200
    )
    reconciliation_version: str = Field(alias="reconciliationVersion", min_length=1, max_length=200)
    review_unit_schema_version: str = Field(
        alias="reviewUnitSchemaVersion", min_length=1, max_length=200
    )
    evidence_schema_version: str = Field(
        alias="evidenceSchemaVersion", min_length=1, max_length=200
    )
    implementation_digest: str = Field(alias="implementationDigest", min_length=1)
    version_set_hash: str = Field(alias="versionSetHash", pattern=SHA256_PATTERN)

    @field_validator("instrument_terms_version_by_instrument")
    @classmethod
    def nonempty_map(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key or not item or len(item) > 200 for key, item in value.items()):
            raise ValueError("Instrument terms map contains an empty entry.")
        return value


class TargetVersionSet(IdentityModel):
    normalizer: NormalizerVersionSet
    reconstruction: ReconstructionVersionSet
    version_set_hash: str = Field(alias="versionSetHash", pattern=SHA256_PATTERN)


class NormalizationInputIdentity(IdentityModel):
    hash_specification_version: str = Field(alias="hashSpecificationVersion", min_length=1)
    trading_book_id: str = Field(alias="tradingBookId", min_length=1)
    source_import_session_ids: tuple[str, ...] = Field(
        alias="sourceImportSessionIds", min_length=1, max_length=1
    )
    artifacts_by_role: dict[Literal["TRADE_HISTORY", "POSITION_HISTORY"], ArtifactIdentity] = Field(
        alias="artifactsByRole"
    )
    source_timezone: str = Field(alias="sourceTimezone", min_length=1)
    review_timezone: str = Field(alias="reviewTimezone", min_length=1)
    declared_product: DeclaredProduct = Field(alias="declaredProduct")
    declared_position_mode: Literal["ONE_WAY"] = Field(alias="declaredPositionMode")
    target_version_set: TargetVersionSet = Field(alias="targetVersionSet")

    @model_validator(mode="after")
    def validate_identity(self) -> NormalizationInputIdentity:
        if len(set(self.source_import_session_ids)) != len(self.source_import_session_ids) or any(
            not value for value in self.source_import_session_ids
        ):
            raise ValueError("Source Import Session IDs must be distinct and nonempty.")
        if set(self.artifacts_by_role) != set(ROLES):
            raise ValueError("Exactly one identity for each Artifact role is required.")
        if len({item.artifact_id for item in self.artifacts_by_role.values()}) != 2:
            raise ValueError("Artifact IDs must be distinct.")
        for name in (self.source_timezone, self.review_timezone):
            try:
                ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise ValueError("Timezone is unsupported.") from error
        return self


def _utf16_key(value: str) -> bytes:
    return value.encode("utf-16-be")


def _canonical_string_object(value: object) -> str:
    """JCS for the V1 identity schema, which contains only objects, arrays and strings."""
    if isinstance(value, str):
        value.encode("utf-8")  # Reject lone surrogates before serialization.
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, dict):
        raw_mapping = cast(Mapping[object, object], value)
        if any(not isinstance(key, str) for key in raw_mapping):
            raise TypeError("Identity object keys must be strings.")
        mapping = cast(Mapping[str, object], value)
        return (
            "{"
            + ",".join(
                _canonical_string_object(key) + ":" + _canonical_string_object(mapping[key])
                for key in sorted(mapping, key=_utf16_key)
            )
            + "}"
        )
    if isinstance(value, (tuple, list)):
        sequence = cast(Sequence[object], value)
        return "[" + ",".join(_canonical_string_object(item) for item in sequence) + "]"
    raise TypeError("V1 identity only supports strings, arrays and objects.")


def canonicalize_input_identity(identity: NormalizationInputIdentity) -> bytes:
    if identity.hash_specification_version != HASH_SPECIFICATION_V1:
        raise ValueError("Unsupported input hash specification.")
    node = identity.model_dump(mode="json", by_alias=True)
    node["sourceImportSessionIds"] = sorted(identity.source_import_session_ids, key=_utf16_key)
    return _canonical_string_object(node).encode("utf-8")


def calculate_input_set_hash(identity: NormalizationInputIdentity) -> str:
    return hashlib.sha256(canonicalize_input_identity(identity)).hexdigest()


def supports_target_version_set(
    target: TargetVersionSet, instrument_terms_versions: dict[str, str]
) -> bool:
    """Bridge the exact Core component VersionSet to this engine's flat registry."""
    normalizer = target.normalizer
    reconstruction = target.reconstruction
    supported = SUPPORTED_VERSION_SET
    return (
        normalizer.source_dialect_versions_by_artifact_role
        == {role.value: value for role, value in supported.source_dialect_versions_by_artifact_role}
        and normalizer.adapter_version == supported.adapter_version
        and normalizer.normalizer_version == supported.normalizer_version
        and normalizer.canonical_schema_version == supported.canonical_schema_version
        and normalizer.implementation_digest == supported.implementation_digest
        and reconstruction.reconstruction_policy_version == supported.reconstruction_policy_version
        and reconstruction.reconciliation_version == supported.reconciliation_version
        and reconstruction.review_unit_schema_version == supported.review_unit_schema_version
        and reconstruction.evidence_schema_version == supported.evidence_schema_version
        and reconstruction.implementation_digest == supported.implementation_digest
        and reconstruction.instrument_terms_version_by_instrument == instrument_terms_versions
    )
