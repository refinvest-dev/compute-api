from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class Provenance:
    artifact_role: str
    source_row: int
    row_hash: str

    def reference(self) -> str:
        return f"{self.artifact_role}:r{self.source_row}:{self.row_hash[:12]}"


@dataclass(frozen=True)
class InstrumentTerms:
    symbol: str
    step_size: Decimal
    tick_size: Decimal
    contract_multiplier: Decimal
    settlement_asset: str
    settlement_precision: int


@dataclass
class Execution:
    key: str
    symbol: str
    occurred_at: datetime
    side: str
    quantity: Decimal
    price: Decimal
    amount: Decimal
    fee: Decimal
    fee_asset: str
    realized_pnl: Decimal
    raw_trade_id: str
    raw_order_id: str
    buyer: bool
    maker: bool
    provenance: list[Provenance]


@dataclass(frozen=True)
class FeeRecord:
    execution_key: str
    source_amount: Decimal
    asset: str
    kind: str
    balance_effect: Decimal
    provenance: tuple[Provenance, ...]


@dataclass(frozen=True)
class VenueReportedPnlRecord:
    execution_key: str
    realized_profit: Decimal
    asset: str
    provenance: tuple[Provenance, ...]


@dataclass(frozen=True)
class PositionSummary:
    key: str
    symbol: str
    direction: str
    margin_mode: str
    entry_price: Decimal
    close_price: Decimal
    max_quantity: Decimal
    closed_volume: Decimal
    closing_pnl: Decimal
    opened_minute: datetime
    closed_minute: datetime
    status: str
    provenance: Provenance


@dataclass
class Allocation:
    execution_key: str
    episode_key: str
    action: str
    quantity: Decimal
    price: Decimal
    fee_asset: str
    fee: Decimal
    reported_pnl: Decimal
    provenance: list[Provenance]
    reversal_group_id: str | None = None


@dataclass
class Episode:
    key: str
    symbol: str
    direction: str
    opened_at: datetime | None
    closed_at: datetime | None
    initial_quantity: Decimal | None
    max_quantity: Decimal | None
    opening_volume: Decimal
    closing_volume: Decimal
    entry_numerator: Decimal | None
    entry_denominator: Decimal | None
    exit_numerator: Decimal | None
    exit_denominator: Decimal | None
    gross_pnl: Decimal | None
    fees: dict[str, Decimal]
    net_pnl: Decimal | None
    reconstruction_status: str
    reconciliation_status: str
    capabilities: set[str]
    eligible: bool
    exclusion_reasons: list[str]
    provenance: list[Provenance]
    allocation_indexes: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class ImportFailure:
    code: str
    source_row: int | None = None


@dataclass
class FixtureResult:
    fixture_path: Path
    actual: JsonObject
    canonical_bytes: bytes | None
    manifest_hash: str | None
