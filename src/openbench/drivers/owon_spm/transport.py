from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass
from typing import Any

from openbench.drivers.owon_spm.protocol import (
    DMM_AUTO_RANGE_FUNCTIONS,
    DMM_FUNCTIONS,
    DMM_RANGES,
    DMM_RELATIVE_FUNCTIONS,
    OwonSPMIdentity,
    parse_identity,
)

DEFAULT_VID = 0x1A86
DEFAULT_PID = 0x7523
DEFAULT_BAUD_RATE = 115200
DEFAULT_TIMEOUT_S = 0.6

READ_COMMANDS = {
    "*IDN?",
    "SYST:VERS?",
    "OUTP?",
    "VOLT?",
    "CURR?",
    "VOLT:LIM?",
    "CURR:LIM?",
    "MEAS:VOLT?",
    "MEAS:CURR?",
    "MEAS:POW?",
    "MEAS:ALL?",
    "MEAS:ALL:INFO?",
    "CONF?",
    "CONF:ALL?",
    "MULT:HOLD?",
}
WRITE_COMMANDS = {"SYST:REM", "SYST:LOC", "OUTP ON", "OUTP OFF"}
WRITE_PREFIXES = ("VOLT ", "CURR ", "VOLT:LIM ", "CURR:LIM ")
WRITE_COMMANDS.update(f"SENS:FUNC:{scpi}" for scpi, _ in DMM_FUNCTIONS.values())
WRITE_COMMANDS.update({"MULT:HOLD ON", "MULT:HOLD OFF"})
for function, (scpi, _) in DMM_FUNCTIONS.items():
    if function in DMM_RANGES or function == "capacitance":
        READ_COMMANDS.add(f"{scpi}:RANG?")
    if function in DMM_RELATIVE_FUNCTIONS:
        READ_COMMANDS.add(f"{scpi}:NULL?")
        WRITE_COMMANDS.update({f"{scpi}:NULL ON", f"{scpi}:NULL OFF"})
    if function in DMM_AUTO_RANGE_FUNCTIONS:
        READ_COMMANDS.add(f"{scpi}:RANG:AUTO?")
        WRITE_COMMANDS.update({f"{scpi}:RANG:AUTO ON", f"{scpi}:RANG:AUTO OFF"})
DMM_RANGE_WRITE_PREFIXES = tuple(f"{DMM_FUNCTIONS[function][0]}:RANG " for function in DMM_RANGES)


class OwonSPMUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OwonSPMDescriptor:
    port: str
    vid: int
    pid: int
    serial_number: str
    location: str
    description: str
    manufacturer: str
    identity: OwonSPMIdentity


def _load_serial() -> tuple[Any, Any]:
    try:
        serial_module = importlib.import_module("serial")
        list_ports_module = importlib.import_module("serial.tools.list_ports")
    except ImportError as exc:
        raise OwonSPMUnavailableError(
            "pyserial is required for OWON SPM support; install OpenBench with [hardware]"
        ) from exc
    return serial_module, list_ports_module


class OwonSPMSerialTransport:
    def __init__(
        self,
        descriptor: OwonSPMDescriptor,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        serial_module: Any | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("OWON SPM serial timeout must be positive")
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
        serial_module: Any | None = None,
    ) -> tuple[OwonSPMDescriptor, ...]:
        if list_ports_module is None or serial_module is None:
            loaded_serial, loaded_ports = _load_serial()
            serial_module = serial_module or loaded_serial
            list_ports_module = list_ports_module or loaded_ports
        descriptors: list[OwonSPMDescriptor] = []
        for item in list_ports_module.comports():
            port_name = str(item.device)
            if port_name in excluded_ports:
                continue
            if getattr(item, "vid", None) != vid or getattr(item, "pid", None) != pid:
                continue
            try:
                port = serial_module.Serial(
                    port=port_name,
                    baudrate=DEFAULT_BAUD_RATE,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    timeout=DEFAULT_TIMEOUT_S,
                    write_timeout=DEFAULT_TIMEOUT_S,
                )
                try:
                    port.reset_input_buffer()
                    port.write(b"*IDN?\n")
                    port.flush()
                    identity = parse_identity(
                        bytes(port.read_until(b"\n", 256)).strip().decode("ascii", errors="strict")
                    )
                finally:
                    port.close()
            except (OSError, TimeoutError, UnicodeError, ValueError):
                continue
            descriptors.append(
                OwonSPMDescriptor(
                    port=port_name,
                    vid=vid,
                    pid=pid,
                    serial_number=str(getattr(item, "serial_number", None) or ""),
                    location=str(getattr(item, "location", None) or ""),
                    description=str(getattr(item, "description", None) or "USB serial"),
                    manufacturer=str(getattr(item, "manufacturer", None) or "WCH.CN"),
                    identity=identity,
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
        self._port = port
        return port

    def _close_locked(self) -> None:
        port = self._port
        self._port = None
        if port is not None:
            port.close()

    def query(self, command: str) -> str:
        normalized = command.strip().upper()
        if normalized not in READ_COMMANDS:
            raise ValueError(f"Unsupported or non-read-only OWON SPM command: {command}")
        with self._lock:
            port = self._open_locked()
            try:
                port.reset_input_buffer()
                port.write(f"{normalized}\n".encode("ascii"))
                port.flush()
                response = bytes(port.read_until(b"\n", 512)).strip()
                if not response:
                    raise TimeoutError(
                        f"OWON SPM {self.descriptor.port} did not answer {normalized}"
                    )
                return response.decode("ascii", errors="strict")
            except Exception:
                self._close_locked()
                raise

    def write(self, command: str) -> None:
        normalized = command.strip().upper()
        if normalized not in WRITE_COMMANDS and not normalized.startswith(
            (*WRITE_PREFIXES, *DMM_RANGE_WRITE_PREFIXES)
        ):
            raise ValueError(f"Unsupported OWON SPM write command: {command}")
        with self._lock:
            port = self._open_locked()
            try:
                port.write(f"{normalized}\n".encode("ascii"))
                port.flush()
                time.sleep(0.03)
            except Exception:
                self._close_locked()
                raise

    def close(self) -> None:
        with self._lock:
            self._close_locked()
