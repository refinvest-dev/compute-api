from __future__ import annotations

from dataclasses import replace

import pytest

from refinvest.trading_review import FailureCode, TradingReviewError, normalize_and_reconstruct
from refinvest.trading_review.parser import POSITION_COLUMNS, TRADE_COLUMNS
from tests.trading_review_support import csv_bytes, request_for, trade_row


@pytest.mark.parametrize("bom", [True, False])
@pytest.mark.parametrize("line_ending", ["\r\n", "\n"])
def test_accepts_optional_bom_and_contract_line_endings(bom: bool, line_ending: str) -> None:
    trades = [trade_row("1", "BUY", "1", "10.00")]
    positions: list[list[str]] = []
    request = request_for(
        trades,
        positions,
        trade_bytes=csv_bytes(TRADE_COLUMNS, trades, bom=bom, line_ending=line_ending),
        position_bytes=csv_bytes(POSITION_COLUMNS, positions, bom=bom, line_ending=line_ending),
    )
    result = normalize_and_reconstruct(request)
    assert result.artifact_coverages[0].row_count == 1


def test_rfc_csv_quoting_and_unknown_extra_column_are_preserved_as_schema_warning() -> None:
    headers = (*TRADE_COLUMNS, "Future Column")
    row = [*trade_row("1", "BUY", "1", "10.00", symbol="SYNTH,USDT"), "quoted,value"]
    request = request_for(
        [row],
        [],
        trade_headers=headers,
        symbols=("SYNTH,USDT",),
    )
    result = normalize_and_reconstruct(request)
    assert result.canonical_records[0].source_symbol == "SYNTH,USDT"  # type: ignore[union-attr]
    assert [item.code for item in result.quality_report.validation_issues] == [
        "UNKNOWN_EXTRA_COLUMN"
    ]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (
            tuple(name for name in TRADE_COLUMNS if name != "Maker"),
            FailureCode.REQUIRED_COLUMN_MISSING,
        ),
        (("UID", *TRADE_COLUMNS[1:]), FailureCode.UNSUPPORTED_SOURCE_SCHEMA),
        ((*TRADE_COLUMNS, "Uid"), FailureCode.UNSUPPORTED_SOURCE_SCHEMA),
    ],
)
def test_header_failures(headers: tuple[str, ...], expected: FailureCode) -> None:
    with pytest.raises(TradingReviewError) as raised:
        normalize_and_reconstruct(request_for([], [], trade_headers=headers))
    assert raised.value.code is expected


def test_rejects_malformed_row_width_and_bare_carriage_return() -> None:
    malformed = ",".join(TRADE_COLUMNS).encode() + b"\nshort,row\n"
    with pytest.raises(TradingReviewError) as raised:
        normalize_and_reconstruct(request_for([], [], trade_bytes=malformed))
    assert raised.value.code is FailureCode.FILE_FORMAT_INVALID
    bare_cr = csv_bytes(TRADE_COLUMNS, []).replace(b"\r\n", b"\r")
    with pytest.raises(TradingReviewError) as raised:
        normalize_and_reconstruct(request_for([], [], trade_bytes=bare_cr))
    assert raised.value.code is FailureCode.FILE_FORMAT_INVALID


@pytest.mark.parametrize(
    ("field_index", "value"),
    [
        (9, "True"),
        (10, "FALSE"),
        (1, "2026-01-02 10:00"),
        (11, "1A"),
        (5, "1e0"),
        (7, "0.1 usdt"),
    ],
)
def test_rejects_non_contract_boolean_timestamp_id_decimal_and_fee(
    field_index: int, value: str
) -> None:
    row = trade_row("1", "BUY", "1", "10.00")
    row[field_index] = value
    with pytest.raises(TradingReviewError) as raised:
        normalize_and_reconstruct(request_for([row], []))
    assert raised.value.code is FailureCode.FILE_FORMAT_INVALID
    assert value not in str(raised.value)


def test_rejects_dst_ambiguous_and_nonexistent_local_timestamp() -> None:
    for local_time in ("2026-11-01 01:30:00", "2026-03-08 02:30:00"):
        row = trade_row("1", "BUY", "1", "10.00", time=local_time)
        with pytest.raises(TradingReviewError) as raised:
            normalize_and_reconstruct(request_for([row], [], source_timezone="America/New_York"))
        assert raised.value.code is FailureCode.FILE_FORMAT_INVALID


def test_unicode_symbol_is_lossless_and_not_normalized() -> None:
    symbol = "A\u030aÅUSDT"
    result = normalize_and_reconstruct(
        request_for(
            [trade_row("1", "BUY", "1", "10.00", symbol=symbol)],
            [],
            symbols=(symbol,),
        )
    )
    assert result.canonical_records[0].source_symbol == symbol  # type: ignore[union-attr]


def test_failure_diagnostic_never_contains_source_value_or_filename() -> None:
    row = trade_row("123456789012345", "BUY", "1", "10.00")
    row[5] = "secret-quantity"
    request = request_for([row], [])
    request = replace(
        request,
        artifacts=tuple(replace(item, artifact_id="opaque-artifact") for item in request.artifacts),
    )
    with pytest.raises(TradingReviewError) as raised:
        normalize_and_reconstruct(request)
    message = str(raised.value)
    assert "123456789012345" not in message
    assert "secret-quantity" not in message
    assert "opaque-artifact" not in message


def test_invalid_utf8_and_interior_bom_are_rejected() -> None:
    for payload in (
        b"\xff\xfe",
        csv_bytes(TRADE_COLUMNS, []).replace(b"Time", b"Ti\xef\xbb\xbfme"),
    ):
        with pytest.raises(TradingReviewError) as raised:
            normalize_and_reconstruct(request_for([], [], trade_bytes=payload))
        assert raised.value.code is FailureCode.FILE_FORMAT_INVALID


def test_source_lexeme_scale_fee_split_and_utc_canonicalization_are_explicit() -> None:
    row = trade_row(
        "1",
        "BUY",
        "1.00",
        "10.500",
        fee="-0.010FEECOIN",
        time="2026-01-02 10:00:01",
    )
    result = normalize_and_reconstruct(
        request_for([row], [], source_timezone="Asia/Seoul", quantity_step="0.01")
    )
    execution = result.canonical_records[0]
    fee = result.canonical_records[1]
    assert execution.price.source_lexeme == "10.500"  # type: ignore[union-attr]
    assert execution.price.scale == 3  # type: ignore[union-attr]
    assert execution.quantity.source_lexeme == "1.00"  # type: ignore[union-attr]
    assert execution.occurred_at.isoformat() == "2026-01-02T01:00:01+00:00"  # type: ignore[union-attr]
    assert fee.source_amount.source_lexeme == "-0.010"  # type: ignore[union-attr]
    assert fee.asset == "FEECOIN"  # type: ignore[union-attr]
