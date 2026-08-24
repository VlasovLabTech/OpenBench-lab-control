from __future__ import annotations

import importlib
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from openbench.drivers.itech_it6000c.protocol import ITechIT6000CIdentity, parse_identity

DEFAULT_VID = 0x2EC7
DEFAULT_PID = 0xA4A7
BAUD_RATES = (115200, 9600)
DEFAULT_TIMEOUT_S = 1.5
QUERY_ATTEMPTS = 2
QUERY_RETRY_DELAY_S = 1.6
QUERY_PACING_S = 0.02

READ_COMMANDS = frozenset(
    {
        "*IDN?",
        "SYST:VERS?",
        "OUTP?",
        "FUNC?",
        "FUNC:MODE?",
        "VOLT?",
        "CURR?",
        "CURR:LIM?",
        "CURR:LIM:NEG?",
        "VOLT:LIM?",
        "VOLT:LIM:NEG?",
        "POW:LIM?",
        "POW:LIM:NEG?",
        "VOLT:SLEW:POS?",
        "VOLT:SLEW:NEG?",
        "CURR:SLEW:POS?",
        "CURR:SLEW:NEG?",
        "VOLT:PROT:STAT?",
        "VOLT:PROT?",
        "VOLT:PROT:DEL?",
        "CURR:PROT:STAT?",
        "CURR:PROT?",
        "CURR:PROT:DEL?",
        "POW:PROT:STAT?",
        "POW:PROT?",
        "POW:PROT:DEL?",
        "VOLT:UND:PROT:STAT?",
        "VOLT:UND:PROT?",
        "VOLT:UND:PROT:DEL?",
        "VOLT:UND:PROT:WARM?",
        "CURR:UND:PROT:STAT?",
        "CURR:UND:PROT?",
        "CURR:UND:PROT:DEL?",
        "CURR:UND:PROT:WARM?",
        "OUTP:DEL:RISE?",
        "OUTP:DEL:FALL?",
        "OUTP:PROT:WDOG:STAT?",
        "OUTP:PROT:WDOG:DEL?",
        "SINK:RES:STAT?",
        "SYST:VOLT:RZERO?",
        "STAT:QUES:COND?",
        "STAT:OPER:COND?",
        "MEAS:VOLT?",
        "MEAS:CURR?",
        "MEAS:POW?",
    }
)
WRITE_EXACT = frozenset(
    {
        "SYST:REM",
        "SYST:LOC",
        "OUTP ON",
        "OUTP OFF",
        "OUTP:PROT:CLE",
        "FUNC VOLT",
        "FUNC CURR",
        "FUNC:MODE FIXED",
        "VOLT:PROT:STAT ON",
        "VOLT:PROT:STAT OFF",
        "CURR:PROT:STAT ON",
        "CURR:PROT:STAT OFF",
        "POW:PROT:STAT ON",
        "POW:PROT:STAT OFF",
        "VOLT:UND:PROT:STAT ON",
        "VOLT:UND:PROT:STAT OFF",
        "CURR:UND:PROT:STAT ON",
        "CURR:UND:PROT:STAT OFF",
        "OUTP:PROT:WDOG:STAT ON",
        "OUTP:PROT:WDOG:STAT OFF",
    }
)
WRITE_PREFIXES = (
    "VOLT ",
    "CURR ",
    "CURR:LIM ",
    "CURR:LIM:NEG ",
    "VOLT:LIM ",
    "VOLT:LIM:NEG ",
    "POW:LIM ",
    "POW:LIM:NEG ",
    "VOLT:SLEW:POS ",
    "VOLT:SLEW:NEG ",
    "CURR:SLEW:POS ",
    "CURR:SLEW:NEG ",
    "VOLT:PROT ",
    "VOLT:PROT:DEL ",
    "CURR:PROT ",
    "CURR:PROT:DEL ",
    "POW:PROT ",
    "POW:PROT:DEL ",
    "VOLT:UND:PROT ",
    "VOLT:UND:PROT:DEL ",
    "VOLT:UND:PROT:WARM ",
    "CURR:UND:PROT ",
    "CURR:UND:PROT:DEL ",
    "CURR:UND:PROT:WARM ",
    "OUTP:DEL:RISE ",
    "OUTP:DEL:FALL ",
    "OUTP:PROT:WDOG:DEL ",
)


class ITechIT6000CUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ITechIT6000CDescriptor:
    port: str
    vid: int
    pid: int
    usb_serial_number: str
    location: str
    description: str
    identity: ITechIT6000CIdentity
    baud_rate: int


def _load_serial() -> tuple[Any, Any]:
    try:
        serial_module = importlib.import_module("serial")
        list_ports_module = importlib.import_module("serial.tools.list_ports")
    except ImportError as exc:
        raise ITechIT6000CUnavailableError(
            "pyserial is required for ITECH IT6000C support"
        ) from exc
    return serial_module, list_ports_module


def _force_release_windows_ni_claims() -> None:
    if os.name != "nt":
        return
    for service in ("niLXIDiscovery", "nimDNSResponder", "NiSvcLoc"):
        subprocess.run(
            ["sc.exe", "stop", service],
            check=False,
            capture_output=True,
            timeout=3,
        )
    for image in ("nimxs.exe", "nisvcloc.exe", "nimdnsResponder.exe", "niLxiDiscovery.exe"):
        subprocess.run(
            ["taskkill.exe", "/F", "/IM", image],
            check=False,
            capture_output=True,
            timeout=3,
        )
    time.sleep(0.25)


def _looks_like_port_claim_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "errno", None) in {5, 13}:
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "access is denied",
            "permission denied",
            "permissionerror",
            "could not open port",
        )
    )


class ITechIT6000CSerialTransport:
    def __init__(
        self,
        descriptor: ITechIT6000CDescriptor,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        serial_module: Any | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("ITECH serial timeout must be positive")
        self.descriptor = descriptor
        self.timeout_s = timeout_s
        self._serial = serial_module
        self._port: Any | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _probe(serial_module: Any, port_name: str, baud_rate: int) -> ITechIT6000CIdentity:
        port = serial_module.Serial(
            port=port_name,
            baudrate=baud_rate,
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
            response = bytes(port.read_until(b"\n", 512)).strip()
            if not response:
                raise TimeoutError(f"ITECH {port_name} did not answer at {baud_rate} baud")
            return parse_identity(response.decode("ascii", errors="strict"))
        finally:
            port.close()

    @classmethod
    def discover(
        cls,
        *,
        list_ports_module: Any | None = None,
        serial_module: Any | None = None,
        release_port: Any | None = None,
    ) -> tuple[ITechIT6000CDescriptor, ...]:
        if list_ports_module is None or serial_module is None:
            loaded_serial, loaded_ports = _load_serial()
            serial_module = serial_module or loaded_serial
            list_ports_module = list_ports_module or loaded_ports
        candidates = tuple(
            item
            for item in list_ports_module.comports()
            if getattr(item, "vid", None) == DEFAULT_VID
            and getattr(item, "pid", None) == DEFAULT_PID
        )
        found: list[ITechIT6000CDescriptor] = []
        released = False
        for item in candidates:
            port_name = str(item.device)
            identity: ITechIT6000CIdentity | None = None
            selected_baud = 0
            for baud_rate in BAUD_RATES:
                try:
                    identity = cls._probe(serial_module, port_name, baud_rate)
                    selected_baud = baud_rate
                    break
                except (OSError, TimeoutError, UnicodeError, ValueError) as exc:
                    if released or not _looks_like_port_claim_error(exc):
                        continue
                    (release_port or _force_release_windows_ni_claims)()
                    released = True
                    try:
                        identity = cls._probe(serial_module, port_name, baud_rate)
                        selected_baud = baud_rate
                        break
                    except (OSError, TimeoutError, UnicodeError, ValueError):
                        continue
            if identity is None:
                continue
            found.append(
                ITechIT6000CDescriptor(
                    port=port_name,
                    vid=DEFAULT_VID,
                    pid=DEFAULT_PID,
                    usb_serial_number=str(getattr(item, "serial_number", None) or ""),
                    location=str(getattr(item, "location", None) or ""),
                    description=str(
                        getattr(item, "description", None) or "USB Serial Device"
                    ),
                    identity=identity,
                    baud_rate=selected_baud,
                )
            )
        return tuple(found)

    def _open_locked(self) -> Any:
        if self._port is not None and self._port.is_open:
            return self._port
        serial_module = self._serial
        if serial_module is None:
            serial_module, _ = _load_serial()
            self._serial = serial_module
        self._port = serial_module.Serial(
            port=self.descriptor.port,
            baudrate=self.descriptor.baud_rate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.timeout_s,
            write_timeout=self.timeout_s,
        )
        return self._port

    def _close_locked(self) -> None:
        port, self._port = self._port, None
        if port is not None:
            port.close()

    def query(self, command: str) -> str:
        normalized = command.strip().upper()
        if normalized not in READ_COMMANDS:
            raise ValueError(f"Unsupported or non-read-only ITECH command: {command}")
        with self._lock:
            for attempt in range(QUERY_ATTEMPTS):
                port = self._open_locked()
                try:
                    port.reset_input_buffer()
                    port.write(f"{normalized}\n".encode("ascii"))
                    port.flush()
                    response = bytes(port.read_until(b"\n", 512)).strip()
                    if not response:
                        raise TimeoutError(
                            f"ITECH {self.descriptor.port} did not answer {normalized}"
                        )
                    decoded = response.decode("ascii", errors="strict")
                    time.sleep(QUERY_PACING_S)
                    return decoded
                except Exception:
                    self._close_locked()
                    if attempt + 1 >= QUERY_ATTEMPTS:
                        raise
                    time.sleep(QUERY_RETRY_DELAY_S)
            raise AssertionError("unreachable")

    def write(self, command: str) -> None:
        normalized = " ".join(command.strip().upper().split())
        if normalized not in WRITE_EXACT and not normalized.startswith(WRITE_PREFIXES):
            raise ValueError(f"Unsupported ITECH write command: {command}")
        with self._lock:
            port = self._open_locked()
            try:
                port.write(f"{normalized}\n".encode("ascii"))
                port.flush()
                time.sleep(0.02)
            except Exception:
                self._close_locked()
                raise

    def close(self) -> None:
        with self._lock:
            self._close_locked()
