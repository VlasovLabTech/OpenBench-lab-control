from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Measurement:
    timestamp_utc: datetime
    monotonic_s: float
    device_id: str
    channel_id: str
    value: float | None
    unit: str
    quality: str = "device_reported"
    status: str = "ok"

    def __post_init__(self) -> None:
        if not self.unit.strip():
            raise ValueError("Measurement unit must not be empty")
        if self.timestamp_utc.tzinfo is None:
            raise ValueError("Measurement timestamp must be timezone-aware")
