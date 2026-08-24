from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Protocol

from openbench.core.capabilities import MeterSample
from openbench.drivers.ut61e.protocol import UT61EReading, parse_reading_frame
from openbench.drivers.ut61e.transport import CH9325HidTransport, UT61EDescriptor
from openbench.drivers.ut61e.ut61d_protocol import UT61DReading, parse_ut61d_reading_frame


class UT61ETransport(Protocol):
    def request_reading_frame(self) -> bytes: ...

    def close(self) -> None: ...


def _descriptor_identity(descriptor: UT61EDescriptor) -> str:
    if descriptor.serial_number:
        identity = descriptor.serial_number
    else:
        identity = hashlib.sha256(descriptor.path).hexdigest()[:12]
    return re.sub(r"[^a-z0-9]+", "_", identity.casefold()).strip("_") or "unknown"


class UT61EMeter:
    def __init__(
        self,
        descriptor: UT61EDescriptor,
        *,
        model: str = "UT61E",
        baud_rate: int | None = None,
        timeout_ms: int = 1500,
        transport: UT61ETransport | None = None,
    ) -> None:
        if model not in {"UT61D", "UT61E"}:
            raise ValueError(f"Unsupported original UT61-series model: {model}")
        identity = _descriptor_identity(descriptor)
        self.device_id = f"{model.casefold()}_{identity}"
        self.channel_id = f"{self.device_id}.primary"
        self.model = model
        self.baud_rate = baud_rate or (2400 if model == "UT61D" else 19200)
        self.descriptor = descriptor
        self._transport = transport or CH9325HidTransport(
            descriptor,
            baud_rate=self.baud_rate,
            timeout_ms=timeout_ms,
        )
        self._read_lock = asyncio.Lock()
        self.last_reading: UT61DReading | UT61EReading | None = None

    async def identify(self) -> str:
        return (
            f"UNI-T {self.model} via {self.descriptor.product_string} "
            f"({self.descriptor.vid:04X}:{self.descriptor.pid:04X})"
        )

    async def read_meter(self, channel_id: str) -> MeterSample:
        if channel_id != self.channel_id:
            raise ValueError(f"Unknown {self.model} channel: {channel_id}")

        async with self._read_lock:
            frame = await asyncio.to_thread(self._transport.request_reading_frame)
            reading: UT61DReading | UT61EReading
            if self.model == "UT61D":
                reading = parse_ut61d_reading_frame(frame)
            else:
                reading = parse_reading_frame(frame)
            self.last_reading = reading

        if reading.value is None:
            if reading.overload:
                status = "overload"
            elif reading.underflow:
                status = "underflow"
            else:
                status = "unavailable"
            return MeterSample(
                value=None,
                unit=reading.unit,
                mode=reading.mode,
                status=status,
            )
        return MeterSample(
            value=reading.value,
            unit=reading.unit,
            mode=reading.mode,
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._transport.close)
