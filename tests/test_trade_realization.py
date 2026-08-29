from datetime import date
from decimal import Decimal, localcontext

import pytest

from refinvest.engine.entries import EntryCandidate, ExitCandidate
from refinvest.engine.execution import EntryExecution, ExitExecution
from refinvest.engine.execution.entry import EXECUTION_DECIMAL_CONTEXT
from refinvest.engine.execution.schedule import EntryExecutionSchedule, ExitExecutionSchedule
from refinvest.engine.position import PlannedPosition
from refinvest.engine.trades import SampleSizeWarning, realize_trades, sample_size_warning
from refinvest.engine.validation.models import AssetSymbol, FeeModel


def planned_position() -> PlannedPosition:
    entry_candidate = EntryCandidate(AssetSymbol.BTCUSDT, date(2025, 1, 4), date(2025, 1, 5))
    exit_candidate = ExitCandidate(entry_candidate, date(2025, 1, 7))
    return PlannedPosition(
        EntryExecutionSchedule(entry_candidate, AssetSymbol.TQQQ, date(2025, 1, 6)),
        ExitExecutionSchedule(exit_candidate, AssetSymbol.TQQQ, date(2025, 1, 8)),
    )


def priced_executions() -> tuple[EntryExecution, ExitExecution]:
    position = planned_position()
    return (
        EntryExecution(
            position.entry.candidate,
            AssetSymbol.TQQQ,
            date(2025, 1, 6),
            Decimal("100"),
            Decimal("100.100"),
        ),
        ExitExecution(
            position.exit.candidate,
            AssetSymbol.TQQQ,
            date(2025, 1, 8),
            Decimal("110"),
            Decimal("109.890"),
        ),
    )


def fee_model() -> FeeModel:
    return FeeModel(commission=Decimal("0.001"), slippage=Decimal("0.001"))


def test_realizes_admitted_position_with_both_side_commission() -> None:
    entry, exit = priced_executions()

    result = realize_trades(fee_model(), (planned_position(),), (entry,), (exit,), 2)

    with localcontext(EXECUTION_DECIMAL_CONTEXT):
        expected_return = (Decimal("109.780110") / Decimal("100.200100")) - Decimal(1)
    assert result[0].signal_session == date(2025, 1, 4)
    assert result[0].entry_price == Decimal("100.100")
    assert result[0].entry_cost == Decimal("100.200100")
    assert result[0].exit_price == Decimal("109.890")
    assert result[0].exit_proceeds == Decimal("109.780110")
    assert result[0].return_pct == expected_return
    assert result[0].holding_signal_sessions == 2


def test_trade_realization_is_deterministic() -> None:
    entry, exit = priced_executions()
    inputs = (fee_model(), (planned_position(),), (entry,), (exit,), 2)

    assert realize_trades(*inputs) == realize_trades(*inputs)


def test_rejects_executions_that_do_not_match_admitted_position() -> None:
    entry, exit = priced_executions()
    mismatched_entry = EntryExecution(
        entry.candidate,
        entry.execution_asset,
        date(2025, 1, 7),
        entry.reference_price,
        entry.entry_price,
    )

    with pytest.raises(ValueError, match="match the admitted position schedules"):
        realize_trades(fee_model(), (planned_position(),), (mismatched_entry,), (exit,), 2)


@pytest.mark.parametrize(
    ("commission", "slippage"),
    [
        (Decimal("-0.001"), Decimal("0")),
        (Decimal("1"), Decimal("0")),
        (Decimal("0"), Decimal("-0.001")),
        (Decimal("0"), Decimal("1")),
    ],
)
def test_rejects_fee_model_percentages_outside_supported_range(
    commission: Decimal, slippage: Decimal
) -> None:
    entry, exit = priced_executions()

    with pytest.raises(ValueError, match="must be in"):
        realize_trades(
            FeeModel(commission=commission, slippage=slippage),
            (planned_position(),),
            (entry,),
            (exit,),
            2,
        )


def test_sample_size_warning_preserves_zero_and_low_semantics() -> None:
    entry, exit = priced_executions()
    trade = realize_trades(fee_model(), (planned_position(),), (entry,), (exit,), 2)[0]

    assert sample_size_warning(()) is SampleSizeWarning.ZERO
    assert sample_size_warning((trade,)) is SampleSizeWarning.LOW
    assert sample_size_warning((trade,) * 10) is SampleSizeWarning.NONE
