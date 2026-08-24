from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MatrixPort:
    id: str
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class MatrixConnection:
    from_port: str
    to_port: str

    @property
    def normalized(self) -> tuple[str, str]:
        if self.from_port <= self.to_port:
            return (self.from_port, self.to_port)
        return (self.to_port, self.from_port)


@dataclass(frozen=True, slots=True)
class MatrixProfile:
    id: str
    name: str
    version: int
    connections: tuple[MatrixConnection, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MatrixApplyResult:
    success: bool
    profile_id: str | None
    profile_name: str | None
    active_connections: tuple[MatrixConnection, ...]
    message: str


@dataclass(frozen=True, slots=True)
class SafetyState:
    state: str
    reason: str | None
    updated_at: datetime
