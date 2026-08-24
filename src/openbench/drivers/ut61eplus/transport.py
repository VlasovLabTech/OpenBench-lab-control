from __future__ import annotations

import importlib
import time
from dataclasses import dataclass
from typing import Protocol, cast

from openbench.drivers.ut61eplus.protocol import REQUEST_READING, UT61EPlusProtocolError

CH9329_VID = 0x1A86
CH9329_PID = 0xE429
CH9329_USAGE_PAGE = 0xFFA0
CH9329_REPORT_SIZE = 65

CP2110_VID = 0x10C4
CP2110_PID = 0xEA80
CP2110_USAGE_PAGE = 0xFF00
CP2110_REPORT_SIZE = 64
CP2110_UART_CONFIG = bytes((0x50, 0x00, 0x00, 0x25, 0x80, 0x00, 0x00, 0x03, 0x00))
CP2110_PURGE_BOTH = bytes((0x43, 0x03))
CP2110_UART_ENABLE = bytes((0x41, 0x01))


class HidDevice(Protocol):
    def open_path(self, path: bytes) -> None: ...

    def write(self, data: bytes) -> int: ...

    def send_feature_report(self, data: bytes) -> int: ...

    def read(self, size: int, timeout_ms: int) -> list[int]: ...

    def close(self) -> None: ...


class HidModule(Protocol):
    def enumerate(self, vid: int, pid: int) -> list[dict[str, object]]: ...

    def device(self) -> HidDevice: ...


class UT61EPlusUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UT61EPlusDescriptor:
    path: bytes
    serial_number: str
    product_string: str
    vid: int
    pid: int
    transport: str = "ch9329"


def _load_hid() -> HidModule:
    try:
        return cast(HidModule, importlib.import_module("hid"))
    except ImportError as exc:
        raise UT61EPlusUnavailableError(
            "hidapi is required for UT61E+ support; install OpenBench with [hardware]"
        ) from exc


class CH9329HidTransport:
    def __init__(
        self,
        descriptor: UT61EPlusDescriptor,
        *,
        timeout_ms: int = 1500,
        hid_module: HidModule | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.timeout_ms = timeout_ms
        self._hid = hid_module or _load_hid()

    @classmethod
    def discover(
        cls,
        *,
        vid: int = CH9329_VID,
        pid: int = CH9329_PID,
        hid_module: HidModule | None = None,
    ) -> tuple[UT61EPlusDescriptor, ...]:
        backend = hid_module or _load_hid()
        descriptors: list[UT61EPlusDescriptor] = []
        for item in backend.enumerate(vid, pid):
            if item.get("usage_page") != CH9329_USAGE_PAGE:
                continue
            path = item.get("path")
            if not isinstance(path, bytes):
                continue
            descriptors.append(
                UT61EPlusDescriptor(
                    path=path,
                    serial_number=str(item.get("serial_number") or ""),
                    product_string=str(item.get("product_string") or "CH9329 HID bridge"),
                    vid=vid,
                    pid=pid,
                    transport="ch9329",
                )
            )
        return tuple(descriptors)

    def request_reading_frame(self) -> bytes:
        device = self._hid.device()
        device.open_path(self.descriptor.path)
        try:
            report = (
                bytes((0, len(REQUEST_READING)))
                + REQUEST_READING
                + bytes(CH9329_REPORT_SIZE - len(REQUEST_READING) - 2)
            )
            written = device.write(report)
            if written != CH9329_REPORT_SIZE:
                raise UT61EPlusProtocolError(
                    f"Short CH9329 HID write: expected {CH9329_REPORT_SIZE}, wrote {written}"
                )

            raw = bytes(device.read(CH9329_REPORT_SIZE - 1, self.timeout_ms))
            if not raw:
                raise TimeoutError("UT61E+ did not answer the reading request")

            if raw[0] == 0:
                if len(raw) < 2:
                    raise UT61EPlusProtocolError("Truncated CH9329 HID report")
                payload_length = raw[1]
                payload_offset = 2
            else:
                payload_length = raw[0]
                payload_offset = 1

            payload_end = payload_offset + payload_length
            if payload_length == 0 or payload_end > len(raw):
                raise UT61EPlusProtocolError("Invalid CH9329 HID payload length")
            return raw[payload_offset:payload_end]
        finally:
            device.close()


class CP2110HidTransport:
    def __init__(
        self,
        descriptor: UT61EPlusDescriptor,
        *,
        timeout_ms: int = 1500,
        hid_module: HidModule | None = None,
    ) -> None:
        if timeout_ms <= 0:
            raise ValueError("UT61E+ timeout must be positive")
        self.descriptor = descriptor
        self.timeout_ms = timeout_ms
        self._hid = hid_module or _load_hid()

    @classmethod
    def discover(
        cls,
        *,
        vid: int = CP2110_VID,
        pid: int = CP2110_PID,
        hid_module: HidModule | None = None,
    ) -> tuple[UT61EPlusDescriptor, ...]:
        backend = hid_module or _load_hid()
        descriptors: list[UT61EPlusDescriptor] = []
        for item in backend.enumerate(vid, pid):
            if item.get("usage_page") != CP2110_USAGE_PAGE:
                continue
            path = item.get("path")
            if not isinstance(path, bytes):
                continue
            descriptors.append(
                UT61EPlusDescriptor(
                    path=path,
                    serial_number=str(item.get("serial_number") or ""),
                    product_string=str(
                        item.get("product_string") or "CP2110 HID USB-to-UART Bridge"
                    ),
                    vid=vid,
                    pid=pid,
                    transport="cp2110",
                )
            )
        return tuple(descriptors)

    @staticmethod
    def _send_feature_report(device: HidDevice, report: bytes) -> None:
        sent = device.send_feature_report(report)
        if sent != len(report):
            raise UT61EPlusProtocolError(
                f"Short CP2110 feature report: expected {len(report)}, sent {sent}"
            )

    def _read_frame(self, device: HidDevice) -> bytes:
        deadline = time.monotonic() + self.timeout_ms / 1000
        pending = bytearray()
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise TimeoutError("UT61E+ did not answer through the CP2110 adapter")
            raw = bytes(device.read(CP2110_REPORT_SIZE, min(remaining_ms, 100)))
            if not raw:
                continue
            payload_length = raw[0]
            if payload_length == 0 or payload_length > CP2110_REPORT_SIZE - 1:
                raise UT61EPlusProtocolError(
                    f"Invalid CP2110 UART payload length: {payload_length}"
                )
            if payload_length >= len(raw):
                raise UT61EPlusProtocolError("Truncated CP2110 UART report")
            pending.extend(raw[1 : payload_length + 1])

            header_at = pending.find(b"\xab\xcd")
            if header_at < 0:
                if len(pending) > 2:
                    del pending[:-1]
                continue
            if header_at:
                del pending[:header_at]
            if len(pending) < 3:
                continue
            frame_size = pending[2] + 3
            if len(pending) >= frame_size:
                return bytes(pending[:frame_size])

    def request_reading_frame(self) -> bytes:
        device = self._hid.device()
        device.open_path(self.descriptor.path)
        try:
            self._send_feature_report(device, CP2110_UART_CONFIG)
            self._send_feature_report(device, CP2110_PURGE_BOTH)
            self._send_feature_report(device, CP2110_UART_ENABLE)

            report = bytes((len(REQUEST_READING),)) + REQUEST_READING
            written = device.write(report)
            if written < len(report):
                raise UT61EPlusProtocolError(
                    f"Short CP2110 HID write: expected {len(report)}, wrote {written}"
                )
            return self._read_frame(device)
        finally:
            device.close()


def discover_ut61eplus_descriptors(
    *,
    hid_module: HidModule | None = None,
) -> tuple[UT61EPlusDescriptor, ...]:
    backend = hid_module or _load_hid()
    return (
        *CH9329HidTransport.discover(hid_module=backend),
        *CP2110HidTransport.discover(hid_module=backend),
    )
