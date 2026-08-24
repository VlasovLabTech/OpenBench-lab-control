from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    name: str
    kind: str
    connected: bool
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Channel:
    id: str
    device_id: str
    name: str
    capability: str
    unit: str
    poll_interval_s: float
    state: str = "ready"

    def __post_init__(self) -> None:
        if not self.unit.strip():
            raise ValueError("Channel unit must not be empty")
        if not 0.01 <= self.poll_interval_s <= 600:
            raise ValueError("Polling interval must be between 0.01 and 600 seconds")
