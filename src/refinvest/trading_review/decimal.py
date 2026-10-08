"""Exact decimal values that retain their source scale."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from refinvest.trading_review.errors import FailureCode, FailureStage, fail

DECIMAL_PATTERN = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$", re.ASCII)


@dataclass(frozen=True, slots=True)
class ScaledDecimal:
    value: Decimal
    source_lexeme: str
    scale: int

    def __post_init__(self) -> None:
        if not self.value.is_finite():
            raise ValueError("ScaledDecimal must be finite")
        if self.scale < 0:
            raise ValueError("ScaledDecimal scale must be non-negative")
        if not DECIMAL_PATTERN.fullmatch(self.source_lexeme):
            raise ValueError("ScaledDecimal lexeme is not plain decimal")
        if Decimal(self.source_lexeme) != self.value:
            raise ValueError("ScaledDecimal lexeme and value differ")
        observed_scale = len(self.source_lexeme.partition(".")[2])
        if observed_scale != self.scale:
            raise ValueError("ScaledDecimal scale and lexeme differ")

    @classmethod
    def parse(
        cls,
        lexeme: str,
        *,
        row_number: int | None = None,
        artifact_role: str | None = None,
        field: str | None = None,
    ) -> ScaledDecimal:
        if not DECIMAL_PATTERN.fullmatch(lexeme):
            raise fail(
                FailureCode.FILE_FORMAT_INVALID,
                FailureStage.PARSING,
                field=field,
                reason="INVALID_DECIMAL",
                row_number=row_number,
                artifact_role=artifact_role,
            )
        try:
            value = Decimal(lexeme)
        except InvalidOperation as exc:  # defensive; regex already excludes invalid forms
            raise fail(
                FailureCode.FILE_FORMAT_INVALID,
                FailureStage.PARSING,
                field=field,
                reason="INVALID_DECIMAL",
                row_number=row_number,
                artifact_role=artifact_role,
            ) from exc
        return cls(value=value, source_lexeme=lexeme, scale=len(lexeme.partition(".")[2]))

    @classmethod
    def from_decimal(cls, value: Decimal, *, scale: int | None = None) -> ScaledDecimal:
        if not value.is_finite():
            raise ValueError("derived decimal must be finite")
        if scale is None:
            lexeme = format(value, "f")
            scale = len(lexeme.partition(".")[2])
            return cls(value=value, source_lexeme=lexeme, scale=scale)
        quantum = Decimal(1).scaleb(-scale)
        quantized = value.quantize(quantum)
        if quantized != value:
            raise ValueError("value cannot be represented at requested scale")
        return cls(value=value, source_lexeme=f"{value:.{scale}f}", scale=scale)

    @classmethod
    def zero(cls, scale: int = 0) -> ScaledDecimal:
        return cls.from_decimal(Decimal(0), scale=scale)

    def canonical_node(self) -> dict[str, int | str]:
        unscaled = self.value.scaleb(self.scale)
        if unscaled != unscaled.to_integral_value():
            raise ValueError("decimal is not representable by its scale")
        return {"unscaled": str(int(unscaled)), "scale": self.scale}


@dataclass(frozen=True, slots=True)
class ExactRational:
    numerator: str
    denominator: str

    def __post_init__(self) -> None:
        if int(self.denominator) <= 0:
            raise ValueError("rational denominator must be positive")

    @classmethod
    def divide(cls, numerator: Decimal, denominator: Decimal) -> ExactRational:
        if denominator == 0:
            raise ZeroDivisionError("rational denominator must be non-zero")
        fraction = Fraction(numerator) / Fraction(denominator)
        return cls(str(fraction.numerator), str(fraction.denominator))

    @property
    def value(self) -> Decimal:
        return Decimal(self.numerator) / Decimal(self.denominator)


def sum_scaled(values: tuple[ScaledDecimal, ...]) -> ScaledDecimal:
    if not values:
        return ScaledDecimal.zero()
    scale = max(value.scale for value in values)
    return ScaledDecimal.from_decimal(
        sum((value.value for value in values), Decimal(0)), scale=scale
    )


def ulp(scale: int) -> Decimal:
    return Decimal(1).scaleb(-scale)


def source_bound(left: ScaledDecimal, right: ScaledDecimal) -> Decimal:
    return ulp(left.scale) / 2 + ulp(right.scale) / 2
