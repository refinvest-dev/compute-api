from __future__ import annotations

import csv
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tests.trading_review_harness.canonical import content_hash
from tests.trading_review_harness.model import (
    Execution,
    FeeRecord,
    ImportFailure,
    InstrumentTerms,
    JsonObject,
    PositionSummary,
    Provenance,
    VenueReportedPnlRecord,
)

TRADE_ROLE = "BINANCE_USDS_TRADE_HISTORY_V1"
POSITION_ROLE = "BINANCE_USDS_POSITION_HISTORY_V1"
DECIMAL_PATTERN = r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
DECIMAL_RE = re.compile(rf"^{DECIMAL_PATTERN}$", re.ASCII)
FEE_RE = re.compile(rf"^({DECIMAL_PATTERN})([A-Z][A-Z0-9]*)$", re.ASCII)
TRADE_ID_RE = re.compile(r"^[0-9]+$", re.ASCII)

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


class ParseFailure(Exception):
    def __init__(self, failure: ImportFailure):
        super().__init__(failure.code)
        self.failure = failure


def parse_decimal(value: str, row_number: int) -> Decimal:
    if not DECIMAL_RE.fullmatch(value):
        raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID", row_number))
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID", row_number)) from exc


def _read_rows(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            headers = reader.fieldnames
            if headers is None:
                raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID"))
            if len(headers) != len(set(headers)):
                raise ParseFailure(ImportFailure("UNSUPPORTED_SOURCE_SCHEMA"))
            if any(column not in headers for column in required):
                raise ParseFailure(ImportFailure("REQUIRED_COLUMN_MISSING"))
            rows: list[dict[str, str]] = []
            for source_row, row in enumerate(reader, start=1):
                if None in row or any(value is None for value in row.values()):
                    raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID", source_row))
                rows.append({key: str(value) for key, value in row.items()})
            return rows
    except UnicodeDecodeError as exc:
        raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID")) from exc


def load_input(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID"))
    return value


def load_terms(config: JsonObject) -> dict[str, InstrumentTerms]:
    if config.get("product") != "BINANCE_USDS_LINEAR_PERPETUAL":
        raise ParseFailure(ImportFailure("UNSUPPORTED_PRODUCT"))
    if config.get("declaredPositionMode") != "ONE_WAY":
        raise ParseFailure(ImportFailure("UNSUPPORTED_POSITION_MODE"))
    terms_by_symbol: dict[str, InstrumentTerms] = {}
    raw_terms = config.get("instrumentTerms")
    if not isinstance(raw_terms, list) or not raw_terms:
        raise ParseFailure(ImportFailure("UNSUPPORTED_PRODUCT"))
    for item in raw_terms:
        if not isinstance(item, dict):
            raise ParseFailure(ImportFailure("UNSUPPORTED_PRODUCT"))
        symbol = str(item.get("symbol", ""))
        if not symbol or symbol in terms_by_symbol:
            raise ParseFailure(ImportFailure("UNSUPPORTED_PRODUCT"))
        terms_by_symbol[symbol] = InstrumentTerms(
            symbol=symbol,
            step_size=parse_decimal(str(item.get("stepSize", "")), 0),
            tick_size=parse_decimal(str(item.get("tickSize", "")), 0),
            contract_multiplier=parse_decimal(str(item.get("contractMultiplier", "")), 0),
            settlement_asset=str(item.get("settlementAsset", "")),
            settlement_precision=int(item.get("settlementPrecision", -1)),
        )
    return terms_by_symbol


def _parse_timezone(config: JsonObject) -> ZoneInfo:
    name = config.get("sourceTimezone")
    if not isinstance(name, str) or not name:
        raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID"))
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID")) from exc


def _parse_time(value: str, fmt: str, timezone: ZoneInfo, row_number: int) -> datetime:
    try:
        naive = datetime.strptime(value, fmt)
    except ValueError as exc:
        raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID", row_number)) from exc
    first = naive.replace(tzinfo=timezone, fold=0)
    second = naive.replace(tzinfo=timezone, fold=1)
    first_roundtrip = first.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)
    second_roundtrip = second.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)
    if (
        first_roundtrip != naive
        or second_roundtrip != naive
        or first.utcoffset() != second.utcoffset()
    ):
        raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID", row_number))
    return first.astimezone(UTC)


def _parse_bool(value: str, row_number: int) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID", row_number))


def parse_executions(
    fixture_path: Path,
    config: JsonObject,
    terms_by_symbol: dict[str, InstrumentTerms],
) -> tuple[list[Execution], str]:
    rows = _read_rows(fixture_path / "trade_history.csv", TRADE_COLUMNS)
    timezone = _parse_timezone(config)
    executions_by_identity: dict[tuple[str, str], Execution] = {}
    numeric_ids: dict[tuple[str, int], str] = {}
    semantic_rows: list[dict[str, str]] = []
    for source_row, row in enumerate(rows, start=1):
        semantic_rows.append(row)
        symbol = row["Symbol"]
        if symbol not in terms_by_symbol:
            raise ParseFailure(ImportFailure("UNSUPPORTED_PRODUCT", source_row))
        raw_trade_id = row["Trade ID"]
        if not TRADE_ID_RE.fullmatch(raw_trade_id):
            raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID", source_row))
        numeric_identity = (symbol, int(raw_trade_id))
        previous_raw = numeric_ids.get(numeric_identity)
        if previous_raw is not None and previous_raw != raw_trade_id:
            raise ParseFailure(ImportFailure("RECORD_CONFLICT", source_row))
        numeric_ids[numeric_identity] = raw_trade_id
        side = row["Side"]
        if side not in {"BUY", "SELL"}:
            raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID", source_row))
        fee_match = FEE_RE.fullmatch(row["Fee"])
        if fee_match is None:
            raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID", source_row))
        quantity = parse_decimal(row["Quantity"], source_row)
        price = parse_decimal(row["Price"], source_row)
        amount = parse_decimal(row["Amount"], source_row)
        if quantity <= 0 or price <= 0 or amount < 0:
            raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID", source_row))
        provenance = Provenance(TRADE_ROLE, source_row, content_hash(row))
        execution = Execution(
            key=f"{symbol}:{raw_trade_id}",
            symbol=symbol,
            occurred_at=_parse_time(row["Time"], "%Y-%m-%d %H:%M:%S", timezone, source_row),
            side=side,
            quantity=quantity,
            price=price,
            amount=amount,
            fee=parse_decimal(fee_match.group(1), source_row),
            fee_asset=fee_match.group(2),
            realized_pnl=parse_decimal(row["Realized Profit"], source_row),
            raw_trade_id=raw_trade_id,
            raw_order_id=row["Order ID"],
            buyer=_parse_bool(row["Buyer"], source_row),
            maker=_parse_bool(row["Maker"], source_row),
            provenance=[provenance],
        )
        identity = (symbol, raw_trade_id)
        existing = executions_by_identity.get(identity)
        if existing is None:
            executions_by_identity[identity] = execution
        elif _execution_content(existing) == _execution_content(execution):
            existing.provenance.append(provenance)
        else:
            raise ParseFailure(ImportFailure("RECORD_CONFLICT", source_row))
    executions = sorted(
        executions_by_identity.values(),
        key=lambda item: (item.symbol, item.occurred_at, int(item.raw_trade_id)),
    )
    semantic_rows.sort(key=lambda row: (row["Symbol"], int(row["Trade ID"]), content_hash(row)))
    return executions, content_hash(semantic_rows)


def _execution_content(execution: Execution) -> tuple[Any, ...]:
    return (
        execution.symbol,
        execution.occurred_at,
        execution.side,
        execution.quantity,
        execution.price,
        execution.amount,
        execution.fee,
        execution.fee_asset,
        execution.realized_pnl,
        execution.raw_trade_id,
        execution.raw_order_id,
        execution.buyer,
        execution.maker,
    )


def derive_trade_records(
    executions: list[Execution], terms_by_symbol: dict[str, InstrumentTerms]
) -> tuple[list[FeeRecord], list[VenueReportedPnlRecord]]:
    fees: list[FeeRecord] = []
    pnl_records: list[VenueReportedPnlRecord] = []
    for execution in executions:
        kind = "COST" if execution.fee > 0 else "REBATE" if execution.fee < 0 else "ZERO"
        fees.append(
            FeeRecord(
                execution_key=execution.key,
                source_amount=execution.fee,
                asset=execution.fee_asset,
                kind=kind,
                balance_effect=-execution.fee,
                provenance=tuple(execution.provenance),
            )
        )
        pnl_records.append(
            VenueReportedPnlRecord(
                execution_key=execution.key,
                realized_profit=execution.realized_pnl,
                asset=terms_by_symbol[execution.symbol].settlement_asset,
                provenance=tuple(execution.provenance),
            )
        )
    return fees, pnl_records


def parse_summaries(
    fixture_path: Path,
    config: JsonObject,
    terms_by_symbol: dict[str, InstrumentTerms],
) -> tuple[list[PositionSummary], str]:
    rows = _read_rows(fixture_path / "position_history.csv", POSITION_COLUMNS)
    timezone = _parse_timezone(config)
    summaries: list[PositionSummary] = []
    semantic_rows: list[dict[str, str]] = []
    for source_row, row in enumerate(rows, start=1):
        semantic_rows.append(row)
        symbol = row["Symbol"]
        if symbol not in terms_by_symbol:
            raise ParseFailure(ImportFailure("UNSUPPORTED_PRODUCT", source_row))
        if row["Margin Mode"] not in {"Isolated", "Cross"}:
            raise ParseFailure(ImportFailure("UNSUPPORTED_SOURCE_SCHEMA", source_row))
        if row["Position Side"] not in {"Long", "Short"}:
            raise ParseFailure(ImportFailure("UNSUPPORTED_SOURCE_SCHEMA", source_row))
        if not row["Status"]:
            raise ParseFailure(ImportFailure("UNSUPPORTED_SOURCE_SCHEMA", source_row))
        provenance = Provenance(POSITION_ROLE, source_row, content_hash(row))
        entry_price = parse_decimal(row["Entry Price"], source_row)
        close_price = parse_decimal(row["Avg. Close Price"], source_row)
        max_quantity = parse_decimal(row["Max Open Interest"], source_row)
        closed_volume = parse_decimal(row["Closed Vol."], source_row)
        if entry_price < 0 or close_price < 0 or max_quantity < 0 or closed_volume < 0:
            raise ParseFailure(ImportFailure("FILE_FORMAT_INVALID", source_row))
        summaries.append(
            PositionSummary(
                key=f"{POSITION_ROLE}:r{source_row}:{provenance.row_hash[:12]}",
                symbol=symbol,
                direction=row["Position Side"].upper(),
                margin_mode=row["Margin Mode"].upper(),
                entry_price=entry_price,
                close_price=close_price,
                max_quantity=max_quantity,
                closed_volume=closed_volume,
                closing_pnl=parse_decimal(row["Closing PNL"], source_row),
                opened_minute=_parse_time(row["Opened"], "%Y-%m-%d %H:%M", timezone, source_row),
                closed_minute=_parse_time(row["Closed"], "%Y-%m-%d %H:%M", timezone, source_row),
                status=row["Status"],
                provenance=provenance,
            )
        )
    summaries.sort(
        key=lambda item: (
            item.symbol,
            item.opened_minute,
            item.closed_minute,
            item.direction,
            item.provenance.row_hash,
        )
    )
    semantic_rows.sort(key=lambda row: (row["Symbol"], row["Opened"], content_hash(row)))
    return summaries, content_hash(semantic_rows)
