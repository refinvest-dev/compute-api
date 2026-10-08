"""Strict byte-level parser for the two supported Binance CSV dialects."""

from __future__ import annotations

import csv
from dataclasses import dataclass

from refinvest.trading_review.errors import FailureCode, FailureStage, fail
from refinvest.trading_review.hashing import content_hash, sha256_hex
from refinvest.trading_review.model import SourceArtifact, SourceArtifactRole, ValidationIssue
from refinvest.trading_review.versions import POSITION_DIALECT_V1, TRADE_DIALECT_V1

TRADE_COLUMNS = (
    "Uid",
    "Time",
    "Symbol",
    "Side",
    "Price",
    "Quantity",
    "Amount",
    "Fee",
    "Realized Profit",
    "Buyer",
    "Maker",
    "Trade ID",
    "Order ID",
)

POSITION_COLUMNS = (
    "Symbol",
    "Margin Mode",
    "Position Side",
    "Entry Price",
    "Avg. Close Price",
    "Max Open Interest",
    "Closed Vol.",
    "Closing PNL",
    "Opened",
    "Closed",
    "Status",
)


@dataclass(frozen=True, slots=True)
class ParsedRow:
    source_row_number: int
    values: tuple[tuple[str, str], ...]
    raw_row_hash: str

    def required(self, name: str) -> str:
        for key, value in self.values:
            if key == name:
                return value
        raise AssertionError(f"validated required field is absent: {name}")


@dataclass(frozen=True, slots=True)
class ParsedArtifact:
    artifact: SourceArtifact
    headers: tuple[str, ...]
    rows: tuple[ParsedRow, ...]
    schema_fingerprint: str
    warnings: tuple[ValidationIssue, ...]


def parse_artifact(artifact: SourceArtifact) -> ParsedArtifact:
    required, expected_dialect = _contract_for(artifact.role)
    if artifact.dialect != expected_dialect:
        raise fail(
            FailureCode.UNSUPPORTED_SOURCE_SCHEMA,
            FailureStage.VALIDATION,
            field="dialect",
            reason="DIALECT_NOT_REGISTERED",
            artifact_role=artifact.role.value,
        )
    text = _decode(artifact)
    lines = text.splitlines(keepends=True)
    if not lines and text == "":
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.PARSING,
            reason="EMPTY_ARTIFACT",
            artifact_role=artifact.role.value,
        )
    reader = csv.reader(iter(lines), delimiter=",", quotechar='"', strict=True)
    try:
        headers = tuple(next(reader))
    except (StopIteration, csv.Error) as exc:
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.PARSING,
            reason="INVALID_HEADER_RECORD",
            artifact_role=artifact.role.value,
        ) from exc
    _validate_headers(headers, required, artifact.role)
    header_line_count = reader.line_num
    indexes = {name: headers.index(name) for name in required}
    parsed_rows: list[ParsedRow] = []
    previous_line = header_line_count
    data_row_number = 0
    while True:
        try:
            values = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            raise fail(
                FailureCode.FILE_FORMAT_INVALID,
                FailureStage.PARSING,
                reason="MALFORMED_CSV_RECORD",
                row_number=data_row_number + 1,
                artifact_role=artifact.role.value,
            ) from exc
        data_row_number += 1
        current_line = reader.line_num
        if len(values) != len(headers):
            raise fail(
                FailureCode.FILE_FORMAT_INVALID,
                FailureStage.PARSING,
                reason="ROW_WIDTH_MISMATCH",
                row_number=data_row_number,
                artifact_role=artifact.role.value,
            )
        for name, index in indexes.items():
            if values[index] == "":
                raise fail(
                    FailureCode.FILE_FORMAT_INVALID,
                    FailureStage.PARSING,
                    field=name,
                    reason="EMPTY_REQUIRED_VALUE",
                    row_number=data_row_number,
                    artifact_role=artifact.role.value,
                )
        raw_record = "".join(lines[previous_line:current_line]).encode("utf-8")
        previous_line = current_line
        parsed_rows.append(
            ParsedRow(
                source_row_number=data_row_number,
                values=tuple(zip(headers, values, strict=True)),
                raw_row_hash=sha256_hex(raw_record),
            )
        )
    unknown = tuple(header for header in headers if header not in required)
    warnings = tuple(
        ValidationIssue(
            code="UNKNOWN_EXTRA_COLUMN",
            severity="WARNING",
            artifact_role=artifact.role,
            field=header,
        )
        for header in sorted(set(unknown))
    )
    return ParsedArtifact(
        artifact=artifact,
        headers=headers,
        rows=tuple(parsed_rows),
        schema_fingerprint=content_hash({"headers": headers, "dialect": artifact.dialect}),
        warnings=warnings,
    )


def _contract_for(role: SourceArtifactRole) -> tuple[tuple[str, ...], str]:
    if role is SourceArtifactRole.TRADE_HISTORY:
        return TRADE_COLUMNS, TRADE_DIALECT_V1
    return POSITION_COLUMNS, POSITION_DIALECT_V1


def _decode(artifact: SourceArtifact) -> str:
    try:
        text = artifact.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.PARSING,
            reason="INVALID_UTF8",
            artifact_role=artifact.role.value,
        ) from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    if "\ufeff" in text or "\r" in text.replace("\r\n", ""):
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.PARSING,
            reason="INVALID_TEXT_MARKER_OR_LINE_ENDING",
            artifact_role=artifact.role.value,
        )
    return text


def _validate_headers(
    headers: tuple[str, ...],
    required: tuple[str, ...],
    role: SourceArtifactRole,
) -> None:
    duplicate_required = any(headers.count(name) > 1 for name in required)
    if duplicate_required:
        raise fail(
            FailureCode.UNSUPPORTED_SOURCE_SCHEMA,
            FailureStage.PARSING,
            reason="DUPLICATE_REQUIRED_HEADER",
            artifact_role=role.value,
        )
    missing = tuple(name for name in required if name not in headers)
    if not missing:
        return
    folded_headers = {header.casefold() for header in headers}
    changed_case = any(name.casefold() in folded_headers for name in missing)
    code = (
        FailureCode.UNSUPPORTED_SOURCE_SCHEMA
        if changed_case
        else FailureCode.REQUIRED_COLUMN_MISSING
    )
    raise fail(
        code,
        FailureStage.PARSING,
        field=missing[0],
        reason="REQUIRED_HEADER_CASE_MISMATCH" if changed_case else "REQUIRED_HEADER_MISSING",
        artifact_role=role.value,
    )
