from __future__ import annotations

from dataclasses import dataclass

MICSIG_SCOPE_KINDS = frozenset(("micsig_mho1", "micsig_eto"))
MICSIG_MAXIMUM_ASCII_CHUNK_POINTS = 15_625
MICSIG_MAXIMUM_MEMORY_POINTS = 1_000_000_000


@dataclass(frozen=True, slots=True)
class MicsigMaximumCaptureInfo:
    memory_depth_points: int


@dataclass(frozen=True, slots=True)
class MicsigMaximumAsciiChunk:
    source: str
    start_point: int
    stop_point: int
    total_points: int
    data: bytes
    preamble_text: str

    @property
    def points(self) -> int:
        return self.stop_point - self.start_point + 1


def is_micsig_scope_kind(kind: str) -> bool:
    return kind in MICSIG_SCOPE_KINDS
