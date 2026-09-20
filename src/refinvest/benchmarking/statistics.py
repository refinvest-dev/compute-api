"""Integer-nanosecond benchmark summary statistics."""

from __future__ import annotations

from math import ceil


def duration_statistics(samples: list[int] | tuple[int, ...]) -> dict[str, int]:
    if not samples:
        raise ValueError("At least one sample is required.")
    if any(value < 0 for value in samples):
        raise ValueError("Duration samples must be non-negative integer nanoseconds.")
    ordered = sorted(samples)
    count = len(ordered)
    middle = count // 2
    median = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) // 2
    rank = ceil(0.95 * count)
    return {
        "minNanoseconds": ordered[0],
        "maxNanoseconds": ordered[-1],
        "medianNanoseconds": median,
        "p95Nanoseconds": ordered[rank - 1],
    }
