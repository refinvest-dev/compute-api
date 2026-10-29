from __future__ import annotations

import csv
import hashlib
import io
from decimal import Decimal

from refinvest.contracts.normalization.identity import (
    NormalizationInputIdentity,
    calculate_input_set_hash,
)
from refinvest.trading_review.decimal import ScaledDecimal
from refinvest.trading_review.model import (
    FeeValuation,
    InstrumentDescriptor,
    NormalizationRequest,
    SourceArtifact,
    SourceArtifactRole,
)
from refinvest.trading_review.parser import POSITION_COLUMNS, TRADE_COLUMNS
from refinvest.trading_review.versions import (
    POSITION_DIALECT_V1,
    SUPPORTED_VERSION_SET,
    TRADE_DIALECT_V1,
)


def trade_row(
    trade_id: str,
    side: str,
    quantity: str,
    price: str,
    realized_pnl: str = "0",
    fee: str = "0.00USDT",
    *,
    time: str = "2026-01-02 10:00:01",
    symbol: str = "SYNTHUSDT",
    order_id: str | None = None,
    buyer: str | None = None,
    maker: str = "false",
    uid: str = "900000001",
) -> list[str]:
    amount = format(Decimal(quantity) * Decimal(price), "f")
    return [
        uid,
        time,
        symbol,
        side,
        price,
        quantity,
        amount,
        fee,
        realized_pnl,
        buyer if buyer is not None else ("true" if side == "BUY" else "false"),
        maker,
        trade_id,
        order_id or str(int(trade_id) * 10),
    ]


def position_row(
    direction: str,
    entry_price: str,
    close_price: str,
    maximum: str,
    closed_volume: str,
    pnl: str,
    *,
    opened: str = "2026-01-02 10:00:01",
    closed: str = "2026-01-02 10:05:01",
    symbol: str = "SYNTHUSDT",
    margin_mode: str = "Isolated",
    status: str = "Closed",
) -> list[str]:
    return [
        symbol,
        margin_mode,
        direction,
        entry_price,
        close_price,
        maximum,
        closed_volume,
        pnl,
        opened,
        closed,
        status,
    ]


def csv_bytes(
    headers: tuple[str, ...],
    rows: list[list[str]],
    *,
    bom: bool = True,
    line_ending: str = "\r\n",
) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator=line_ending)
    writer.writerow(headers)
    writer.writerows(rows)
    payload = stream.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + payload) if bom else payload


def request_for(
    trades: list[list[str]],
    positions: list[list[str]],
    *,
    trade_headers: tuple[str, ...] = TRADE_COLUMNS,
    position_headers: tuple[str, ...] = POSITION_COLUMNS,
    trade_bytes: bytes | None = None,
    position_bytes: bytes | None = None,
    source_timezone: str = "UTC",
    artifact_ids: tuple[str, str] = ("trade-runtime-id", "position-runtime-id"),
    symbols: tuple[str, ...] | None = None,
    quantity_step: str = "0.01",
    price_tick: str = "0.01",
    settlement_precision: int = 2,
    fee_valuations: tuple[FeeValuation, ...] = (),
) -> NormalizationRequest:
    if symbols is None:
        symbol_index = trade_headers.index("Symbol") if "Symbol" in trade_headers else 2
        position_symbol_index = (
            position_headers.index("Symbol") if "Symbol" in position_headers else 0
        )
        symbols = tuple(
            sorted(
                {
                    *(row[symbol_index] for row in trades if len(row) > symbol_index),
                    *(
                        row[position_symbol_index]
                        for row in positions
                        if len(row) > position_symbol_index
                    ),
                }
            )
        ) or ("SYNTHUSDT",)
    instruments = tuple(
        InstrumentDescriptor(
            instrument_id=f"BINANCE:USDS_FUTURES:{symbol}",
            venue_symbol=symbol,
            settlement_asset="USDT",
            quantity_step=ScaledDecimal.parse(quantity_step),
            price_tick=ScaledDecimal.parse(price_tick),
            contract_multiplier=ScaledDecimal.parse("1"),
            settlement_precision=settlement_precision,
            instrument_terms_version=f"{symbol}_TERMS_V1",
        )
        for symbol in symbols
    )
    return NormalizationRequest(
        artifacts=(
            SourceArtifact(
                artifact_id=artifact_ids[0],
                role=SourceArtifactRole.TRADE_HISTORY,
                dialect=TRADE_DIALECT_V1,
                content=trade_bytes or csv_bytes(trade_headers, trades),
            ),
            SourceArtifact(
                artifact_id=artifact_ids[1],
                role=SourceArtifactRole.POSITION_HISTORY,
                dialect=POSITION_DIALECT_V1,
                content=position_bytes or csv_bytes(position_headers, positions),
            ),
        ),
        source_timezone=source_timezone,
        timezone_database_version="TZDB_TEST_V1",
        declared_position_mode="ONE_WAY",
        instruments=instruments,
        version_set=SUPPORTED_VERSION_SET,
        stable_identity_key=b"synthetic-test-identity-key",
        fee_valuations=fee_valuations,
    )


def simple_long_request(**kwargs: object) -> NormalizationRequest:
    trades = [
        trade_row("101", "BUY", "2", "10.00"),
        trade_row(
            "102",
            "SELL",
            "2",
            "11.00",
            "2.00",
            time="2026-01-02 10:05:01",
        ),
    ]
    positions = [position_row("Long", "10.00", "11.00", "2", "2", "2.00")]
    return request_for(trades, positions, **kwargs)  # type: ignore[arg-type]


def normalization_identity_for(
    request: NormalizationRequest, *, book_id: str = "book-1", session_id: str = "session-1"
) -> dict[str, object]:
    version = request.version_set
    return {
        "hashSpecificationVersion": "TRADING_INPUT_SET_HASH_V1",
        "tradingBookId": book_id,
        "sourceImportSessionIds": [session_id],
        "artifactsByRole": {
            artifact.role.value: {
                "artifactId": artifact.artifact_id,
                "sha256": hashlib.sha256(artifact.content).hexdigest(),
            }
            for artifact in request.artifacts
        },
        "sourceTimezone": request.source_timezone,
        "reviewTimezone": "UTC",
        "declaredProduct": {
            "venue": "BINANCE",
            "productFamily": "LINEAR_PERPETUAL",
            "settlementAsset": "USDT",
        },
        "declaredPositionMode": "ONE_WAY",
        "targetVersionSet": {
            "normalizer": {
                "sourceDialectVersionsByArtifactRole": {
                    role.value: value
                    for role, value in version.source_dialect_versions_by_artifact_role
                },
                "adapterVersion": version.adapter_version,
                "normalizerVersion": version.normalizer_version,
                "canonicalSchemaVersion": version.canonical_schema_version,
                "implementationDigest": version.implementation_digest,
                "versionSetHash": "a" * 64,
            },
            "reconstruction": {
                "instrumentTermsVersionByInstrument": {
                    term.instrument_id: term.instrument_terms_version
                    for term in request.instruments
                },
                "reconstructionPolicyVersion": version.reconstruction_policy_version,
                "reconciliationVersion": version.reconciliation_version,
                "reviewUnitSchemaVersion": version.review_unit_schema_version,
                "evidenceSchemaVersion": version.evidence_schema_version,
                "implementationDigest": version.implementation_digest,
                "versionSetHash": "b" * 64,
            },
            "versionSetHash": "c" * 64,
        },
    }


def input_set_hash_for(identity: dict[str, object]) -> str:
    return calculate_input_set_hash(NormalizationInputIdentity.model_validate(identity))
