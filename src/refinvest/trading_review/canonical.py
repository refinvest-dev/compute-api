"""Canonical record construction, deduplication, and identity conflict checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from refinvest.trading_review.decimal import ScaledDecimal
from refinvest.trading_review.errors import FailureCode, FailureStage, fail
from refinvest.trading_review.hashing import content_hash, stable_id
from refinvest.trading_review.model import (
    CanonicalRecord,
    Direction,
    ExecutionRecord,
    FeeKind,
    FeeRecord,
    InstrumentDescriptor,
    MarginMode,
    Side,
    SourceArtifactRole,
    SourceRowProvenance,
    ValidationIssue,
    VenuePositionSummaryRecord,
    VenueReportedPnlRecord,
)
from refinvest.trading_review.parser import ParsedArtifact, ParsedRow
from refinvest.trading_review.provenance import (
    account_fingerprint,
    position_source_identity,
    source_execution_key,
)

TRADE_ID_RE = re.compile(r"^[0-9]+$", re.ASCII)
FEE_RE = re.compile(
    r"^(-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)([A-Z][A-Z0-9]*)$",
    re.ASCII,
)
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$")


@dataclass(frozen=True, slots=True)
class CanonicalizationResult:
    executions: tuple[ExecutionRecord, ...]
    fees: tuple[FeeRecord, ...]
    pnl_records: tuple[VenueReportedPnlRecord, ...]
    summaries: tuple[VenuePositionSummaryRecord, ...]
    records: tuple[CanonicalRecord, ...]
    duplicate_count: int
    validation_issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True, slots=True)
class _TradeBundle:
    execution: ExecutionRecord
    fee: FeeRecord
    pnl: VenueReportedPnlRecord
    semantic_hash: str


def canonicalize(
    trade_artifact: ParsedArtifact,
    position_artifact: ParsedArtifact,
    *,
    source_timezone: str,
    instruments: tuple[InstrumentDescriptor, ...],
    stable_identity_key: bytes,
) -> CanonicalizationResult:
    timezone = parse_timezone(source_timezone)
    by_symbol = _instrument_registry(instruments)
    bundles: dict[str, _TradeBundle] = {}
    numeric_id_lexemes: dict[tuple[str, str, int], str] = {}
    observed_accounts: set[str] = set()
    duplicates = 0
    for row in trade_artifact.rows:
        bundle, fingerprint, trade_id = _trade_bundle(
            row,
            trade_artifact,
            timezone,
            by_symbol,
            stable_identity_key,
        )
        observed_accounts.add(fingerprint)
        if len(observed_accounts) > 1:
            raise fail(
                FailureCode.RECORD_CONFLICT,
                FailureStage.CANONICALIZATION,
                field="Uid",
                reason="MULTIPLE_ACCOUNT_IDENTITIES",
                row_number=row.source_row_number,
                artifact_role=trade_artifact.artifact.role.value,
            )
        numeric_key = (fingerprint, bundle.execution.instrument_id, int(trade_id))
        previous_lexeme = numeric_id_lexemes.get(numeric_key)
        if previous_lexeme is not None and previous_lexeme != trade_id:
            raise fail(
                FailureCode.RECORD_CONFLICT,
                FailureStage.CANONICALIZATION,
                field="Trade ID",
                reason="NUMERIC_EQUIVALENT_TRADE_ID",
                row_number=row.source_row_number,
                artifact_role=trade_artifact.artifact.role.value,
            )
        numeric_id_lexemes[numeric_key] = trade_id
        existing = bundles.get(bundle.execution.source_execution_key)
        if existing is None:
            bundles[bundle.execution.source_execution_key] = bundle
        elif existing.semantic_hash != bundle.semantic_hash:
            raise fail(
                FailureCode.RECORD_CONFLICT,
                FailureStage.CANONICALIZATION,
                reason="SOURCE_IDENTITY_CONTENT_CONFLICT",
                row_number=row.source_row_number,
                artifact_role=trade_artifact.artifact.role.value,
            )
        else:
            duplicates += 1
            bundles[bundle.execution.source_execution_key] = _merge_bundle(existing, bundle)

    executions = tuple(
        sorted(
            (bundle.execution for bundle in bundles.values()),
            key=lambda item: (item.instrument_id, item.occurred_at, item.canonical_trade_order),
        )
    )
    by_key = {bundle.execution.source_execution_key: bundle for bundle in bundles.values()}
    fees = tuple(by_key[item.source_execution_key].fee for item in executions)
    pnl_records = tuple(by_key[item.source_execution_key].pnl for item in executions)
    summaries = tuple(
        sorted(
            (
                _position_record(row, position_artifact, timezone, by_symbol)
                for row in position_artifact.rows
            ),
            key=lambda item: (
                item.instrument_id,
                item.opened_at,
                item.closed_at,
                item.direction.value,
                item.source_identity,
            ),
        )
    )
    records: list[CanonicalRecord] = []
    for execution, fee, pnl in zip(executions, fees, pnl_records, strict=True):
        records.extend((execution, fee, pnl))
    records.extend(summaries)
    return CanonicalizationResult(
        executions=executions,
        fees=fees,
        pnl_records=pnl_records,
        summaries=summaries,
        records=tuple(records),
        duplicate_count=duplicates,
        validation_issues=trade_artifact.warnings + position_artifact.warnings,
    )


def parse_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.VALIDATION,
            field="sourceTimezone",
            reason="INVALID_IANA_TIMEZONE",
        ) from exc


def parse_local_timestamp(
    lexeme: str,
    timezone: ZoneInfo,
    *,
    row: ParsedRow,
    role: SourceArtifactRole,
    field: str,
) -> datetime:
    if not TIMESTAMP_RE.fullmatch(lexeme):
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.PARSING,
            field=field,
            reason="TIMESTAMP_REQUIRES_SECOND_PRECISION",
            row_number=row.source_row_number,
            artifact_role=role.value,
        )
    try:
        naive = datetime.strptime(lexeme, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.PARSING,
            field=field,
            reason="INVALID_LOCAL_TIMESTAMP",
            row_number=row.source_row_number,
            artifact_role=role.value,
        ) from exc
    first = naive.replace(tzinfo=timezone, fold=0)
    second = naive.replace(tzinfo=timezone, fold=1)
    first_roundtrip = first.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)
    second_roundtrip = second.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)
    if (
        first_roundtrip != naive
        or second_roundtrip != naive
        or first.utcoffset() != second.utcoffset()
    ):
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.PARSING,
            field=field,
            reason="AMBIGUOUS_OR_NONEXISTENT_LOCAL_TIMESTAMP",
            row_number=row.source_row_number,
            artifact_role=role.value,
        )
    return first.astimezone(UTC)


def _trade_bundle(
    row: ParsedRow,
    parsed: ParsedArtifact,
    timezone: ZoneInfo,
    instruments: dict[str, InstrumentDescriptor],
    stable_identity_key: bytes,
) -> tuple[_TradeBundle, str, str]:
    role = parsed.artifact.role
    symbol = row.required("Symbol")
    instrument = _require_instrument(symbol, instruments, row, role)
    trade_id = row.required("Trade ID")
    if not TRADE_ID_RE.fullmatch(trade_id):
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.PARSING,
            field="Trade ID",
            reason="TRADE_ID_MUST_BE_ASCII_DIGITS",
            row_number=row.source_row_number,
            artifact_role=role.value,
        )
    side_lexeme = row.required("Side")
    if side_lexeme not in (Side.BUY.value, Side.SELL.value):
        raise _enum_failure(row, role, "Side")
    buyer = _parse_boolean(row.required("Buyer"), row, role, "Buyer")
    maker = _parse_boolean(row.required("Maker"), row, role, "Maker")
    price = _decimal(row, role, "Price")
    quantity = _decimal(row, role, "Quantity")
    amount = _decimal(row, role, "Amount")
    pnl = _decimal(row, role, "Realized Profit")
    if price.value <= 0 or quantity.value <= 0 or amount.value < 0:
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.PARSING,
            reason="INVALID_POSITIVE_TRADE_VALUE",
            row_number=row.source_row_number,
            artifact_role=role.value,
        )
    fee_match = FEE_RE.fullmatch(row.required("Fee"))
    if fee_match is None:
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.PARSING,
            field="Fee",
            reason="INVALID_FEE_AMOUNT_OR_ASSET",
            row_number=row.source_row_number,
            artifact_role=role.value,
        )
    fee_amount = ScaledDecimal.parse(
        fee_match.group(1),
        row_number=row.source_row_number,
        artifact_role=role.value,
        field="Fee",
    )
    fee_asset = fee_match.group(2)
    occurred_at = parse_local_timestamp(
        row.required("Time"), timezone, row=row, role=role, field="Time"
    )
    fingerprint = account_fingerprint(stable_identity_key, row.required("Uid"))
    execution_key = source_execution_key(fingerprint, symbol, trade_id)
    provenance = (_provenance(row, parsed),)
    semantic_payload = {
        "accountFingerprint": fingerprint,
        "instrumentId": instrument.instrument_id,
        "sourceSymbol": symbol,
        "occurredAt": occurred_at,
        "side": side_lexeme,
        "price": price,
        "quantity": quantity,
        "sourceAmount": amount,
        "fee": fee_amount,
        "feeAsset": fee_asset,
        "realizedPnl": pnl,
        "buyer": buyer,
        "maker": maker,
        "tradeIdDigest": content_hash(trade_id),
        "orderIdDigest": content_hash(row.required("Order ID")),
    }
    semantic_hash = content_hash(semantic_payload)
    execution_payload = {
        "recordType": "EXECUTION",
        "sourceExecutionKey": execution_key,
        **{
            key: value
            for key, value in semantic_payload.items()
            if key not in {"fee", "feeAsset", "realizedPnl"}
        },
        "executionOrigin": "UNKNOWN",
    }
    execution_hash = content_hash(execution_payload)
    execution = ExecutionRecord(
        record_id=stable_id("execution", execution_hash),
        record_hash=execution_hash,
        source_execution_key=execution_key,
        instrument_id=instrument.instrument_id,
        source_symbol=symbol,
        occurred_at=occurred_at,
        side=Side(side_lexeme),
        price=price,
        quantity=quantity,
        source_amount=amount,
        buyer=buyer,
        maker=maker,
        execution_origin="UNKNOWN",
        provenance=provenance,
        _trade_id_lexeme=trade_id,
        _trade_id_numeric=int(trade_id),
    )
    fee_kind = (
        FeeKind.COST
        if fee_amount.value > 0
        else FeeKind.REBATE
        if fee_amount.value < 0
        else FeeKind.ZERO
    )
    fee_payload = {
        "recordType": "FEE",
        "sourceExecutionKey": execution_key,
        "instrumentId": instrument.instrument_id,
        "occurredAt": occurred_at,
        "sourceAmount": fee_amount,
        "asset": fee_asset,
        "feeKind": fee_kind,
        "balanceEffect": ScaledDecimal.from_decimal(-fee_amount.value, scale=fee_amount.scale),
    }
    fee_hash = content_hash(fee_payload)
    fee = FeeRecord(
        record_id=stable_id("fee", fee_hash),
        record_hash=fee_hash,
        source_execution_key=execution_key,
        instrument_id=instrument.instrument_id,
        occurred_at=occurred_at,
        source_amount=fee_amount,
        asset=fee_asset,
        fee_kind=fee_kind,
        balance_effect=ScaledDecimal.from_decimal(-fee_amount.value, scale=fee_amount.scale),
        provenance=provenance,
    )
    pnl_payload = {
        "recordType": "VENUE_REPORTED_PNL",
        "sourceExecutionKey": execution_key,
        "instrumentId": instrument.instrument_id,
        "occurredAt": occurred_at,
        "realizedPnl": pnl,
        "settlementAsset": instrument.settlement_asset,
    }
    pnl_hash = content_hash(pnl_payload)
    pnl_record = VenueReportedPnlRecord(
        record_id=stable_id("pnl", pnl_hash),
        record_hash=pnl_hash,
        source_execution_key=execution_key,
        instrument_id=instrument.instrument_id,
        occurred_at=occurred_at,
        realized_pnl=pnl,
        settlement_asset=instrument.settlement_asset,
        provenance=provenance,
    )
    return _TradeBundle(execution, fee, pnl_record, semantic_hash), fingerprint, trade_id


def _position_record(
    row: ParsedRow,
    parsed: ParsedArtifact,
    timezone: ZoneInfo,
    instruments: dict[str, InstrumentDescriptor],
) -> VenuePositionSummaryRecord:
    role = parsed.artifact.role
    symbol = row.required("Symbol")
    instrument = _require_instrument(symbol, instruments, row, role)
    direction_values = {"Long": Direction.LONG, "Short": Direction.SHORT}
    margin_values = {"Isolated": MarginMode.ISOLATED, "Cross": MarginMode.CROSS}
    direction = direction_values.get(row.required("Position Side"))
    margin_mode = margin_values.get(row.required("Margin Mode"))
    if direction is None:
        raise fail(
            FailureCode.UNSUPPORTED_SOURCE_SCHEMA,
            FailureStage.PARSING,
            field="Position Side",
            reason="UNSUPPORTED_ENUM_VALUE",
            row_number=row.source_row_number,
            artifact_role=role.value,
        )
    if margin_mode is None:
        raise fail(
            FailureCode.UNSUPPORTED_SOURCE_SCHEMA,
            FailureStage.PARSING,
            field="Margin Mode",
            reason="UNSUPPORTED_ENUM_VALUE",
            row_number=row.source_row_number,
            artifact_role=role.value,
        )
    opened_at = parse_local_timestamp(
        row.required("Opened"), timezone, row=row, role=role, field="Opened"
    )
    closed_at = parse_local_timestamp(
        row.required("Closed"), timezone, row=row, role=role, field="Closed"
    )
    entry = _decimal(row, role, "Entry Price")
    close = _decimal(row, role, "Avg. Close Price")
    maximum = _decimal(row, role, "Max Open Interest")
    closed_volume = _decimal(row, role, "Closed Vol.")
    closing_pnl = _decimal(row, role, "Closing PNL")
    if any(value.value < 0 for value in (entry, close, maximum, closed_volume)):
        raise fail(
            FailureCode.FILE_FORMAT_INVALID,
            FailureStage.PARSING,
            reason="NEGATIVE_POSITION_VALUE",
            row_number=row.source_row_number,
            artifact_role=role.value,
        )
    identity = position_source_identity(parsed.artifact.content_hash, row.source_row_number)
    payload = {
        "recordType": "VENUE_POSITION_SUMMARY",
        "sourceIdentity": identity,
        "instrumentId": instrument.instrument_id,
        "sourceSymbol": symbol,
        "openedAt": opened_at,
        "closedAt": closed_at,
        "direction": direction,
        "marginMode": margin_mode,
        "entryPrice": entry,
        "averageClosePrice": close,
        "maxOpenInterest": maximum,
        "closedVolume": closed_volume,
        "closingPnl": closing_pnl,
        "sourceStatus": row.required("Status"),
    }
    record_hash = content_hash(payload)
    return VenuePositionSummaryRecord(
        record_id=stable_id("position_summary", record_hash),
        record_hash=record_hash,
        source_identity=identity,
        instrument_id=instrument.instrument_id,
        source_symbol=symbol,
        opened_at=opened_at,
        closed_at=closed_at,
        direction=direction,
        margin_mode=margin_mode,
        entry_price=entry,
        average_close_price=close,
        max_open_interest=maximum,
        closed_volume=closed_volume,
        closing_pnl=closing_pnl,
        source_status=row.required("Status"),
        provenance=(_provenance(row, parsed),),
    )


def _merge_bundle(left: _TradeBundle, right: _TradeBundle) -> _TradeBundle:
    provenance = tuple(
        sorted(
            {*left.execution.provenance, *right.execution.provenance},
            key=lambda value: (
                value.artifact_role.value,
                value.artifact_content_hash,
                value.source_row_number,
                value.source_row_hash,
            ),
        )
    )
    return _TradeBundle(
        execution=replace(left.execution, provenance=provenance),
        fee=replace(left.fee, provenance=provenance),
        pnl=replace(left.pnl, provenance=provenance),
        semantic_hash=left.semantic_hash,
    )


def _provenance(row: ParsedRow, parsed: ParsedArtifact) -> SourceRowProvenance:
    return SourceRowProvenance(
        artifact_id=parsed.artifact.artifact_id,
        artifact_role=parsed.artifact.role,
        artifact_content_hash=parsed.artifact.content_hash,
        source_row_number=row.source_row_number,
        source_row_hash=row.raw_row_hash,
    )


def _instrument_registry(
    instruments: tuple[InstrumentDescriptor, ...],
) -> dict[str, InstrumentDescriptor]:
    registry: dict[str, InstrumentDescriptor] = {}
    for instrument in instruments:
        if (
            not instrument.venue_symbol
            or instrument.venue_symbol in registry
            or instrument.venue != "BINANCE"
            or instrument.product_family != "LINEAR_PERPETUAL"
            or instrument.quantity_step.value <= 0
            or instrument.price_tick.value <= 0
            or instrument.contract_multiplier.value <= 0
            or instrument.settlement_precision < 0
        ):
            raise fail(
                FailureCode.UNSUPPORTED_PRODUCT,
                FailureStage.VALIDATION,
                reason="INVALID_OR_DUPLICATE_PINNED_INSTRUMENT",
            )
        registry[instrument.venue_symbol] = instrument
    if not registry:
        raise fail(
            FailureCode.INSTRUMENT_TERMS_UNAVAILABLE,
            FailureStage.VALIDATION,
            reason="PINNED_INSTRUMENTS_REQUIRED",
        )
    return registry


def _require_instrument(
    symbol: str,
    instruments: dict[str, InstrumentDescriptor],
    row: ParsedRow,
    role: SourceArtifactRole,
) -> InstrumentDescriptor:
    instrument = instruments.get(symbol)
    if instrument is None:
        raise fail(
            FailureCode.UNSUPPORTED_PRODUCT,
            FailureStage.CANONICALIZATION,
            field="Symbol",
            reason="PINNED_INSTRUMENT_NOT_FOUND",
            row_number=row.source_row_number,
            artifact_role=role.value,
        )
    return instrument


def _decimal(row: ParsedRow, role: SourceArtifactRole, field: str) -> ScaledDecimal:
    return ScaledDecimal.parse(
        row.required(field),
        row_number=row.source_row_number,
        artifact_role=role.value,
        field=field,
    )


def _parse_boolean(value: str, row: ParsedRow, role: SourceArtifactRole, field: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise _enum_failure(row, role, field)


def _enum_failure(row: ParsedRow, role: SourceArtifactRole, field: str) -> Exception:
    return fail(
        FailureCode.FILE_FORMAT_INVALID,
        FailureStage.PARSING,
        field=field,
        reason="UNSUPPORTED_ENUM_VALUE",
        row_number=row.source_row_number,
        artifact_role=role.value,
    )
