from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from openbench.drivers.ut61e.protocol import (
    FRAME_SIZE,
    FRAME_TERMINATOR,
    UT61EProtocolError,
    is_plausible_frame,
)
from openbench.drivers.ut61e.ut61d_protocol import is_plausible_ut61d_frame

DEFAULT_VID = 0x1A86
DEFAULT_PID = 0xE008
CUSTOM_HID_USAGE_PAGE = 0xFFA0
HID_REPORT_SIZE = 8
MAX_UART_BYTES_PER_REPORT = 7
DEFAULT_TIMEOUT_MS = 1500

# Report ID 0, baud little-endian, format fields 0/0, 8 captured bits.
CH9325_2400_REPORT = bytes((0x00, 0x60, 0x09, 0x00, 0x00, 0x03))
CH9325_19200_REPORT = bytes((0x00, 0x00, 0x4B, 0x00, 0x00, 0x03))
CH9325_CONFIGURATION_REPORTS = {
    2400: CH9325_2400_REPORT,
    19200: CH9325_19200_REPORT,
}


class HidDevice(Protocol):
    def open_path(self, path: bytes) -> None: ...

    def send_feature_report(self, data: bytes) -> int: ...

    def read(self, size: int, timeout_ms: int) -> list[int]: ...

    def close(self) -> None: ...


class HidModule(Protocol):
    def enumerate(self, vid: int, pid: int) -> list[dict[str, object]]: ...

    def device(self) -> HidDevice: ...


class UT61EUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UT61EDescriptor:
    path: bytes
    serial_number: str
    product_string: str
    manufacturer_string: str
    vid: int
    pid: int


class _FrameBuffer:
    def __init__(self, validator: Callable[[bytes], bool]) -> None:
        self._data = bytearray()
        self._validator = validator

    def clear(self) -> None:
        self._data.clear()

    def feed(self, data: bytes) -> tuple[bytes, ...]:
        self._data.extend(data)
        frames: list[bytes] = []
        while True:
            terminator_at = self._data.find(FRAME_TERMINATOR)
            if terminator_at < 0:
                if len(self._data) > FRAME_SIZE * 2:
                    del self._data[: -FRAME_SIZE + 1]
                break
            packet_end = terminator_at + len(FRAME_TERMINATOR)
            if packet_end >= FRAME_SIZE:
                candidate = bytes(self._data[packet_end - FRAME_SIZE : packet_end])
                if self._validator(candidate):
                    frames.append(candidate)
            del self._data[:packet_end]
        return tuple(frames)


def _load_hid() -> HidModule:
    try:
        return cast(HidModule, importlib.import_module("hid"))
    except ImportError as exc:
        raise UT61EUnavailableError(
            "hidapi is required for UT61D/UT61E support; install OpenBench with [hardware]"
        ) from exc


class CH9325HidTransport:
    def __init__(
        self,
        descriptor: UT61EDescriptor,
        *,
        baud_rate: int = 19200,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        hid_module: HidModule | None = None,
    ) -> None:
        if timeout_ms <= 0:
            raise ValueError("UT61D/UT61E timeout must be positive")
        if baud_rate not in CH9325_CONFIGURATION_REPORTS:
            raise ValueError(f"Unsupported CH9325 baud rate: {baud_rate}")
        self.descriptor = descriptor
        self.baud_rate = baud_rate
        self.timeout_ms = timeout_ms
        self._hid = hid_module or _load_hid()
        self._device: HidDevice | None = None
        validator = is_plausible_ut61d_frame if baud_rate == 2400 else is_plausible_frame
        self._buffer = _FrameBuffer(validator)
        self._lock = threading.Lock()

    @classmethod
    def discover(
        cls,
        *,
        vid: int = DEFAULT_VID,
        pid: int = DEFAULT_PID,
        hid_module: HidModule | None = None,
    ) -> tuple[UT61EDescriptor, ...]:
        backend = hid_module or _load_hid()
        descriptors: list[UT61EDescriptor] = []
        for item in backend.enumerate(vid, pid):
            if item.get("usage_page") != CUSTOM_HID_USAGE_PAGE:
                continue
            path = item.get("path")
            if not isinstance(path, bytes):
                continue
            descriptors.append(
                UT61EDescriptor(
                    path=path,
                    serial_number=str(item.get("serial_number") or ""),
                    product_string=str(item.get("product_string") or "USB to Serial"),
                    manufacturer_string=str(item.get("manufacturer_string") or "WCH.CN"),
                    vid=vid,
                    pid=pid,
                )
            )
        return tuple(descriptors)

    def _open_locked(self) -> HidDevice:
        if self._device is not None:
            return self._device
        device = self._hid.device()
        try:
            device.open_path(self.descriptor.path)
            configuration = CH9325_CONFIGURATION_REPORTS[self.baud_rate]
            sent = device.send_feature_report(configuration)
            if sent != len(configuration):
                raise OSError(
                    f"Short CH9325 configuration report: expected {len(configuration)}, sent {sent}"
                )
        except Exception:
            device.close()
            raise
        self._device = device
        return device

    def _close_locked(self) -> None:
        device = self._device
        self._device = None
        self._buffer.clear()
        if device is not None:
            device.close()

    def _read_frame_locked(self, device: HidDevice) -> bytes:
        deadline = time.monotonic() + self.timeout_ms / 1000
        report_count = 0
        uart_byte_count = 0
        uart_sample = bytearray()
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                sample = uart_sample.hex(" ") or "none"
                raise TimeoutError(
                    "UT61D/UT61E did not produce a complete measurement frame "
                    f"(HID reports: {report_count}, UART bytes: {uart_byte_count}, "
                    f"sample: {sample})"
                )
            raw = bytes(device.read(HID_REPORT_SIZE, min(remaining_ms, 100)))
            if not raw:
                continue
            report_count += 1
            if len(raw) != HID_REPORT_SIZE:
                raise UT61EProtocolError(f"Unexpected CH9325 HID report length: {len(raw)}")
            marker = raw[0]
            if marker & 0xF0 != 0xF0:
                raise UT61EProtocolError(f"Invalid CH9325 HID byte-count marker: 0x{marker:02X}")
            count = marker & 0x0F
            if count > MAX_UART_BYTES_PER_REPORT:
                raise UT61EProtocolError(f"Invalid CH9325 UART payload length: {count}")
            if not count:
                continue
            uart_data = bytes(raw[1 : 1 + count])
            if self.baud_rate == 19200:
                uart_data = bytes(value & 0x7F for value in uart_data)
            uart_byte_count += len(uart_data)
            if len(uart_sample) < 32:
                uart_sample.extend(uart_data[: 32 - len(uart_sample)])
            frames = self._buffer.feed(uart_data)
            if frames:
                return frames[0]

    def request_reading_frame(self) -> bytes:
        with self._lock:
            device = self._open_locked()
            try:
                return self._read_frame_locked(device)
            except Exception:
                self._close_locked()
                raise

    def close(self) -> None:
        with self._lock:
            self._close_locked()
