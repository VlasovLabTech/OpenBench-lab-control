from __future__ import annotations

from typing import cast

import pytest

from openbench.drivers.ut61e import (
    CH9325HidTransport,
    UT61EDescriptor,
    UT61EMeter,
    UT61EProtocolError,
    parse_reading_frame,
)
from openbench.drivers.ut61e.transport import (
    CH9325_2400_REPORT,
    CH9325_19200_REPORT,
    HidModule,
)
from openbench.drivers.ut61e.ut61d_protocol import parse_ut61d_reading_frame

REAL_OVERLOAD_FRAME = b"622580330020\r\n"
DCV_FRAME = b"012345;000:0\r\n"
UT61D_DCV_FRAME = b"+0753 2" + bytes((0x31, 0x00, 0x00, 0x80, 0x87)) + b"\r\n"


def with_parity(frame: bytes) -> bytes:
    return bytes(value | 0x80 for value in frame)


class FakeHidDevice:
    def __init__(self, frame: bytes, *, parity: bool = True) -> None:
        encoded = with_parity(frame) if parity else frame
        self.reports = [
            bytes(8),
            bytes((0xF7,)) + encoded[:7],
            bytes((0xF7,)) + encoded[7:],
        ]
        self.path: bytes | None = None
        self.feature_report = b""
        self.closed = False

    def open_path(self, path: bytes) -> None:
        self.path = path

    def send_feature_report(self, data: bytes) -> int:
        self.feature_report = bytes(data)
        return len(data)

    def read(self, size: int, timeout_ms: int) -> list[int]:
        assert size == 8
        assert timeout_ms > 0
        if not self.reports:
            return []
        report = self.reports.pop(0)
        if report == bytes(8):
            return list(bytes((0xF0,)) + bytes(7))
        return list(report)

    def close(self) -> None:
        self.closed = True


class FakeHidModule:
    def __init__(self, device: FakeHidDevice) -> None:
        self._device = device

    def enumerate(self, vid: int, pid: int) -> list[dict[str, object]]:
        return [
            {
                "path": b"fake-ut-d04",
                "serial_number": "",
                "product_string": "USB to Serial",
                "manufacturer_string": "WCH.CN",
                "usage_page": 0xFFA0,
                "vendor_id": vid,
                "product_id": pid,
            }
        ]

    def device(self) -> FakeHidDevice:
        return self._device


class StaticTransport:
    def __init__(self, frame: bytes) -> None:
        self.frame = frame
        self.closed = False

    def request_reading_frame(self) -> bytes:
        return self.frame

    def close(self) -> None:
        self.closed = True


def descriptor() -> UT61EDescriptor:
    return UT61EDescriptor(
        path=b"fake-ut-d04",
        serial_number="",
        product_string="USB to Serial",
        manufacturer_string="WCH.CN",
        vid=0x1A86,
        pid=0xE008,
    )


def test_parse_real_ut61e_overload_frame() -> None:
    reading = parse_reading_frame(REAL_OVERLOAD_FRAME)

    assert reading.mode == "OHM"
    assert reading.value is None
    assert reading.unit == "Ω"
    assert reading.overload is True
    assert reading.auto_range is True
    assert reading.battery_warning is True


def test_parse_ut61e_dcv_frame_to_si_units() -> None:
    reading = parse_reading_frame(DCV_FRAME)

    assert reading.mode == "DCV"
    assert reading.value == pytest.approx(1.2345)
    assert reading.unit == "V"
    assert reading.overload is False
    assert reading.dc is True
    assert reading.auto_range is True


def test_parse_real_ut61d_dcv_frame_to_si_units() -> None:
    reading = parse_ut61d_reading_frame(UT61D_DCV_FRAME)

    assert reading.mode == "DCV"
    assert reading.value == pytest.approx(7.53)
    assert reading.unit == "V"
    assert reading.overload is False
    assert reading.dc is True
    assert reading.auto_range is True


@pytest.mark.parametrize(
    ("frame", "mode", "value", "unit"),
    (
        (
            b"+0123 4" + bytes((0x28, 0x00, 0x40, 0x80, 0x00)) + b"\r\n",
            "ACV",
            0.0123,
            "V",
        ),
        (
            b"+1234 0" + bytes((0x20, 0x00, 0x20, 0x20, 0x00)) + b"\r\n",
            "OHM",
            1_234_000.0,
            "Ω",
        ),
        (
            b"+1234 2" + bytes((0x20, 0x00, 0x80, 0x04, 0x00)) + b"\r\n",
            "CAP",
            12.34e-6,
            "F",
        ),
        (
            b"+0239 4" + bytes((0x00, 0x00, 0x00, 0x02, 0x00)) + b"\r\n",
            "°C",
            23.9,
            "°C",
        ),
    ),
)
def test_parse_ut61d_measurement_modes(
    frame: bytes,
    mode: str,
    value: float,
    unit: str,
) -> None:
    reading = parse_ut61d_reading_frame(frame)

    assert reading.mode == mode
    assert reading.value == pytest.approx(value)
    assert reading.unit == unit


def test_parse_ut61d_overload() -> None:
    frame = b"+?0:? 0" + bytes((0x20, 0x00, 0x00, 0x20, 0x00)) + b"\r\n"

    reading = parse_ut61d_reading_frame(frame)

    assert reading.mode == "OHM"
    assert reading.value is None
    assert reading.overload is True


@pytest.mark.parametrize(
    ("frame", "mode", "value", "unit"),
    (
        (b"112345?00060\r\n", "ACmA", 0.12345, "A"),
        (b"212345300020\r\n", "OHM", 12345.0, "Ω"),
        (b"412345200000\r\n", "Hz", 123450.0, "Hz"),
        (b"012345280000\r\n", "%", 1234.5, "%"),
        (b"512345600000\r\n", "CAP", 0.0012345, "F"),
        (b"012345;400:2\r\n", "DCV", -1.2345, "V"),
    ),
)
def test_parse_ut61e_measurement_modes(
    frame: bytes,
    mode: str,
    value: float,
    unit: str,
) -> None:
    reading = parse_reading_frame(frame)

    assert reading.mode == mode
    assert reading.value == pytest.approx(value)
    assert reading.unit == unit
    assert reading.hold is (frame == b"012345;400:2\r\n")


def test_rejects_malformed_ut61e_frame() -> None:
    with pytest.raises(UT61EProtocolError, match="terminator"):
        parse_reading_frame(DCV_FRAME[:-2] + b"xx")


def test_ch9325_transport_discovers_configures_and_reads() -> None:
    fake_device = FakeHidDevice(DCV_FRAME)
    fake_module = FakeHidModule(fake_device)
    descriptors = CH9325HidTransport.discover(
        hid_module=cast(HidModule, fake_module),
    )
    transport = CH9325HidTransport(
        descriptors[0],
        hid_module=cast(HidModule, fake_module),
    )

    frame = transport.request_reading_frame()
    transport.close()

    assert frame == DCV_FRAME
    assert descriptors[0].product_string == "USB to Serial"
    assert fake_device.path == b"fake-ut-d04"
    assert fake_device.feature_report == CH9325_19200_REPORT
    assert fake_device.closed is True


def test_ch9325_transport_uses_ut61d_2400_baud_profile() -> None:
    fake_device = FakeHidDevice(UT61D_DCV_FRAME, parity=False)
    fake_module = FakeHidModule(fake_device)
    transport = CH9325HidTransport(
        descriptor(),
        baud_rate=2400,
        hid_module=cast(HidModule, fake_module),
    )

    assert transport.request_reading_frame() == UT61D_DCV_FRAME
    assert fake_device.feature_report == CH9325_2400_REPORT


@pytest.mark.asyncio
async def test_ut61e_meter_returns_dynamic_sample_and_closes() -> None:
    transport = StaticTransport(DCV_FRAME)
    meter = UT61EMeter(descriptor(), transport=transport)

    sample = await meter.read_meter(meter.channel_id)
    await meter.close()

    assert meter.device_id.startswith("ut61e_")
    assert sample.value == pytest.approx(1.2345)
    assert sample.unit == "V"
    assert sample.mode == "DCV"
    assert sample.status == "ok"
    assert transport.closed is True


@pytest.mark.asyncio
async def test_original_ut61d_uses_shared_stream_protocol_with_its_own_model() -> None:
    transport = StaticTransport(UT61D_DCV_FRAME)
    meter = UT61EMeter(descriptor(), model="UT61D", transport=transport)

    sample = await meter.read_meter(meter.channel_id)
    identity = await meter.identify()

    assert meter.model == "UT61D"
    assert meter.baud_rate == 2400
    assert meter.device_id.startswith("ut61d_")
    assert identity.startswith("UNI-T UT61D via")
    assert sample.value == pytest.approx(7.53)
    assert sample.mode == "DCV"


def test_original_ut61_meter_rejects_unknown_model() -> None:
    with pytest.raises(ValueError, match="Unsupported original UT61-series model"):
        UT61EMeter(descriptor(), model="UT61C", transport=StaticTransport(DCV_FRAME))


def test_multiple_ut61e_adapters_have_separate_device_ids_without_serials() -> None:
    first_descriptor = descriptor()
    second_descriptor = UT61EDescriptor(
        path=b"second-ut-d04",
        serial_number="",
        product_string="USB to Serial",
        manufacturer_string="WCH.CN",
        vid=0x1A86,
        pid=0xE008,
    )

    first = UT61EMeter(first_descriptor, transport=StaticTransport(DCV_FRAME))
    second = UT61EMeter(second_descriptor, transport=StaticTransport(DCV_FRAME))

    assert first.device_id != second.device_id
    assert first.channel_id != second.channel_id
