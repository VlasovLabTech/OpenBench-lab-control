from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

FRAME_HEADER = b"\xab\xcd"
READ_READING_COMMAND = bytes.fromhex("AB CD 00 04 05 00 01 81")

VOLTAGE_FUNCTIONS = frozenset(
    {
        "ACV",
        "ACmV",
        "DCV",
        "DCmV",
        "Lo_DCV",
        "Lo_ACV",
        "ACDCV",
        "ACDCmV",
        "VFD",
        "DCV_L",
        "ACV_L",
        "DCmV_L",
        "ACmV_L",
    }
)

SI_PREFIXES = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
}

UNIT_ALIASES = {
    "o": "Ω",
    "ko": "kΩ",
    "Mo": "MΩ",
    "oC": "°C",
    "oF": "°F",
    "KVA": "kVA",
    "KVAR": "kVAr",
}

PREFIXABLE_UNITS = frozenset({"V", "A", "Ω", "F", "Hz", "W", "VA", "VAr"})
NON_NUMERIC_FUNCTIONS = frozenset({"NCV"})


class UT197ProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UT197Reading:
    raw_frame: bytes
    function: str
    range_primary: str
    range_secondary: str
    display: str
    display_unit: str
    value: float | None
    unit: str
    overload: bool
    max_min_status: int
    peak_max_min_status: int
    auto_range: bool
    hold: bool
    relative: bool
    battery_warning: bool
    red_led: bool
    backlight: bool
    auto_hold: bool
    high_voltage_warning: bool
    reserved_status: int

    @property
    def is_voltage(self) -> bool:
        return self.function in VOLTAGE_FUNCTIONS and self.unit == "V"


def build_command(command: int, argument: int) -> bytes:
    if not 0 <= command <= 0xFF:
        raise ValueError("UT197 command must fit in one byte")
    if not 0 <= argument <= 0xFF:
        raise ValueError("UT197 command argument must fit in one byte")
    prefix = FRAME_HEADER + b"\x00\x04" + bytes((command, argument))
    return prefix + (sum(prefix) & 0xFFFF).to_bytes(2, "big")


def select_command(index: int) -> bytes:
    if not 0 <= index <= 4:
        raise ValueError("UT197 SELECT index must be between 0 and 4")
    return build_command(0x01, index)


def range_command(*, automatic: bool) -> bytes:
    return build_command(0x02, 0 if automatic else 1)


def relative_command() -> bytes:
    return build_command(0x03, 0x5A)


def max_min_command(*, enabled: bool) -> bytes:
    return build_command(0x04, int(enabled))


def auto_hold_command() -> bytes:
    return build_command(0x11, 0x5A)


def hold_command() -> bytes:
    return build_command(0x12, 0x5A)


def peak_max_min_command(*, enabled: bool) -> bytes:
    return build_command(0x16, int(enabled))


def frequency_command() -> bytes:
    return build_command(0x17, 0x5A)


def backlight_off_command() -> bytes:
    return build_command(0x18, 0x5A)


def ac_dc_command(*, enabled: bool) -> bytes:
    return build_command(0x19, int(enabled))


def counts_command() -> bytes:
    return build_command(0x1A, 0x5A)


def _ascii_field(frame: bytes, start: int, end: int, name: str) -> str:
    try:
        return frame[start:end].replace(b"\x00", b" ").decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise UT197ProtocolError(f"UT197 {name} field is not ASCII") from exc


def _status_nibble(value: int, name: str) -> int:
    try:
        return int(chr(value), 16)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UT197ProtocolError(f"Invalid UT197 {name} status byte: 0x{value:02X}") from exc


def _normalize_unit(display_unit: str) -> str:
    return UNIT_ALIASES.get(display_unit, display_unit.replace("u", "µ", 1))


def _to_si(
    display: str,
    display_unit: str,
    overload: bool,
    function: str,
) -> tuple[float | None, str]:
    normalized_unit = _normalize_unit(display_unit)
    if overload or "OL" in display.upper():
        return None, normalized_unit

    try:
        value = Decimal(display)
    except InvalidOperation as exc:
        if function in NON_NUMERIC_FUNCTIONS:
            return None, normalized_unit
        raise UT197ProtocolError(f"Invalid UT197 display value: {display!r}") from exc

    ascii_unit = display_unit
    if len(ascii_unit) > 1 and ascii_unit[0] in SI_PREFIXES:
        remainder = _normalize_unit(ascii_unit[1:])
        if remainder in PREFIXABLE_UNITS:
            return float(value * SI_PREFIXES[ascii_unit[0]]), remainder
    return float(value), normalized_unit


def parse_reading_frame(frame: bytes) -> UT197Reading:
    if len(frame) < 8 or frame[:2] != FRAME_HEADER:
        raise UT197ProtocolError("Invalid UT197 frame header")

    declared_length = int.from_bytes(frame[2:4], "big")
    expected_length = declared_length + 4
    if len(frame) != expected_length:
        raise UT197ProtocolError(
            f"UT197 frame length mismatch: declared {declared_length}, got {len(frame) - 4}"
        )

    expected_checksum = int.from_bytes(frame[-2:], "big")
    actual_checksum = sum(frame[:-2]) & 0xFFFF
    if actual_checksum != expected_checksum:
        raise UT197ProtocolError(
            f"UT197 checksum mismatch: expected 0x{expected_checksum:04X}, "
            f"calculated 0x{actual_checksum:04X}"
        )

    if frame[4] != 0x02:
        raise UT197ProtocolError(f"Unexpected UT197 response type: 0x{frame[4]:02X}")
    if len(frame) != 39:
        raise UT197ProtocolError(f"Unexpected UT197 reading frame length: {len(frame)}")

    function = _ascii_field(frame, 5, 11, "function")
    range_primary = _ascii_field(frame, 11, 15, "primary range")
    range_secondary = _ascii_field(frame, 15, 19, "secondary range")
    display = _ascii_field(frame, 19, 27, "display")
    display_unit = _ascii_field(frame, 27, 31, "unit")

    overload_status = _status_nibble(frame[31], "overload")
    max_min_status = _status_nibble(frame[32], "MAX/MIN")
    peak_max_min_status = _status_nibble(frame[33], "PEAK MAX/MIN")
    flags = _status_nibble(frame[34], "primary flags")
    secondary_flags = _status_nibble(frame[35], "secondary flags")
    reserved_status = _status_nibble(frame[36], "reserved")
    overload = overload_status != 0
    value, unit = _to_si(display, display_unit, overload, function)

    return UT197Reading(
        raw_frame=frame,
        function=function,
        range_primary=range_primary,
        range_secondary=range_secondary,
        display=display,
        display_unit=_normalize_unit(display_unit),
        value=value,
        unit=unit,
        overload=overload,
        max_min_status=max_min_status,
        peak_max_min_status=peak_max_min_status,
        auto_range=flags & 0x08 == 0,
        hold=flags & 0x04 != 0,
        relative=flags & 0x02 != 0,
        battery_warning=flags & 0x01 != 0,
        red_led=secondary_flags & 0x08 != 0,
        backlight=secondary_flags & 0x04 != 0,
        auto_hold=secondary_flags & 0x02 != 0,
        high_voltage_warning=secondary_flags & 0x01 != 0,
        reserved_status=reserved_status,
    )
