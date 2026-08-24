from __future__ import annotations

import asyncio
import re
from typing import Protocol

from openbench.core.capabilities import MeterSample
from openbench.drivers.ut197.protocol import (
    UT197Reading,
    ac_dc_command,
    auto_hold_command,
    backlight_off_command,
    counts_command,
    frequency_command,
    hold_command,
    max_min_command,
    parse_reading_frame,
    peak_max_min_command,
    range_command,
    relative_command,
    select_command,
)
from openbench.drivers.ut197.transport import UT197BleTransport, UT197Descriptor


class UT197Transport(Protocol):
    async def request_reading_frame(self) -> bytes: ...

    async def send_control(self, command: bytes) -> None: ...

    async def close(self) -> None: ...


class UT197Meter:
    def __init__(
        self,
        descriptor: UT197Descriptor,
        *,
        response_timeout_s: float = 2.0,
        transport: UT197Transport | None = None,
    ) -> None:
        address = re.sub(r"[^a-z0-9]+", "_", descriptor.address.lower()).strip("_")
        self.device_id = f"ut197_{address or 'unknown'}"
        self.channel_id = f"{self.device_id}.voltage"
        self.descriptor = descriptor
        self._transport = transport or UT197BleTransport(
            descriptor,
            response_timeout_s=response_timeout_s,
        )
        self._read_lock = asyncio.Lock()
        self.last_reading: UT197Reading | None = None

    async def identify(self) -> str:
        return f"UNI-T {self.descriptor.name} BLE ({self.descriptor.address})"

    async def read_meter(self, channel_id: str) -> MeterSample:
        if channel_id != self.channel_id:
            raise ValueError(f"Unknown UT197 channel: {channel_id}")

        async with self._read_lock:
            frame = await self._transport.request_reading_frame()
            reading = parse_reading_frame(frame)
            self.last_reading = reading

        if reading.value is None:
            if reading.function == "NCV" and not reading.overload:
                status = "no_signal"
            elif reading.function in {"oC", "oF"} and reading.overload:
                status = "open_input"
            else:
                status = "overload" if reading.overload else "unavailable"
            return MeterSample(
                value=None,
                unit=reading.unit or ("NCV" if reading.function == "NCV" else ""),
                mode=reading.function,
                status=status,
            )
        return MeterSample(
            value=reading.value,
            unit=reading.unit,
            mode=reading.function,
        )

    async def select(self, index: int) -> None:
        await self._transport.send_control(select_command(index))

    async def set_auto_range(self) -> None:
        await self._transport.send_control(range_command(automatic=True))

    async def next_manual_range(self) -> None:
        await self._transport.send_control(range_command(automatic=False))

    async def toggle_relative(self) -> None:
        await self._transport.send_control(relative_command())

    async def set_max_min(self, *, enabled: bool) -> None:
        await self._transport.send_control(max_min_command(enabled=enabled))

    async def toggle_auto_hold(self) -> None:
        await self._transport.send_control(auto_hold_command())

    async def toggle_hold(self) -> None:
        await self._transport.send_control(hold_command())

    async def set_peak_max_min(self, *, enabled: bool) -> None:
        await self._transport.send_control(peak_max_min_command(enabled=enabled))

    async def toggle_frequency(self) -> None:
        await self._transport.send_control(frequency_command())

    async def turn_backlight_off(self) -> None:
        await self._transport.send_control(backlight_off_command())

    async def set_ac_dc(self, *, enabled: bool) -> None:
        await self._transport.send_control(ac_dc_command(enabled=enabled))

    async def toggle_counts(self) -> None:
        await self._transport.send_control(counts_command())

    async def close(self) -> None:
        await self._transport.close()
