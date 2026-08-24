from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass
from typing import Any

from openbench.drivers.feeltech_fy.protocol import READ_COMMANDS, validate_write_command

DEFAULT_VID = 0x1A86
DEFAULT_PID = 0x7523
DEFAULT_BAUD_RATE = 115200
DEFAULT_TIMEOUT_S = 0.45
MODEL_TIMEOUT_S = 1.2


class FeelTechUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FeelTechDescriptor:
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
        raise FeelTechUnavailableError(
            "pyserial is required for FeelTech FY-series support; install OpenBench with [hardware]"
        ) from exc
    return serial_module, list_ports_module


class FeelTechSerialTransport:
    def __init__(
        self,
        descriptor: FeelTechDescriptor,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        serial_module: Any | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("FeelTech serial timeout must be positive")
        self.descriptor = descriptor
        self.timeout_s = timeout_s
        self._serial = serial_module
        self._port: Any | None = None
        self._lock = threading.Lock()

    @staticmethod
    def discover(
        *,
        vid: int = DEFAULT_VID,
        pid: int = DEFAULT_PID,
        excluded_ports: frozenset[str] = frozenset(),
        list_ports_module: Any | None = None,
    ) -> tuple[FeelTechDescriptor, ...]:
        if list_ports_module is None:
            _, list_ports_module = _load_serial()
        descriptors: list[FeelTechDescriptor] = []
        for item in list_ports_module.comports():
            if str(item.device) in excluded_ports:
                continue
            if getattr(item, "vid", None) != vid or getattr(item, "pid", None) != pid:
                continue
            descriptors.append(
                FeelTechDescriptor(
                    port=str(item.device),
                    vid=vid,
                    pid=pid,
                    serial_number=str(getattr(item, "serial_number", None) or ""),
                    location=str(getattr(item, "location", None) or ""),
                    description=str(getattr(item, "description", None) or "USB serial"),
                    manufacturer=str(getattr(item, "manufacturer", None) or "WCH.CN"),
                )
            )
        return tuple(descriptors)

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
            timeout=self.timeout_s,
            write_timeout=self.timeout_s,
        )
        port.dtr = True
        port.rts = False
        time.sleep(0.5)
        self._port = port
        return port

    def _close_locked(self) -> None:
        port = self._port
        self._port = None
        if port is not None:
            port.close()

    def query(self, command: str) -> str:
        if command not in READ_COMMANDS:
            raise ValueError(f"Unsupported or non-read-only FeelTech command: {command}")
        with self._lock:
            port = self._open_locked()
            try:
                # Some FY6200 firmware revisions sporadically miss a request
                # while the USB-UART bridge is busy. A third bounded attempt is
                # cheap in the exceptional path and avoids reporting a failed
                # write after the instrument has already accepted it.
                attempts = 3
                for attempt in range(attempts):
                    port.reset_input_buffer()
                    port.timeout = MODEL_TIMEOUT_S if command == "UMO" else self.timeout_s
                    port.write(f"{command}\n".encode("ascii"))
                    port.flush()
                    response = bytes(port.read_until(b"\n", 128)).strip()
                    if response:
                        return response.decode("ascii", errors="strict")
                    if attempt + 1 < attempts:
                        time.sleep(0.08)
                raise TimeoutError(f"FeelTech {self.descriptor.port} did not answer {command}")
            except Exception:
                self._close_locked()
                raise

    def write(self, command: str) -> None:
        validate_write_command(command)
        with self._lock:
            port = self._open_locked()
            try:
                port.reset_input_buffer()
                port.write(f"{command}\n".encode("ascii"))
                port.flush()
                # FY6200 firmware variants do not consistently send the LF
                # acknowledgement documented for later FY-series models.
                # Every state-changing driver call performs an explicit
                # parameter read-back instead.
                time.sleep(0.03)
            except Exception:
                self._close_locked()
                raise

    def close(self) -> None:
        with self._lock:
            self._close_locked()
