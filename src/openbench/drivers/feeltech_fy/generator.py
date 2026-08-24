from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from dataclasses import dataclass, replace
from typing import Protocol

from openbench.core.capabilities import MeterSample
from openbench.drivers.feeltech_fy.protocol import (
    COUNTER_PARAMETERS,
    FEELTECH_PARAMETERS,
    PARAMETER_BY_CHANNEL_KEY,
    SYNC_PARAMETER_INDEX,
    FeelTechParameter,
    encode_parameter_command,
    parse_model,
    parse_parameter,
    waveform_name,
)
from openbench.drivers.feeltech_fy.transport import FeelTechDescriptor, FeelTechSerialTransport

CACHE_LIFETIME_S = 0.2
PULSE_WIDTH_COMMAND_UNIT_NS = 10
OUTPUT_RETRY_DELAY_S = 0.1


class FeelTechTransport(Protocol):
    def query(self, command: str) -> str: ...

    def write(self, command: str) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FeelTechChannelState:
    channel: int
    waveform_code: int
    waveform_name: str
    frequency_hz: float
    amplitude_vpp: float
    offset_v: float
    duty_percent: float
    phase_deg: float
    output_enabled: bool


@dataclass(frozen=True, slots=True)
class FeelTechGeneratorState:
    model: str
    channels: tuple[FeelTechChannelState, FeelTechChannelState]

    def channel(self, number: int) -> FeelTechChannelState:
        if number not in (1, 2):
            raise ValueError("FeelTech channel must be 1 or 2")
        return self.channels[number - 1]


@dataclass(frozen=True, slots=True)
class FeelTechChannelUpdate:
    waveform_code: int | None = None
    frequency_hz: float | None = None
    amplitude_vpp: float | None = None
    offset_v: float | None = None
    duty_percent: float | None = None
    phase_deg: float | None = None
    pulse_width_ns: float | None = None
    output_enabled: bool | None = None

    @property
    def changes_signal(self) -> bool:
        return any(
            value is not None
            for value in (
                self.waveform_code,
                self.frequency_hz,
                self.amplitude_vpp,
                self.offset_v,
                self.duty_percent,
                self.phase_deg,
                self.pulse_width_ns,
            )
        )


@dataclass(frozen=True, slots=True)
class FeelTechBurstState:
    trigger_mode: int | None
    trigger_source: str
    cycles: int | None


@dataclass(frozen=True, slots=True)
class FeelTechModulationState:
    ask_mode: int | None
    ask_source: str
    fsk_mode: int | None
    fsk_source: str
    fsk_secondary_frequency_hz: float | None
    psk_mode: int | None
    psk_source: str


@dataclass(frozen=True, slots=True)
class FeelTechCounterState:
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


@dataclass(frozen=True, slots=True)
class FeelTechSweepState:
    target: str
    start: float
    end: float
    duration_s: float
    mode: str
    source: str
    enabled: bool
    verified: bool = False


@dataclass(frozen=True, slots=True)
class FeelTechAdvancedState:
    burst: FeelTechBurstState
    modulation: FeelTechModulationState
    counter: FeelTechCounterState
    main_pulse_width_ns: float | None
    sweep: FeelTechSweepState | None
    unavailable_reads: tuple[str, ...]


BURST_SOURCE_NAMES = {
    0: "off",
    1: "ch2",
    2: "external",
    3: "manual",
}
KEYING_SOURCE_NAMES = {
    0: "off",
    1: "external",
    2: "manual",
}
SWEEP_TARGET_CODES = {
    "frequency": 0,
    "amplitude": 1,
    "offset": 2,
    "duty": 3,
}
SWEEP_MODE_CODES = {"linear": 0, "logarithmic": 1}
SWEEP_SOURCE_CODES = {"time": 0, "vco": 1}
COUNTER_GATE_SECONDS = {0: 1, 1: 10, 2: 100}


def _command_number(value: float, *, places: int = 6) -> str:
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def _descriptor_identity(descriptor: FeelTechDescriptor) -> str:
    source = descriptor.serial_number or descriptor.location or descriptor.port
    normalized = re.sub(r"[^a-z0-9]+", "_", source.casefold()).strip("_")
    if normalized and source != descriptor.port:
        return normalized
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


class FeelTechFYGenerator:
    def __init__(
        self,
        descriptor: FeelTechDescriptor,
        *,
        transport: FeelTechTransport | None = None,
    ) -> None:
        identity = _descriptor_identity(descriptor)
        self.device_id = f"feeltech_fy_{identity}"
        self.descriptor = descriptor
        self.model = "FY-series"
        self._transport = transport or FeelTechSerialTransport(descriptor)
        self._parameters_by_channel_id = {
            f"{self.device_id}.{parameter.channel_suffix}": parameter
            for parameter in (*FEELTECH_PARAMETERS, *COUNTER_PARAMETERS)
        }
        self._read_lock = asyncio.Lock()
        self._cache: dict[str, MeterSample] = {}
        self._cache_at = 0.0
        self._counter_paused = True
        self._counter_mode = "frequency"
        self._counter_gate_code = 0
        self._counter_coupling: str | None = None
        self._sweep_state: FeelTechSweepState | None = None
        self._unsupported_advanced_reads: set[str] = set()

    @property
    def parameters(self) -> tuple[tuple[str, FeelTechParameter], ...]:
        return tuple(self._parameters_by_channel_id.items())

    async def identify(self) -> str:
        response = await asyncio.to_thread(self._transport.query, "UMO")
        self.model = parse_model(response)
        return f"FeelElec {self.model} on {self.descriptor.port}"

    def _read_state(self) -> dict[str, MeterSample]:
        result = {
            f"{self.device_id}.{parameter.channel_suffix}": parse_parameter(
                parameter,
                self._transport.query(parameter.command),
            )
            for parameter in FEELTECH_PARAMETERS
        }
        if self._counter_paused:
            for parameter in COUNTER_PARAMETERS:
                result[f"{self.device_id}.{parameter.channel_suffix}"] = replace(
                    parse_parameter(parameter, "0"),
                    value=None,
                    status="paused",
                )
            return result

        gate_code = int(self._transport.query("RCG").strip())
        if gate_code not in COUNTER_GATE_SECONDS:
            raise RuntimeError(f"{self.model} returned unsupported counter gate code {gate_code}")
        self._counter_gate_code = gate_code
        gate_time_s = COUNTER_GATE_SECONDS[gate_code]
        for parameter in COUNTER_PARAMETERS:
            selected = (
                self._counter_mode == "both"
                or (self._counter_mode == "count" and parameter.key == "count")
                or (self._counter_mode == "frequency" and parameter.key != "count")
            )
            if not selected:
                result[f"{self.device_id}.{parameter.channel_suffix}"] = replace(
                    parse_parameter(parameter, "0"),
                    value=None,
                    status="inactive",
                )
                continue
            sample = parse_parameter(parameter, self._transport.query(parameter.command))
            if parameter.key == "frequency" and sample.value is not None:
                sample = replace(sample, value=sample.value / gate_time_s)
            result[f"{self.device_id}.{parameter.channel_suffix}"] = sample
        frequency = result[f"{self.device_id}.counter.frequency"]
        if self._counter_mode != "count" and not frequency.value:
            # FY6200 keeps stale/free-running timing registers when no counter
            # signal is present. Do not expose those numbers as measurements.
            for key in ("period", "positive_width", "negative_width", "duty"):
                channel_id = f"{self.device_id}.counter.{key}"
                result[channel_id] = replace(
                    result[channel_id],
                    value=None,
                    status="idle",
                )
        return result

    def _state_from_cache(self) -> FeelTechGeneratorState:
        channels = []
        for channel in (1, 2):
            samples = {
                key: self._cache[f"{self.device_id}.ch{channel}.{key}"]
                for key in (
                    "waveform",
                    "frequency",
                    "amplitude",
                    "offset",
                    "duty",
                    "phase",
                    "output",
                )
            }
            waveform_code = int(samples["waveform"].value or 0)
            channels.append(
                FeelTechChannelState(
                    channel=channel,
                    waveform_code=waveform_code,
                    waveform_name=waveform_name(waveform_code),
                    frequency_hz=float(samples["frequency"].value or 0),
                    amplitude_vpp=float(samples["amplitude"].value or 0),
                    offset_v=float(samples["offset"].value or 0),
                    duty_percent=float(samples["duty"].value or 0),
                    phase_deg=float(samples["phase"].value or 0),
                    output_enabled=bool(samples["output"].value),
                )
            )
        return FeelTechGeneratorState(
            model=self.model,
            channels=(channels[0], channels[1]),
        )

    async def _refresh_cache_locked(self, *, force: bool = False) -> None:
        if force or not self._cache or time.monotonic() - self._cache_at > CACHE_LIFETIME_S:
            self._cache = await asyncio.to_thread(self._read_state)
            self._cache_at = time.monotonic()

    async def read_meter(self, channel_id: str) -> MeterSample:
        if channel_id not in self._parameters_by_channel_id:
            raise ValueError(f"Unknown FeelTech FY-series channel: {channel_id}")
        async with self._read_lock:
            await self._refresh_cache_locked()
            return self._cache[channel_id]

    async def read_state(self, *, force: bool = False) -> FeelTechGeneratorState:
        async with self._read_lock:
            await self._refresh_cache_locked(force=force)
            return self._state_from_cache()

    def frequency_limit_hz(self, waveform_code: int) -> float:
        match = re.search(r"-(\d+)M$", self.model)
        sine_limit_mhz = float(match.group(1)) if match else 10.0
        if waveform_code == 0:
            return sine_limit_mhz * 1_000_000
        if waveform_code == 1:
            if sine_limit_mhz <= 20:
                return 15_000_000
            if sine_limit_mhz <= 30:
                return 20_000_000
            return 25_000_000
        return 10_000_000

    @staticmethod
    def amplitude_limit_vpp(frequency_hz: float, waveform_code: int) -> float:
        # Live FY6200-20M read-back shows that the built-in shaped waveforms
        # (triangle through Gaussian noise) clamp at 10 Vpp. Sine, square,
        # modulation, and arbitrary slots accept up to 19.999 Vpp; the exact
        # value 20.000 is clamped by this firmware and therefore is not a
        # representable verified setting.
        if 2 <= waveform_code <= 27:
            return 10.0
        if frequency_hz <= 10_000_000:
            return 19.999
        if frequency_hz <= 20_000_000:
            return 10.0
        return 5.0

    @staticmethod
    def offset_limit_v(frequency_hz: float) -> float:
        return 10.0 if frequency_hz <= 20_000_000 else 2.5

    def _validate_channel_state(self, state: FeelTechChannelState) -> None:
        if state.waveform_code not in range(95):
            raise ValueError("Waveform code must be between 0 and 94")
        frequency_limit = self.frequency_limit_hz(state.waveform_code)
        if not 0 <= state.frequency_hz <= frequency_limit:
            raise ValueError(
                f"{state.waveform_name} frequency must be between 0 and "
                f"{frequency_limit:g} Hz for {self.model}"
            )
        amplitude_limit = self.amplitude_limit_vpp(
            state.frequency_hz,
            state.waveform_code,
        )
        if not 0.001 <= state.amplitude_vpp <= amplitude_limit:
            raise ValueError(
                f"Amplitude must be between 0.001 and {amplitude_limit:g} Vpp "
                f"at {state.frequency_hz:g} Hz"
            )
        offset_limit = self.offset_limit_v(state.frequency_hz)
        if not -offset_limit <= state.offset_v <= offset_limit:
            raise ValueError(
                f"Offset must be between {-offset_limit:g} and {offset_limit:g} V "
                f"at {state.frequency_hz:g} Hz"
            )
        if not 0.01 <= state.duty_percent <= 99.99:
            raise ValueError("Duty cycle must be between 0.01 and 99.99 percent")
        if not 0 <= state.phase_deg <= 359.99:
            raise ValueError("Phase must be between 0 and 359.99 degrees")

    @staticmethod
    def _matches(
        parameter: FeelTechParameter, expected: int | float | bool, sample: MeterSample
    ) -> bool:
        actual = float(sample.value or 0)
        if parameter.key in {"waveform", "output"}:
            return int(actual) == int(expected)
        tolerance = {
            # The FY6200 accepts six decimal places in the command, but its
            # DDS read-back is quantized across the whole range. Keep the
            # relative allowance at high frequencies and a 0.1 Hz floor for
            # low-frequency settings.
            "frequency": max(0.1, abs(float(expected)) * 1e-7),
            "amplitude": 0.001,
            "offset": 0.001,
            "duty": 0.001,
            "phase": 0.1,
        }[parameter.key]
        return math.isclose(actual, float(expected), abs_tol=tolerance)

    async def _write_parameter_locked(
        self,
        parameter: FeelTechParameter,
        value: int | float | bool,
    ) -> None:
        command = encode_parameter_command(parameter, value)
        await asyncio.to_thread(self._transport.write, command)
        response = await asyncio.to_thread(self._transport.query, parameter.command)
        sample = parse_parameter(parameter, response)
        if not self._matches(parameter, value, sample):
            raise RuntimeError(
                f"{self.model} rejected {parameter.name}: requested {value}, "
                f"read back {sample.value}"
            )
        self._cache[f"{self.device_id}.{parameter.channel_suffix}"] = sample
        self._cache_at = time.monotonic()

    async def update_channel(
        self,
        channel: int,
        update: FeelTechChannelUpdate,
    ) -> FeelTechGeneratorState:
        if channel not in (1, 2):
            raise ValueError("FeelTech channel must be 1 or 2")
        async with self._read_lock:
            # The service refreshes the state immediately before applying an
            # update. Reuse that fresh snapshot instead of issuing another
            # complete dual-channel read over the relatively slow serial link.
            # Direct driver callers still refresh normally when the cache is
            # empty or older than CACHE_LIFETIME_S.
            await self._refresh_cache_locked()
            current = self._state_from_cache().channel(channel)
            proposed = replace(
                current,
                waveform_code=(
                    current.waveform_code if update.waveform_code is None else update.waveform_code
                ),
                waveform_name=waveform_name(
                    current.waveform_code if update.waveform_code is None else update.waveform_code
                ),
                frequency_hz=(
                    current.frequency_hz if update.frequency_hz is None else update.frequency_hz
                ),
                amplitude_vpp=(
                    current.amplitude_vpp if update.amplitude_vpp is None else update.amplitude_vpp
                ),
                offset_v=current.offset_v if update.offset_v is None else update.offset_v,
                duty_percent=(
                    current.duty_percent if update.duty_percent is None else update.duty_percent
                ),
                phase_deg=(current.phase_deg if update.phase_deg is None else update.phase_deg),
                output_enabled=(
                    current.output_enabled
                    if update.output_enabled is None
                    else update.output_enabled
                ),
            )
            self._validate_channel_state(proposed)
            if update.pulse_width_ns is not None:
                if channel != 1:
                    raise ValueError("Pulse width is available only on CH1")
                if not 100 <= update.pulse_width_ns <= 1_000_000_000:
                    raise ValueError("Pulse width must be between 100 ns and 1 s")
                effective_width_ns = (
                    round(update.pulse_width_ns / PULSE_WIDTH_COMMAND_UNIT_NS)
                    * PULSE_WIDTH_COMMAND_UNIT_NS
                )
                if proposed.frequency_hz > 0:
                    period_ns = 1_000_000_000 / proposed.frequency_hz
                    if effective_width_ns >= period_ns:
                        raise ValueError(
                            f"Pulse width must be shorter than the {period_ns:g} ns waveform period"
                        )

            parameter_values: tuple[tuple[str, int | float | bool | None], ...] = (
                ("waveform", update.waveform_code),
                ("frequency", update.frequency_hz),
                ("amplitude", update.amplitude_vpp),
                ("offset", update.offset_v),
                ("duty", update.duty_percent),
                ("phase", update.phase_deg),
            )
            temporarily_disabled = current.output_enabled and update.changes_signal
            output_parameter = PARAMETER_BY_CHANNEL_KEY[(channel, "output")]
            if temporarily_disabled:
                await self._write_parameter_locked(output_parameter, False)

            for key, value in parameter_values:
                if value is None:
                    continue
                parameter = PARAMETER_BY_CHANNEL_KEY[(channel, key)]
                await self._write_parameter_locked(parameter, value)

            if update.pulse_width_ns is not None:
                encoded_width = round(update.pulse_width_ns / PULSE_WIDTH_COMMAND_UNIT_NS)
                requested_width = encoded_width * PULSE_WIDTH_COMMAND_UNIT_NS
                await asyncio.to_thread(
                    self._transport.write,
                    f"WMS{encoded_width}",
                )
                read_width = self._optional_scaled(
                    await asyncio.to_thread(self._optional_query, "RSS")
                )
                if read_width is not None and not math.isclose(
                    read_width,
                    requested_width,
                    abs_tol=1,
                ):
                    raise RuntimeError(
                        f"{self.model} rejected CH1 pulse width: requested "
                        f"{requested_width} ns, read back {read_width} ns"
                    )

            desired_output = proposed.output_enabled
            output_is_enabled = current.output_enabled and not temporarily_disabled
            if desired_output != output_is_enabled:
                await self._write_parameter_locked(output_parameter, desired_output)

            return self._state_from_cache()

    async def set_channel_output(self, channel: int, enabled: bool) -> bool:
        """Write and verify one output without refreshing unrelated channel state."""
        if channel not in (1, 2):
            raise ValueError("FeelTech channel must be 1 or 2")
        parameter = PARAMETER_BY_CHANNEL_KEY[(channel, "output")]
        async with self._read_lock:
            for attempt in range(2):
                try:
                    await self._write_parameter_locked(parameter, enabled)
                    break
                except RuntimeError:
                    if attempt == 1:
                        raise
                    await asyncio.sleep(OUTPUT_RETRY_DELAY_S)
            sample = self._cache[f"{self.device_id}.{parameter.channel_suffix}"]
            return bool(sample.value)

    async def set_outputs(
        self,
        *,
        channel_1: bool,
        channel_2: bool,
    ) -> FeelTechGeneratorState:
        async with self._read_lock:
            # Populate every cached field before changing only the two output
            # entries, then return the explicitly verified cache.
            await self._refresh_cache_locked(force=True)
            for channel, enabled in ((1, channel_1), (2, channel_2)):
                parameter = PARAMETER_BY_CHANNEL_KEY[(channel, "output")]
                await self._write_parameter_locked(parameter, enabled)
            return self._state_from_cache()

    async def synchronization(self) -> dict[str, bool]:
        async with self._read_lock:
            result = {}
            for key, index in SYNC_PARAMETER_INDEX.items():
                response = await asyncio.to_thread(self._transport.query, f"RSA{index}")
                result[key] = int(float(response.strip())) != 0
            return result

    async def set_synchronization(self, key: str, enabled: bool) -> dict[str, bool]:
        try:
            index = SYNC_PARAMETER_INDEX[key]
        except KeyError as exc:
            raise ValueError(f"Unknown synchronization parameter: {key}") from exc
        async with self._read_lock:
            command = f"{'USA' if enabled else 'USD'}{index}"
            await asyncio.to_thread(self._transport.write, command)
            response = await asyncio.to_thread(self._transport.query, f"RSA{index}")
            accepted = int(float(response.strip())) != 0
            if accepted != enabled:
                raise RuntimeError(f"{self.model} did not apply {key} synchronization={enabled}")
        return await self.synchronization()

    @staticmethod
    def _integer_scaled(response: str, scale: float = 1.0) -> float:
        raw = response.strip()
        value = float(raw)
        return value if "." in raw else value / scale

    def _optional_query(self, command: str) -> str | None:
        if command in self._unsupported_advanced_reads:
            return None
        try:
            return self._transport.query(command)
        except TimeoutError:
            self._unsupported_advanced_reads.add(command)
            return None

    @staticmethod
    def _optional_int(response: str | None) -> int | None:
        return None if response is None else int(float(response))

    @classmethod
    def _optional_scaled(
        cls,
        response: str | None,
        scale: float = 1.0,
    ) -> float | None:
        return None if response is None else cls._integer_scaled(response, scale)

    def _read_advanced_state(self) -> FeelTechAdvancedState:
        trigger_mode = self._optional_int(self._optional_query("RPM"))
        cycles = self._optional_int(self._optional_query("RPN"))
        ask_mode = self._optional_int(self._optional_query("RTA"))
        fsk_mode = self._optional_int(self._optional_query("RTF"))
        fsk_frequency = self._optional_scaled(self._optional_query("RFK"), 10)
        psk_mode = self._optional_int(self._optional_query("RTP"))
        main_pulse_width_ns = self._optional_scaled(self._optional_query("RSS"))

        gate_code = self._counter_gate_code
        gate_time_s = COUNTER_GATE_SECONDS[gate_code]
        frequency_hz: float | None = None
        count: int | None = None
        period_ns: float | None = None
        positive_width_ns: float | None = None
        negative_width_ns: float | None = None
        duty_percent: float | None = None
        if not self._counter_paused:
            read_gate_code = self._optional_int(self._optional_query("RCG"))
            if read_gate_code is not None and read_gate_code not in COUNTER_GATE_SECONDS:
                raise RuntimeError(
                    f"{self.model} returned unsupported counter gate code {read_gate_code}"
                )
            if read_gate_code is not None:
                gate_code = read_gate_code
                self._counter_gate_code = read_gate_code
                gate_time_s = COUNTER_GATE_SECONDS[read_gate_code]
            if self._counter_mode in {"count", "both"}:
                count = self._optional_int(self._optional_query("RCC"))
            if self._counter_mode in {"frequency", "both"}:
                raw_frequency = self._optional_scaled(self._optional_query("RCF"))
                frequency_hz = raw_frequency / gate_time_s if raw_frequency is not None else None
                period_ns = self._optional_scaled(self._optional_query("RCT"))
                positive_width_ns = self._optional_scaled(self._optional_query("RC+"))
                negative_width_ns = self._optional_scaled(self._optional_query("RC-"))
                duty_percent = self._optional_scaled(self._optional_query("RCD"), 10)
        if self._counter_mode != "count" and not frequency_hz:
            period_ns = None
            positive_width_ns = None
            negative_width_ns = None
            duty_percent = None

        return FeelTechAdvancedState(
            burst=FeelTechBurstState(
                trigger_mode=trigger_mode,
                trigger_source=(
                    BURST_SOURCE_NAMES.get(trigger_mode, f"mode-{trigger_mode}")
                    if trigger_mode is not None
                    else "unavailable"
                ),
                cycles=cycles,
            ),
            modulation=FeelTechModulationState(
                ask_mode=ask_mode,
                ask_source=(
                    KEYING_SOURCE_NAMES.get(ask_mode, f"mode-{ask_mode}")
                    if ask_mode is not None
                    else "unavailable"
                ),
                fsk_mode=fsk_mode,
                fsk_source=(
                    KEYING_SOURCE_NAMES.get(fsk_mode, f"mode-{fsk_mode}")
                    if fsk_mode is not None
                    else "unavailable"
                ),
                fsk_secondary_frequency_hz=fsk_frequency,
                psk_mode=psk_mode,
                psk_source=(
                    KEYING_SOURCE_NAMES.get(psk_mode, f"mode-{psk_mode}")
                    if psk_mode is not None
                    else "unavailable"
                ),
            ),
            counter=FeelTechCounterState(
                paused=self._counter_paused,
                mode=self._counter_mode,
                gate_code=gate_code,
                gate_time_s=gate_time_s,
                coupling=self._counter_coupling,
                frequency_hz=frequency_hz,
                count=count,
                period_ns=period_ns,
                positive_width_ns=positive_width_ns,
                negative_width_ns=negative_width_ns,
                duty_percent=duty_percent,
            ),
            main_pulse_width_ns=main_pulse_width_ns,
            sweep=self._sweep_state,
            unavailable_reads=tuple(sorted(self._unsupported_advanced_reads)),
        )

    async def read_advanced_state(self) -> FeelTechAdvancedState:
        async with self._read_lock:
            return await asyncio.to_thread(self._read_advanced_state)

    async def configure_burst(
        self,
        *,
        trigger_mode: int,
        cycles: int,
    ) -> FeelTechAdvancedState:
        if trigger_mode not in (0, 1, 2):
            raise ValueError("Burst source must be off, CH2, or external")
        if not 1 <= cycles <= 1_048_575:
            raise ValueError("Burst cycles must be between 1 and 1048575")
        async with self._read_lock:
            await asyncio.to_thread(self._transport.write, f"WPN{cycles}")
            accepted_cycles = self._optional_int(
                await asyncio.to_thread(self._optional_query, "RPN")
            )
            if accepted_cycles is not None and accepted_cycles != cycles:
                raise RuntimeError(
                    f"{self.model} rejected burst cycles: requested {cycles}, "
                    f"read back {accepted_cycles}"
                )
            await asyncio.to_thread(self._transport.write, f"WPM{trigger_mode}")
            accepted_mode = self._optional_int(await asyncio.to_thread(self._optional_query, "RPM"))
            if accepted_mode is not None and accepted_mode != trigger_mode:
                raise RuntimeError(
                    f"{self.model} rejected burst source: requested {trigger_mode}, "
                    f"read back {accepted_mode}"
                )
            return await asyncio.to_thread(self._read_advanced_state)

    async def trigger_once(self, *, cycles: int | None = None) -> FeelTechAdvancedState:
        if cycles is not None and not 1 <= cycles <= 1_048_575:
            raise ValueError("Burst cycles must be between 1 and 1048575")
        async with self._read_lock:
            if cycles is not None:
                await asyncio.to_thread(self._transport.write, f"WPN{cycles}")
                accepted_cycles = self._optional_int(
                    await asyncio.to_thread(self._optional_query, "RPN")
                )
                if accepted_cycles is not None and accepted_cycles != cycles:
                    raise RuntimeError(
                        f"{self.model} rejected burst cycles: requested {cycles}, "
                        f"read back {accepted_cycles}"
                    )
            await asyncio.to_thread(self._transport.write, "WPM3")
            return await asyncio.to_thread(self._read_advanced_state)

    async def configure_keying(
        self,
        *,
        kind: str,
        mode: int,
        secondary_frequency_hz: float | None = None,
    ) -> FeelTechAdvancedState:
        normalized_kind = kind.casefold()
        commands = {
            "ask": ("WTA", "RTA"),
            "fsk": ("WTF", "RTF"),
            "psk": ("WTP", "RTP"),
        }
        if normalized_kind not in commands:
            raise ValueError("Keying kind must be ASK, FSK, or PSK")
        if mode not in (0, 1, 2):
            raise ValueError("Keying source must be off, external, or manual")
        if secondary_frequency_hz is not None:
            if normalized_kind != "fsk":
                raise ValueError("Secondary frequency is only valid for FSK")
            if not 0.000001 <= secondary_frequency_hz <= 10_000_000:
                raise ValueError("FSK secondary frequency must be 1 µHz to 10 MHz")

        async with self._read_lock:
            if secondary_frequency_hz is not None:
                secondary_microhertz = round(secondary_frequency_hz * 1_000_000)
                await asyncio.to_thread(
                    self._transport.write,
                    f"WFK{secondary_microhertz:014d}",
                )
                accepted_frequency = self._optional_scaled(
                    await asyncio.to_thread(self._optional_query, "RFK"),
                    10,
                )
                if accepted_frequency is not None and not math.isclose(
                    accepted_frequency, secondary_frequency_hz, abs_tol=0.1
                ):
                    raise RuntimeError(
                        f"{self.model} rejected FSK secondary frequency: requested "
                        f"{secondary_frequency_hz}, read back {accepted_frequency}"
                    )

            write_command, read_command = commands[normalized_kind]
            await asyncio.to_thread(self._transport.write, f"{write_command}{mode}")
            accepted_mode = self._optional_int(
                await asyncio.to_thread(self._optional_query, read_command)
            )
            if accepted_mode is not None and accepted_mode != mode:
                raise RuntimeError(
                    f"{self.model} rejected {normalized_kind.upper()} source: "
                    f"requested {mode}, read back {accepted_mode}"
                )
            return await asyncio.to_thread(self._read_advanced_state)

    async def configure_counter(
        self,
        *,
        gate_code: int,
        coupling: str,
        mode: str = "frequency",
    ) -> FeelTechAdvancedState:
        if gate_code not in COUNTER_GATE_SECONDS:
            raise ValueError("Counter gate must be 1, 10, or 100 seconds")
        normalized_coupling = coupling.casefold()
        if normalized_coupling not in {"dc", "ac"}:
            raise ValueError("Counter coupling must be DC or AC")
        normalized_mode = mode.casefold()
        if normalized_mode not in {"frequency", "count", "both"}:
            raise ValueError("Counter mode must be frequency, count, or both")
        async with self._read_lock:
            await asyncio.to_thread(self._transport.write, f"WCG{gate_code}")
            accepted_gate = self._optional_int(await asyncio.to_thread(self._optional_query, "RCG"))
            if accepted_gate is not None and accepted_gate != gate_code:
                raise RuntimeError(
                    f"{self.model} rejected counter gate: requested {gate_code}, "
                    f"read back {accepted_gate}"
                )
            await asyncio.to_thread(
                self._transport.write,
                f"WCC{1 if normalized_coupling == 'ac' else 0}",
            )
            self._counter_paused = False
            self._counter_mode = normalized_mode
            self._counter_gate_code = gate_code
            self._counter_coupling = normalized_coupling
            self._cache_at = 0.0
            return await asyncio.to_thread(self._read_advanced_state)

    async def pause_counter(self) -> FeelTechAdvancedState:
        async with self._read_lock:
            await asyncio.to_thread(self._transport.write, "WCP0")
            self._counter_paused = True
            self._cache_at = 0.0
            return await asyncio.to_thread(self._read_advanced_state)

    async def reset_counter(self) -> FeelTechAdvancedState:
        async with self._read_lock:
            await asyncio.to_thread(self._transport.write, "WCZ0")
            return await asyncio.to_thread(self._read_advanced_state)

    async def configure_sweep(
        self,
        *,
        target: str,
        start: float,
        end: float,
        duration_s: float,
        mode: str,
        source: str,
        enabled: bool,
    ) -> FeelTechSweepState:
        normalized_target = target.casefold()
        normalized_mode = mode.casefold()
        normalized_source = source.casefold()
        if normalized_target not in SWEEP_TARGET_CODES:
            raise ValueError("Sweep target must be frequency, amplitude, offset, or duty")
        if normalized_mode not in SWEEP_MODE_CODES:
            raise ValueError("Sweep mode must be linear or logarithmic")
        if normalized_source not in SWEEP_SOURCE_CODES:
            raise ValueError("Sweep source must be time or VCO")
        if not 0.01 <= duration_s <= 999.99:
            raise ValueError("Sweep duration must be between 0.01 and 999.99 seconds")

        limits = {
            "frequency": (0.000001, self.frequency_limit_hz(0)),
            "amplitude": (0.001, 19.999),
            "offset": (-10.0, 10.0),
            "duty": (0.01, 99.99),
        }
        minimum, maximum = limits[normalized_target]
        if not minimum <= start <= maximum or not minimum <= end <= maximum:
            raise ValueError(
                f"Sweep {normalized_target} endpoints must be between {minimum:g} and {maximum:g}"
            )
        if normalized_mode == "logarithmic" and (start <= 0 or end <= 0):
            raise ValueError("Logarithmic sweep endpoints must be positive")

        state = FeelTechSweepState(
            target=normalized_target,
            start=start,
            end=end,
            duration_s=duration_s,
            mode=normalized_mode,
            source=normalized_source,
            enabled=enabled,
        )
        commands = (
            "SBE0",
            f"SOB{SWEEP_TARGET_CODES[normalized_target]}",
            f"SST{_command_number(start)}",
            f"SEN{_command_number(end)}",
            f"STI{_command_number(duration_s, places=2)}",
            f"SMO{SWEEP_MODE_CODES[normalized_mode]}",
            f"SXY{SWEEP_SOURCE_CODES[normalized_source]}",
            f"SBE{1 if enabled else 0}",
        )
        async with self._read_lock:
            for command in commands:
                await asyncio.to_thread(self._transport.write, command)
            self._sweep_state = state
        return state

    async def save_preset(self, slot: int) -> None:
        if not 1 <= slot <= 20:
            raise ValueError("Preset slot must be between 1 and 20")
        async with self._read_lock:
            await asyncio.to_thread(self._transport.write, f"USN{slot:02d}")

    async def load_preset(self, slot: int) -> FeelTechGeneratorState:
        if not 1 <= slot <= 20:
            raise ValueError("Preset slot must be between 1 and 20")
        async with self._read_lock:
            await asyncio.to_thread(self._transport.write, f"ULN{slot:02d}")
            await self._refresh_cache_locked(force=True)
            return self._state_from_cache()

    async def close(self) -> None:
        await asyncio.to_thread(self._transport.close)
