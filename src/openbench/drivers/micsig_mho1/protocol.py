from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass


class MicsigProtocolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MicsigIdentification:
    manufacturer: str
    model: str
    serial_number: str
    firmware_version: str

    @property
    def is_supported_mho1(self) -> bool:
        return self.manufacturer.casefold() == "micsig" and self.model.upper().startswith("MHO1")

    @property
    def has_integrated_multimeter(self) -> bool:
        return self.is_supported_mho1 and not self.model.upper().endswith("N")


@dataclass(frozen=True, slots=True)
class MicsigChannelState:
    channel: int
    displayed: bool
    scale_v_per_div: float
    position: float
    coupling: str
    probe_attenuation: float
    input_impedance: str


@dataclass(frozen=True, slots=True)
class MicsigTriggerState:
    trigger_type: str
    mode: str
    status: str
    source: str
    slope: str
    level_v: float
    coupling: str


@dataclass(frozen=True, slots=True)
class MicsigScopeStatus:
    acquisition_type: str
    averaging_count: int
    sample_rate_sps: float
    memory_depth_setting: str
    memory_depth_points: int
    timebase_s_per_div: float
    timebase_position_s: float
    timebase_mode: str
    channels: tuple[MicsigChannelState, ...]
    trigger: MicsigTriggerState
    waveform_source: str
    waveform_mode: str
    waveform_format: str


@dataclass(frozen=True, slots=True)
class MicsigChannelUpdate:
    channel: int
    displayed: bool | None = None
    scale_v_per_div: float | None = None
    position: float | None = None
    coupling: str | None = None
    probe_attenuation: float | None = None
    input_impedance: str | None = None


@dataclass(frozen=True, slots=True)
class MicsigTriggerUpdate:
    trigger_type: str | None = None
    mode: str | None = None
    source: str | None = None
    slope: str | None = None
    level_v: float | None = None
    coupling: str | None = None


@dataclass(frozen=True, slots=True)
class MicsigScopeUpdate:
    channels: tuple[MicsigChannelUpdate, ...] = ()
    acquisition_type: str | None = None
    averaging_count: int | None = None
    memory_depth_setting: str | None = None
    timebase_s_per_div: float | None = None
    timebase_position_s: float | None = None
    timebase_mode: str | None = None
    trigger: MicsigTriggerUpdate | None = None


@dataclass(frozen=True, slots=True)
class MicsigWaveformPreamble:
    format_code: int
    mode_code: int
    averaging_count: int
    x_increment_s: float
    x_origin_s: float
    x_reference: float
    y_increment: float
    y_origin: float
    y_reference: float

    def validate_for_conversion(self) -> None:
        values = (
            self.x_increment_s,
            self.x_origin_s,
            self.x_reference,
            self.y_increment,
            self.y_origin,
            self.y_reference,
        )
        if not all(math.isfinite(value) for value in values):
            raise MicsigProtocolError("Micsig waveform preamble contains non-finite values")
        if self.x_increment_s <= 0:
            raise MicsigProtocolError("Micsig waveform X increment must be positive")
        if self.y_increment == 0:
            raise MicsigProtocolError("Micsig waveform Y increment must not be zero")


@dataclass(frozen=True, slots=True)
class MicsigWaveformCapture:
    source: str
    mode: str
    samples: tuple[int | float, ...]
    preamble: MicsigWaveformPreamble
    ascii_data: bytes = b""
    preamble_text: str = ""
    reported_preamble: MicsigWaveformPreamble | None = None

    @property
    def points(self) -> int:
        return len(self.samples)

    def time_at(self, index: int) -> float:
        if not 0 <= index < self.points:
            raise IndexError(index)
        return (
            index - self.preamble.x_reference
        ) * self.preamble.x_increment_s + self.preamble.x_origin_s

    def voltage_at(self, index: int) -> float:
        if not 0 <= index < self.points:
            raise IndexError(index)
        code = self.samples[index]
        if self.preamble.format_code == 2:
            return float(code)
        return (
            code - self.preamble.y_reference
        ) * self.preamble.y_increment - self.preamble.y_origin


@dataclass(frozen=True, slots=True)
class MicsigStoredWaveform:
    source: str
    file_type: str
    scope_filename: str
    http_path: str
    data: bytes


@dataclass(frozen=True, slots=True)
class MicsigScalarMeasurementSpec:
    channel: str
    item: str
    secondary_channel: str | None = None
    source_edge: str | None = None
    target_edge: str | None = None


@dataclass(frozen=True, slots=True)
class MicsigScalarMeasurement:
    item: str
    channel: str
    value: float | None
    unit: str
    status: str
    secondary_channel: str | None = None
    source_edge: str | None = None
    target_edge: str | None = None


@dataclass(frozen=True, slots=True)
class MicsigScreenshot:
    data: bytes
    image_format: str


@dataclass(frozen=True, slots=True)
class MicsigScreenshotProbe:
    transport: str
    raw_bytes: int
    declared_payload_bytes: int | None
    payload_bytes: int
    prefix_hex: str
    image_format: str | None
    error: str | None
    elapsed_s: float


@dataclass(frozen=True, slots=True)
class MicsigFastBinaryProbe:
    source: str
    data: bytes
    payload_bytes: int
    points: int | None
    prefix_hex: str
    error: str | None
    elapsed_s: float


@dataclass(frozen=True, slots=True)
class MicsigScreenFrame:
    data: bytes
    codec: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class MicsigSnapshot:
    region: str
    measurements: tuple[MicsigScalarMeasurement, ...]
    measurements_csv: bytes
    screenshot: MicsigScreenshot | None
    screenshot_error: str | None
    waveform_error: str | None = None
    waveforms: tuple[MicsigWaveformCapture, ...] = ()
    waveform_csv: bytes = b""
    elapsed_s: float = 0.0

    @property
    def channels(self) -> tuple[str, ...]:
        return ("CH1", "CH2", "CH3", "CH4")


@dataclass(frozen=True, slots=True)
class MicsigDmmSupport:
    hardware_present: bool
    direct_protocol_available: bool
    reason: str


SCALAR_MEASUREMENT_COMMANDS: dict[str, tuple[str, str]] = {
    "period": ("PERiod", "s"),
    "frequency": ("FREQ", "Hz"),
    "rise_time": ("RISetime", "s"),
    "fall_time": ("FALLtime", "s"),
    "positive_duty": ("PDUTy", "%"),
    "negative_duty": ("NDUTy", "%"),
    "positive_width": ("PWIDth", "s"),
    "negative_width": ("NWIDth", "s"),
    "burst_width": ("BURStw", "s"),
    "positive_overshoot": ("ROV", "%"),
    "negative_overshoot": ("FOV", "%"),
    "phase": ("PHASe", "deg"),
    "delay": ("DELay", "s"),
    "peak_to_peak": ("PKPK", "V"),
    "amplitude": ("AMP", "V"),
    "high": ("HIGH", "V"),
    "low": ("LOW", "V"),
    "maximum": ("MAX", "V"),
    "minimum": ("MIN", "V"),
    "rms": ("RMS", "V"),
    "cycle_rms": ("CRMS", "V"),
    "mean": ("MEAN", "V"),
    "cycle_mean": ("CMEAn", "V"),
    "ac_rms": ("ACRMS", "V"),
    "positive_rate": ("+RATE", "V/s"),
    "negative_rate": ("-RATE", "V/s"),
}

MICSIG_DELAY_EDGES = ("FRISe", "FFALL", "LRISe", "LFALL")

# Firmware 2.154.75 exposes ten global front-panel measurement slots. The card
# may distribute them across all four analog channels.
MAX_SCALAR_MEASUREMENTS = 10

SCALAR_MEASUREMENT_MULTIPLIERS: dict[str, float] = {
    "positive_duty": 100.0,
    "negative_duty": 100.0,
    "positive_overshoot": 100.0,
    "negative_overshoot": 100.0,
}

DEFAULT_SCALAR_MEASUREMENTS = (
    "amplitude",
    "peak_to_peak",
    "period",
    "positive_duty",
    "frequency",
    "rms",
)

_SCPI_NUMBER_RE = re.compile(r"^[ \t]*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)")


def parse_identification(response: str) -> MicsigIdentification:
    fields = tuple(field.strip() for field in response.strip().split(","))
    if len(fields) != 4 or any(not field for field in fields):
        raise MicsigProtocolError(f"Invalid Micsig *IDN? response: {response!r}")
    identification = MicsigIdentification(*fields)
    if not identification.is_supported_mho1:
        raise MicsigProtocolError(
            f"Unsupported SCPI instrument: {identification.manufacturer},{identification.model}"
        )
    return identification


def normalize_channel(channel: int | str) -> int:
    if isinstance(channel, int):
        number = channel
    else:
        normalized = channel.strip().upper()
        if normalized.startswith("CH"):
            normalized = normalized[2:]
        try:
            number = int(normalized)
        except ValueError as exc:
            raise ValueError(f"Invalid Micsig channel: {channel!r}") from exc
    if number not in {1, 2, 3, 4}:
        raise ValueError(f"Micsig channel must be between 1 and 4, got {number}")
    return number


def channel_source(channel: int | str) -> str:
    return f"CH{normalize_channel(channel)}"


def parse_bool(response: str) -> bool:
    normalized = response.strip().upper()
    if normalized in {"1", "ON"}:
        return True
    if normalized in {"0", "OFF"}:
        return False
    raise MicsigProtocolError(f"Invalid SCPI boolean response: {response!r}")


def parse_scpi_float(response: str) -> float:
    normalized = response.strip()
    if normalized.startswith("Error:"):
        raise MicsigProtocolError(normalized)
    match = _SCPI_NUMBER_RE.match(normalized)
    if match is None:
        raise MicsigProtocolError(f"Invalid numeric SCPI response: {response!r}")
    value = float(match.group(1))
    if not math.isfinite(value):
        raise MicsigProtocolError(f"Non-finite numeric SCPI response: {response!r}")
    return value


def parse_optional_scpi_float(response: str) -> float | None:
    normalized = response.strip()
    if normalized in {"", "--"}:
        return None
    return parse_scpi_float(normalized)


def parse_scpi_int(response: str) -> int:
    value = parse_scpi_float(response)
    if not value.is_integer():
        raise MicsigProtocolError(f"Expected integer SCPI response, got {response!r}")
    return int(value)


def parse_waveform_preamble(response: str) -> MicsigWaveformPreamble:
    fields = tuple(field.strip() for field in response.strip().split(","))
    if len(fields) != 9:
        raise MicsigProtocolError(
            f"Micsig waveform preamble must contain 9 fields, got {len(fields)}"
        )
    try:
        preamble = MicsigWaveformPreamble(
            format_code=int(fields[0]),
            mode_code=int(fields[1]),
            averaging_count=int(fields[2]),
            x_increment_s=float(fields[3]),
            x_origin_s=float(fields[4]),
            x_reference=float(fields[5]),
            y_increment=float(fields[6]),
            y_origin=float(fields[7]),
            y_reference=float(fields[8]),
        )
    except ValueError as exc:
        raise MicsigProtocolError(f"Invalid Micsig waveform preamble: {response!r}") from exc
    preamble.validate_for_conversion()
    return preamble


def parse_fast_binary_waveform(payload: bytes) -> tuple[int, ...]:
    if not payload:
        raise MicsigProtocolError("Micsig returned an empty waveform block")
    if len(payload) % 4:
        raise MicsigProtocolError(
            f"Micsig fast waveform length must be divisible by 4, got {len(payload)}"
        )
    # MHO14-200 firmware 2.154.75 returns one little-endian signed 32-bit code
    # per 12-bit ADC sample for :WAVeform:DATA:BIN?.
    return tuple(value[0] for value in struct.iter_unpack("<i", payload))


def parse_ascii_waveform(response: str) -> tuple[float, ...]:
    normalized = response.strip()
    if normalized.startswith("#"):
        if len(normalized) < 2 or not normalized[1].isdigit() or normalized[1] == "0":
            raise MicsigProtocolError("Invalid ASCII waveform block header")
        digits = int(normalized[1])
        header_end = 2 + digits
        if len(normalized) < header_end or not normalized[2:header_end].isdigit():
            raise MicsigProtocolError("Invalid ASCII waveform block length")
        declared_length = int(normalized[2:header_end])
        normalized = normalized[header_end : header_end + declared_length]
    fields = tuple(field for field in re.split(r"[,\s]+", normalized.strip(" ,\t\r\n")) if field)
    if not fields or any(not field for field in fields):
        raise MicsigProtocolError("Micsig returned an empty ASCII waveform")
    try:
        values = tuple(float(field) for field in fields)
    except ValueError as exc:
        raise MicsigProtocolError("Micsig returned invalid ASCII waveform data") from exc
    if not all(math.isfinite(value) for value in values):
        raise MicsigProtocolError("Micsig ASCII waveform contains non-finite values")
    return values


def detect_image_format(payload: bytes) -> str:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    raise MicsigProtocolError(f"Unknown Micsig screenshot signature: {payload[:16].hex(' ')}")


def normalize_screenshot_image(payload: bytes) -> tuple[bytes, str]:
    """Repair the malformed JFIF marker returned by tested MHO1 firmware."""
    try:
        return payload, detect_image_format(payload)
    except MicsigProtocolError:
        pass
    if len(payload) >= 10 and payload.startswith(b"\xff\xd8") and payload[4:10] == b"\x00\x10JFIF":
        repaired = payload[:2] + b"\xff\xe0" + payload[4:]
        return repaired, detect_image_format(repaired)
    raise MicsigProtocolError(f"Unknown Micsig screenshot signature: {payload[:16].hex(' ')}")


def format_scpi_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("SCPI numeric values must be finite")
    return format(value, ".12g")
