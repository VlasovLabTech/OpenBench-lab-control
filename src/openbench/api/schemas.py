from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from openbench.domain import (
    Channel,
    Device,
    MatrixApplyResult,
    MatrixConnection,
    MatrixPort,
    MatrixProfile,
    Measurement,
    SafetyState,
)
from openbench.drivers.feeltech_fy import (
    FeelTechAdvancedState,
    FeelTechChannelState,
    FeelTechGeneratorState,
)
from openbench.drivers.fnirsi_dps150 import (
    DPS150Identity,
    DPS150State,
)
from openbench.drivers.itech_it6000c import (
    ITechIT6000C,
    ITechIT6000CState,
    safety_warnings,
)
from openbench.drivers.kingst_la2016 import KingstTrigger
from openbench.drivers.micsig_mho1 import (
    MAX_SCALAR_MEASUREMENTS,
    MicsigScopeStatus,
    MicsigWaveformCapture,
)
from openbench.drivers.owon_spm import OwonSPMIdentity, OwonSPMState
from openbench.services.capture_service import CaptureStatus
from openbench.services.dc_power_supply_service import PowerProgramStatus
from openbench.services.instrument_settings_service import InstrumentSettings
from openbench.services.logic_analyzer_service import (
    LogicAnalyzerSettings,
    LogicCaptureStatus,
)

ScopeChannelName = Literal["CH1", "CH2", "CH3", "CH4"]


def _default_scope_capture_channels() -> list[ScopeChannelName]:
    return ["CH1", "CH2", "CH3", "CH4"]


class DeviceOut(BaseModel):
    id: str
    name: str
    kind: str
    connected: bool
    capabilities: list[str]

    @classmethod
    def from_domain(cls, value: Device) -> DeviceOut:
        return cls(
            id=value.id,
            name=value.name,
            kind=value.kind,
            connected=value.connected,
            capabilities=list(value.capabilities),
        )


class ChannelOut(BaseModel):
    id: str
    device_id: str
    name: str
    capability: str
    unit: str
    poll_interval_s: float
    state: str

    @classmethod
    def from_domain(cls, value: Channel) -> ChannelOut:
        return cls(
            id=value.id,
            device_id=value.device_id,
            name=value.name,
            capability=value.capability,
            unit=value.unit,
            poll_interval_s=value.poll_interval_s,
            state=value.state,
        )


class MeasurementOut(BaseModel):
    timestamp_utc: datetime
    monotonic_s: float
    device_id: str
    channel_id: str
    value: float | None
    unit: str
    quality: str
    status: str

    @classmethod
    def from_domain(cls, value: Measurement) -> MeasurementOut:
        return cls(
            timestamp_utc=value.timestamp_utc,
            monotonic_s=value.monotonic_s,
            device_id=value.device_id,
            channel_id=value.channel_id,
            value=value.value,
            unit=value.unit,
            quality=value.quality,
            status=value.status,
        )


class GeneratorChannelUpdateIn(BaseModel):
    waveform_code: int | None = Field(default=None, ge=0, le=94)
    frequency_hz: float | None = Field(default=None, ge=0)
    amplitude_vpp: float | None = Field(default=None, ge=0.001, le=20)
    offset_v: float | None = Field(default=None, ge=-10, le=10)
    duty_percent: float | None = Field(default=None, ge=0.01, le=99.99)
    phase_deg: float | None = Field(default=None, ge=0, le=359.99)
    pulse_width_ns: float | None = Field(default=None, ge=100, le=1_000_000_000)
    output_enabled: bool | None = None


class GeneratorOutputsUpdateIn(BaseModel):
    channel_1: bool
    channel_2: bool


class GeneratorSynchronizationUpdateIn(BaseModel):
    parameter: Literal["waveform", "frequency", "amplitude", "offset", "duty"]
    enabled: bool


class GeneratorBurstUpdateIn(BaseModel):
    source: Literal["off", "ch2", "external"]
    cycles: int = Field(ge=1, le=1_048_575)


class GeneratorTriggerIn(BaseModel):
    cycles: int | None = Field(default=None, ge=1, le=1_048_575)


class GeneratorKeyingUpdateIn(BaseModel):
    kind: Literal["ask", "fsk", "psk"]
    source: Literal["off", "external", "manual"]
    secondary_frequency_hz: float | None = Field(
        default=None,
        ge=0.000001,
        le=10_000_000,
    )


class GeneratorCounterUpdateIn(BaseModel):
    gate_time_s: Literal[1, 10, 100]
    coupling: Literal["dc", "ac"]
    mode: Literal["frequency", "count", "both"] = "frequency"


class GeneratorSweepUpdateIn(BaseModel):
    target: Literal["frequency", "amplitude", "offset", "duty"]
    start: float
    end: float
    duration_s: float = Field(ge=0.01, le=999.99)
    mode: Literal["linear", "logarithmic"]
    source: Literal["time", "vco"]
    enabled: bool


class GeneratorPresetActionOut(BaseModel):
    device_id: str
    slot: int
    action: Literal["saved", "loaded"]


class GeneratorWaveformOut(BaseModel):
    code: int
    name: str
    maximum_frequency_hz: float


class GeneratorChannelOut(BaseModel):
    channel: int
    waveform_code: int
    waveform_name: str
    frequency_hz: float
    amplitude_vpp: float
    offset_v: float
    duty_percent: float
    phase_deg: float
    output_enabled: bool

    @classmethod
    def from_driver(cls, value: FeelTechChannelState) -> GeneratorChannelOut:
        return cls(
            channel=value.channel,
            waveform_code=value.waveform_code,
            waveform_name=value.waveform_name,
            frequency_hz=value.frequency_hz,
            amplitude_vpp=value.amplitude_vpp,
            offset_v=value.offset_v,
            duty_percent=value.duty_percent,
            phase_deg=value.phase_deg,
            output_enabled=value.output_enabled,
        )


class GeneratorBurstOut(BaseModel):
    trigger_mode: int | None
    trigger_source: str
    cycles: int | None


class GeneratorModulationOut(BaseModel):
    ask_mode: int | None
    ask_source: str
    fsk_mode: int | None
    fsk_source: str
    fsk_secondary_frequency_hz: float | None
    psk_mode: int | None
    psk_source: str


class GeneratorCounterOut(BaseModel):
    paused: bool
    mode: str
    gate_code: int | None
    gate_time_s: int | None
    coupling: str | None
    frequency_hz: float | None
    count: int | None
    period_ns: float | None
    positive_width_ns: float | None
    negative_width_ns: float | None
    duty_percent: float | None


class GeneratorSweepOut(BaseModel):
    target: str
    start: float
    end: float
    duration_s: float
    mode: str
    source: str
    enabled: bool
    verified: bool


class GeneratorAdvancedOut(BaseModel):
    burst: GeneratorBurstOut
    modulation: GeneratorModulationOut
    counter: GeneratorCounterOut
    main_pulse_width_ns: float | None
    sweep: GeneratorSweepOut | None
    unavailable_reads: list[str]

    @classmethod
    def from_driver(cls, value: FeelTechAdvancedState) -> GeneratorAdvancedOut:
        return cls(
            burst=GeneratorBurstOut(
                trigger_mode=value.burst.trigger_mode,
                trigger_source=value.burst.trigger_source,
                cycles=value.burst.cycles,
            ),
            modulation=GeneratorModulationOut(
                ask_mode=value.modulation.ask_mode,
                ask_source=value.modulation.ask_source,
                fsk_mode=value.modulation.fsk_mode,
                fsk_source=value.modulation.fsk_source,
                fsk_secondary_frequency_hz=value.modulation.fsk_secondary_frequency_hz,
                psk_mode=value.modulation.psk_mode,
                psk_source=value.modulation.psk_source,
            ),
            counter=GeneratorCounterOut(
                paused=value.counter.paused,
                mode=value.counter.mode,
                gate_code=value.counter.gate_code,
                gate_time_s=value.counter.gate_time_s,
                coupling=value.counter.coupling,
                frequency_hz=value.counter.frequency_hz,
                count=value.counter.count,
                period_ns=value.counter.period_ns,
                positive_width_ns=value.counter.positive_width_ns,
                negative_width_ns=value.counter.negative_width_ns,
                duty_percent=value.counter.duty_percent,
            ),
            main_pulse_width_ns=value.main_pulse_width_ns,
            sweep=(
                GeneratorSweepOut(
                    target=value.sweep.target,
                    start=value.sweep.start,
                    end=value.sweep.end,
                    duration_s=value.sweep.duration_s,
                    mode=value.sweep.mode,
                    source=value.sweep.source,
                    enabled=value.sweep.enabled,
                    verified=value.sweep.verified,
                )
                if value.sweep is not None
                else None
            ),
            unavailable_reads=list(value.unavailable_reads),
        )


class SignalGeneratorOut(BaseModel):
    device_id: str
    model: str
    safety_state: str
    channels: list[GeneratorChannelOut]
    waveforms: list[GeneratorWaveformOut]
    synchronization: dict[str, bool]
    advanced: GeneratorAdvancedOut

    @classmethod
    def from_driver(
        cls,
        *,
        device_id: str,
        state: FeelTechGeneratorState,
        safety_state: str,
        waveforms: tuple[tuple[int, str, float], ...],
        synchronization: dict[str, bool],
        advanced: FeelTechAdvancedState,
    ) -> SignalGeneratorOut:
        return cls(
            device_id=device_id,
            model=state.model,
            safety_state=safety_state,
            channels=[GeneratorChannelOut.from_driver(item) for item in state.channels],
            waveforms=[
                GeneratorWaveformOut(
                    code=code,
                    name=name,
                    maximum_frequency_hz=maximum_frequency_hz,
                )
                for code, name, maximum_frequency_hz in waveforms
            ],
            synchronization=synchronization,
            advanced=GeneratorAdvancedOut.from_driver(advanced),
        )


class ScopeChannelUpdateIn(BaseModel):
    channel: int = Field(ge=1, le=4)
    displayed: bool | None = None
    scale_v_per_div: float | None = Field(default=None, gt=0)
    position_v: float | None = None
    coupling: Literal["AC", "DC", "GND"] | None = None
    probe_attenuation: float | None = Field(default=None, gt=0)
    input_impedance: Literal["MEGA", "FIFTY"] | None = None


class ScopeTriggerUpdateIn(BaseModel):
    trigger_type: Literal["EDGE"] | None = None
    mode: Literal["AUTO", "NORMAL"] | None = None
    source: Literal["CH1", "CH2", "CH3", "CH4"] | None = None
    slope: Literal["RISE", "FALL", "DUAL"] | None = None
    level_v: float | None = None
    coupling: Literal["DC", "AC", "HFREJ", "LFREJ", "NOISEREJ"] | None = None


class ScopeSettingsUpdateIn(BaseModel):
    channels: list[ScopeChannelUpdateIn] = Field(default_factory=list, max_length=4)
    acquisition_type: Literal["NORMAL", "MEAN", "ENVELOP", "PEAK"] | None = None
    averaging_count: Literal[2, 4, 8, 16, 32, 64, 128, 256] | None = None
    memory_depth_setting: str | None = Field(default=None, pattern=r"^(?:AUTO|[1-9]\d*)$")
    timebase_s_per_div: float | None = Field(default=None, gt=0)
    timebase_position_s: float | None = None
    timebase_mode: Literal["YT", "XY"] | None = None
    trigger: ScopeTriggerUpdateIn | None = None


class ScopeChannelOut(BaseModel):
    channel: int
    displayed: bool
    scale_v_per_div: float
    position_v: float
    coupling: str
    probe_attenuation: float
    input_impedance: str


class ScopeTriggerOut(BaseModel):
    trigger_type: str
    mode: str
    status: str
    source: str
    slope: str
    level_v: float
    coupling: str


class OscilloscopeOut(BaseModel):
    device_id: str
    model: str
    serial_number: str
    firmware_version: str
    acquisition_type: str
    averaging_count: int
    sample_rate_sps: float
    memory_depth_setting: str
    memory_depth_points: int
    timebase_s_per_div: float
    timebase_position_s: float
    timebase_mode: str
    channels: list[ScopeChannelOut]
    trigger: ScopeTriggerOut
    waveform_source: str
    waveform_mode: str
    waveform_format: str
    calibrated_waveform_transfer_available: bool
    waveform_transfer_method: str

    @classmethod
    def from_driver(
        cls,
        *,
        device_id: str,
        model: str,
        serial_number: str,
        firmware_version: str,
        calibrated_waveform_transfer_available: bool,
        waveform_transfer_method: str,
        state: MicsigScopeStatus,
    ) -> OscilloscopeOut:
        return cls(
            device_id=device_id,
            model=model,
            serial_number=serial_number,
            firmware_version=firmware_version,
            acquisition_type=state.acquisition_type,
            averaging_count=state.averaging_count,
            sample_rate_sps=state.sample_rate_sps,
            memory_depth_setting=state.memory_depth_setting,
            memory_depth_points=state.memory_depth_points,
            timebase_s_per_div=state.timebase_s_per_div,
            timebase_position_s=state.timebase_position_s,
            timebase_mode=state.timebase_mode,
            channels=[
                ScopeChannelOut(
                    channel=item.channel,
                    displayed=item.displayed,
                    scale_v_per_div=item.scale_v_per_div,
                    position_v=item.position,
                    coupling=item.coupling,
                    probe_attenuation=item.probe_attenuation,
                    input_impedance=item.input_impedance,
                )
                for item in state.channels
            ],
            trigger=ScopeTriggerOut(
                trigger_type=state.trigger.trigger_type,
                mode=state.trigger.mode,
                status=state.trigger.status,
                source=state.trigger.source,
                slope=state.trigger.slope,
                level_v=state.trigger.level_v,
                coupling=state.trigger.coupling,
            ),
            waveform_source=state.waveform_source,
            waveform_mode=state.waveform_mode,
            waveform_format=state.waveform_format,
            calibrated_waveform_transfer_available=calibrated_waveform_transfer_available,
            waveform_transfer_method=waveform_transfer_method,
        )


class ScopeAcquisitionActionOut(BaseModel):
    device_id: str
    status: str
    elapsed_s: float


class ScopeSingleIn(BaseModel):
    timeout_s: float = Field(default=2.0, gt=0, le=60)


class ScopeWaveformCaptureIn(BaseModel):
    channels: list[ScopeChannelName] = Field(
        default_factory=_default_scope_capture_channels,
        min_length=1,
        max_length=4,
    )
    mode: Literal["NORMAL", "MAXIMUM", "RAW"] = "NORMAL"


ScopeScalarMeasurementName = Literal[
    "period",
    "frequency",
    "rise_time",
    "fall_time",
    "positive_duty",
    "negative_duty",
    "positive_width",
    "negative_width",
    "burst_width",
    "positive_overshoot",
    "negative_overshoot",
    "phase",
    "delay",
    "peak_to_peak",
    "amplitude",
    "high",
    "low",
    "maximum",
    "minimum",
    "rms",
    "cycle_rms",
    "mean",
    "cycle_mean",
    "ac_rms",
    "positive_rate",
    "negative_rate",
]


class ScopeScalarMeasurementSelectionIn(BaseModel):
    channel: ScopeChannelName
    item: ScopeScalarMeasurementName
    secondary_channel: ScopeChannelName | None = None
    source_edge: Literal["FRISe", "FFALL", "LRISe", "LFALL"] | None = None
    target_edge: Literal["FRISe", "FFALL", "LRISe", "LFALL"] | None = None


class ScopeScalarMeasurementProfileIn(BaseModel):
    measurements: list[ScopeScalarMeasurementSelectionIn] = Field(
        max_length=MAX_SCALAR_MEASUREMENTS
    )


class ScopeScalarMeasurementOut(BaseModel):
    channel: str
    item: str
    value: float | None
    unit: str
    status: str
    secondary_channel: str | None = None
    source_edge: str | None = None
    target_edge: str | None = None


class ScopeScalarMeasurementProfileOut(BaseModel):
    device_id: str
    measurements: list[ScopeScalarMeasurementOut]
    elapsed_s: float


class ScopeScreenshotProbeIn(BaseModel):
    transport: Literal["tcp", "vxi11"]


class ScopeScreenshotProbeOut(BaseModel):
    device_id: str
    transport: str
    raw_bytes: int
    declared_payload_bytes: int | None
    payload_bytes: int
    prefix_hex: str
    image_format: str | None
    error: str | None
    elapsed_s: float


class ScopeFastBinaryProbeIn(BaseModel):
    channel: ScopeChannelName = "CH1"


class ScopeFastBinaryProbeOut(BaseModel):
    device_id: str
    source: str
    payload_bytes: int
    points: int | None
    prefix_hex: str
    error: str | None
    elapsed_s: float
    filename: str | None
    download_url: str | None


class ScopeWaveformChannelOut(BaseModel):
    source: str
    mode: str
    points: int
    time_s: list[float]
    voltage_v: list[float]

    @classmethod
    def from_driver(cls, value: MicsigWaveformCapture) -> ScopeWaveformChannelOut:
        return cls(
            source=value.source,
            mode=value.mode,
            points=value.points,
            time_s=[value.time_at(index) for index in range(value.points)],
            voltage_v=[value.voltage_at(index) for index in range(value.points)],
        )


class ScopeWaveformOut(BaseModel):
    device_id: str
    channels: list[ScopeWaveformChannelOut]


class ScopeNumericCsvCaptureIn(ScopeWaveformCaptureIn):
    filename_prefix: str = Field(
        default="openbench_numeric",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )


class ScopeNumericCsvOut(BaseModel):
    device_id: str
    channels: list[str]
    points: int
    transfer_method: str
    filename: str
    bytes: int
    download_url: str


class ScopeMaximumCaptureIn(BaseModel):
    channels: list[ScopeChannelName] = Field(
        default_factory=_default_scope_capture_channels,
        min_length=1,
        max_length=4,
    )


class ScopeMaximumCaptureFileOut(BaseModel):
    filename: str
    bytes: int
    download_url: str


class ScopeMaximumCaptureStatusOut(BaseModel):
    device_id: str
    state: str
    active: bool
    capture_id: str
    channels: list[str]
    current_channel: str | None
    memory_depth_points: int
    points_total: int
    points_completed: int
    progress_percent: float
    requested_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    artifact_directory: str | None
    files: list[ScopeMaximumCaptureFileOut]
    metadata_download_url: str | None
    message: str
    error: str


class ScopeStoredWaveformCaptureIn(BaseModel):
    channels: list[ScopeChannelName] = Field(
        default_factory=_default_scope_capture_channels,
        min_length=1,
        max_length=4,
    )
    format: Literal["CSV", "BIN"] = "CSV"
    filename_prefix: str = Field(
        default="openbench",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    timeout_s: float = Field(default=30.0, gt=0, le=120)


class ScopeStoredFileImportIn(BaseModel):
    scope_paths: list[str] = Field(min_length=1, max_length=20)


class ScopeStoredWaveformFileOut(BaseModel):
    source: str
    format: str
    filename: str
    bytes: int
    scope_path: str
    download_url: str
    attempts: int = Field(default=1, ge=1, le=2)


class ScopeStoredWaveformOut(BaseModel):
    device_id: str
    files: list[ScopeStoredWaveformFileOut]


class PowerSupplyOutputUpdateIn(BaseModel):
    voltage_v: float | None = Field(default=None, ge=0, le=30)
    current_a: float | None = Field(default=None, ge=0, le=5)
    enabled: bool | None = None


class PowerSupplyProtectionUpdateIn(BaseModel):
    over_voltage_v: float | None = Field(default=None, ge=0, le=30)
    over_current_a: float | None = Field(default=None, ge=0, le=5.1)
    over_power_w: float | None = Field(default=None, ge=0, le=150)
    over_temperature_c: float | None = Field(default=None, ge=0, le=100)
    low_input_voltage_v: float | None = Field(default=None, ge=0, le=30)


class PowerSupplyDisplayUpdateIn(BaseModel):
    brightness: int | None = Field(default=None, ge=0, le=10)
    volume: int | None = Field(default=None, ge=0, le=10)


class PowerSupplyMeteringUpdateIn(BaseModel):
    enabled: bool


class PowerSupplyPresetUpdateIn(BaseModel):
    voltage_v: float = Field(ge=0, le=30)
    current_a: float = Field(ge=0, le=5)


class PowerSupplyPresetApplyIn(BaseModel):
    enabled: bool | None = None


class PowerSequenceStepIn(BaseModel):
    voltage_v: float = Field(ge=0, le=30)
    current_a: float = Field(ge=0, le=5)
    dwell_s: float = Field(ge=0.1, le=86400)


class PowerSequenceStartIn(BaseModel):
    steps: list[PowerSequenceStepIn] = Field(min_length=1, max_length=10_000)
    loops: int = Field(default=1, ge=1, le=1000)


class PowerSweepStartIn(BaseModel):
    parameter: Literal["voltage", "current"]
    start: float = Field(ge=0, le=30)
    end: float = Field(ge=0, le=30)
    step: float = Field(gt=0, le=30)
    fixed_value: float = Field(ge=0, le=30)
    dwell_s: float = Field(default=1, ge=1, le=86400)
    loops: int = Field(default=1, ge=1, le=1000)


class PowerProgramStopIn(BaseModel):
    output_off: bool = True


class SourceMeasureOutputUpdateIn(BaseModel):
    voltage_v: float | None = Field(default=None, ge=0, le=60)
    current_a: float | None = Field(default=None, ge=0, le=10)
    enabled: bool | None = None


class BidirectionalOperatingPointUpdateIn(BaseModel):
    priority: Literal["CV", "CC"] | None = None
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


class BidirectionalProtectionUpdateIn(BaseModel):
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


class BidirectionalAdvancedUpdateIn(BaseModel):
    voltage_slew_positive_v_per_ms: float | None = None
    voltage_slew_negative_v_per_ms: float | None = None
    current_slew_positive_a_per_ms: float | None = None
    current_slew_negative_a_per_ms: float | None = None
    output_rise_delay_s: float | None = None
    output_fall_delay_s: float | None = None
    watchdog_enabled: bool | None = None
    watchdog_delay_s: float | None = None


class SourceMeasureProtectionUpdateIn(BaseModel):
    over_voltage_v: float | None = Field(default=None, ge=0, le=62)
    over_current_a: float | None = Field(default=None, ge=0, le=10)


class SourceMeasureMultimeterUpdateIn(BaseModel):
    function: (
        Literal[
            "dc_voltage",
            "ac_voltage",
            "dc_current",
            "ac_current",
            "resistance",
            "capacitance",
            "diode",
            "continuity",
        ]
        | None
    ) = None
    range_mode: Literal["auto", "manual"] | None = None
    range_value: float | None = Field(default=None, gt=0)
    relative_enabled: bool | None = None
    hold_enabled: bool | None = None


class SourceMeasureIdentityOut(BaseModel):
    manufacturer: str
    model: str
    serial_number: str
    firmware_version: str


class SourceMeasureSourceOut(BaseModel):
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


class SourceMeasureMultimeterOut(BaseModel):
    function: str
    value: float | None
    unit: str
    range_mode: str
    range_label: str
    status: str
    range_value: float | None
    relative_enabled: bool
    hold_enabled: bool


class SourceMeasureUnitOut(BaseModel):
    device_id: str
    identity: SourceMeasureIdentityOut
    safety_state: str
    source: SourceMeasureSourceOut
    multimeter: SourceMeasureMultimeterOut

    @classmethod
    def from_driver(
        cls,
        *,
        device_id: str,
        identity: OwonSPMIdentity,
        state: OwonSPMState,
        safety_state: str,
    ) -> SourceMeasureUnitOut:
        return cls(
            device_id=device_id,
            identity=SourceMeasureIdentityOut(
                manufacturer=identity.manufacturer,
                model=identity.model,
                serial_number=identity.serial_number,
                firmware_version=identity.firmware_version,
            ),
            safety_state=safety_state,
            source=SourceMeasureSourceOut(
                set_voltage_v=state.source.set_voltage_v,
                set_current_a=state.source.set_current_a,
                output_voltage_v=state.source.output_voltage_v,
                output_current_a=state.source.output_current_a,
                output_power_w=state.source.output_power_w,
                output_enabled=state.source.output_enabled,
                over_voltage_v=state.source.over_voltage_v,
                over_current_a=state.source.over_current_a,
                over_voltage_fault=state.source.over_voltage_fault,
                over_current_fault=state.source.over_current_fault,
                over_temperature_fault=state.source.over_temperature_fault,
                mode=state.source.mode,
            ),
            multimeter=SourceMeasureMultimeterOut(
                function=state.multimeter.function,
                value=state.multimeter.value,
                unit=state.multimeter.unit,
                range_mode=state.multimeter.range_mode,
                range_label=state.multimeter.range_label,
                status=state.multimeter.status,
                range_value=state.multimeter.range_value,
                relative_enabled=state.multimeter.relative_enabled,
                hold_enabled=state.multimeter.hold_enabled,
            ),
        )


class BidirectionalIdentityOut(BaseModel):
    manufacturer: str
    model: str
    serial_number: str
    main_firmware: str
    controller_1_firmware: str
    controller_2_firmware: str


class BidirectionalProfileOut(BaseModel):
    rated_voltage_v: float
    rated_current_a: float
    rated_power_w: float
    voltage_resolution_v: float
    current_resolution_a: float
    power_resolution_w: float


class BidirectionalWarningOut(BaseModel):
    field: str
    severity: str
    message: str


class BidirectionalStateOut(BaseModel):
    priority: str
    function_mode: str
    output_enabled: bool
    direction: str
    regulation: str
    faults: list[str]
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

    @classmethod
    def from_driver(cls, value: ITechIT6000CState) -> BidirectionalStateOut:
        return cls(
            **asdict(value),
            direction=value.direction,
            regulation=value.regulation,
            faults=list(value.faults),
        )


class BidirectionalPowerSupplyOut(BaseModel):
    device_id: str
    identity: BidirectionalIdentityOut
    profile: BidirectionalProfileOut
    safety_state: str
    port: str
    baud_rate: int
    state: BidirectionalStateOut
    warnings: list[BidirectionalWarningOut]

    @classmethod
    def from_driver(
        cls,
        *,
        device_id: str,
        instrument: ITechIT6000C,
        state: ITechIT6000CState,
        safety_state: str,
    ) -> BidirectionalPowerSupplyOut:
        identity = instrument.identity
        profile = instrument.profile
        return cls(
            device_id=device_id,
            identity=BidirectionalIdentityOut(
                manufacturer=identity.manufacturer,
                model=identity.model,
                serial_number=identity.serial_number,
                main_firmware=identity.main_firmware,
                controller_1_firmware=identity.controller_1_firmware,
                controller_2_firmware=identity.controller_2_firmware,
            ),
            profile=BidirectionalProfileOut(
                rated_voltage_v=profile.rated_voltage_v,
                rated_current_a=profile.rated_current_a,
                rated_power_w=profile.rated_power_w,
                voltage_resolution_v=profile.voltage_resolution_v,
                current_resolution_a=profile.current_resolution_a,
                power_resolution_w=profile.power_resolution_w,
            ),
            safety_state=safety_state,
            port=instrument.descriptor.port,
            baud_rate=instrument.descriptor.baud_rate,
            state=BidirectionalStateOut.from_driver(state),
            warnings=[BidirectionalWarningOut(**item) for item in safety_warnings(state)],
        )


class BidirectionalMeasurementsOut(BaseModel):
    device_id: str
    timestamp_utc: datetime
    measured_voltage_v: float | None
    measured_current_a: float | None
    measured_power_w: float | None


class BidirectionalExperimentReservationOut(BaseModel):
    device_id: str
    active: bool
    polling_targets_suspended: int


class PowerSupplyIdentityOut(BaseModel):
    model: str
    hardware_version: str
    firmware_version: str

    @classmethod
    def from_driver(cls, value: DPS150Identity | None) -> PowerSupplyIdentityOut:
        if value is None:
            return cls(model="DPS-150", hardware_version="", firmware_version="")
        return cls(
            model=value.model,
            hardware_version=value.hardware_version,
            firmware_version=value.firmware_version,
        )


class PowerSupplyPresetOut(BaseModel):
    slot: int
    voltage_v: float
    current_a: float


class PowerSupplyProtectionOut(BaseModel):
    over_voltage_v: float
    over_current_a: float
    over_power_w: float
    over_temperature_c: float
    low_input_voltage_v: float
    active_code: int
    active: str


class PowerSupplyLiveOut(BaseModel):
    input_voltage_v: float
    output_voltage_v: float
    output_current_a: float
    output_power_w: float
    temperature_c: float
    output_capacity_ah: float
    output_energy_wh: float
    mode: str


class PowerSupplyOut(BaseModel):
    device_id: str
    identity: PowerSupplyIdentityOut
    safety_state: str
    set_voltage_v: float
    set_current_a: float
    output_enabled: bool
    metering_enabled: bool
    brightness: int
    volume: int
    upper_voltage_v: float
    upper_current_a: float
    live: PowerSupplyLiveOut
    protections: PowerSupplyProtectionOut
    presets: list[PowerSupplyPresetOut]

    @classmethod
    def from_driver(
        cls,
        *,
        device_id: str,
        identity: DPS150Identity | None,
        state: DPS150State,
        safety_state: str,
    ) -> PowerSupplyOut:
        return cls(
            device_id=device_id,
            identity=PowerSupplyIdentityOut.from_driver(identity),
            safety_state=safety_state,
            set_voltage_v=state.set_voltage_v,
            set_current_a=state.set_current_a,
            output_enabled=state.output_enabled,
            metering_enabled=state.metering_enabled,
            brightness=state.brightness,
            volume=state.volume,
            upper_voltage_v=state.upper_voltage_v,
            upper_current_a=state.upper_current_a,
            live=PowerSupplyLiveOut(
                input_voltage_v=state.input_voltage_v,
                output_voltage_v=state.output_voltage_v,
                output_current_a=state.output_current_a,
                output_power_w=state.output_power_w,
                temperature_c=state.temperature_c,
                output_capacity_ah=state.output_capacity_ah,
                output_energy_wh=state.output_energy_wh,
                mode=state.mode,
            ),
            protections=PowerSupplyProtectionOut(
                over_voltage_v=state.protections.over_voltage_v,
                over_current_a=state.protections.over_current_a,
                over_power_w=state.protections.over_power_w,
                over_temperature_c=state.protections.over_temperature_c,
                low_input_voltage_v=state.protections.low_input_voltage_v,
                active_code=state.protection_code,
                active=state.protection,
            ),
            presets=[
                PowerSupplyPresetOut(
                    slot=preset.slot,
                    voltage_v=preset.voltage_v,
                    current_a=preset.current_a,
                )
                for preset in state.presets
            ],
        )


class PowerProgramStatusOut(BaseModel):
    device_id: str
    kind: str
    active: bool
    paused: bool
    started_at: datetime | None
    current_step: int
    total_steps: int
    current_loop: int
    loops: int
    progress_percent: float
    last_error: str

    @classmethod
    def from_service(cls, value: PowerProgramStatus) -> PowerProgramStatusOut:
        return cls(
            device_id=value.device_id,
            kind=value.kind,
            active=value.active,
            paused=value.paused,
            started_at=value.started_at,
            current_step=value.current_step,
            total_steps=value.total_steps,
            current_loop=value.current_loop,
            loops=value.loops,
            progress_percent=value.progress_percent,
            last_error=value.last_error,
        )


class DeviceSettingsUpdate(BaseModel):
    context: str | None = Field(default=None, max_length=10000)
    poll_interval_s: float | None = Field(default=None, gt=0, le=600)
    scope_screen: bool | None = None
    scope_data: bool | None = None
    scope_channels: list[ScopeChannelName] | None = Field(default=None, max_length=4)
    scope_wait_for_trigger: bool | None = None


class DeviceSettingsOut(BaseModel):
    device_id: str
    context: str
    poll_interval_s: float | None
    minimum_poll_interval_s: float | None
    maximum_poll_interval_s: float
    scope_screen: bool | None
    scope_data: bool | None
    scope_channels: list[ScopeChannelName] | None
    scope_wait_for_trigger: bool | None

    @classmethod
    def from_service(cls, value: InstrumentSettings) -> DeviceSettingsOut:
        return cls(
            device_id=value.device_id,
            context=value.context,
            poll_interval_s=value.poll_interval_s,
            minimum_poll_interval_s=value.minimum_poll_interval_s,
            maximum_poll_interval_s=600,
            scope_screen=value.scope_screen,
            scope_data=value.scope_data,
            scope_channels=(
                list(value.scope_channels) if value.scope_channels is not None else None
            ),
            scope_wait_for_trigger=value.scope_wait_for_trigger,
        )


class CaptureMetadataIn(BaseModel):
    title: str = Field(default="", max_length=120)
    comment: str = Field(default="", max_length=10000)


class RecordingStartIn(CaptureMetadataIn):
    duration_s: float | None = Field(default=None, ge=1, le=86400)
    scope_capture_mode: Literal["periodic", "manual"] = "periodic"


class RecordingScopeFrameIn(BaseModel):
    label: str = Field(default="", max_length=120)


class RecordingScopeFrameOut(BaseModel):
    device_id: str
    capture_id: str
    timestamp_utc: datetime
    status: str
    screen_file: str
    data_file: str
    error: str


class SnapshotOut(BaseModel):
    file_name: str
    download_url: str
    measurement_count: int


class CaptureStatusOut(BaseModel):
    active: bool
    started_at: datetime | None
    current_file: str | None
    last_recording_file: str | None
    last_snapshot_file: str | None
    samples_written: int
    title: str
    comment: str
    duration_s: float | None
    elapsed_s: float
    remaining_s: float | None
    scope_capture_mode: str

    @classmethod
    def from_service(cls, value: CaptureStatus) -> CaptureStatusOut:
        return cls(
            active=value.active,
            started_at=value.started_at,
            current_file=value.current_file.name if value.current_file else None,
            last_recording_file=(
                value.last_recording_file.name if value.last_recording_file else None
            ),
            last_snapshot_file=(
                value.last_snapshot_file.name if value.last_snapshot_file else None
            ),
            samples_written=value.samples_written,
            title=value.current_title,
            comment=value.current_comment,
            duration_s=value.duration_s,
            elapsed_s=value.elapsed_s,
            remaining_s=value.remaining_s,
            scope_capture_mode=value.scope_capture_mode,
        )


class LogicTriggerIn(BaseModel):
    channel: int = Field(ge=0, le=15)
    condition: Literal["low", "high", "rising", "falling"]

    def to_domain(self) -> KingstTrigger:
        return KingstTrigger(channel=self.channel, condition=self.condition)


class LogicAnalyzerSettingsUpdate(BaseModel):
    channels: list[int] | None = Field(default=None, min_length=1, max_length=16)
    sample_rate_hz: int | None = None
    sample_count: int | None = Field(default=None, ge=1, le=10_000_000_000)
    threshold_v: float | None = None
    capture_ratio_percent: int | None = Field(default=None, ge=0, le=100)
    triggers: list[LogicTriggerIn] | None = Field(default=None, max_length=16)
    auto_start_enabled: bool | None = None
    auto_start_delay_s: float | None = Field(default=None, ge=0, le=86400)


class LogicTriggerOut(BaseModel):
    channel: int
    condition: str


class LogicAnalyzerSettingsOut(BaseModel):
    device_id: str
    channels: list[int]
    sample_rate_hz: int
    sample_count: int
    duration_s: float
    threshold_v: float
    capture_ratio_percent: int
    triggers: list[LogicTriggerOut]
    trigger_label: str
    auto_start_enabled: bool
    auto_start_delay_s: float
    supported_sample_rates_hz: list[int]
    supported_thresholds_v: list[float]
    maximum_samples: int = 10_000_000_000

    @classmethod
    def from_service(
        cls,
        value: LogicAnalyzerSettings,
        *,
        supported_sample_rates_hz: tuple[int, ...],
        supported_thresholds_v: tuple[float, ...],
    ) -> LogicAnalyzerSettingsOut:
        return cls(
            device_id=value.device_id,
            channels=list(value.channels),
            sample_rate_hz=value.sample_rate_hz,
            sample_count=value.sample_count,
            duration_s=value.duration_s,
            threshold_v=value.threshold_v,
            capture_ratio_percent=value.capture_ratio_percent,
            triggers=[
                LogicTriggerOut(channel=trigger.channel, condition=trigger.condition)
                for trigger in value.triggers
            ],
            trigger_label=value.trigger_label,
            auto_start_enabled=value.auto_start_enabled,
            auto_start_delay_s=value.auto_start_delay_s,
            supported_sample_rates_hz=list(supported_sample_rates_hz),
            supported_thresholds_v=list(supported_thresholds_v),
        )


class LogicCaptureStartIn(CaptureMetadataIn):
    pass


class LogicCaptureStatusOut(BaseModel):
    device_id: str
    state: str
    active: bool
    capture_id: str
    title: str
    comment: str
    requested_at: datetime | None
    started_at: datetime | None
    triggered_at: datetime | None
    completed_at: datetime | None
    scheduled_start_at: datetime | None
    estimated_duration_s: float | None
    remaining_s: float | None
    trigger_timestamp_quality: str
    trigger: str
    artifact_directory: str | None
    artifact_file: str | None
    artifact_download_url: str | None
    metadata_download_url: str | None
    recording_file: str | None
    source: str
    message: str
    error: str

    @classmethod
    def from_service(cls, value: LogicCaptureStatus) -> LogicCaptureStatusOut:
        base = (
            f"/api/v1/logic-analyzers/{value.device_id}/captures/{value.capture_id}/files"
            if value.capture_id
            else ""
        )
        return cls(
            device_id=value.device_id,
            state=value.state,
            active=value.active,
            capture_id=value.capture_id,
            title=value.title,
            comment=value.comment,
            requested_at=value.requested_at,
            started_at=value.started_at,
            triggered_at=value.triggered_at,
            completed_at=value.completed_at,
            scheduled_start_at=value.scheduled_start_at,
            estimated_duration_s=value.estimated_duration_s,
            remaining_s=value.remaining_s,
            trigger_timestamp_quality=value.trigger_timestamp_quality,
            trigger=value.trigger,
            artifact_directory=(
                value.artifact_directory.name if value.artifact_directory else None
            ),
            artifact_file=value.artifact_file.name if value.artifact_file else None,
            artifact_download_url=(
                f"{base}/{value.artifact_file.name}" if value.artifact_file else None
            ),
            metadata_download_url=(f"{base}/metadata.json" if value.artifact_directory else None),
            recording_file=value.recording_file.name if value.recording_file else None,
            source=value.source,
            message=value.message,
            error=value.error,
        )


class MatrixPortOut(BaseModel):
    id: str
    name: str
    type: str

    @classmethod
    def from_domain(cls, value: MatrixPort) -> MatrixPortOut:
        return cls(id=value.id, name=value.name, type=value.type)


class MatrixConnectionSchema(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_port: str = Field(alias="from")
    to_port: str = Field(alias="to")

    def to_domain(self) -> MatrixConnection:
        return MatrixConnection(from_port=self.from_port, to_port=self.to_port)

    @classmethod
    def from_domain(cls, value: MatrixConnection) -> MatrixConnectionSchema:
        return cls(from_port=value.from_port, to_port=value.to_port)


class MatrixProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    connections: list[MatrixConnectionSchema] = Field(default_factory=list)


class MatrixProfileUpdate(MatrixProfileCreate):
    pass


class MatrixProfileOut(BaseModel):
    id: str
    name: str
    version: int
    connections: list[MatrixConnectionSchema]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: MatrixProfile) -> MatrixProfileOut:
        return cls(
            id=value.id,
            name=value.name,
            version=value.version,
            connections=[
                MatrixConnectionSchema.from_domain(connection) for connection in value.connections
            ],
            created_at=value.created_at,
            updated_at=value.updated_at,
        )


class MatrixValidationOut(BaseModel):
    valid: bool
    errors: list[str]


class MatrixApplyResultOut(BaseModel):
    success: bool
    profile_id: str | None
    profile_name: str | None
    active_connections: list[MatrixConnectionSchema]
    message: str

    @classmethod
    def from_domain(cls, value: MatrixApplyResult) -> MatrixApplyResultOut:
        return cls(
            success=value.success,
            profile_id=value.profile_id,
            profile_name=value.profile_name,
            active_connections=[
                MatrixConnectionSchema.from_domain(connection)
                for connection in value.active_connections
            ],
            message=value.message,
        )


class SafetyStateOut(BaseModel):
    state: str
    reason: str | None
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: SafetyState) -> SafetyStateOut:
        return cls(state=value.state, reason=value.reason, updated_at=value.updated_at)


class EmergencyStopRequest(BaseModel):
    reason: str = Field(default="operator request", max_length=500)


class EmergencyStopOut(BaseModel):
    success: bool
    safety: SafetyStateOut
    matrix: MatrixApplyResultOut
    generator_errors: list[str] = Field(default_factory=list)
    power_supply_errors: list[str] = Field(default_factory=list)
    source_measure_unit_errors: list[str] = Field(default_factory=list)
    bidirectional_power_supply_errors: list[str] = Field(default_factory=list)


class HealthOut(BaseModel):
    status: str
    safety_state: str
    scheduler_running: bool
    devices: int
