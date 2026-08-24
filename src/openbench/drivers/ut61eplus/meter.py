from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Protocol

from openbench.core.capabilities import MeterSample
from openbench.drivers.ut61eplus.protocol import (
    UT61EPlusReading,
    parse_reading_frame,
)
from openbench.drivers.ut61eplus.transport import (
    CH9329HidTransport,
    CP2110HidTransport,
    UT61EPlusDescriptor,
)


class UT61EPlusTransport(Protocol):
    def request_reading_frame(self) -> bytes: ...


def _descriptor_identity(descriptor: UT61EPlusDescriptor) -> str:
    serial = re.sub(r"[^a-z0-9]+", "_", descriptor.serial_number.casefold()).strip("_")
    if serial and serial not in {"none", "unknown"}:
        return serial
    return hashlib.sha256(descriptor.path).hexdigest()[:12]


class UT61EPlusMeter:
    def __init__(
        self,
        descriptor: UT61EPlusDescriptor,
        *,
        timeout_ms: int = 1500,
        transport: UT61EPlusTransport | None = None,
    ) -> None:
        identity = _descriptor_identity(descriptor)
        self.device_id = f"ut61eplus_{identity}"
        self.channel_id = f"{self.device_id}.voltage"
        self.descriptor = descriptor
        if transport is not None:
            self._transport = transport
        elif descriptor.transport == "cp2110":
            self._transport = CP2110HidTransport(descriptor, timeout_ms=timeout_ms)
        else:
            self._transport = CH9329HidTransport(descriptor, timeout_ms=timeout_ms)
        self._read_lock = asyncio.Lock()
        self.last_reading: UT61EPlusReading | None = None

    async def identify(self) -> str:
        return (
            f"UNI-T UT61E+ via {self.descriptor.product_string} "
            f"(serial {self.descriptor.serial_number})"
        )

    async def read_meter(self, channel_id: str) -> MeterSample:
        if channel_id != self.channel_id:
            raise ValueError(f"Unknown UT61E+ channel: {channel_id}")

        async with self._read_lock:
            frame = await asyncio.to_thread(self._transport.request_reading_frame)
            reading = parse_reading_frame(frame)
            self.last_reading = reading

        if reading.value is None:
            return MeterSample(
                value=None,
                unit=reading.unit,
                mode=reading.mode,
                status="overload" if reading.overload else "unavailable",
            )
        return MeterSample(
            value=reading.value,
            unit=reading.unit,
            mode=reading.mode,
        )
