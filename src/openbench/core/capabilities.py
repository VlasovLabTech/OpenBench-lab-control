from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from openbench.domain import MatrixConnection, MatrixPort


@dataclass(frozen=True, slots=True)
class MeterSample:
    value: float | None
    unit: str
    mode: str
    status: str = "ok"


@runtime_checkable
class Instrument(Protocol):
    @property
    def device_id(self) -> str: ...

    async def identify(self) -> str: ...


@runtime_checkable
class MeterCapability(Instrument, Protocol):
    async def read_meter(self, channel_id: str) -> float | MeterSample: ...


@runtime_checkable
class AsyncClosable(Protocol):
    async def close(self) -> None: ...


@runtime_checkable
class MatrixCapability(Instrument, Protocol):
    def list_ports(self) -> Sequence[MatrixPort]: ...

    def apply_connections(self, connections: Sequence[MatrixConnection]) -> None: ...

    def open_all(self) -> None: ...
