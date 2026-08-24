from __future__ import annotations

from dataclasses import dataclass

FRAME_SIZE = 14
FRAME_TERMINATOR = b"\r\n"

FUNCTION_CODES = frozenset(b";=?09351264><8:")
FLAG_BYTE_MIN = 0x30
FLAG_BYTE_MAX = 0x3F

VOLTAGE_EXPONENTS = (-4, -3, -2, -1, -5, 0, 0, 0)
MICROAMP_EXPONENTS = (-8, -7, 0, 0, 0, 0, 0, 0)
MILLIAMP_EXPONENTS = (-6, -5, 0, 0, 0, 0, 0, 0)
AMP_EXPONENTS = (-3, 0, 0, 0, 0, 0, 0, 0)
MANUAL_AMP_EXPONENTS = (-4, -3, -2, -1, 0, 0, 0, 0)
RESISTANCE_EXPONENTS = (-2, -1, 0, 1, 2, 3, 4, 0)
FREQUENCY_EXPONENTS = (-2, -1, 0, 0, 1, 2, 3, 4)
CAPACITANCE_EXPONENTS = (-12, -11, -10, -9, -8, -7, -6, -5)
DIODE_EXPONENTS = (-4, 0, 0, 0, 0, 0, 0, 0)


class UT61EProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UT61EReading:
    raw_frame: bytes
    range_code: int
    display: str
    mode: str
    value: float | None
    unit: str
    overload: bool
    underflow: bool
    auto_range: bool
    ac: bool
    dc: bool
    hold: bool
    relative: bool
    maximum: bool
    minimum: bool
    battery_warning: bool
    low_pass_filter: bool

    @property
    def is_voltage(self) -> bool:
        return self.unit == "V"

    @property
    def is_current(self) -> bool:
        return self.unit == "A"


def _flag_nibble(value: int, name: str) -> int:
    if not FLAG_BYTE_MIN <= value <= FLAG_BYTE_MAX:
        raise UT61EProtocolError(f"Invalid UT61E {name} byte: 0x{value:02X}")
    return value & 0x0F


def _validate_frame(frame: bytes) -> None:
    if len(frame) != FRAME_SIZE:
        raise UT61EProtocolError(f"UT61E frame must contain {FRAME_SIZE} bytes, got {len(frame)}")
    if not frame.endswith(FRAME_TERMINATOR):
        raise UT61EProtocolError("UT61E frame terminator is missing")
    if frame[0] not in b"01234567":
        raise UT61EProtocolError(f"Invalid UT61E range byte: 0x{frame[0]:02X}")
    if not all(ord("0") <= value <= ord("9") for value in frame[1:6]):
        raise UT61EProtocolError("UT61E display contains non-decimal digits")
    if frame[6] not in FUNCTION_CODES:
        raise UT61EProtocolError(f"Unknown UT61E function byte: 0x{frame[6]:02X}")
    for index, name in zip(
        range(7, 12),
        ("status", "option 1", "option 2", "option 3", "option 4"),
        strict=True,
    ):
        _flag_nibble(frame[index], name)


def is_plausible_frame(frame: bytes) -> bool:
    try:
        _validate_frame(frame)
    except UT61EProtocolError:
        return False
    return True


def _mode_and_scale(
    function: int,
    *,
    range_code: int,
    judge: bool,
    ac: bool,
    dc: bool,
    va_hz: bool,
) -> tuple[str, str, int]:
    if va_hz and function in b";=?09":
        if judge:
            return "%", "%", -1
        return "Hz", "Hz", FREQUENCY_EXPONENTS[range_code]

    if function == ord(";"):
        prefix = "AC" if ac else "DC" if dc else ""
        return f"{prefix}V", "V", VOLTAGE_EXPONENTS[range_code]
    if function == ord("="):
        prefix = "AC" if ac else "DC" if dc else ""
        return f"{prefix}uA", "A", MICROAMP_EXPONENTS[range_code]
    if function == ord("?"):
        prefix = "AC" if ac else "DC" if dc else ""
        return f"{prefix}mA", "A", MILLIAMP_EXPONENTS[range_code]
    if function == ord("0"):
        prefix = "AC" if ac else "DC" if dc else ""
        return f"{prefix}A", "A", AMP_EXPONENTS[range_code]
    if function == ord("9"):
        prefix = "AC" if ac else "DC" if dc else ""
        return f"{prefix}A", "A", MANUAL_AMP_EXPONENTS[range_code]
    if function == ord("3"):
        return "OHM", "Ω", RESISTANCE_EXPONENTS[range_code]
    if function == ord("5"):
        return "CONT", "Ω", RESISTANCE_EXPONENTS[range_code]
    if function == ord("1"):
        return "DIODE", "V", DIODE_EXPONENTS[range_code]
    if function == ord("2"):
        if judge:
            return "%", "%", -1
        return "Hz", "Hz", FREQUENCY_EXPONENTS[range_code]
    if function == ord("6"):
        return "CAP", "F", CAPACITANCE_EXPONENTS[range_code]
    if function == ord("4"):
        return ("°C", "°C", 0) if judge else ("°F", "°F", 0)
    if function == ord(">"):
        return "ADP0", "raw", 0
    if function == ord("<"):
        return "ADP1", "raw", 0
    if function == ord("8"):
        return "ADP2", "raw", 0
    if function == ord(":"):
        return "ADP3", "raw", 0
    raise UT61EProtocolError(f"Unsupported UT61E function byte: 0x{function:02X}")


def parse_reading_frame(frame: bytes) -> UT61EReading:
    _validate_frame(frame)

    range_code = frame[0] - ord("0")
    display = frame[1:6].decode("ascii")
    function = frame[6]
    status = _flag_nibble(frame[7], "status")
    option_1 = _flag_nibble(frame[8], "option 1")
    option_2 = _flag_nibble(frame[9], "option 2")
    option_3 = _flag_nibble(frame[10], "option 3")
    option_4 = _flag_nibble(frame[11], "option 4")

    judge = bool(status & 0x08)
    negative = bool(status & 0x04)
    battery_warning = bool(status & 0x02)
    overload = bool(status & 0x01)
    underflow = bool(option_2 & 0x08)
    dc = bool(option_3 & 0x08)
    ac = bool(option_3 & 0x04)
    auto_range = bool(option_3 & 0x02)
    va_hz = bool(option_3 & 0x01)
    if ac and dc:
        raise UT61EProtocolError("UT61E frame has both AC and DC flags")

    mode, unit, exponent = _mode_and_scale(
        function,
        range_code=range_code,
        judge=judge,
        ac=ac,
        dc=dc,
        va_hz=va_hz,
    )

    value: float | None
    if overload or underflow:
        value = None
    else:
        sign = -1 if negative else 1
        value = sign * int(display) * (10.0**exponent)
        if mode == "°F":
            value = value * 9 / 5 + 32

    return UT61EReading(
        raw_frame=frame,
        range_code=range_code,
        display=display,
        mode=mode,
        value=value,
        unit=unit,
        overload=overload,
        underflow=underflow,
        auto_range=auto_range,
        ac=ac,
        dc=dc,
        hold=bool(option_4 & 0x02),
        relative=bool(option_1 & 0x02),
        maximum=bool(option_1 & 0x08),
        minimum=bool(option_1 & 0x04),
        battery_warning=battery_warning,
        low_pass_filter=bool(option_4 & 0x01),
    )
