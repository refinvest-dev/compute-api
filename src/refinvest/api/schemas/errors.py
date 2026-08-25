"""Contract-shaped validation error values, without HTTP framework dependencies."""

from typing import Literal

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from refinvest.engine.validation import StrategyValidationError


class ValidationErrorResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message: str
    error_code: Literal["DSL_INVALID"] = Field(alias="errorCode")
    details: list[str]


def map_strategy_validation_error(error: StrategyValidationError) -> ValidationErrorResponse:
    """Map pure DSL validation failures to the OpenAPI error representation."""
    return ValidationErrorResponse(
        message="StrategyVersion validation failed.",
        errorCode="DSL_INVALID",
        details=[f"{issue.path}: {issue.message}" for issue in error.issues],
    )


def map_transport_validation_error(
    error: ValidationError | RequestValidationError,
) -> ValidationErrorResponse:
    """Map transport shape errors to the same contract error representation."""
    return ValidationErrorResponse(
        message="Backtest request validation failed.",
        errorCode="DSL_INVALID",
        details=[
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        ],
    )
