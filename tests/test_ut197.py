from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

import pytest

from openbench.drivers.ut197 import (
    NOTIFY_UUID,
    READ_READING_COMMAND,
    WRITE_UUID,
    UT197BleTransport,
    UT197Descriptor,
    UT197Meter,
    UT197ProtocolError,
    build_command,
    parse_reading_frame,
)
from openbench.drivers.ut197.protocol import (
    ac_dc_command,
    hold_command,
    max_min_command,
    range_command,
    select_command,
)

REAL_ACMV_FRAME = bytes.fromhex(
    "AB CD 00 23 02 41 43 6D 56 20 20 36 30 30 20 00 00 00 00 "
    "20 20 30 32 33 2E 30 34 6D 56 20 20 30 30 30 38 30 30 07 6C"
)


def reading_frame(function: str, display: str, unit: str) -> bytes:
    frame = bytearray(REAL_ACMV_FRAME)
    frame[5:11] = function.encode("ascii").ljust(6)
    frame[19:27] = display.encode("ascii").rjust(8)
    frame[27:31] = unit.encode("ascii").ljust(4)
    frame[-2:] = (sum(frame[:-2]) & 0xFFFF).to_bytes(2, "big")
    return bytes(frame)


class StaticTransport:
    def __init__(self, frame: bytes) -> None:
        self.frame = frame

    async def request_reading_frame(self) -> bytes:
        return self.frame

    async def send_control(self, command: bytes) -> None:
        del command

    async def close(self) -> None:
        return None


class FakeBleDevice:
    name = "UT197"
    address = "02:00:00:00:00:02"


class FakeBleakScanner:
    @staticmethod
    async def discover(**kwargs: float) -> list[FakeBleDevice]:
        assert kwargs["timeout"] > 0
        return [FakeBleDevice()]


class FakeBleakClient:
    instances: ClassVar[list[FakeBleakClient]] = []

    def __init__(
        self,
        device: object,
        disconnected_callback: Callable[[object], None],
        timeout: float,
        pair: bool,
    ) -> None:
        self.device = device
        self.disconnected_callback = disconnected_callback
        self.timeout = timeout
        self.pair = pair
        self.is_connected = False
        self.notification_callback: Callable[[object, bytearray], None] | None = None
        self.writes: list[tuple[str, bytes, bool]] = []
        self.stopped_notifications: list[str] = []
        self.__class__.instances.append(self)

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def start_notify(
        self,
        uuid: str,
        callback: Callable[[object, bytearray], None],
    ) -> None:
        assert uuid == NOTIFY_UUID
        self.notification_callback = callback

    async def stop_notify(self, uuid: str) -> None:
        self.stopped_notifications.append(uuid)

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool) -> None:
        self.writes.append((uuid, bytes(data), response))
        if data == READ_READING_COMMAND:
            assert self.notification_callback is not None
            payload = REAL_ACMV_FRAME + b"\x00\x00"
            self.notification_callback(object(), bytearray(payload[:17]))
            self.notification_callback(object(), bytearray(payload[17:]))


class FakeBleakModule:
    BleakScanner = FakeBleakScanner
    BleakClient = FakeBleakClient


def test_parse_real_ut197_acmv_frame() -> None:
    reading = parse_reading_frame(REAL_ACMV_FRAME)

    assert reading.function == "ACmV"
    assert reading.range_primary == "600"
    assert reading.range_secondary == ""
    assert reading.display == "023.04"
    assert reading.display_unit == "mV"
    assert reading.value == pytest.approx(0.02304)
    assert reading.unit == "V"
    assert reading.auto_range is False
    assert reading.hold is False
    assert reading.relative is False
    assert reading.battery_warning is False
    assert reading.is_voltage is True


def test_rejects_bad_ut197_checksum() -> None:
    damaged = REAL_ACMV_FRAME[:-1] + b"\x00"

    with pytest.raises(UT197ProtocolError, match="checksum mismatch"):
        parse_reading_frame(damaged)


def test_builds_ut197_commands_and_checksums() -> None:
    assert build_command(0x05, 0) == READ_READING_COMMAND
    assert select_command(4) == bytes.fromhex("AB CD 00 04 01 04 01 81")
    assert range_command(automatic=True) == bytes.fromhex("AB CD 00 04 02 00 01 7E")
    assert range_command(automatic=False) == bytes.fromhex("AB CD 00 04 02 01 01 7F")
    assert hold_command() == bytes.fromhex("AB CD 00 04 12 5A 01 E8")
    assert max_min_command(enabled=True) == bytes.fromhex("AB CD 00 04 04 01 01 81")
    assert ac_dc_command(enabled=False) == bytes.fromhex("AB CD 00 04 19 00 01 95")


@pytest.mark.asyncio
async def test_ut197_transport_discovers_reads_and_closes() -> None:
    FakeBleakClient.instances.clear()
    descriptors = await UT197BleTransport.discover(
        timeout_s=0.01,
        bleak_module=FakeBleakModule,
    )
    transport = UT197BleTransport(
        descriptors[0],
        bleak_module=FakeBleakModule,
    )

    frame = await transport.request_reading_frame()
    await transport.close()

    assert frame == REAL_ACMV_FRAME
    assert descriptors[0].address == "02:00:00:00:00:02"
    client = FakeBleakClient.instances[-1]
    assert client.writes == [(WRITE_UUID, READ_READING_COMMAND, False)]
    assert client.stopped_notifications == [NOTIFY_UUID]
    assert client.is_connected is False


@pytest.mark.asyncio
async def test_ut197_meter_returns_si_voltage() -> None:
    descriptors = await UT197BleTransport.discover(
        timeout_s=0.01,
        bleak_module=FakeBleakModule,
    )
    transport = UT197BleTransport(
        descriptors[0],
        bleak_module=FakeBleakModule,
    )
    meter = UT197Meter(descriptors[0], transport=transport)

    sample = await meter.read_meter(meter.channel_id)
    await meter.close()

    assert sample.value == pytest.approx(0.02304)
    assert sample.unit == "V"
    assert sample.mode == "ACmV"
    assert sample.status == "ok"
    assert meter.last_reading is not None
    assert meter.last_reading.function == "ACmV"


@pytest.mark.asyncio
async def test_ut197_meter_accepts_non_voltage_mode() -> None:
    descriptor = UT197Descriptor(name="UT197", address="02:00:00:00:00:02", device=object())
    meter = UT197Meter(
        descriptor,
        transport=StaticTransport(reading_frame("OHM", "12.345", "ko")),
    )

    sample = await meter.read_meter(meter.channel_id)

    assert sample.value == pytest.approx(12345)
    assert sample.unit == "Ω"
    assert sample.mode == "OHM"
    assert sample.status == "ok"


@pytest.mark.asyncio
async def test_ut197_ncv_without_numeric_signal_is_not_invalid() -> None:
    descriptor = UT197Descriptor(name="UT197", address="02:00:00:00:00:02", device=object())
    meter = UT197Meter(
        descriptor,
        transport=StaticTransport(reading_frame("NCV", "EF", "")),
    )

    sample = await meter.read_meter(meter.channel_id)

    assert sample.value is None
    assert sample.unit == "NCV"
    assert sample.mode == "NCV"
    assert sample.status == "no_signal"


@pytest.mark.asyncio
async def test_ut197_open_temperature_probe_is_neutral() -> None:
    frame = bytearray(reading_frame("oC", "OL", "oC"))
    frame[31] = ord("1")
    frame[-2:] = (sum(frame[:-2]) & 0xFFFF).to_bytes(2, "big")
    descriptor = UT197Descriptor(name="UT197", address="02:00:00:00:00:02", device=object())
    meter = UT197Meter(descriptor, transport=StaticTransport(bytes(frame)))

    sample = await meter.read_meter(meter.channel_id)

    assert sample.value is None
    assert sample.unit == "°C"
    assert sample.mode == "oC"
    assert sample.status == "open_input"


def test_fake_module_satisfies_dynamic_bleak_surface() -> None:
    module: Any = FakeBleakModule
    assert module.BleakScanner is FakeBleakScanner
