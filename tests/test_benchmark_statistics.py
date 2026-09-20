import pytest

from refinvest.benchmarking.statistics import duration_statistics


def test_odd_and_even_medians_keep_integer_nanoseconds() -> None:
    assert duration_statistics([9, 1, 5])["medianNanoseconds"] == 5
    assert duration_statistics([1, 2, 8, 9])["medianNanoseconds"] == 5


def test_p95_uses_nearest_rank_and_supports_one_sample() -> None:
    assert duration_statistics(list(range(1, 21)))["p95Nanoseconds"] == 19
    assert duration_statistics([7]) == {
        "minNanoseconds": 7,
        "maxNanoseconds": 7,
        "medianNanoseconds": 7,
        "p95Nanoseconds": 7,
    }


def test_empty_and_negative_samples_are_rejected() -> None:
    with pytest.raises(ValueError):
        duration_statistics([])
    with pytest.raises(ValueError):
        duration_statistics([1, -1])
