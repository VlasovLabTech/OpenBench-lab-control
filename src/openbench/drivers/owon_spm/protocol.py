from __future__ import annotations

import math
import re
from dataclasses import dataclass


class OwonSPMProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OwonSPMIdentity:
    manufacturer: str
    model: str
    serial_number: str
    firmware_version: str


@dataclass(frozen=True, slots=True)
class OwonSPMSourceState:
    set_voltage_v: float
    set_current_a: float
    output_voltage_v: float
    output_current_a: float
    output_power_w: float
    output_enabled: bool
    over_voltage_v: float
    over_current_a: float
    over_voltage_fault: bool
    over_current_fault: bool
    over_temperature_fault: bool
    mode: str


@dataclass(frozen=True, slots=True)
class OwonSPMDMMState:
    function: str
    value: float | None
    unit: str
    range_mode: str
    range_label: str
    status: str = "ok"
    range_value: float | None = None
    relative_enabled: bool = False
    hold_enabled: bool = False


@dataclass(frozen=True, slots=True)
class OwonSPMState:
    source: OwonSPMSourceState
    multimeter: OwonSPMDMMState


@dataclass(frozen=True, slots=True)
class OwonSPMOutputUpdate:
    voltage_v: float | None = None
    current_a: float | None = None
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class OwonSPMProtectionUpdate:
    over_voltage_v: float | None = None
    over_current_a: float | None = None


@dataclass(frozen=True, slots=True)
class OwonSPMDMMUpdate:
    function: str | None = None
    range_mode: str | None = None
    range_value: float | None = None
    relative_enabled: bool | None = None
    hold_enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class OwonSPMMeterParameter:
    key: str
    channel_suffix: str
    name: str
    unit: str
    capability: str


OWON_SPM_PARAMETERS = (
    OwonSPMMeterParameter(
        "set_voltage", "set_voltage", "Source voltage setpoint", "V", "voltage_setpoint"
    ),
    OwonSPMMeterParameter(
        "set_current", "set_current", "Source current limit", "A", "current_limit"
    ),
    OwonSPMMeterParameter(
        "output_voltage", "output_voltage", "Source output voltage", "V", "live_voltage"
    ),
    OwonSPMMeterParameter(
        "output_current", "output_current", "Source output current", "A", "live_current"
    ),
    OwonSPMMeterParameter("output_power", "output_power", "Source output power", "W", "live_power"),
    OwonSPMMeterParameter("output", "output", "Source output state", "state", "output_state"),
    OwonSPMMeterParameter("mode", "mode", "Source regulation mode", "mode", "regulation_mode"),
    OwonSPMMeterParameter(
        "protection", "protection", "Source protection state", "state", "protections"
    ),
    OwonSPMMeterParameter(
        "dmm", "dmm", "Multimeter primary display", "reading", "multimeter_reading"
    ),
)


DMM_FUNCTIONS = {
    "dc_voltage": ("VOLT:DC", "V"),
    "ac_voltage": ("VOLT:AC", "V"),
    "dc_current": ("CURR:DC", "A"),
    "ac_current": ("CURR:AC", "A"),
    "resistance": ("RES", "Ohm"),
    "capacitance": ("CAP", "F"),
    "diode": ("DIOD", "V"),
    "continuity": ("CONT", "Ohm"),
}
DMM_RANGES = {
    "dc_voltage": (0.2, 2.0, 20.0, 200.0, 1000.0),
    "ac_voltage": (0.2, 2.0, 20.0, 200.0, 750.0),
    "dc_current": (0.2, 10.0),
    "ac_current": (0.2, 10.0),
    "resistance": (200.0, 2e3, 20e3, 200e3, 2e6, 20e6, 100e6),
}
DMM_AUTO_RANGE_FUNCTIONS = frozenset({"dc_voltage", "ac_voltage", "resistance"})
DMM_RELATIVE_FUNCTIONS = frozenset(
    {
        "dc_voltage",
        "ac_voltage",
        "dc_current",
        "ac_current",
        "resistance",
        "capacitance",
    }
)
_DMM_BY_SCPI = {scpi: (name, unit) for name, (scpi, unit) in DMM_FUNCTIONS.items()}
_VALUE_RE = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)([A-Za-z\u03a9]*)$")
_UNIT_SCALE = {
    "V": ("V", 1.0),
    "mV": ("V", 1e-3),
    "uV": ("V", 1e-6),
    "A": ("A", 1.0),
    "mA": ("A", 1e-3),
    "uA": ("A", 1e-6),
    "F": ("F", 1.0),
    "mF": ("F", 1e-3),
    "uF": ("F", 1e-6),
    "nF": ("F", 1e-9),
    "pF": ("F", 1e-12),
    "OHM": ("Ohm", 1.0),
    "Ohm": ("Ohm", 1.0),
    "kOHM": ("Ohm", 1e3),
    "kOhm": ("Ohm", 1e3),
    "MOHM": ("Ohm", 1e6),
    "MOhm": ("Ohm", 1e6),
}


def parse_identity(response: str) -> OwonSPMIdentity:
    parts = tuple(part.strip() for part in response.strip().split(","))
    if len(parts) != 4 or not all(parts):
        raise OwonSPMProtocolError(f"Malformed OWON identification: {response!r}")
    manufacturer, model, serial_number, firmware = parts
    if manufacturer.casefold() != "owon" or model.upper() != "SPM6103":
        raise OwonSPMProtocolError(f"Not an OWON SPM instrument: {response!r}")
    return OwonSPMIdentity(manufacturer, model.upper(), serial_number, firmware)


def parse_float(response: str, field: str) -> float:
    try:
        value = float(response.strip())
    except ValueError as exc:
        raise OwonSPMProtocolError(f"Malformed OWON {field}: {response!r}") from exc
    if not math.isfinite(value):
        raise OwonSPMProtocolError(f"Non-finite OWON {field}: {response!r}")
    return value


def parse_on_off(response: str, field: str = "state") -> bool:
    normalized = response.strip().upper()
    if normalized in {"ON", "1"}:
        return True
    if normalized in {"OFF", "0"}:
        return False
    raise OwonSPMProtocolError(f"Malformed OWON {field}: {response!r}")


def parse_measurement_info(response: str) -> tuple[float, float, float, bool, bool, bool, str]:
    parts = tuple(part.strip() for part in response.strip().split(","))
    if len(parts) != 7:
        raise OwonSPMProtocolError(f"Malformed OWON measurement info: {response!r}")
    modes = {"0": "standby", "1": "CV", "2": "CC", "3": "fault"}
    if parts[6] not in modes:
        raise OwonSPMProtocolError(f"Unknown OWON regulation mode: {parts[6]!r}")
    return (
        parse_float(parts[0], "output voltage"),
        parse_float(parts[1], "output current"),
        parse_float(parts[2], "output power"),
        parse_on_off(parts[3], "OVP fault"),
        parse_on_off(parts[4], "OCP fault"),
        parse_on_off(parts[5], "OTP fault"),
        modes[parts[6]],
    )


def parse_dmm_state(response: str) -> OwonSPMDMMState:
    parts = tuple(part.strip() for part in response.strip().split(","))
    if len(parts) != 4:
        raise OwonSPMProtocolError(f"Malformed OWON DMM response: {response!r}")
    scpi_function = parts[0].upper()
    try:
        function, default_unit = _DMM_BY_SCPI[scpi_function]
    except KeyError as exc:
        raise OwonSPMProtocolError(f"Unknown OWON DMM function: {parts[0]!r}") from exc
    raw_value = parts[1].strip()
    normalized = raw_value.upper()
    if normalized in {"OL", "OVERLOAD", "INF", "+INF", "-INF"}:
        value = None
        unit = default_unit
        status = "overload"
    else:
        match = _VALUE_RE.fullmatch(raw_value)
        if match is None:
            raise OwonSPMProtocolError(f"Malformed OWON DMM value: {raw_value!r}")
        value = parse_float(match.group(1), "DMM value")
        suffix = match.group(2)
        if suffix == "\u03a9":
            unit, scale = "Ohm", 1.0
        elif suffix in {"k\u03a9", "K\u03a9"}:
            unit, scale = "Ohm", 1e3
        elif suffix == "M\u03a9":
            unit, scale = "Ohm", 1e6
        else:
            try:
                unit, scale = _UNIT_SCALE[suffix] if suffix else (default_unit, 1.0)
            except KeyError as exc:
                raise OwonSPMProtocolError(f"Unknown OWON DMM unit: {suffix!r}") from exc
        value *= scale
        if abs(value) >= 9e36:
            value = None
            status = "overload"
        else:
            status = "ok"
    return OwonSPMDMMState(
        function=function,
        value=value,
        unit=unit,
        range_mode=parts[2].upper(),
        range_label=parts[3],
        status=status,
    )
