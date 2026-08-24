from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass
from typing import Any

from openbench.drivers.fnirsi_dps150.protocol import (
    BAUD_RATE_CODE,
    COMMAND_BAUD,
    COMMAND_SESSION,
    DEFAULT_BAUD_RATE,
    HEADER_OUTPUT,
    TYPE_ALL,
    build_get_packet,
    build_packet,
    build_set_byte_packet,
    build_set_float_packet,
    extract_packets,
)

DEFAULT_VID = 0x2E3C
DEFAULT_PID = 0x5740
DEFAULT_TIMEOUT_S = 1.0


class DPS150UnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DPS150Descriptor:
    port: str
    vid: int
    pid: int
    serial_number: str
    location: str
    description: str
    manufacturer: str


def _load_serial() -> tuple[Any, Any]:
    try:
        serial_module = importlib.import_module("serial")
        list_ports_module = importlib.import_module("serial.tools.list_ports")
    except ImportError as exc:
        raise DPS150UnavailableError(
            "pyserial is required for FNIRSI DPS-150 support; install OpenBench with [hardware]"
        ) from exc
    return serial_module, list_ports_module


class DPS150SerialTransport:
    def __init__(
        self,
        descriptor: DPS150Descriptor,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        serial_module: Any | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("DPS-150 serial timeout must be positive")
        self.descriptor = descriptor
        self.timeout_s = timeout_s
        self._serial = serial_module
        self._port: Any | None = None
        self._buffer = bytearray()
        self._lock = threading.Lock()

    @staticmethod
    def discover(
        *,
        vid: int = DEFAULT_VID,
        pid: int = DEFAULT_PID,
        list_ports_module: Any | None = None,
    ) -> tuple[DPS150Descriptor, ...]:
        if list_ports_module is None:
            _, list_ports_module = _load_serial()
        descriptors: list[DPS150Descriptor] = []
        for item in list_ports_module.comports():
            if getattr(item, "vid", None) != vid or getattr(item, "pid", None) != pid:
                continue
            descriptors.append(
                DPS150Descriptor(
                    port=str(item.device),
                    vid=vid,
                    pid=pid,
                    serial_number=str(getattr(item, "serial_number", None) or ""),
                    location=str(getattr(item, "location", None) or ""),
                    description=str(getattr(item, "description", None) or "USB serial"),
                    manufacturer=str(getattr(item, "manufacturer", None) or "FNIRSI"),
                )
            )
        return tuple(descriptors)

    def _write_locked(self, payload: bytes) -> None:
        port = self._open_locked()
        written = port.write(payload)
        if written != len(payload):
            raise OSError(f"DPS-150 {self.descriptor.port} accepted {written}/{len(payload)} bytes")
        port.flush()

    def _open_locked(self) -> Any:
        if self._port is not None and self._port.is_open:
            return self._port
        serial_module = self._serial
        if serial_module is None:
            serial_module, _ = _load_serial()
            self._serial = serial_module
        port = serial_module.Serial(
            port=self.descriptor.port,
            baudrate=DEFAULT_BAUD_RATE,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=min(0.1, self.timeout_s),
            write_timeout=self.timeout_s,
            rtscts=True,
        )
        self._port = port
        self._buffer.clear()
        self._write_locked(build_packet(COMMAND_SESSION, 0, b"\x01"))
        self._write_locked(build_packet(COMMAND_BAUD, 0, bytes((BAUD_RATE_CODE,))))
        time.sleep(0.05)
        return port

    def _read_type_locked(self, data_type: int) -> bytes:
        port = self._open_locked()
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            for packet in extract_packets(self._buffer):
                if packet.data_type == data_type:
                    return packet.payload
            waiting = int(getattr(port, "in_waiting", 0) or 0)
            chunk = bytes(port.read(waiting or 1))
            if chunk:
                self._buffer.extend(chunk)
                continue
            time.sleep(0.005)
        raise TimeoutError(f"DPS-150 {self.descriptor.port} did not answer type 0x{data_type:02X}")

    def query(self, data_type: int) -> bytes:
        with self._lock:
            try:
                port = self._open_locked()
                port.reset_input_buffer()
                self._buffer.clear()
                self._write_locked(build_get_packet(data_type))
                return self._read_type_locked(data_type)
            except Exception:
                self._close_locked()
                raise

    def write_float(self, data_type: int, value: float) -> None:
        with self._lock:
            try:
                self._write_locked(build_set_float_packet(data_type, value))
                time.sleep(0.05)
            except Exception:
                self._close_locked()
                raise

    def write_byte(self, data_type: int, value: int) -> None:
        with self._lock:
            try:
                self._write_locked(build_set_byte_packet(data_type, value))
                time.sleep(0.05)
            except Exception:
                self._close_locked()
                raise

    def _close_locked(self) -> None:
        port = self._port
        self._port = None
        self._buffer.clear()
        if port is None:
            return
        try:
            if port.is_open:
                payload = build_packet(
                    COMMAND_SESSION,
                    0,
                    b"\x00",
                    header=HEADER_OUTPUT,
                )
                port.write(payload)
                port.flush()
                time.sleep(0.05)
        finally:
            port.close()

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def probe(self) -> bytes:
        return self.query(TYPE_ALL)
