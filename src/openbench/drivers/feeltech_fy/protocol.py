from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from openbench.core.capabilities import MeterSample

MODEL_PATTERN = re.compile(r"^FY\d{4}(?:-\d+M)?$", re.IGNORECASE)

WAVEFORM_NAMES = {
    0: "SINE",
    1: "SQUARE",
    2: "TRIANGLE",
    3: "RISE SAW",
    4: "FALL SAW",
    5: "STEP TRIANGLE",
    6: "POS STEP",
    7: "NEG STEP",
    8: "POS EXP",
    9: "NEG EXP",
    10: "POS FALL EXP",
    11: "NEG FALL EXP",
    12: "POS LOG",
    13: "NEG LOG",
    14: "POS FALL LOG",
    15: "NEG FALL LOG",
    16: "POS FULL WAVE",
    17: "NEG FULL WAVE",
    18: "POS HALF WAVE",
    19: "NEG HALF WAVE",
    20: "LORENTZ",
    21: "MULTITONE",
    22: "NOISE",
    23: "ECG",
    24: "TRAPEZOID PULSE",
    25: "SINC PULSE",
    26: "NARROW PULSE",
    27: "GAUSS NOISE",
    28: "AM",
    29: "FM",
    30: "LINEAR FM",
}
for _arbitrary_index in range(1, 65):
    WAVEFORM_NAMES[30 + _arbitrary_index] = f"ARB {_arbitrary_index}"

WAVEFORM_OPTIONS = tuple(WAVEFORM_NAMES.items())
CHANNEL_PREFIX = {1: "M", 2: "F"}
SYNC_PARAMETER_INDEX = {
    "waveform": 0,
    "frequency": 1,
    "amplitude": 2,
    "offset": 3,
    "duty": 4,
}


class FeelTechProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FeelTechParameter:
    channel: int
    key: str
    name: str
    command: str
    unit: str

    @property
    def channel_suffix(self) -> str:
        if self.channel == 0:
            return f"counter.{self.key}"
        return f"ch{self.channel}.{self.key}"


def _parameters_for_channel(channel: int, prefix: str) -> tuple[FeelTechParameter, ...]:
    return (
        FeelTechParameter(channel, "waveform", f"CH{channel} waveform", f"R{prefix}W", "code"),
        FeelTechParameter(channel, "frequency", f"CH{channel} frequency", f"R{prefix}F", "Hz"),
        FeelTechParameter(channel, "amplitude", f"CH{channel} amplitude", f"R{prefix}A", "Vpp"),
        FeelTechParameter(channel, "offset", f"CH{channel} offset", f"R{prefix}O", "V"),
        FeelTechParameter(channel, "duty", f"CH{channel} duty cycle", f"R{prefix}D", "%"),
        FeelTechParameter(channel, "phase", f"CH{channel} phase", f"R{prefix}P", "°"),
        FeelTechParameter(channel, "output", f"CH{channel} output", f"R{prefix}N", "state"),
    )


FEELTECH_PARAMETERS = (
    *_parameters_for_channel(1, "M"),
    *_parameters_for_channel(2, "F"),
)
COUNTER_PARAMETERS = (
    FeelTechParameter(0, "frequency", "Counter input frequency", "RCF", "Hz"),
    FeelTechParameter(0, "count", "Counter input count", "RCC", "pulses"),
    FeelTechParameter(0, "period", "Counter input period", "RCT", "ns"),
    FeelTechParameter(0, "positive_width", "Counter positive pulse width", "RC+", "ns"),
    FeelTechParameter(0, "negative_width", "Counter negative pulse width", "RC-", "ns"),
    FeelTechParameter(0, "duty", "Counter input duty cycle", "RCD", "%"),
)
PARAMETER_BY_CHANNEL_KEY = {
    (parameter.channel, parameter.key): parameter for parameter in FEELTECH_PARAMETERS
}
READ_COMMANDS = frozenset(
    {
        "UMO",
        *(parameter.command for parameter in FEELTECH_PARAMETERS),
        *(f"RSA{index}" for index in range(5)),
        "RPM",
        "RPN",
        "RTA",
        "RTF",
        "RFK",
        "RTP",
        "RSS",
        "RCG",
        "RCF",
        "RCC",
        "RCT",
        "RC+",
        "RC-",
        "RCD",
    }
)

_BASIC_WRITE_PATTERN = re.compile(r"^W[MF](?:W\d{1,2}|F\d{8}\.\d{6}|[AODP]-?\d+(?:\.\d+)?|N[01])$")
_PRESET_WRITE_PATTERN = re.compile(r"^U[SL]N(?:0[1-9]|1\d|20)$")
_SYNC_WRITE_PATTERN = re.compile(r"^US[AD][0-4]$")
_PULSE_WRITE_PATTERN = re.compile(r"^WMS\d{1,10}$")
_ADVANCED_WRITE_PATTERN = re.compile(
    r"^(?:"
    r"WPM[0-3]|WPN\d{1,7}|WT[AFP][0-2]|WFK\d+(?:\.\d{1,6})?|"
    r"WCG[0-2]|WCC[01]|WC[ZP]0|"
    r"SOB[0-3]|S(?:ST|EN)-?\d+(?:\.\d+)?|STI\d+(?:\.\d+)?|"
    r"SMO[01]|SBE[01]|SXY[01]"
    r")$"
)


def parse_model(response: str) -> str:
    normalized = response.strip().upper()
    if not MODEL_PATTERN.fullmatch(normalized):
        raise FeelTechProtocolError(f"Unexpected FeelTech model response: {response!r}")
    return normalized


def waveform_name(code: int) -> str:
    return WAVEFORM_NAMES.get(code, f"WAVE {code}")


def validate_write_command(command: str) -> None:
    if not (
        _BASIC_WRITE_PATTERN.fullmatch(command)
        or _PRESET_WRITE_PATTERN.fullmatch(command)
        or _SYNC_WRITE_PATTERN.fullmatch(command)
        or _PULSE_WRITE_PATTERN.fullmatch(command)
        or _ADVANCED_WRITE_PATTERN.fullmatch(command)
    ):
        raise ValueError(f"Unsupported FeelTech write command: {command}")


def _fixed_decimal(value: float, places: int) -> str:
    quantizer = Decimal(1).scaleb(-places)
    normalized = Decimal(str(value)).quantize(quantizer, rounding=ROUND_HALF_UP)
    return f"{normalized:.{places}f}"


def encode_parameter_command(
    parameter: FeelTechParameter,
    value: int | float | bool,
) -> str:
    prefix = f"W{CHANNEL_PREFIX[parameter.channel]}"
    if parameter.key == "waveform":
        code = int(value)
        if code not in WAVEFORM_NAMES:
            raise FeelTechProtocolError(f"Unsupported waveform code: {code}")
        return f"{prefix}W{code:02d}"
    if parameter.key == "frequency":
        frequency_hz = float(value)
        if not 0 <= frequency_hz <= 99_999_999.999999:
            raise FeelTechProtocolError(f"Frequency is outside protocol range: {value}")
        return f"{prefix}F{_fixed_decimal(frequency_hz, 6):0>15}"
    if parameter.key == "amplitude":
        return f"{prefix}A{_fixed_decimal(float(value), 3)}"
    if parameter.key == "offset":
        return f"{prefix}O{_fixed_decimal(float(value), 3)}"
    if parameter.key == "duty":
        return f"{prefix}D{_fixed_decimal(float(value), 3)}"
    if parameter.key == "phase":
        return f"{prefix}P{_fixed_decimal(float(value), 2)}"
    if parameter.key == "output":
        return f"{prefix}N{1 if bool(value) else 0}"
    raise FeelTechProtocolError(f"Unsupported FeelTech parameter: {parameter.key}")


def _number(response: str, command: str) -> float:
    try:
        return float(response.strip())
    except ValueError as exc:
        raise FeelTechProtocolError(f"Invalid numeric response to {command}: {response!r}") from exc


def parse_parameter(parameter: FeelTechParameter, response: str) -> MeterSample:
    raw = response.strip()
    if not raw:
        raise FeelTechProtocolError(f"Empty response to {parameter.command}")

    if parameter.channel == 0:
        value = _number(raw, parameter.command)
        if parameter.key == "frequency":
            return MeterSample(value=value, unit="Hz", mode="COUNTER FREQUENCY")
        if parameter.key == "count":
            return MeterSample(value=float(int(value)), unit="pulses", mode="COUNTER")
        if parameter.key == "period":
            return MeterSample(value=value, unit="ns", mode="COUNTER PERIOD")
        if parameter.key == "positive_width":
            return MeterSample(value=value, unit="ns", mode="POSITIVE WIDTH")
        if parameter.key == "negative_width":
            return MeterSample(value=value, unit="ns", mode="NEGATIVE WIDTH")
        if parameter.key == "duty":
            if "." not in raw:
                value /= 10
            return MeterSample(value=value, unit="%", mode="COUNTER DUTY")

    if parameter.key == "waveform":
        code = int(_number(raw, parameter.command))
        label = waveform_name(code)
        return MeterSample(value=float(code), unit="code", mode=label)
    if parameter.key == "frequency":
        return MeterSample(value=_number(raw, parameter.command), unit="Hz", mode="FREQUENCY")
    if parameter.key == "amplitude":
        value = _number(raw, parameter.command)
        if "." not in raw:
            value /= 10_000
        return MeterSample(value=value, unit="Vpp", mode="AMPLITUDE")
    if parameter.key == "offset":
        value = _number(raw, parameter.command)
        if "." not in raw:
            encoded = int(value)
            if encoded > 0x7FFF_FFFF:
                encoded -= 0x1_0000_0000
            value = encoded / 1_000
        return MeterSample(value=value, unit="V", mode="OFFSET")
    if parameter.key == "duty":
        value = _number(raw, parameter.command)
        if "." not in raw:
            value /= 1_000
        return MeterSample(value=value, unit="%", mode="DUTY")
    if parameter.key == "phase":
        value = _number(raw, parameter.command)
        if "." not in raw:
            value /= 1_000
        return MeterSample(value=value, unit="°", mode="PHASE")
    if parameter.key == "output":
        enabled = int(_number(raw, parameter.command)) != 0
        return MeterSample(
            value=1.0 if enabled else 0.0,
            unit="state",
            mode="ON" if enabled else "OFF",
        )
    raise FeelTechProtocolError(f"Unsupported FeelTech parameter: {parameter.key}")
