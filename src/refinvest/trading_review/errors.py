"""Safe, structured failures for Trading Review normalization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureCode(StrEnum):
    FILE_FORMAT_INVALID = "FILE_FORMAT_INVALID"
    REQUIRED_COLUMN_MISSING = "REQUIRED_COLUMN_MISSING"
    UNSUPPORTED_SOURCE_SCHEMA = "UNSUPPORTED_SOURCE_SCHEMA"
    UNSUPPORTED_PRODUCT = "UNSUPPORTED_PRODUCT"
    UNSUPPORTED_POSITION_MODE = "UNSUPPORTED_POSITION_MODE"
    RECORD_CONFLICT = "RECORD_CONFLICT"
    VERSION_UNAVAILABLE = "VERSION_UNAVAILABLE"
    VERSION_INCOMPATIBLE = "VERSION_INCOMPATIBLE"
    CANONICAL_SCHEMA_INCOMPATIBLE = "CANONICAL_SCHEMA_INCOMPATIBLE"
    INSTRUMENT_TERMS_UNAVAILABLE = "INSTRUMENT_TERMS_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class FailureStage(StrEnum):
    VALIDATION = "VALIDATION"
    PARSING = "PARSING"
    CANONICALIZATION = "CANONICALIZATION"
    RECONSTRUCTION = "RECONSTRUCTION"
    RECONCILIATION = "RECONCILIATION"
    CONSERVATION = "CONSERVATION"


@dataclass(frozen=True, slots=True)
class FailureDetail:
    field: str | None = None
    reason: str | None = None
    row_number: int | None = None
    artifact_role: str | None = None


class TradingReviewError(Exception):
    """A failure whose message and details contain only allowlisted diagnostics."""

    def __init__(
        self,
        code: FailureCode,
        stage: FailureStage,
        *,
        detail: FailureDetail | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.stage = stage
        self.detail = detail
        self.retryable = retryable
        super().__init__(f"{stage.value}:{code.value}")


def fail(
    code: FailureCode,
    stage: FailureStage,
    *,
    field: str | None = None,
    reason: str | None = None,
    row_number: int | None = None,
    artifact_role: str | None = None,
) -> TradingReviewError:
    return TradingReviewError(
        code,
        stage,
        detail=FailureDetail(
            field=field,
            reason=reason,
            row_number=row_number,
            artifact_role=artifact_role,
        ),
    )
