from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

REQUEST_READING = bytes.fromhex("AB CD 03 5E 01 D9")

MODE_NAMES = (
    "ACV",
    "ACmV",
    "DCV",
    "DCmV",
    "Hz",
    "%",
    "OHM",
    "CONT",
    "DIODE",
    "CAP",
    "°C",
    "°F",
    "DCuA",
    "ACuA",
    "DCmA",
    "ACmA",
    "DCA",
    "ACA",
    "HFE",
    "Live",
    "NCV",
    "LozV",
    "ACA",
    "DCA",
    "LPF",
    "AC/DC",
    "LPF",
    "AC+DC",
    "LPF",
    "AC+DC2",
    "INRUSH",
)

VOLTAGE_MODE_CODES = frozenset({0, 1, 2, 3, 21, 24, 25, 26, 27, 28, 29})
OVERLOAD_DISPLAYS = frozenset({".OL", "O.L", "OL.", "OL", "-.OL", "-O.L", "-OL.", "-OL"})

DISPLAY_UNITS: dict[str, tuple[str, ...]] = {
    "ACV": ("V", "V", "V", "V"),
    "ACmV": ("mV",),
    "DCV": ("V", "V", "V", "V"),
    "DCmV": ("mV",),
    "Hz": ("Hz", "Hz", "kHz", "kHz", "kHz", "MHz", "MHz", "MHz"),
    "%": ("%",),
    "OHM": ("Ω", "kΩ", "kΩ", "kΩ", "MΩ", "MΩ", "MΩ"),
    "CONT": ("Ω",),
    "DIODE": ("V",),
    "CAP": ("nF", "nF", "uF", "uF", "uF", "mF", "mF", "mF"),
    "°C": ("°C", "°C"),
    "°F": ("°F", "°F"),
    "DCuA": ("uA", "uA"),
    "ACuA": ("uA", "uA"),
    "DCmA": ("mA", "mA"),
    "ACmA": ("mA", "mA"),
    "DCA": ("A", "A"),
    "ACA": ("A", "A"),
    "LozV": ("V", "V", "V", "V"),
    "LPF": ("V", "V", "V", "V"),
    "AC/DC": ("V", "V", "V", "V"),
    "AC+DC": ("V", "A"),
    "AC+DC2": ("V", "A"),
}

SI_PREFIXES = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
}


class UT61EPlusProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UT61EPlusReading:
    raw_frame: bytes
    mode_code: int
    mode: str
    range_code: int
    display: str
    display_unit: str
    value: float | None
    unit: str
    overload: bool
    auto_range: bool
    hold: bool
    relative: bool
    battery_warning: bool
    high_voltage_warning: bool

    @property
    def is_voltage(self) -> bool:
        return self.mode_code in VOLTAGE_MODE_CODES and self.unit == "V"


def _unit(mode: str, range_code: int) -> str:
    choices = DISPLAY_UNITS.get(mode, ())
    if not choices:
        return ""
    return choices[min(range_code, len(choices) - 1)]


def _to_si(display: str, display_unit: str) -> tuple[float | None, str]:
    if display in OVERLOAD_DISPLAYS:
        return None, display_unit.lstrip("numkM")
    try:
        value = Decimal(display)
    except InvalidOperation as exc:
        raise UT61EPlusProtocolError(f"Invalid UT61E+ display value: {display!r}") from exc

    if display_unit and display_unit[0] in SI_PREFIXES:
        value *= SI_PREFIXES[display_unit[0]]
        unit = display_unit[1:]
    else:
        unit = display_unit
    return float(value), unit


def parse_reading_frame(frame: bytes) -> UT61EPlusReading:
    if len(frame) < 5 or frame[:2] != b"\xab\xcd":
        raise UT61EPlusProtocolError("Invalid UT61E+ frame header")

    declared_length = frame[2]
    if len(frame) != declared_length + 3:
        raise UT61EPlusProtocolError(
            f"UT61E+ frame length mismatch: declared {declared_length}, got {len(frame) - 3}"
        )

    expected_checksum = int.from_bytes(frame[-2:], "big")
    actual_checksum = sum(frame[:-2]) & 0xFFFF
    if actual_checksum != expected_checksum:
        raise UT61EPlusProtocolError(
            f"UT61E+ checksum mismatch: expected 0x{expected_checksum:04X}, "
            f"calculated 0x{actual_checksum:04X}"
        )

    body = frame[3:-2]
    if len(body) != 14:
        raise UT61EPlusProtocolError(f"Unexpected UT61E+ reading payload length: {len(body)}")

    mode_code = body[0]
    mode = MODE_NAMES[mode_code] if mode_code < len(MODE_NAMES) else f"0x{mode_code:02X}"
    range_code = body[1] & 0x0F
    try:
        display = body[2:9].decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise UT61EPlusProtocolError("UT61E+ display is not ASCII") from exc

    display_unit = _unit(mode, range_code)
    overload = display in OVERLOAD_DISPLAYS
    value, unit = _to_si(display, display_unit)

    return UT61EPlusReading(
        raw_frame=frame,
        mode_code=mode_code,
        mode=mode,
        range_code=range_code,
        display=display,
        display_unit=display_unit,
        value=value,
        unit=unit,
        overload=overload,
        auto_range=body[12] & 0x04 == 0,
        hold=body[11] & 0x02 != 0,
        relative=body[11] & 0x01 != 0,
        battery_warning=body[12] & 0x02 != 0,
        high_voltage_warning=body[12] & 0x01 != 0,
    )
