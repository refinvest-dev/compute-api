"""Fenced, payload-free Normalization runtime cleanup capability."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DeleteNormalizationRuntime:
    run_id: str
    session_id: str
    source_generation: int
    request_id: str
    generation: int

    def __post_init__(self) -> None:
        if any(
            not value or len(value) > 128
            for value in (self.run_id, self.session_id, self.request_id)
        ):
            raise ValueError("Invalid cleanup identity")
        if self.source_generation < 0 or self.generation <= 0:
            raise ValueError("Invalid cleanup generation")


@dataclass(frozen=True, slots=True)
class NormalizationRuntimeAbsence:
    identity: DeleteNormalizationRuntime
    verified_at: datetime | None


class NormalizationRuntimeDeletion(Protocol):
    def purge(self, command: DeleteNormalizationRuntime) -> NormalizationRuntimeAbsence: ...
