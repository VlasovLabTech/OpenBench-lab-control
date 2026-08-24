from __future__ import annotations

from typing import cast

import pytest

from openbench.drivers.ut61eplus import (
    CH9329HidTransport,
    CP2110HidTransport,
    UT61EPlusDescriptor,
    UT61EPlusMeter,
    UT61EPlusProtocolError,
    discover_ut61eplus_descriptors,
    parse_reading_frame,
)
from openbench.drivers.ut61eplus.protocol import REQUEST_READING
from openbench.drivers.ut61eplus.transport import (
    CP2110_PURGE_BOTH,
    CP2110_UART_CONFIG,
    CP2110_UART_ENABLE,
    HidModule,
)

REAL_DCV_FRAME = bytes.fromhex("AB CD 10 02 30 20 30 2E 30 30 30 32 00 03 30 30 30 03 8D")


class FakeHidDevice:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.path: bytes | None = None
        self.written = b""
        self.closed = False

    def open_path(self, path: bytes) -> None:
        self.path = path

    def write(self, data: bytes) -> int:
        self.written = data
        return len(data)

    def send_feature_report(self, data: bytes) -> int:
        del data
        return 0

    def read(self, size: int, timeout_ms: int) -> list[int]:
        del size, timeout_ms
        return list(bytes((len(self.response),)) + self.response + bytes(44))

    def close(self) -> None:
        self.closed = True


class FakeHidModule:
    def __init__(self, device: FakeHidDevice) -> None:
        self._device = device

    def enumerate(self, vid: int, pid: int) -> list[dict[str, object]]:
        return [
            {
                "path": b"fake-path",
                "serial_number": "2019A95204EA",
                "product_string": "WCH UART TO KB-MS_V1.7",
                "usage_page": 0xFFA0,
                "vendor_id": vid,
                "product_id": pid,
            }
        ]

    def device(self) -> FakeHidDevice:
        return self._device


class FakeCP2110Device:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.path: bytes | None = None
        self.written = b""
        self.feature_reports: list[bytes] = []
        self.closed = False

    def open_path(self, path: bytes) -> None:
        self.path = path

    def write(self, data: bytes) -> int:
        self.written = bytes(data)
        return len(data)

    def send_feature_report(self, data: bytes) -> int:
        self.feature_reports.append(bytes(data))
        return len(data)

    def read(self, size: int, timeout_ms: int) -> list[int]:
        assert size == 64
        assert timeout_ms > 0
        if not self.response:
            return []
        response = self.response
        self.response = b""
        return list(bytes((len(response),)) + response)

    def close(self) -> None:
        self.closed = True


class FakeMultiHidModule:
    def __init__(self, device: FakeCP2110Device) -> None:
        self._device = device

    def enumerate(self, vid: int, pid: int) -> list[dict[str, object]]:
        if (vid, pid) == (0x1A86, 0xE429):
            return [
                {
                    "path": b"ch9329-path",
                    "serial_number": "CH9329-A",
                    "product_string": "WCH UART TO KB-MS_V1.7",
                    "usage_page": 0xFFA0,
                }
            ]
        if (vid, pid) == (0x10C4, 0xEA80):
            return [
                {
                    "path": b"cp2110-path",
                    "serial_number": "006FF91A",
                    "product_string": "CP2110 HID USB-to-UART Bridge",
                    "usage_page": 0xFF00,
                }
            ]
        return []

    def device(self) -> FakeCP2110Device:
        return self._device


class StaticTransport:
    def request_reading_frame(self) -> bytes:
        return REAL_DCV_FRAME


def test_parse_real_ut61eplus_dcv_frame() -> None:
    reading = parse_reading_frame(REAL_DCV_FRAME)

    assert reading.mode == "DCV"
    assert reading.range_code == 0
    assert reading.display == "0.0002"
    assert reading.display_unit == "V"
    assert reading.value == pytest.approx(0.0002)
    assert reading.unit == "V"
    assert reading.auto_range is True
    assert reading.is_voltage is True


def test_rejects_bad_ut61eplus_checksum() -> None:
    damaged = REAL_DCV_FRAME[:-1] + b"\x00"

    with pytest.raises(UT61EPlusProtocolError, match="checksum mismatch"):
        parse_reading_frame(damaged)


def test_ch9329_transport_wraps_and_unwraps_hid_report() -> None:
    fake_device = FakeHidDevice(REAL_DCV_FRAME)
    fake_module = FakeHidModule(fake_device)
    descriptors = CH9329HidTransport.discover(
        hid_module=cast(HidModule, fake_module),
    )
    transport = CH9329HidTransport(
        descriptors[0],
        hid_module=cast(HidModule, fake_module),
    )

    frame = transport.request_reading_frame()

    assert frame == REAL_DCV_FRAME
    assert len(fake_device.written) == 65
    assert fake_device.written[:8] == bytes.fromhex("00 06 AB CD 03 5E 01 D9")
    assert fake_device.closed is True


def test_cp2110_transport_configures_9600_8n1_and_reads() -> None:
    fake_device = FakeCP2110Device(REAL_DCV_FRAME)
    fake_module = FakeMultiHidModule(fake_device)
    descriptors = CP2110HidTransport.discover(
        hid_module=cast(HidModule, fake_module),
    )
    transport = CP2110HidTransport(
        descriptors[0],
        hid_module=cast(HidModule, fake_module),
    )

    frame = transport.request_reading_frame()

    assert frame == REAL_DCV_FRAME
    assert descriptors[0].transport == "cp2110"
    assert fake_device.path == b"cp2110-path"
    assert fake_device.feature_reports == [
        CP2110_UART_CONFIG,
        CP2110_PURGE_BOTH,
        CP2110_UART_ENABLE,
    ]
    assert fake_device.written == bytes((len(REQUEST_READING),)) + REQUEST_READING
    assert fake_device.closed is True


def test_discovery_returns_both_ut61eplus_adapter_families() -> None:
    fake_module = FakeMultiHidModule(FakeCP2110Device(REAL_DCV_FRAME))

    descriptors = discover_ut61eplus_descriptors(
        hid_module=cast(HidModule, fake_module),
    )

    assert [(item.transport, item.serial_number) for item in descriptors] == [
        ("ch9329", "CH9329-A"),
        ("cp2110", "006FF91A"),
    ]


@pytest.mark.asyncio
async def test_multiple_ut61eplus_adapters_have_separate_device_ids() -> None:
    first = UT61EPlusMeter(
        UT61EPlusDescriptor(
            path=b"first-no-serial",
            serial_number="",
            product_string="CP2110",
            vid=0x10C4,
            pid=0xEA80,
            transport="cp2110",
        ),
        transport=StaticTransport(),
    )
    second = UT61EPlusMeter(
        UT61EPlusDescriptor(
            path=b"second-no-serial",
            serial_number="",
            product_string="CP2110",
            vid=0x10C4,
            pid=0xEA80,
            transport="cp2110",
        ),
        transport=StaticTransport(),
    )

    first_sample = await first.read_meter(first.channel_id)
    second_sample = await second.read_meter(second.channel_id)

    assert first.device_id != second.device_id
    assert first.channel_id != second.channel_id
    assert first_sample.value == pytest.approx(0.0002)
    assert second_sample.value == pytest.approx(0.0002)
