from __future__ import annotations

from dataclasses import dataclass

from openbench.drivers.ut61e.protocol import FRAME_SIZE, FRAME_TERMINATOR, UT61EProtocolError

DECIMAL_FACTORS = {
    ord("0"): 1.0,
    ord("1"): 1e-3,
    ord("2"): 1e-2,
    ord("4"): 1e-1,
}


@dataclass(frozen=True, slots=True)
class UT61DReading:
    raw_frame: bytes
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


def _validate_frame(frame: bytes) -> None:
    if len(frame) != FRAME_SIZE:
        raise UT61EProtocolError(f"UT61D frame must contain {FRAME_SIZE} bytes, got {len(frame)}")
    if not frame.endswith(FRAME_TERMINATOR):
        raise UT61EProtocolError("UT61D frame terminator is missing")
    if frame[0] not in b"+-":
        raise UT61EProtocolError(f"Invalid UT61D sign byte: 0x{frame[0]:02X}")
    if frame[1:5] != b"?0:?" and not all(ord("0") <= value <= ord("9") for value in frame[1:5]):
        raise UT61EProtocolError("UT61D display contains invalid digits")
    if frame[5] != ord(" "):
        raise UT61EProtocolError(f"Invalid UT61D separator byte: 0x{frame[5]:02X}")
    if frame[6] not in DECIMAL_FACTORS:
        raise UT61EProtocolError(f"Invalid UT61D decimal-position byte: 0x{frame[6]:02X}")


def is_plausible_ut61d_frame(frame: bytes) -> bool:
    try:
        _validate_frame(frame)
    except UT61EProtocolError:
        return False
    return True


def _mode_and_unit(status: int, special: int, unit_flags: int) -> tuple[str, str]:
    ac = bool(status & 0x08)
    dc = bool(status & 0x10)
    prefix = "AC" if ac else "DC" if dc else ""

    if special & 0x02:
        return "%", "%"
    if special & 0x04:
        return "DIODE", "V"
    if special & 0x08:
        return "CONT", "Ω"
    if unit_flags & 0x80:
        return f"{prefix}V", "V"
    if unit_flags & 0x40:
        return f"{prefix}A", "A"
    if unit_flags & 0x20:
        return "OHM", "Ω"
    if unit_flags & 0x10:
        return "hFE", ""
    if unit_flags & 0x08:
        return "Hz", "Hz"
    if unit_flags & 0x04:
        return "CAP", "F"
    if unit_flags & 0x02:
        return "°C", "°C"
    if unit_flags & 0x01:
        return "°F", "°F"
    return "device_reported", ""


def _prefix_factor(option_1: int, option_2: int) -> float:
    if option_1 & 0x02:
        return 1e-9
    if option_2 & 0x80:
        return 1e-6
    if option_2 & 0x40:
        return 1e-3
    if option_2 & 0x20:
        return 1e3
    if option_2 & 0x10:
        return 1e6
    return 1.0


def parse_ut61d_reading_frame(frame: bytes) -> UT61DReading:
    _validate_frame(frame)

    status = frame[7]
    option_1 = frame[8]
    option_2 = frame[9]
    unit_flags = frame[10]
    ac = bool(status & 0x08)
    dc = bool(status & 0x10)
    if ac and dc:
        raise UT61EProtocolError("UT61D frame has both AC and DC flags")

    display = frame[1:5].decode("ascii")
    overload = frame[1:5] == b"?0:?"
    mode, unit = _mode_and_unit(status, option_2, unit_flags)
    value: float | None
    if overload:
        value = None
    else:
        sign = -1 if frame[0] == ord("-") else 1
        value = sign * int(display) * DECIMAL_FACTORS[frame[6]] * _prefix_factor(option_1, option_2)

    return UT61DReading(
        raw_frame=frame,
        display=display,
        mode=mode,
        value=value,
        unit=unit,
        overload=overload,
        underflow=False,
        auto_range=bool(status & 0x20),
        ac=ac,
        dc=dc,
        hold=bool(status & 0x02),
        relative=bool(status & 0x04),
        maximum=bool(option_1 & 0x20),
        minimum=bool(option_1 & 0x10),
        battery_warning=bool(option_1 & 0x04),
        low_pass_filter=False,
    )
