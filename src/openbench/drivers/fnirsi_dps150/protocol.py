from __future__ import annotations

import math
import struct
from dataclasses import dataclass

HEADER_INPUT = 0xF0
HEADER_OUTPUT = 0xF1

COMMAND_GET = 0xA1
COMMAND_BAUD = 0xB0
COMMAND_SET = 0xB1
COMMAND_SESSION = 0xC1

TYPE_INPUT_VOLTAGE = 0xC0
TYPE_SET_VOLTAGE = 0xC1
TYPE_SET_CURRENT = 0xC2
TYPE_OUTPUT_MEASUREMENTS = 0xC3
TYPE_TEMPERATURE = 0xC4
TYPE_PRESET_1_VOLTAGE = 0xC5
TYPE_PRESET_1_CURRENT = 0xC6
TYPE_OVP = 0xD1
TYPE_OCP = 0xD2
TYPE_OPP = 0xD3
TYPE_OTP = 0xD4
TYPE_LVP = 0xD5
TYPE_BRIGHTNESS = 0xD6
TYPE_VOLUME = 0xD7
TYPE_METERING_ENABLE = 0xD8
TYPE_CAPACITY = 0xD9
TYPE_ENERGY = 0xDA
TYPE_OUTPUT_ENABLE = 0xDB
TYPE_PROTECTION = 0xDC
TYPE_MODE = 0xDD
TYPE_MODEL = 0xDE
TYPE_HARDWARE_VERSION = 0xDF
TYPE_FIRMWARE_VERSION = 0xE0
TYPE_UPPER_VOLTAGE = 0xE2
TYPE_UPPER_CURRENT = 0xE3
TYPE_ALL = 0xFF

DEFAULT_BAUD_RATE = 115_200
BAUD_RATE_CODE = 5
ALL_STATE_PAYLOAD_BYTES = 139
PRESET_COUNT = 6

PROTECTION_NAMES = {
    0: "OK",
    1: "OVP",
    2: "OCP",
    3: "OPP",
    4: "OTP",
    5: "LVP",
    6: "REP",
}


class DPS150ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DPS150Packet:
    command: int
    data_type: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class DPS150Identity:
    model: str
    hardware_version: str
    firmware_version: str


@dataclass(frozen=True, slots=True)
class DPS150Preset:
    slot: int
    voltage_v: float
    current_a: float


@dataclass(frozen=True, slots=True)
class DPS150ProtectionSettings:
    over_voltage_v: float
    over_current_a: float
    over_power_w: float
    over_temperature_c: float
    low_input_voltage_v: float


@dataclass(frozen=True, slots=True)
class DPS150State:
    input_voltage_v: float
    set_voltage_v: float
    set_current_a: float
    output_voltage_v: float
    output_current_a: float
    output_power_w: float
    temperature_c: float
    presets: tuple[DPS150Preset, ...]
    protections: DPS150ProtectionSettings
    brightness: int
    volume: int
    metering_enabled: bool
    output_capacity_ah: float
    output_energy_wh: float
    output_enabled: bool
    protection_code: int
    protection: str
    mode: str
    upper_voltage_v: float
    upper_current_a: float


@dataclass(frozen=True, slots=True)
class DPS150OutputUpdate:
    voltage_v: float | None = None
    current_a: float | None = None
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class DPS150ProtectionUpdate:
    over_voltage_v: float | None = None
    over_current_a: float | None = None
    over_power_w: float | None = None
    over_temperature_c: float | None = None
    low_input_voltage_v: float | None = None


@dataclass(frozen=True, slots=True)
class DPS150DisplayUpdate:
    brightness: int | None = None
    volume: int | None = None


@dataclass(frozen=True, slots=True)
class DPS150MeterParameter:
    key: str
    name: str
    capability: str
    unit: str

    @property
    def channel_suffix(self) -> str:
        return self.key


DPS150_PARAMETERS = (
    DPS150MeterParameter("input_voltage", "Input voltage", "dc_input_voltage", "V"),
    DPS150MeterParameter("set_voltage", "Voltage setpoint", "dc_voltage_setpoint", "V"),
    DPS150MeterParameter("set_current", "Current limit", "dc_current_setpoint", "A"),
    DPS150MeterParameter("output_voltage", "Output voltage", "dc_output_voltage", "V"),
    DPS150MeterParameter("output_current", "Output current", "dc_output_current", "A"),
    DPS150MeterParameter("output_power", "Output power", "dc_output_power", "W"),
    DPS150MeterParameter("temperature", "Temperature", "temperature", "°C"),
    DPS150MeterParameter("capacity", "Output capacity", "charge_capacity", "Ah"),
    DPS150MeterParameter("energy", "Output energy", "energy", "Wh"),
    DPS150MeterParameter("output", "Output state", "output_state", "state"),
    DPS150MeterParameter("mode", "Regulation mode", "regulation_mode", "state"),
    DPS150MeterParameter("protection", "Protection state", "protection_state", "code"),
    DPS150MeterParameter("metering", "Ah/Wh metering", "metering_state", "state"),
    DPS150MeterParameter(
        "available_voltage",
        "Available voltage limit",
        "available_output_voltage",
        "V",
    ),
    DPS150MeterParameter(
        "available_current",
        "Available current limit",
        "available_output_current",
        "A",
    ),
)


def checksum(data_type: int, payload: bytes) -> int:
    return (data_type + len(payload) + sum(payload)) & 0xFF


def build_packet(
    command: int,
    data_type: int,
    payload: bytes = b"\x00",
    *,
    header: int = HEADER_OUTPUT,
) -> bytes:
    if not 0 <= command <= 0xFF:
        raise ValueError("DPS-150 command byte must be between 0 and 255")
    if not 0 <= data_type <= 0xFF:
        raise ValueError("DPS-150 data type must be between 0 and 255")
    if len(payload) > 0xFF:
        raise ValueError("DPS-150 payload is too long")
    return (
        bytes((header, command, data_type, len(payload)))
        + payload
        + bytes((checksum(data_type, payload),))
    )


def build_get_packet(data_type: int) -> bytes:
    return build_packet(COMMAND_GET, data_type)


def build_set_float_packet(data_type: int, value: float) -> bytes:
    if not math.isfinite(value):
        raise ValueError("DPS-150 float value must be finite")
    return build_packet(COMMAND_SET, data_type, struct.pack("<f", value))


def build_set_byte_packet(data_type: int, value: int) -> bytes:
    if not 0 <= value <= 0xFF:
        raise ValueError("DPS-150 byte value must be between 0 and 255")
    return build_packet(COMMAND_SET, data_type, bytes((value,)))


def extract_packets(buffer: bytearray) -> tuple[DPS150Packet, ...]:
    packets: list[DPS150Packet] = []
    while len(buffer) >= 5:
        try:
            start = buffer.index(HEADER_INPUT)
        except ValueError:
            buffer.clear()
            break
        if start:
            del buffer[:start]
        if len(buffer) < 5:
            break
        payload_length = buffer[3]
        packet_length = payload_length + 5
        if len(buffer) < packet_length:
            break
        raw = bytes(buffer[:packet_length])
        del buffer[:packet_length]
        payload = raw[4:-1]
        if raw[-1] != checksum(raw[2], payload):
            # The alleged header may have occurred inside damaged data. Put
            # everything after that byte back and continue resynchronizing.
            buffer[:0] = raw[1:]
            continue
        packets.append(
            DPS150Packet(
                command=raw[1],
                data_type=raw[2],
                payload=payload,
            )
        )
    return tuple(packets)


def parse_text(payload: bytes, *, field: str) -> str:
    try:
        value = payload.rstrip(b"\x00").decode("ascii")
    except UnicodeDecodeError as exc:
        raise DPS150ProtocolError(f"DPS-150 {field} is not ASCII") from exc
    if not value.strip():
        raise DPS150ProtocolError(f"DPS-150 {field} is empty")
    return value.strip()


def parse_identity(
    model_payload: bytes,
    hardware_payload: bytes,
    firmware_payload: bytes,
) -> DPS150Identity:
    identity = DPS150Identity(
        model=parse_text(model_payload, field="model"),
        hardware_version=parse_text(hardware_payload, field="hardware version"),
        firmware_version=parse_text(firmware_payload, field="firmware version"),
    )
    if identity.model.upper() != "DPS-150":
        raise DPS150ProtocolError(f"Unsupported FNIRSI model: {identity.model}")
    return identity


def _float(payload: bytes, offset: int, field: str) -> float:
    value = struct.unpack_from("<f", payload, offset)[0]
    if not math.isfinite(value):
        raise DPS150ProtocolError(f"DPS-150 {field} is not finite")
    return float(value)


def parse_all_state(payload: bytes) -> DPS150State:
    if len(payload) != ALL_STATE_PAYLOAD_BYTES:
        raise DPS150ProtocolError(
            f"DPS-150 all-state payload must be {ALL_STATE_PAYLOAD_BYTES} bytes, got {len(payload)}"
        )

    presets = tuple(
        DPS150Preset(
            slot=slot,
            voltage_v=_float(payload, 28 + (slot - 1) * 8, f"preset {slot} voltage"),
            current_a=_float(payload, 32 + (slot - 1) * 8, f"preset {slot} current"),
        )
        for slot in range(1, PRESET_COUNT + 1)
    )
    protection_code = payload[108]
    return DPS150State(
        input_voltage_v=_float(payload, 0, "input voltage"),
        set_voltage_v=_float(payload, 4, "voltage setpoint"),
        set_current_a=_float(payload, 8, "current setpoint"),
        output_voltage_v=_float(payload, 12, "output voltage"),
        output_current_a=_float(payload, 16, "output current"),
        output_power_w=_float(payload, 20, "output power"),
        temperature_c=_float(payload, 24, "temperature"),
        presets=presets,
        protections=DPS150ProtectionSettings(
            over_voltage_v=_float(payload, 76, "over-voltage protection"),
            over_current_a=_float(payload, 80, "over-current protection"),
            over_power_w=_float(payload, 84, "over-power protection"),
            over_temperature_c=_float(payload, 88, "over-temperature protection"),
            low_input_voltage_v=_float(payload, 92, "low-input-voltage protection"),
        ),
        brightness=payload[96],
        volume=payload[97],
        metering_enabled=payload[98] != 0,
        output_capacity_ah=_float(payload, 99, "output capacity"),
        output_energy_wh=_float(payload, 103, "output energy"),
        output_enabled=payload[107] != 0,
        protection_code=protection_code,
        protection=PROTECTION_NAMES.get(protection_code, f"UNKNOWN-{protection_code}"),
        mode="CC" if payload[109] == 0 else "CV",
        upper_voltage_v=_float(payload, 111, "available voltage limit"),
        upper_current_a=_float(payload, 115, "available current limit"),
    )


def preset_type(slot: int, *, current: bool) -> int:
    if not 1 <= slot <= PRESET_COUNT:
        raise ValueError(f"DPS-150 preset slot must be between 1 and {PRESET_COUNT}")
    return TYPE_PRESET_1_VOLTAGE + (slot - 1) * 2 + int(current)
