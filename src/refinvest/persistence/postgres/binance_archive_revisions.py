"""PostgreSQL storage for immutable, verified Binance archive revisions."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

from psycopg import connect
from psycopg.types.json import Jsonb

from refinvest.application.dataset_snapshots import SourceArtifact
from refinvest.ingestion.vendors.binance_btcusdt import (
    BTCUSDT,
    BinanceBtcusdtIngestionError,
    BtcusdtArchiveBatch,
)
from refinvest.marketdata.domain import DailyBar


class PostgresBinanceArchiveRevisionRepository:
    """Never overwrites a normalized archive revision after checksum verification."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def get(self, uri: str, sha256: str) -> BtcusdtArchiveBatch | None:
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT retrieved_at, daily_bars
                FROM compute_binance_btcusdt_archive_revisions
                WHERE uri = %s AND sha256 = %s
                """,
                (uri, sha256),
            )
            row = cursor.fetchone()
        return _batch_from_row(uri, sha256, row)

    def put_if_absent(self, batch: BtcusdtArchiveBatch) -> BtcusdtArchiveBatch:
        artifact = _artifact(batch)
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO compute_binance_btcusdt_archive_revisions
                        (uri, sha256, retrieved_at, daily_bars)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (uri, sha256) DO NOTHING
                """,
                (
                    artifact.uri,
                    artifact.sha256,
                    artifact.retrieved_at,
                    Jsonb([_bar_record(bar) for bar in batch.daily_bars]),
                ),
            )
            cursor.execute(
                """
                SELECT retrieved_at, daily_bars
                FROM compute_binance_btcusdt_archive_revisions
                WHERE uri = %s AND sha256 = %s
                """,
                (artifact.uri, artifact.sha256),
            )
            row = cursor.fetchone()
        persisted = _batch_from_row(artifact.uri, artifact.sha256, row)
        if persisted is None:
            raise BinanceBtcusdtIngestionError("Binance archive revision was not persisted.")
        return persisted


def _artifact(batch: BtcusdtArchiveBatch) -> SourceArtifact:
    if len(batch.source_artifacts) != 1:
        raise BinanceBtcusdtIngestionError("One archive revision must have one source artifact.")
    return batch.source_artifacts[0]


def _batch_from_row(
    uri: str, sha256: str, row: tuple[object, ...] | None
) -> BtcusdtArchiveBatch | None:
    if row is None:
        return None
    retrieved_at, raw_bars = cast(tuple[datetime, list[dict[str, object]]], row)
    try:
        bars = tuple(
            DailyBar(
                BTCUSDT,
                date.fromisoformat(str(item["sessionDate"])),
                Decimal(str(item["open"])),
                Decimal(str(item["high"])),
                Decimal(str(item["low"])),
                Decimal(str(item["close"])),
                Decimal(str(item["volume"])),
            )
            for item in raw_bars
        )
    except (KeyError, ValueError) as error:
        raise BinanceBtcusdtIngestionError(
            "Persisted Binance archive revision is invalid."
        ) from error
    return BtcusdtArchiveBatch(
        bars,
        (SourceArtifact(uri, sha256, retrieved_at.astimezone(UTC)),),
    )


def _bar_record(bar: DailyBar) -> dict[str, str]:
    return {
        "sessionDate": bar.session_date.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
    }
