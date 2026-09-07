import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile

import pytest

from refinvest.ingestion.vendors.binance_btcusdt import (
    BinanceBtcusdtDailyBarsClient,
    BinanceBtcusdtIngestionError,
    BtcusdtArchiveBatch,
)
from refinvest.marketdata.domain import DailyBar


def _zip(csv_content: str) -> bytes:
    result = BytesIO()
    with ZipFile(result, "w") as archive:
        archive.writestr("BTCUSDT-1d.csv", csv_content)
    return result.getvalue()


def test_monthly_backfill_verifies_checksum_and_normalizes_daily_bar() -> None:
    archive = _zip(
        "open_time,open,high,low,close,volume\n"
        "1735776000000,95000.10,96000.20,94000.30,95500.40,12.34567890\n"
    )
    client = _client({"BTCUSDT-1d-2025-01.zip": archive}, date(2025, 2, 1))

    batch = client.fetch_daily_bars(date(2025, 1, 2), date(2025, 1, 2))

    assert batch.daily_bars == (_bar(date(2025, 1, 2)),)
    assert batch.source_artifacts[0].uri.endswith(
        "monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2025-01.zip"
    )
    assert batch.source_artifacts[0].sha256 == hashlib.sha256(archive).hexdigest()
    assert batch.source_artifacts[0].retrieved_at == datetime(2025, 2, 1, tzinfo=UTC)


def test_current_month_completed_day_uses_daily_archive_and_microsecond_timestamp() -> None:
    archive = _zip(
        "open_time,open,high,low,close,volume\n1735776000000000,1.00000001,2,1,1.5,0.12345678\n"
    )
    client = _client({"BTCUSDT-1d-2025-01-02.zip": archive}, date(2025, 1, 3))

    batch = client.fetch_daily_bars(date(2025, 1, 2), date(2025, 1, 2))

    assert batch.daily_bars[0].session_date == date(2025, 1, 2)
    assert batch.daily_bars[0].open == Decimal("1.00000001")
    assert batch.source_artifacts[0].uri.endswith(
        "daily/klines/BTCUSDT/1d/BTCUSDT-1d-2025-01-02.zip"
    )


@pytest.mark.parametrize(
    ("archive", "checksum", "message"),
    [
        (b"not-a-zip", None, "ZIP or CSV"),
        (_zip("open_time,open\n1735776000000,1\n"), None, "lacks required"),
        (
            _zip("open_time,open,high,low,close,volume\nnot-a-time,1,1,1,1,1\n"),
            None,
            "open_time",
        ),
        (
            _zip("open_time,open,high,low,close,volume\n1735776000000,1,1,1,1,1\n"),
            "0" * 64,
            "SHA-256 mismatch",
        ),
    ],
)
def test_rejects_corrupt_or_unverifiable_archives(
    archive: bytes, checksum: str | None, message: str
) -> None:
    client = _client({"BTCUSDT-1d-2025-01-02.zip": archive}, date(2025, 1, 3), checksum)

    with pytest.raises(BinanceBtcusdtIngestionError, match=message):
        client.fetch_daily_bars(date(2025, 1, 2), date(2025, 1, 2))


def test_rejects_current_utc_day_before_any_download() -> None:
    client = BinanceBtcusdtDailyBarsClient(
        lambda _: (_ for _ in ()).throw(AssertionError()),
        now=lambda: datetime(2025, 1, 3, tzinfo=UTC),
    )

    with pytest.raises(BinanceBtcusdtIngestionError, match="Current or future"):
        client.fetch_daily_bars(date(2025, 1, 3), date(2025, 1, 3))


def test_reuses_same_uri_and_hash_without_renormalizing() -> None:
    archive = _zip("open_time,open,high,low,close,volume\n1735776000000,1,1,1,1,1\n")
    client = _client({"BTCUSDT-1d-2025-01-02.zip": archive}, date(2025, 1, 3))

    first = client.fetch_daily_bars(date(2025, 1, 2), date(2025, 1, 2))
    second = client.fetch_daily_bars(date(2025, 1, 2), date(2025, 1, 2))

    assert second.daily_bars[0] is first.daily_bars[0]
    assert second.source_artifacts == first.source_artifacts


def test_changed_hash_creates_a_new_immutable_archive_revision() -> None:
    original = _zip("open_time,open,high,low,close,volume\n1735776000000,1,1,1,1,1\n")
    revised = _zip("open_time,open,high,low,close,volume\n1735776000000,2,2,2,2,1\n")
    active = {"content": original}

    def download(uri: str) -> bytes:
        if uri.endswith(".CHECKSUM"):
            return (
                hashlib.sha256(active["content"]).hexdigest() + "  BTCUSDT-1d-2025-01-02.zip\n"
            ).encode()
        return active["content"]

    client = BinanceBtcusdtDailyBarsClient(
        download,
        now=lambda: datetime(2025, 1, 3, tzinfo=UTC),
    )
    original_batch = client.fetch_daily_bars(date(2025, 1, 2), date(2025, 1, 2))
    active["content"] = revised
    revised_batch = client.fetch_daily_bars(date(2025, 1, 2), date(2025, 1, 2))

    assert original_batch.daily_bars[0].close == Decimal("1")
    assert revised_batch.daily_bars[0].close == Decimal("2")
    assert original_batch.source_artifacts[0].sha256 != revised_batch.source_artifacts[0].sha256


def test_reuses_persisted_revision_across_client_instances() -> None:
    archive = _zip("open_time,open,high,low,close,volume\n1735776000000,1,1,1,1,1\n")
    revisions = FakeRevisions()
    first = BinanceBtcusdtDailyBarsClient(
        _client_download({"BTCUSDT-1d-2025-01-02.zip": archive}),
        now=lambda: datetime(2025, 1, 3, tzinfo=UTC),
        revisions=revisions,
    ).fetch_daily_bars(date(2025, 1, 2), date(2025, 1, 2))

    second = BinanceBtcusdtDailyBarsClient(
        _client_download({"BTCUSDT-1d-2025-01-02.zip": archive}),
        now=lambda: datetime(2025, 1, 4, tzinfo=UTC),
        revisions=revisions,
    ).fetch_daily_bars(date(2025, 1, 2), date(2025, 1, 2))

    assert second.daily_bars[0] is first.daily_bars[0]
    assert second.source_artifacts == first.source_artifacts
    assert revisions.put_calls == 1


def _client(
    files: dict[str, bytes], today: date, checksum: str | None = None
) -> BinanceBtcusdtDailyBarsClient:
    return BinanceBtcusdtDailyBarsClient(
        _client_download(files, checksum),
        base_url="https://fixture.invalid",
        now=lambda: datetime.combine(today, datetime.min.time(), UTC),
    )


def _client_download(files: dict[str, bytes], checksum: str | None = None):
    def download(uri: str) -> bytes:
        name = uri.rsplit("/", maxsplit=1)[-1]
        if name.endswith(".CHECKSUM"):
            archive_name = name.removesuffix(".CHECKSUM")
            content = files[archive_name]
            digest = checksum or hashlib.sha256(content).hexdigest()
            return f"{digest}  {archive_name}\n".encode()
        return files[name]

    return download


def _bar(session_date: date) -> DailyBar:
    return DailyBar(
        "BTCUSDT",
        session_date,
        Decimal("95000.10"),
        Decimal("96000.20"),
        Decimal("94000.30"),
        Decimal("95500.40"),
        Decimal("12.34567890"),
    )


class FakeRevisions:
    def __init__(self) -> None:
        self.batches: dict[tuple[str, str], BtcusdtArchiveBatch] = {}
        self.put_calls = 0

    def get(self, uri: str, sha256: str) -> BtcusdtArchiveBatch | None:
        return self.batches.get((uri, sha256))

    def put_if_absent(self, batch: BtcusdtArchiveBatch) -> BtcusdtArchiveBatch:
        self.put_calls += 1
        artifact = batch.source_artifacts[0]
        return self.batches.setdefault((artifact.uri, artifact.sha256), batch)
