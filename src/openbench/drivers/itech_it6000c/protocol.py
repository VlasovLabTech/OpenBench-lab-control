from __future__ import annotations

import math
from dataclasses import dataclass


class ITechIT6000CProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ITechIT6000CProfile:
    model: str
    rated_voltage_v: float
    rated_current_a: float
    rated_power_w: float
    voltage_resolution_v: float
    current_resolution_a: float
    power_resolution_w: float
    scpi_voltage_max_v: float
    scpi_current_max_a: float
    scpi_power_max_w: float


IT6054C_800_225 = ITechIT6000CProfile(
    model="IT6054C-800-225",
    rated_voltage_v=800.0,
    rated_current_a=225.0,
    rated_power_w=54_000.0,
    voltage_resolution_v=0.01,
    current_resolution_a=0.01,
    power_resolution_w=1.0,
    scpi_voltage_max_v=808.0,
    scpi_current_max_a=227.25,
    scpi_power_max_w=55_080.0,
)
SUPPORTED_PROFILES = {IT6054C_800_225.model: IT6054C_800_225}

# These thresholds are visual operator warnings, not additional hardware limits.
LAB_WARNING_VOLTAGE_V = 60.0
LAB_WARNING_CURRENT_A = 5.0
LAB_WARNING_POWER_W = 1_000.0


@dataclass(frozen=True, slots=True)
class ITechIT6000CIdentity:
    manufacturer: str
    model: str
    serial_number: str
    main_firmware: str
    controller_1_firmware: str
    controller_2_firmware: str


@dataclass(frozen=True, slots=True)
class ITechIT6000CState:
    priority: str
    function_mode: str
    output_enabled: bool
    voltage_setpoint_v: float
    current_setpoint_a: float
    current_limit_positive_a: float
    current_limit_negative_a: float
    voltage_limit_positive_v: float
    voltage_limit_negative_v: float
    power_limit_positive_w: float
    power_limit_negative_w: float
    measured_voltage_v: float
    measured_current_a: float
    measured_power_w: float
    voltage_slew_positive_v_per_ms: float
    voltage_slew_negative_v_per_ms: float
    current_slew_positive_a_per_ms: float
    current_slew_negative_a_per_ms: float
    ovp_enabled: bool
    ovp_level_v: float
    ovp_delay_s: float
    ocp_enabled: bool
    ocp_level_a: float
    ocp_delay_s: float
    opp_enabled: bool
    opp_level_w: float
    opp_delay_s: float
    uvp_enabled: bool
    uvp_level_v: float
    uvp_delay_s: float
    uvp_warmup_s: float
    ucp_enabled: bool
    ucp_level_a: float
    ucp_delay_s: float
    ucp_warmup_s: float
    output_rise_delay_s: float
    output_fall_delay_s: float
    watchdog_enabled: bool
    watchdog_delay_s: float
    sink_resistance_enabled: bool
    voltage_rzero_enabled: bool
    questionable_status: int
    operation_status: int

    @property
    def direction(self) -> str:
        if not self.output_enabled or abs(self.measured_current_a) < 0.005:
            return "IDLE"
        return "SOURCE" if self.measured_current_a > 0 else "SINK"

    @property
    def regulation(self) -> str:
        if not self.output_enabled:
            return "OFF"
        bits = self.operation_status
        if bits & 2048:
            return "CC-"
        if bits & 4096:
            return "CP-"
        if bits & 128:
            return "CC+"
        if bits & 256:
            return "CV"
        if bits & 512:
            return "CP+"
        return self.priority

    @property
    def faults(self) -> tuple[str, ...]:
        names = (
            (1, "OVP"),
            (2, "OCP+"),
            (4, "OCP-"),
            (8, "OPP+"),
            (16, "OPP-"),
            (32, "UVP"),
            (64, "OTP"),
            (128, "UCP"),
            (256, "SENSE"),
            (512, "SHARE"),
            (1024, "REVERSE"),
            (2048, "INHIBIT"),
            (4096, "POWER-STATE"),
            (8192, "PROTECTION"),
            (16384, "INTERNAL"),
        )
        return tuple(name for bit, name in names if self.questionable_status & bit)


@dataclass(frozen=True, slots=True)
class ITechOperatingPointUpdate:
    priority: str | None = None
    voltage_setpoint_v: float | None = None
    current_setpoint_a: float | None = None
    current_limit_positive_a: float | None = None
    current_limit_negative_a: float | None = None
    voltage_limit_positive_v: float | None = None
    voltage_limit_negative_v: float | None = None
    power_limit_positive_w: float | None = None
    power_limit_negative_w: float | None = None
    output_enabled: bool | None = None
    wiring_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class ITechProtectionUpdate:
    ovp_enabled: bool | None = None
    ovp_level_v: float | None = None
    ovp_delay_s: float | None = None
    ocp_enabled: bool | None = None
    ocp_level_a: float | None = None
    ocp_delay_s: float | None = None
    opp_enabled: bool | None = None
    opp_level_w: float | None = None
    opp_delay_s: float | None = None
    uvp_enabled: bool | None = None
    uvp_level_v: float | None = None
    uvp_delay_s: float | None = None
    uvp_warmup_s: float | None = None
    ucp_enabled: bool | None = None
    ucp_level_a: float | None = None
    ucp_delay_s: float | None = None
    ucp_warmup_s: float | None = None


@dataclass(frozen=True, slots=True)
class ITechAdvancedUpdate:
    voltage_slew_positive_v_per_ms: float | None = None
    voltage_slew_negative_v_per_ms: float | None = None
    current_slew_positive_a_per_ms: float | None = None
    current_slew_negative_a_per_ms: float | None = None
    output_rise_delay_s: float | None = None
    output_fall_delay_s: float | None = None
    watchdog_enabled: bool | None = None
    watchdog_delay_s: float | None = None


@dataclass(frozen=True, slots=True)
class ITechMeterParameter:
    key: str
    suffix: str
    name: str
    unit: str
    capability: str


ITECH_PARAMETERS = (
    ITechMeterParameter("measured_voltage", "voltage", "Output voltage", "V", "live_voltage"),
    ITechMeterParameter("measured_current", "current", "Output current", "A", "live_current"),
    ITechMeterParameter("measured_power", "power", "Output power", "W", "live_power"),
    ITechMeterParameter(
        "voltage_setpoint", "set_voltage", "Voltage setpoint", "V", "voltage_setpoint"
    ),
    ITechMeterParameter(
        "current_setpoint", "set_current", "Current setpoint", "A", "current_setpoint"
    ),
    ITechMeterParameter(
        "current_limit_positive",
        "current_limit_positive",
        "Positive current limit",
        "A",
        "current_limit",
    ),
    ITechMeterParameter(
        "current_limit_negative",
        "current_limit_negative",
        "Negative current limit",
        "A",
        "current_limit",
    ),
    ITechMeterParameter(
        "voltage_limit_positive",
        "voltage_limit_positive",
        "Positive voltage limit",
        "V",
        "voltage_limit",
    ),
    ITechMeterParameter(
        "voltage_limit_negative",
        "voltage_limit_negative",
        "Negative voltage limit",
        "V",
        "voltage_limit",
    ),
    ITechMeterParameter(
        "power_limit_positive", "power_limit_positive", "Positive power limit", "W", "power_limit"
    ),
    ITechMeterParameter(
        "power_limit_negative", "power_limit_negative", "Negative power limit", "W", "power_limit"
    ),
    ITechMeterParameter("output", "output", "Output state", "state", "output_state"),
    ITechMeterParameter("priority", "priority", "Control priority", "mode", "regulation_mode"),
    ITechMeterParameter("direction", "direction", "Power-flow direction", "mode", "power_flow"),
)


def parse_identity(response: str) -> ITechIT6000CIdentity:
    parts = tuple(part.strip() for part in response.strip().split(","))
    if len(parts) < 4 or not all(parts[:4]):
        raise ITechIT6000CProtocolError(f"Malformed ITECH identification: {response!r}")
    manufacturer, model, serial_number, main_firmware, *controllers = parts
    if not manufacturer.casefold().startswith("itech") or model.upper() not in SUPPORTED_PROFILES:
        raise ITechIT6000CProtocolError(f"Unsupported ITECH instrument: {response!r}")
    return ITechIT6000CIdentity(
        manufacturer=manufacturer,
        model=model.upper(),
        serial_number=serial_number,
        main_firmware=main_firmware,
        controller_1_firmware=controllers[0] if controllers else "",
        controller_2_firmware=controllers[1] if len(controllers) > 1 else "",
    )


def parse_float(response: str, field: str) -> float:
    try:
        value = float(response.strip())
    except ValueError as exc:
        raise ITechIT6000CProtocolError(f"Malformed ITECH {field}: {response!r}") from exc
    if not math.isfinite(value):
        raise ITechIT6000CProtocolError(f"Non-finite ITECH {field}: {response!r}")
    return value


def parse_bool(response: str, field: str) -> bool:
    normalized = response.strip().upper()
    if normalized in {"1", "ON"}:
        return True
    if normalized in {"0", "OFF"}:
        return False
    raise ITechIT6000CProtocolError(f"Malformed ITECH {field}: {response!r}")


def parse_priority(response: str) -> str:
    normalized = response.strip().upper()
    if normalized.startswith("VOLT"):
        return "CV"
    if normalized.startswith("CURR"):
        return "CC"
    raise ITechIT6000CProtocolError(f"Unknown ITECH priority: {response!r}")


def parse_function_mode(response: str) -> str:
    normalized = response.strip().upper()
    names = {
        "FIXED": "FIXED",
        "LIST": "LIST",
        "BATTERY": "BATTERY",
        "SOLAR": "SOLAR",
        "CARPROFILE": "CAR PROFILE",
    }
    try:
        return names[normalized]
    except KeyError as exc:
        raise ITechIT6000CProtocolError(f"Unknown ITECH function mode: {response!r}") from exc


def safety_warnings(state: ITechIT6000CState) -> tuple[dict[str, str], ...]:
    warnings: list[dict[str, str]] = []

    def add(field: str, message: str, severity: str = "danger") -> None:
        warnings.append({"field": field, "severity": severity, "message": message})

    if state.output_enabled:
        add("output_enabled", "Output is energized")
    if state.function_mode != "FIXED":
        add("function_mode", f"Non-fixed function mode: {state.function_mode}")
    if state.voltage_setpoint_v > LAB_WARNING_VOLTAGE_V:
        add("voltage_setpoint_v", "Voltage setpoint is above 60 V")
    if abs(state.current_setpoint_a) > LAB_WARNING_CURRENT_A:
        add("current_setpoint_a", "Current setpoint magnitude is above 5 A")
    for field, value in (
        ("current_limit_positive_a", state.current_limit_positive_a),
        ("current_limit_negative_a", state.current_limit_negative_a),
    ):
        if abs(value) > LAB_WARNING_CURRENT_A:
            add(field, "Current limit magnitude is above 5 A")
    if state.voltage_limit_positive_v > LAB_WARNING_VOLTAGE_V:
        add("voltage_limit_positive_v", "Voltage limit is above 60 V")
    for field, value in (
        ("power_limit_positive_w", state.power_limit_positive_w),
        ("power_limit_negative_w", state.power_limit_negative_w),
    ):
        if abs(value) > LAB_WARNING_POWER_W:
            add(field, "Power limit magnitude is above 1 kW")
    for field, enabled, label in (
        ("ovp_enabled", state.ovp_enabled, "OVP"),
        ("ocp_enabled", state.ocp_enabled, "OCP"),
        ("opp_enabled", state.opp_enabled, "OPP"),
    ):
        if not enabled:
            add(field, f"{label} protection is disabled")
    if state.ovp_level_v > LAB_WARNING_VOLTAGE_V:
        add("ovp_level_v", "OVP threshold is above 60 V")
    if abs(state.ocp_level_a) > LAB_WARNING_CURRENT_A:
        add("ocp_level_a", "OCP threshold magnitude is above 5 A")
    if abs(state.opp_level_w) > LAB_WARNING_POWER_W:
        add("opp_level_w", "OPP threshold magnitude is above 1 kW")
    for fault in state.faults:
        add("questionable_status", f"Active hardware fault: {fault}")
    return tuple(warnings)
