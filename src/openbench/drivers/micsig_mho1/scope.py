from __future__ import annotations

import asyncio
import csv
import io
import math
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Protocol

from openbench.drivers.micsig_common import (
    MICSIG_MAXIMUM_ASCII_CHUNK_POINTS,
    MICSIG_MAXIMUM_MEMORY_POINTS,
    MicsigMaximumAsciiChunk,
    MicsigMaximumCaptureInfo,
)
from openbench.drivers.micsig_mho1.protocol import (
    DEFAULT_SCALAR_MEASUREMENTS,
    MAX_SCALAR_MEASUREMENTS,
    MICSIG_DELAY_EDGES,
    SCALAR_MEASUREMENT_COMMANDS,
    SCALAR_MEASUREMENT_MULTIPLIERS,
    MicsigChannelState,
    MicsigChannelUpdate,
    MicsigDmmSupport,
    MicsigFastBinaryProbe,
    MicsigProtocolError,
    MicsigScalarMeasurement,
    MicsigScalarMeasurementSpec,
    MicsigScopeStatus,
    MicsigScopeUpdate,
    MicsigScreenshot,
    MicsigScreenshotProbe,
    MicsigSnapshot,
    MicsigStoredWaveform,
    MicsigTriggerState,
    MicsigTriggerUpdate,
    MicsigWaveformCapture,
    channel_source,
    format_scpi_number,
    normalize_channel,
    normalize_screenshot_image,
    parse_ascii_waveform,
    parse_bool,
    parse_fast_binary_waveform,
    parse_identification,
    parse_optional_scpi_float,
    parse_scpi_float,
    parse_scpi_int,
    parse_waveform_preamble,
)
from openbench.drivers.micsig_mho1.transport import (
    MicsigDescriptor,
    MicsigScpiTransport,
)

SCREENSHOT_MIN_INTERVAL_S = 1.0
MEASUREMENT_COMMAND_DELAY_S = 0.1
STORAGE_COMMAND_DELAY_S = 0.005
RUN_SETTLE_DELAY_S = 0.05
STOP_SETTLE_DELAY_S = 0.1
STORAGE_FILE_SETTLE_DELAY_S = 0.1


class MicsigTransport(Protocol):
    async def query_text(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
        attempts: int = 2,
    ) -> str: ...

    async def query_block(
        self,
        command: str,
        *,
        length_multiplier: int = 1,
    ) -> bytes: ...

    async def query_ascii_block(self, command: str) -> bytes: ...

    async def write(self, command: str) -> None: ...

    async def capture_stored_screenshot(self, *, timeout_s: float = 5.0) -> bytes: ...

    async def query_vxi11_raw(self, command: str) -> bytes: ...

    async def list_http_links(self, path: str = "/") -> tuple[str, ...]: ...

    async def download_http_file(self, path: str) -> bytes: ...

    async def close(self) -> None: ...


class MicsigMHO1Scope:
    """Bounded automation driver for capture and coordinated MHO1 experiments."""

    def __init__(
        self,
        descriptor: MicsigDescriptor,
        *,
        transport: MicsigTransport | None = None,
    ) -> None:
        model = re.sub(r"[^a-z0-9]+", "_", descriptor.model.lower()).strip("_")
        serial = re.sub(
            r"[^a-z0-9]+",
            "_",
            descriptor.serial_number.lower(),
        ).strip("_")
        self.device_id = f"micsig_{model}_{serial}"
        self.descriptor = descriptor
        self._transport = transport or MicsigScpiTransport(descriptor)
        self._operation_lock = asyncio.Lock()
        self._cached_screenshot: MicsigScreenshot | None = None
        self._cached_screenshot_completed_at = 0.0
        self._last_screenshot_command_at = 0.0

    @property
    def screenshot_supported(self) -> bool:
        return True

    @property
    def calibrated_waveform_transfer_available(self) -> bool:
        return True

    @property
    def waveform_transfer_method(self) -> str:
        # Firmware 2.154.75 returns an empty DATA:BIN block. Its documented,
        # fast ASCII block uses the same numeric waveform transport without
        # depending on that broken binary encoding.
        if self.descriptor.firmware_version == "2.154.75":
            return "fast_ascii"
        return "fast_binary"

    @property
    def dmm_support(self) -> MicsigDmmSupport:
        hardware_present = self.descriptor.identification.has_integrated_multimeter
        if hardware_present:
            reason = (
                "The integrated DMM is present, but Micsig firmware "
                f"{self.descriptor.firmware_version} exposes no documented or "
                "working SCPI DMM query."
            )
        else:
            reason = "This MHO1 model does not include the integrated DMM."
        return MicsigDmmSupport(
            hardware_present=hardware_present,
            direct_protocol_available=False,
            reason=reason,
        )

    async def identify(self) -> str:
        async with self._operation_lock:
            identification = parse_identification(await self._transport.query_text("*IDN?"))
        if identification.serial_number != self.descriptor.serial_number:
            raise MicsigProtocolError(
                f"Expected Micsig serial {self.descriptor.serial_number}, "
                f"got {identification.serial_number}"
            )
        return (
            f"{identification.manufacturer} {identification.model} "
            f"SN {identification.serial_number} FW {identification.firmware_version}"
        )

    async def acquisition_status(self) -> str:
        async with self._operation_lock:
            return (await self._transport.query_text(":TRIGger:STATus?")).strip().upper()

    @staticmethod
    def _enum(response: str, *, command: str, allowed: set[str]) -> str:
        normalized = response.strip().upper()
        if normalized.startswith("ERROR:") or normalized not in allowed:
            raise MicsigProtocolError(f"Unexpected {command} response: {response!r}")
        return normalized

    async def _read_state_locked(self) -> MicsigScopeStatus:
        acquisition_type = self._enum(
            await self._transport.query_text(":ACQuire:TYPE?"),
            command=":ACQuire:TYPE?",
            allowed={"NORMAL", "MEAN", "ENVELOP", "PEAK"},
        )
        averaging_count = parse_scpi_int(await self._transport.query_text(":ACQuire:MEAN?"))
        sample_rate = parse_scpi_float(await self._transport.query_text(":ACQuire:SRATe?"))
        memory_setting = (await self._transport.query_text(":ACQuire:DEPSelect?")).strip().upper()
        if memory_setting.startswith("ERROR:") or not memory_setting:
            raise MicsigProtocolError(
                f"Unexpected :ACQuire:DEPSelect? response: {memory_setting!r}"
            )
        memory_depth = parse_scpi_int(await self._transport.query_text(":ACQuire:DEPTh?"))
        timebase = parse_scpi_float(await self._transport.query_text(":TIMebase:EXTent?"))
        timebase_position = parse_scpi_float(
            await self._transport.query_text(":TIMebase:POSition?")
        )
        timebase_mode = self._enum(
            await self._transport.query_text(":TIMebase:MODE?"),
            command=":TIMebase:MODE?",
            allowed={"YT", "XY"},
        )

        channels = []
        for number in range(1, 5):
            prefix = f":CHANnel{number}"
            channels.append(
                MicsigChannelState(
                    channel=number,
                    displayed=parse_bool(await self._transport.query_text(f"{prefix}:DISPlay?")),
                    scale_v_per_div=parse_scpi_float(
                        await self._transport.query_text(f"{prefix}:SCALe?")
                    ),
                    position=parse_scpi_float(
                        await self._transport.query_text(f"{prefix}:POSition?")
                    ),
                    coupling=self._enum(
                        await self._transport.query_text(f"{prefix}:COUPle?"),
                        command=f"{prefix}:COUPle?",
                        allowed={"AC", "DC", "GND"},
                    ),
                    probe_attenuation=parse_scpi_float(
                        await self._transport.query_text(f"{prefix}:PROBe?")
                    ),
                    input_impedance=self._enum(
                        await self._transport.query_text(f"{prefix}:INPutres?"),
                        command=f"{prefix}:INPutres?",
                        allowed={"MEGA", "FIFTY"},
                    ),
                )
            )

        trigger_type = self._enum(
            await self._transport.query_text(":TRIGger:TYPE?"),
            command=":TRIGger:TYPE?",
            allowed={
                "EDGE",
                "PULSE",
                "LOGIC",
                "NEDGE",
                "DWART",
                "SLOPE",
                "TIMEOUT",
                "VIDEO",
                "S1",
                "S2",
            },
        )
        trigger_mode = self._enum(
            await self._transport.query_text(":TRIGger:MODE?"),
            command=":TRIGger:MODE?",
            allowed={"AUTO", "NORMAL"},
        )
        trigger_status = self._enum(
            await self._transport.query_text(":TRIGger:STATus?"),
            command=":TRIGger:STATus?",
            allowed={"RUN", "WAIT", "AUTO", "STOP"},
        )
        trigger_source = self._enum(
            await self._transport.query_text(":TRIGger:EDGE:SOURce?"),
            command=":TRIGger:EDGE:SOURce?",
            allowed={"CH1", "CH2", "CH3", "CH4"},
        )
        trigger_slope = self._enum(
            await self._transport.query_text(":TRIGger:EDGE:SLOPe?"),
            command=":TRIGger:EDGE:SLOPe?",
            allowed={"RISE", "FALL", "DUAL"},
        )
        trigger_level = parse_scpi_float(await self._transport.query_text(":TRIGger:EDGE:LEVel?"))
        trigger_coupling = self._enum(
            await self._transport.query_text(":TRIGger:EDGE:COUPle?"),
            command=":TRIGger:EDGE:COUPle?",
            allowed={"DC", "AC", "HFREJ", "LFREJ", "NOISEREJ"},
        )
        waveform_source = self._enum(
            await self._transport.query_text(":WAVeform:SOURce?"),
            command=":WAVeform:SOURce?",
            allowed={"CH1", "CH2", "CH3", "CH4"},
        )
        waveform_mode = self._enum(
            await self._transport.query_text(":WAVeform:MODE?"),
            command=":WAVeform:MODE?",
            allowed={"NORMAL", "MAXIMUM", "RAW"},
        )
        waveform_format = self._enum(
            await self._transport.query_text(":WAVeform:FORMat?"),
            command=":WAVeform:FORMat?",
            allowed={"WORD", "ASCII"},
        )
        return MicsigScopeStatus(
            acquisition_type=acquisition_type,
            averaging_count=averaging_count,
            sample_rate_sps=sample_rate,
            memory_depth_setting=memory_setting,
            memory_depth_points=memory_depth,
            timebase_s_per_div=timebase,
            timebase_position_s=timebase_position,
            timebase_mode=timebase_mode,
            channels=(channels[0], channels[1], channels[2], channels[3]),
            trigger=MicsigTriggerState(
                trigger_type=trigger_type,
                mode=trigger_mode,
                status=trigger_status,
                source=trigger_source,
                slope=trigger_slope,
                level_v=trigger_level,
                coupling=trigger_coupling,
            ),
            waveform_source=waveform_source,
            waveform_mode=waveform_mode,
            waveform_format=waveform_format,
        )

    async def read_state(self) -> MicsigScopeStatus:
        async with self._operation_lock:
            return await self._read_state_locked()

    async def _write_readback_locked(self, command: str, query: str) -> str:
        await self._transport.write(command)
        response = (await self._transport.query_text(query)).strip()
        if not response or response.upper().startswith("ERROR:"):
            raise MicsigProtocolError(f"Micsig rejected {command}: {response!r}")
        return response

    @staticmethod
    def _validate_update(update: MicsigScopeUpdate) -> None:
        if update.acquisition_type is not None and update.acquisition_type.upper() not in {
            "NORMAL",
            "MEAN",
            "ENVELOP",
            "PEAK",
        }:
            raise ValueError("Micsig acquisition type must be NORMAL, MEAN, ENVELOP, or PEAK")
        if update.averaging_count is not None and update.averaging_count not in {
            2,
            4,
            8,
            16,
            32,
            64,
            128,
            256,
        }:
            raise ValueError("Micsig averaging count must be 2, 4, 8, 16, 32, 64, 128, or 256")
        if update.timebase_s_per_div is not None and update.timebase_s_per_div <= 0:
            raise ValueError("Micsig timebase must be positive")
        if update.timebase_mode is not None and update.timebase_mode.upper() not in {"YT", "XY"}:
            raise ValueError("Micsig timebase mode must be YT or XY")
        seen: set[int] = set()
        for channel in update.channels:
            number = normalize_channel(channel.channel)
            if number in seen:
                raise ValueError(f"Micsig channel {number} appears more than once")
            seen.add(number)
            if channel.scale_v_per_div is not None and channel.scale_v_per_div <= 0:
                raise ValueError("Micsig channel scale must be positive")
            if channel.probe_attenuation is not None and channel.probe_attenuation <= 0:
                raise ValueError("Micsig probe attenuation must be positive")
            if channel.coupling is not None and channel.coupling.upper() not in {"AC", "DC", "GND"}:
                raise ValueError("Micsig channel coupling must be AC, DC, or GND")
            if channel.input_impedance is not None and channel.input_impedance.upper() not in {
                "MEGA",
                "FIFTY",
            }:
                raise ValueError("Micsig input impedance must be MEGA or FIFTY")
        trigger = update.trigger
        if trigger is None:
            return
        if trigger.trigger_type is not None and trigger.trigger_type.upper() != "EDGE":
            raise ValueError("The bounded OpenBench scope controller supports EDGE trigger only")
        if trigger.mode is not None and trigger.mode.upper() not in {"AUTO", "NORMAL"}:
            raise ValueError("Micsig trigger mode must be AUTO or NORMAL")
        if trigger.source is not None:
            normalize_channel(trigger.source)
        if trigger.slope is not None and trigger.slope.upper() not in {"RISE", "FALL", "DUAL"}:
            raise ValueError("Micsig edge slope must be RISE, FALL, or DUAL")
        if trigger.coupling is not None and trigger.coupling.upper() not in {
            "DC",
            "AC",
            "HFREJ",
            "LFREJ",
            "NOISEREJ",
        }:
            raise ValueError("Unsupported Micsig edge trigger coupling")

    async def _write_update_locked(self, update: MicsigScopeUpdate) -> None:
        self._validate_update(update)
        for channel in update.channels:
            prefix = f":CHANnel{normalize_channel(channel.channel)}"
            if channel.displayed is not None:
                await self._write_readback_locked(
                    f"{prefix}:DISPlay {'ON' if channel.displayed else 'OFF'}",
                    f"{prefix}:DISPlay?",
                )
            if channel.probe_attenuation is not None:
                await self._write_readback_locked(
                    f"{prefix}:PROBe {format_scpi_number(channel.probe_attenuation)}",
                    f"{prefix}:PROBe?",
                )
            if channel.coupling is not None:
                await self._write_readback_locked(
                    f"{prefix}:COUPle {channel.coupling.upper()}",
                    f"{prefix}:COUPle?",
                )
            if channel.input_impedance is not None:
                await self._write_readback_locked(
                    f"{prefix}:INPutres {channel.input_impedance.upper()}",
                    f"{prefix}:INPutres?",
                )
            if channel.scale_v_per_div is not None:
                await self._write_readback_locked(
                    f"{prefix}:SCALe {format_scpi_number(channel.scale_v_per_div)}",
                    f"{prefix}:SCALe?",
                )
            if channel.position is not None:
                await self._write_readback_locked(
                    f"{prefix}:POSition {format_scpi_number(channel.position)}",
                    f"{prefix}:POSition?",
                )

        if update.acquisition_type is not None:
            await self._write_readback_locked(
                f":ACQuire:TYPE {update.acquisition_type.upper()}",
                ":ACQuire:TYPE?",
            )
        if update.averaging_count is not None:
            await self._write_readback_locked(
                f":ACQuire:MEAN {update.averaging_count}",
                ":ACQuire:MEAN?",
            )
        if update.memory_depth_setting is not None:
            normalized_depth = update.memory_depth_setting.strip().upper()
            if normalized_depth != "AUTO":
                try:
                    depth = int(normalized_depth)
                except ValueError as exc:
                    raise ValueError("Micsig memory depth must be AUTO or an integer") from exc
                if depth <= 0:
                    raise ValueError("Micsig memory depth must be positive")
            await self._write_readback_locked(
                f":ACQuire:DEPSelect {normalized_depth}",
                ":ACQuire:DEPSelect?",
            )
        if update.timebase_mode is not None:
            await self._write_readback_locked(
                f":TIMebase:MODE {update.timebase_mode.upper()}",
                ":TIMebase:MODE?",
            )
        if update.timebase_s_per_div is not None:
            response = await self._write_readback_locked(
                f":TIMebase:EXTent {format_scpi_number(update.timebase_s_per_div)}",
                ":TIMebase:EXTent?",
            )
            accepted = parse_scpi_float(response)
            if not math.isclose(
                accepted,
                update.timebase_s_per_div,
                rel_tol=1e-9,
                abs_tol=1e-15,
            ):
                raise MicsigProtocolError(
                    "Micsig rejected the horizontal timebase gear: "
                    f"requested {update.timebase_s_per_div:g} s/div, "
                    f"read back {accepted:g} s/div"
                )
        if update.timebase_position_s is not None:
            await self._write_readback_locked(
                f":TIMebase:POSition {format_scpi_number(update.timebase_position_s)}",
                ":TIMebase:POSition?",
            )

        trigger = update.trigger
        if trigger is not None:
            if trigger.trigger_type is not None:
                await self._write_readback_locked(
                    f":TRIGger:TYPE {trigger.trigger_type.upper()}",
                    ":TRIGger:TYPE?",
                )
            if trigger.source is not None:
                await self._write_readback_locked(
                    f":TRIGger:EDGE:SOURce {channel_source(trigger.source)}",
                    ":TRIGger:EDGE:SOURce?",
                )
            if trigger.slope is not None:
                await self._write_readback_locked(
                    f":TRIGger:EDGE:SLOPe {trigger.slope.upper()}",
                    ":TRIGger:EDGE:SLOPe?",
                )
            if trigger.level_v is not None:
                await self._write_readback_locked(
                    f":TRIGger:EDGE:LEVel {format_scpi_number(trigger.level_v)}",
                    ":TRIGger:EDGE:LEVel?",
                )
            if trigger.coupling is not None:
                await self._write_readback_locked(
                    f":TRIGger:EDGE:COUPle {trigger.coupling.upper()}",
                    ":TRIGger:EDGE:COUPle?",
                )
            if trigger.mode is not None:
                await self._write_readback_locked(
                    f":TRIGger:MODE {trigger.mode.upper()}",
                    ":TRIGger:MODE?",
                )

    async def apply_update(self, update: MicsigScopeUpdate) -> MicsigScopeStatus:
        async with self._operation_lock:
            await self._write_update_locked(update)
            return await self._read_state_locked()

    async def apply_update_without_snapshot(self, update: MicsigScopeUpdate) -> None:
        """Apply a verified bounded update without rereading unrelated settings."""
        async with self._operation_lock:
            await self._write_update_locked(update)

    @staticmethod
    def update_from_state(state: MicsigScopeStatus) -> MicsigScopeUpdate:
        return MicsigScopeUpdate(
            channels=tuple(
                MicsigChannelUpdate(
                    channel=item.channel,
                    displayed=item.displayed,
                    scale_v_per_div=item.scale_v_per_div,
                    position=item.position,
                    coupling=item.coupling,
                    probe_attenuation=item.probe_attenuation,
                    input_impedance=item.input_impedance,
                )
                for item in state.channels
            ),
            acquisition_type=state.acquisition_type,
            averaging_count=state.averaging_count,
            memory_depth_setting=state.memory_depth_setting,
            timebase_s_per_div=state.timebase_s_per_div,
            timebase_position_s=state.timebase_position_s,
            timebase_mode=state.timebase_mode,
            trigger=MicsigTriggerUpdate(
                trigger_type=state.trigger.trigger_type,
                mode=state.trigger.mode,
                source=state.trigger.source,
                slope=state.trigger.slope,
                level_v=state.trigger.level_v,
                coupling=state.trigger.coupling,
            ),
        )

    async def restore_state(self, state: MicsigScopeStatus) -> MicsigScopeStatus:
        async with self._operation_lock:
            await self._write_menu_action_locked(":MENU:STOP")
            await self._wait_until_stopped_locked(timeout_s=2.0, action="state restore")
            await self._write_update_locked(self.update_from_state(state))
            await self._write_readback_locked(
                f":WAVeform:SOURce {state.waveform_source}",
                ":WAVeform:SOURce?",
            )
            await self._write_readback_locked(
                f":WAVeform:MODE {state.waveform_mode}",
                ":WAVeform:MODE?",
            )
            await self._write_readback_locked(
                f":WAVeform:FORMat {state.waveform_format}",
                ":WAVeform:FORMat?",
            )
            if state.trigger.status != "STOP":
                await self._write_menu_action_locked(":MENU:RUN")
            return await self._read_state_locked()

    async def capture_waveforms(
        self,
        channels: Sequence[int | str],
        *,
        mode: str = "NORMAL",
    ) -> tuple[MicsigWaveformCapture, ...]:
        sources = tuple(dict.fromkeys(channel_source(channel) for channel in channels))
        if not sources:
            raise ValueError("At least one Micsig waveform channel is required")
        normalized_mode = mode.strip().upper()
        if normalized_mode not in {"NORMAL", "MAXIMUM", "RAW"}:
            raise ValueError("Micsig waveform mode must be NORMAL, MAXIMUM, or RAW")
        if self.waveform_transfer_method == "fast_ascii":
            return await self._capture_fast_ascii_waveforms(
                sources,
                mode=normalized_mode,
            )

        async with self._operation_lock:
            initial_source = self._enum(
                await self._transport.query_text(":WAVeform:SOURce?"),
                command=":WAVeform:SOURce?",
                allowed={"CH1", "CH2", "CH3", "CH4"},
            )
            initial_mode = self._enum(
                await self._transport.query_text(":WAVeform:MODE?"),
                command=":WAVeform:MODE?",
                allowed={"NORMAL", "MAXIMUM", "RAW"},
            )
            captures: list[MicsigWaveformCapture] = []
            try:
                await self._write_readback_locked(
                    f":WAVeform:MODE {normalized_mode}",
                    ":WAVeform:MODE?",
                )
                for source in sources:
                    await self._write_readback_locked(
                        f":WAVeform:SOURce {source}",
                        ":WAVeform:SOURce?",
                    )
                    payload = await self._transport.query_block(
                        ":WAVeform:DATA:BIN?",
                        length_multiplier=4,
                    )
                    preamble = parse_waveform_preamble(
                        await self._transport.query_text(":WAVeform:PREamble?")
                    )
                    captures.append(
                        MicsigWaveformCapture(
                            source=source,
                            mode=normalized_mode,
                            samples=parse_fast_binary_waveform(payload),
                            preamble=preamble,
                        )
                    )
            finally:
                await self._write_readback_locked(
                    f":WAVeform:SOURce {initial_source}",
                    ":WAVeform:SOURce?",
                )
                await self._write_readback_locked(
                    f":WAVeform:MODE {initial_mode}",
                    ":WAVeform:MODE?",
                )
        return tuple(captures)

    async def maximum_ascii_capture_info(self) -> MicsigMaximumCaptureInfo:
        """Validate STOP and report current MHO1 memory depth without changing it."""
        async with self._operation_lock:
            return await self._maximum_ascii_capture_info_locked()

    async def _maximum_ascii_capture_info_locked(self) -> MicsigMaximumCaptureInfo:
        status = self._enum(
            await self._query_text_fresh_locked(":TRIGger:STATus?"),
            command=":TRIGger:STATus?",
            allowed={"RUN", "WAIT", "AUTO", "STOP"},
        )
        if status != "STOP":
            raise RuntimeError(
                "MHO1 MAXIMUM ASCII capture requires the oscilloscope to already be in STOP"
            )
        points = parse_scpi_int(await self._query_text_fresh_locked(":ACQuire:DEPTh?"))
        if not 1 <= points <= MICSIG_MAXIMUM_MEMORY_POINTS:
            raise MicsigProtocolError(f"MHO1 reported unsupported memory depth: {points} points")
        return MicsigMaximumCaptureInfo(memory_depth_points=points)

    async def stream_maximum_ascii(
        self,
        channels: Sequence[int | str],
        *,
        on_chunk: Callable[[MicsigMaximumAsciiChunk], Awaitable[None]],
    ) -> MicsigMaximumCaptureInfo:
        """Stream current stopped MHO1 memory through documented MAXIMUM ASCII."""
        sources = tuple(dict.fromkeys(channel_source(channel) for channel in channels))
        if not sources:
            raise ValueError("Select at least one MHO1 MAXIMUM ASCII channel")
        async with self._operation_lock:
            info = await self._maximum_ascii_capture_info_locked()
            initial_source = self._enum(
                await self._query_text_fresh_locked(":WAVeform:SOURce?"),
                command=":WAVeform:SOURce?",
                allowed={"CH1", "CH2", "CH3", "CH4"},
            )
            initial_mode = self._enum(
                await self._query_text_fresh_locked(":WAVeform:MODE?"),
                command=":WAVeform:MODE?",
                allowed={"NORMAL", "MAXIMUM", "RAW"},
            )
            initial_format = self._enum(
                await self._query_text_fresh_locked(":WAVeform:FORMat?"),
                command=":WAVeform:FORMat?",
                allowed={"WORD", "ASCII"},
            )
            initial_start = parse_scpi_int(await self._query_text_fresh_locked(":WAVeform:STARt?"))
            initial_stop = parse_scpi_int(await self._query_text_fresh_locked(":WAVeform:STOP?"))
            try:
                await self._write_readback_fresh_locked(
                    ":WAVeform:MODE MAXimum",
                    ":WAVeform:MODE?",
                )
                await self._write_readback_fresh_locked(
                    ":WAVeform:FORMat ASCII",
                    ":WAVeform:FORMat?",
                )
                for source in sources:
                    await self._write_readback_fresh_locked(
                        f":WAVeform:SOURce {source}",
                        ":WAVeform:SOURce?",
                    )
                    preamble_text = await self._query_text_fresh_locked(":WAVeform:PREamble?")
                    preamble = parse_waveform_preamble(preamble_text)
                    if preamble.format_code != 2 or preamble.mode_code != 1:
                        raise MicsigProtocolError(
                            "MHO1 MAXIMUM ASCII preamble did not report ASCII/MAXIMUM"
                        )
                    for start in range(
                        1,
                        info.memory_depth_points + 1,
                        MICSIG_MAXIMUM_ASCII_CHUNK_POINTS,
                    ):
                        stop = min(
                            start + MICSIG_MAXIMUM_ASCII_CHUNK_POINTS - 1,
                            info.memory_depth_points,
                        )
                        await self._write_readback_fresh_locked(
                            f":WAVeform:STARt {start}",
                            ":WAVeform:STARt?",
                        )
                        await self._write_readback_fresh_locked(
                            f":WAVeform:STOP {stop}",
                            ":WAVeform:STOP?",
                        )
                        payload = await self._query_ascii_fresh_locked(":WAVeform:DATA?")
                        samples = parse_ascii_waveform(payload.decode("ascii", errors="strict"))
                        expected_points = stop - start + 1
                        if len(samples) != expected_points:
                            raise MicsigProtocolError(
                                f"MHO1 {source} MAXIMUM block {start}:{stop} returned "
                                f"{len(samples)} points; expected {expected_points}"
                            )
                        await on_chunk(
                            MicsigMaximumAsciiChunk(
                                source=source,
                                start_point=start,
                                stop_point=stop,
                                total_points=info.memory_depth_points,
                                data=payload,
                                preamble_text=preamble_text,
                            )
                        )
            finally:
                # Never start acquisition or write ACQuire:DEPSelect. Restore
                # only the waveform-reader context through fresh MHO1 sessions.
                await self._write_readback_fresh_locked(
                    ":WAVeform:STARt 1",
                    ":WAVeform:STARt?",
                )
                await self._write_readback_fresh_locked(
                    f":WAVeform:MODE {initial_mode}",
                    ":WAVeform:MODE?",
                )
                await self._write_readback_fresh_locked(
                    f":WAVeform:FORMat {initial_format}",
                    ":WAVeform:FORMat?",
                )
                await self._write_readback_fresh_locked(
                    f":WAVeform:STOP {initial_stop}",
                    ":WAVeform:STOP?",
                )
                await self._write_readback_fresh_locked(
                    f":WAVeform:STARt {initial_start}",
                    ":WAVeform:STARt?",
                )
                await self._write_readback_fresh_locked(
                    f":WAVeform:SOURce {initial_source}",
                    ":WAVeform:SOURce?",
                )
            return info

    async def probe_fast_binary_waveform(
        self,
        channel: int | str,
    ) -> MicsigFastBinaryProbe:
        """Run one bounded undocumented DATA:BIN query without fallback."""
        source = channel_source(channel)
        started = time.monotonic()
        payload = b""
        error: str | None = None
        async with self._operation_lock:
            initial_source = self._enum(
                await self._transport.query_text(":WAVeform:SOURce?"),
                command=":WAVeform:SOURce?",
                allowed={"CH1", "CH2", "CH3", "CH4"},
            )
            try:
                await self._write_readback_locked(
                    f":WAVeform:SOURce {source}",
                    ":WAVeform:SOURce?",
                )
                payload = await self._transport.query_block(
                    ":WAVeform:DATA:BIN?",
                    length_multiplier=4,
                )
                if not payload:
                    error = "Micsig returned an empty DATA:BIN block"
                elif len(payload) % 4:
                    error = (
                        f"Micsig DATA:BIN payload length is not divisible by four: {len(payload)}"
                    )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            finally:
                await self._write_readback_locked(
                    f":WAVeform:SOURce {initial_source}",
                    ":WAVeform:SOURce?",
                )
        return MicsigFastBinaryProbe(
            source=source,
            data=payload,
            payload_bytes=len(payload),
            points=len(payload) // 4 if payload and len(payload) % 4 == 0 else None,
            prefix_hex=payload[:32].hex(" "),
            error=error,
            elapsed_s=time.monotonic() - started,
        )

    async def _capture_fast_ascii_waveforms(
        self,
        sources: tuple[str, ...],
        *,
        mode: str,
    ) -> tuple[MicsigWaveformCapture, ...]:
        if mode != "NORMAL":
            raise ValueError(
                "Micsig firmware 2.154.75 supports the verified fast ASCII frame in NORMAL mode"
            )
        async with self._operation_lock:
            await self._write_menu_action_locked(":MENU:STOP")
            try:
                return await self._read_fast_ascii_waveforms_locked(sources)
            finally:
                await self._write_menu_action_locked(":MENU:RUN")

    async def _read_fast_ascii_waveforms_locked(
        self,
        sources: tuple[str, ...],
    ) -> tuple[MicsigWaveformCapture, ...]:
        preamble = None
        reported_preamble = None
        preamble_text = ""
        captures: list[MicsigWaveformCapture] = []
        for source in sources:
            await self._write_fresh_locked(f":WAVeform:SOURce {source}")
            await self._write_fresh_locked(":WAVeform:MODE NORMal")
            if preamble is None:
                preamble_text = await self._query_text_fresh_locked(":WAVeform:PREamble?")
                reported_preamble = parse_waveform_preamble(preamble_text)
                # DATA:ASCii? is already in engineering-unit volts even though
                # firmware 2.154.75 reports the display's WORD format.
                preamble = replace(reported_preamble, format_code=2)
            payload = await self._query_ascii_fresh_locked(":WAVeform:DATA:ASCii?")
            if not payload:
                raise MicsigProtocolError(
                    f"Micsig returned an empty fast ASCII waveform for {source}"
                )
            if preamble is None or reported_preamble is None:
                raise MicsigProtocolError("Micsig fast ASCII preamble is unavailable")
            captures.append(
                MicsigWaveformCapture(
                    source=source,
                    mode="NORMAL",
                    samples=parse_ascii_waveform(payload.decode("ascii", errors="strict")),
                    preamble=preamble,
                    ascii_data=payload,
                    preamble_text=preamble_text,
                    reported_preamble=reported_preamble,
                )
            )
        return tuple(captures)

    async def _read_frame_waveforms_locked(
        self,
        sources: tuple[str, ...],
    ) -> tuple[MicsigWaveformCapture, ...]:
        """Read the model-specific NORMAL waveform payload for an explicit frame."""
        return await self._read_fast_ascii_waveforms_locked(sources)

    async def _capture_standard_ascii_waveforms(
        self,
        sources: tuple[str, ...],
        *,
        mode: str,
    ) -> tuple[MicsigWaveformCapture, ...]:
        if mode != "NORMAL":
            raise ValueError(
                "Micsig firmware 2.154.75 currently supports bounded NORMAL ASCII transfer only"
            )
        async with self._operation_lock:
            initial_status = (await self._transport.query_text(":TRIGger:STATus?")).strip().upper()
            initial_source = self._enum(
                await self._transport.query_text(":WAVeform:SOURce?"),
                command=":WAVeform:SOURce?",
                allowed={"CH1", "CH2", "CH3", "CH4"},
            )
            initial_mode = self._enum(
                await self._transport.query_text(":WAVeform:MODE?"),
                command=":WAVeform:MODE?",
                allowed={"NORMAL", "MAXIMUM", "RAW"},
            )
            initial_format = self._enum(
                await self._transport.query_text(":WAVeform:FORMat?"),
                command=":WAVeform:FORMat?",
                allowed={"WORD", "ASCII"},
            )
            initial_start = parse_scpi_int(await self._transport.query_text(":WAVeform:STARt?"))
            initial_stop = parse_scpi_int(await self._transport.query_text(":WAVeform:STOP?"))
            captures: list[MicsigWaveformCapture] = []
            try:
                if initial_status != "STOP":
                    await self._write_menu_action_locked(":MENU:STOP")
                    await self._wait_until_stopped_locked(
                        timeout_s=2.0,
                        action="ASCII waveform capture stop",
                    )
                await self._write_readback_locked(
                    ":WAVeform:MODE NORMAL",
                    ":WAVeform:MODE?",
                )
                await self._write_readback_locked(
                    ":WAVeform:FORMat ASCII",
                    ":WAVeform:FORMat?",
                )
                await self._write_readback_locked(
                    ":WAVeform:STARt 1",
                    ":WAVeform:STARt?",
                )
                await self._write_readback_locked(
                    ":WAVeform:STOP 1100",
                    ":WAVeform:STOP?",
                )
                for source in sources:
                    await self._write_readback_locked(
                        f":WAVeform:SOURce {source}",
                        ":WAVeform:SOURce?",
                    )
                    samples = parse_ascii_waveform(
                        await self._transport.query_text(":WAVeform:DATA?")
                    )
                    preamble = parse_waveform_preamble(
                        await self._transport.query_text(":WAVeform:PREamble?")
                    )
                    if preamble.format_code != 2:
                        raise MicsigProtocolError(
                            "Micsig ASCII waveform preamble did not report ASCII format"
                        )
                    captures.append(
                        MicsigWaveformCapture(
                            source=source,
                            mode="NORMAL",
                            samples=samples,
                            preamble=preamble,
                        )
                    )
            finally:
                await self._write_readback_locked(
                    f":WAVeform:STOP {initial_stop}",
                    ":WAVeform:STOP?",
                )
                await self._write_readback_locked(
                    f":WAVeform:STARt {initial_start}",
                    ":WAVeform:STARt?",
                )
                await self._write_readback_locked(
                    f":WAVeform:FORMat {initial_format}",
                    ":WAVeform:FORMat?",
                )
                await self._write_readback_locked(
                    f":WAVeform:MODE {initial_mode}",
                    ":WAVeform:MODE?",
                )
                await self._write_readback_locked(
                    f":WAVeform:SOURce {initial_source}",
                    ":WAVeform:SOURce?",
                )
                if initial_status != "STOP":
                    await self._write_menu_action_locked(":MENU:RUN")
            return tuple(captures)

    async def start(self) -> None:
        async with self._operation_lock:
            await self._write_menu_action_locked(":MENU:RUN")
            await asyncio.sleep(RUN_SETTLE_DELAY_S)

    async def run(self) -> None:
        """Compatibility alias for start()."""
        await self.start()

    async def stop(self, *, wait_timeout_s: float = 2.0) -> None:
        if wait_timeout_s <= 0:
            raise ValueError("Stop timeout must be positive")
        async with self._operation_lock:
            await self._write_menu_action_locked(":MENU:STOP")
            await self._wait_until_stopped_locked(
                timeout_s=wait_timeout_s,
                action="stop",
            )
            await self._storage_readiness_barrier_locked()
            await asyncio.sleep(STOP_SETTLE_DELAY_S)

    async def single(self, *, wait_timeout_s: float | None = None) -> None:
        """Arm one acquisition using the trigger settings already on the scope."""
        if wait_timeout_s is not None and wait_timeout_s <= 0:
            raise ValueError("Single acquisition timeout must be positive")
        async with self._operation_lock:
            await self._write_menu_action_locked(":MENU:SINGle")
            if wait_timeout_s is not None:
                await self._wait_until_stopped_locked(
                    timeout_s=wait_timeout_s,
                    action="single acquisition",
                )

    async def arm(self) -> None:
        """Compatibility alias for non-blocking single()."""
        await self.single()

    async def _write_menu_action_locked(self, command: str) -> None:
        """Send a menu action and use a fresh session for later queries."""
        await self._write_fresh_locked(command)

    async def _write_fresh_locked(self, command: str) -> None:
        await self._transport.write(command)
        await self._transport.close()

    async def _write_readback_fresh_locked(self, command: str, query: str) -> str:
        await self._write_fresh_locked(command)
        response = (await self._query_text_fresh_locked(query)).strip()
        if not response or response.upper().startswith("ERROR:"):
            raise MicsigProtocolError(f"Micsig rejected {command}: {response!r}")
        return response

    async def _query_text_fresh_locked(self, command: str) -> str:
        try:
            return await self._transport.query_text(command)
        finally:
            await self._transport.close()

    async def _query_block_fresh_locked(self, command: str) -> bytes:
        try:
            return await self._transport.query_block(command)
        finally:
            await self._transport.close()

    async def _query_ascii_fresh_locked(self, command: str) -> bytes:
        try:
            return await self._transport.query_ascii_block(command)
        finally:
            await self._transport.close()

    async def _storage_readiness_barrier_locked(self) -> None:
        """Complete one harmless request before the next storage transaction."""
        response = (await self._transport.query_text(":ACQuire:TYPE?")).strip()
        if not response or response.upper().startswith("ERROR:"):
            raise MicsigProtocolError(f"Unexpected :ACQuire:TYPE? response: {response!r}")

    async def wait_for_trigger(
        self,
        *,
        timeout_s: float | None = None,
        poll_interval_s: float = 0.05,
    ) -> str:
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("Trigger timeout must be positive")
        if poll_interval_s <= 0:
            raise ValueError("Trigger polling interval must be positive")
        async with self._operation_lock:
            await self._wait_until_stopped_locked(
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                action="trigger",
            )
        return "STOP"

    async def _wait_until_stopped_locked(
        self,
        *,
        timeout_s: float | None,
        action: str,
        poll_interval_s: float = 0.05,
    ) -> None:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        status = "UNKNOWN"
        while True:
            status = (await self._transport.query_text(":TRIGger:STATus?")).strip().upper()
            if status == "STOP":
                return
            if deadline is not None and time.monotonic() >= deadline:
                assert timeout_s is not None
                raise TimeoutError(
                    f"Micsig {action} did not complete within {timeout_s:g} seconds "
                    f"(last status: {status})"
                )
            await asyncio.sleep(poll_interval_s)

    async def capture_screenshot(self) -> MicsigScreenshot:
        async with self._operation_lock:
            return await self._capture_screenshot_locked()

    async def _pace_screenshot_command_locked(self) -> None:
        remaining = SCREENSHOT_MIN_INTERVAL_S - (
            time.monotonic() - self._last_screenshot_command_at
        )
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_screenshot_command_at = time.monotonic()

    async def probe_direct_screenshot(self, transport: str) -> MicsigScreenshotProbe:
        """Probe one direct screenshot transport without creating a stored file."""
        normalized = transport.strip().lower()
        if normalized not in {"tcp", "vxi11"}:
            raise ValueError("Screenshot probe transport must be tcp or vxi11")
        started = time.monotonic()
        raw = b""
        payload = b""
        declared_length: int | None = None
        error: str | None = None
        image_format: str | None = None
        async with self._operation_lock:
            try:
                await self._pace_screenshot_command_locked()
                if normalized == "tcp":
                    payload = await self._query_block_fresh_locked(":SYS:SCR?")
                    raw = payload
                    declared_length = len(payload)
                else:
                    raw = await self._transport.query_vxi11_raw(":SYS:SCR?")
                    payload = raw
                    if raw.startswith(b"#") and len(raw) >= 2 and raw[1:2].isdigit():
                        digits = int(raw[1:2])
                        header_end = 2 + digits
                        length_raw = raw[2:header_end]
                        if digits and length_raw.isdigit():
                            declared_length = int(length_raw)
                            payload = raw[header_end : header_end + declared_length]
                if payload:
                    _, image_format = normalize_screenshot_image(payload)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        return MicsigScreenshotProbe(
            transport=normalized,
            raw_bytes=len(raw),
            declared_payload_bytes=declared_length,
            payload_bytes=len(payload),
            prefix_hex=payload[:32].hex(" "),
            image_format=image_format,
            error=error,
            elapsed_s=time.monotonic() - started,
        )

    async def _capture_screenshot_locked(self) -> MicsigScreenshot:
        return await self._capture_direct_screenshot_locked(allow_cached=True)

    async def _capture_direct_screenshot_locked(
        self,
        *,
        allow_cached: bool,
    ) -> MicsigScreenshot:
        if (
            allow_cached
            and self._cached_screenshot is not None
            and time.monotonic() - self._cached_screenshot_completed_at < SCREENSHOT_MIN_INTERVAL_S
        ):
            return self._cached_screenshot

        image_format: str | None = None
        payload = b""
        # The first direct screenshot after connecting is occasionally empty on
        # firmware 2.154.75. One paced retry is still much faster than creating
        # and discovering a file in the scope's HTTP storage.
        for _attempt in range(2):
            await self._pace_screenshot_command_locked()
            payload = await self._query_block_fresh_locked(":SYS:SCR?")
            if payload:
                try:
                    payload, image_format = normalize_screenshot_image(payload)
                except MicsigProtocolError:
                    payload = b""
                if payload:
                    break
        if not payload:
            raise MicsigProtocolError(
                "Micsig returned no valid direct screenshot after two attempts"
            )
        if image_format is None:
            raise MicsigProtocolError("Micsig screenshot format was not detected")
        screenshot = MicsigScreenshot(
            data=payload,
            image_format=image_format,
        )
        self._cached_screenshot = screenshot
        self._cached_screenshot_completed_at = time.monotonic()
        return screenshot

    async def read_scalar_measurements(
        self,
        channel: int | str,
        items: Sequence[str] = DEFAULT_SCALAR_MEASUREMENTS,
    ) -> tuple[MicsigScalarMeasurement, ...]:
        source = channel_source(channel)
        async with self._operation_lock:
            return await self._read_scalar_measurements_locked(source, items)

    async def read_scalar_measurement_profile(
        self,
        measurements: Sequence[MicsigScalarMeasurementSpec | tuple[int | str, str]],
    ) -> tuple[MicsigScalarMeasurement, ...]:
        """Read one bounded multi-channel scalar profile without reconfiguring it."""
        normalized = self._normalize_scalar_measurement_profile(measurements)
        async with self._operation_lock:
            return await self._read_scalar_measurement_profile_locked(normalized)

    async def replace_scalar_measurements(
        self,
        measurements: Sequence[MicsigScalarMeasurementSpec | tuple[int | str, str]],
    ) -> tuple[MicsigScalarMeasurement, ...]:
        """Replace the measurement pills shown by the oscilloscope."""
        normalized = self._normalize_scalar_measurement_profile(measurements)

        async with self._operation_lock:
            await self._write_fresh_locked(":MEASure:CLEar all")
            await asyncio.sleep(MEASUREMENT_COMMAND_DELAY_S)
            for spec in normalized:
                await self._write_fresh_locked(self._scalar_open_command(spec))
                await asyncio.sleep(MEASUREMENT_COMMAND_DELAY_S)
            await asyncio.sleep(MEASUREMENT_COMMAND_DELAY_S)
            return await self._read_scalar_measurement_profile_locked(normalized)

    @staticmethod
    def _normalize_scalar_measurement_profile(
        measurements: Sequence[MicsigScalarMeasurementSpec | tuple[int | str, str]],
    ) -> tuple[MicsigScalarMeasurementSpec, ...]:
        normalized: list[MicsigScalarMeasurementSpec] = []
        for measurement in measurements:
            if isinstance(measurement, MicsigScalarMeasurementSpec):
                raw_channel: int | str = measurement.channel
                item = measurement.item
                secondary_channel = measurement.secondary_channel
                source_edge = measurement.source_edge
                target_edge = measurement.target_edge
            else:
                raw_channel, item = measurement
                secondary_channel = None
                source_edge = None
                target_edge = None

            source = channel_source(raw_channel)
            normalized_item = item.strip().lower()
            if normalized_item not in SCALAR_MEASUREMENT_COMMANDS:
                raise ValueError(f"Unknown Micsig scalar measurement: {item}")
            secondary = channel_source(secondary_channel) if secondary_channel is not None else None
            if normalized_item in {"phase", "delay"}:
                if secondary is None:
                    raise ValueError(
                        f"{normalized_item.upper()} requires primary and secondary channels"
                    )
                if secondary == source:
                    raise ValueError(f"{normalized_item.upper()} channels must be different")
            elif secondary is not None or source_edge is not None or target_edge is not None:
                raise ValueError(
                    f"{normalized_item.upper()} does not accept a secondary channel or edges"
                )

            if normalized_item == "delay":
                normalized_source_edge = MicsigMHO1Scope._normalize_delay_edge(
                    source_edge or "FRISe"
                )
                normalized_target_edge = MicsigMHO1Scope._normalize_delay_edge(
                    target_edge or "FRISe"
                )
            else:
                normalized_source_edge = None
                normalized_target_edge = None

            spec = MicsigScalarMeasurementSpec(
                channel=source,
                item=normalized_item,
                secondary_channel=secondary,
                source_edge=normalized_source_edge,
                target_edge=normalized_target_edge,
            )
            if spec not in normalized:
                normalized.append(spec)
        if len(normalized) > MAX_SCALAR_MEASUREMENTS:
            raise ValueError(
                f"No more than {MAX_SCALAR_MEASUREMENTS} scalar measurements can be displayed"
            )
        return tuple(normalized)

    @staticmethod
    def _normalize_delay_edge(edge: str) -> str:
        normalized = edge.strip().casefold()
        for choice in MICSIG_DELAY_EDGES:
            if choice.casefold() == normalized:
                return choice
        raise ValueError(f"Unknown Micsig delay edge: {edge}")

    @staticmethod
    def _scalar_open_command(spec: MicsigScalarMeasurementSpec) -> str:
        command, _unit = SCALAR_MEASUREMENT_COMMANDS[spec.item]
        arguments = [spec.channel]
        if spec.secondary_channel is not None:
            arguments.append(spec.secondary_channel)
        if spec.item == "delay":
            arguments.extend((spec.source_edge or "FRISe", spec.target_edge or "FRISe"))
        return f":MEASure:OPEN {command},{','.join(arguments)}"

    @staticmethod
    def _scalar_query_command(spec: MicsigScalarMeasurementSpec) -> str:
        command, _unit = SCALAR_MEASUREMENT_COMMANDS[spec.item]
        arguments = [spec.channel]
        if spec.secondary_channel is not None:
            arguments.append(spec.secondary_channel)
        if spec.item == "delay":
            arguments.extend((spec.source_edge or "FRISe", spec.target_edge or "FRISe"))
        return f":MEASure:{command}? {','.join(arguments)}"

    async def _read_scalar_measurement_profile_locked(
        self,
        measurements: Sequence[MicsigScalarMeasurementSpec],
    ) -> tuple[MicsigScalarMeasurement, ...]:
        try:
            return tuple(
                [await self._read_scalar_measurement_spec_locked(spec) for spec in measurements]
            )
        finally:
            # A live dashboard refresh is one read-only transaction. Keeping
            # its scalar queries on one VXI-11 session avoids repeatedly
            # opening and closing the remote connection while the scope runs.
            await self._transport.close()

    async def _read_scalar_measurement_spec_locked(
        self,
        spec: MicsigScalarMeasurementSpec,
    ) -> MicsigScalarMeasurement:
        _command, unit = SCALAR_MEASUREMENT_COMMANDS[spec.item]
        response = await self._transport.query_text(self._scalar_query_command(spec))
        try:
            value = parse_optional_scpi_float(response)
        except MicsigProtocolError:
            value = None
        if value is not None:
            value *= SCALAR_MEASUREMENT_MULTIPLIERS.get(spec.item, 1.0)
        return MicsigScalarMeasurement(
            item=spec.item,
            channel=spec.channel,
            secondary_channel=spec.secondary_channel,
            source_edge=spec.source_edge,
            target_edge=spec.target_edge,
            value=value,
            unit=unit,
            status="ok" if value is not None else "unavailable",
        )

    async def _read_scalar_measurements_locked(
        self,
        source: str,
        items: Sequence[str],
    ) -> tuple[MicsigScalarMeasurement, ...]:
        requested = tuple(item.strip().lower() for item in items)
        unknown = tuple(item for item in requested if item not in SCALAR_MEASUREMENT_COMMANDS)
        if unknown:
            raise ValueError(f"Unknown Micsig scalar measurements: {', '.join(unknown)}")

        pair_items = tuple(item for item in requested if item in {"phase", "delay"})
        if pair_items:
            raise ValueError(
                f"{', '.join(item.upper() for item in pair_items)} requires two channels"
            )

        try:
            measurements = []
            for item in requested:
                measurements.append(
                    await self._read_scalar_measurement_spec_locked(
                        MicsigScalarMeasurementSpec(channel=source, item=item)
                    )
                )
            return tuple(measurements)
        finally:
            await self._transport.close()

    @staticmethod
    def _measurements_to_csv(
        measurements: Sequence[MicsigScalarMeasurement],
    ) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            (
                "channel",
                "measurement",
                "value",
                "unit",
                "status",
                "secondary_channel",
                "source_edge",
                "target_edge",
            )
        )
        for measurement in measurements:
            value = "" if measurement.value is None else format(measurement.value, ".17g")
            writer.writerow(
                (
                    measurement.channel,
                    measurement.item,
                    value,
                    measurement.unit,
                    measurement.status,
                    measurement.secondary_channel or "",
                    measurement.source_edge or "",
                    measurement.target_edge or "",
                )
            )
        return output.getvalue().encode("utf-8")

    @staticmethod
    def _waveforms_to_csv(
        waveforms: Sequence[MicsigWaveformCapture],
    ) -> bytes:
        if not waveforms:
            return b""
        point_counts = {waveform.points for waveform in waveforms}
        if len(point_counts) != 1 or not point_counts:
            raise MicsigProtocolError(
                "Micsig frame channels must contain the same non-zero point count"
            )
        points = point_counts.pop()
        if points <= 0:
            raise MicsigProtocolError("Micsig frame contains no waveform points")
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            (
                "sample_index",
                "time_s",
                *(f"{waveform.source.lower()}_v" for waveform in waveforms),
            )
        )
        reference = waveforms[0]
        for index in range(points):
            writer.writerow(
                (
                    index,
                    format(reference.time_at(index), ".17g"),
                    *(format(waveform.voltage_at(index), ".17g") for waveform in waveforms),
                )
            )
        return output.getvalue().encode("utf-8")

    async def capture_frame(
        self,
        measurements: Sequence[MicsigScalarMeasurementSpec | tuple[int | str, str]],
        *,
        channels: Sequence[int | str] = (1, 2, 3, 4),
        include_screenshot: bool = True,
        stop_before_capture: bool = True,
        resume_after: bool = True,
    ) -> MicsigSnapshot:
        """Read scalars and only the explicitly enabled frame artifacts."""
        normalized_measurements = self._normalize_scalar_measurement_profile(measurements)
        sources: list[str] = []
        for channel in channels:
            source = channel_source(channel)
            if source not in sources:
                sources.append(source)
        selected_sources = tuple(sources)
        async with self._operation_lock:
            started = time.monotonic()
            screenshot: MicsigScreenshot | None = None
            screenshot_error: str | None = None
            waveform_error: str | None = None
            waveforms: tuple[MicsigWaveformCapture, ...] = ()
            if selected_sources and stop_before_capture:
                await self._write_menu_action_locked(":MENU:STOP")
            try:
                if selected_sources:
                    try:
                        waveforms = await self._read_frame_waveforms_locked(selected_sources)
                    except Exception as exc:
                        waveform_error = f"{type(exc).__name__}: {exc}"
                if include_screenshot:
                    try:
                        screenshot = await self._capture_direct_screenshot_locked(
                            allow_cached=False
                        )
                    except Exception as exc:
                        screenshot_error = f"{type(exc).__name__}: {exc}"
                scalar_measurements = await self._read_scalar_measurement_profile_locked(
                    normalized_measurements
                )
            finally:
                if selected_sources and resume_after:
                    await self._write_menu_action_locked(":MENU:RUN")
            elapsed_s = time.monotonic() - started

        return MicsigSnapshot(
            region="screen",
            measurements=scalar_measurements,
            measurements_csv=self._measurements_to_csv(scalar_measurements),
            screenshot=screenshot,
            screenshot_error=screenshot_error,
            waveform_error=waveform_error,
            waveforms=waveforms,
            waveform_csv=self._waveforms_to_csv(waveforms),
            elapsed_s=elapsed_s,
        )

    async def capture_snapshot(
        self,
        *,
        region: str = "screen",
        measurement_items: Sequence[str] = DEFAULT_SCALAR_MEASUREMENTS,
        include_screenshot: bool = True,
        resume: bool = False,
        stop_timeout_s: float = 2.0,
    ) -> MicsigSnapshot:
        """Stop and capture a scope PNG plus direct scalar measurements.

        By default the scope remains stopped. Set ``resume=True`` only when the
        caller explicitly wants to restore RUN after the capture.
        """
        if stop_timeout_s <= 0:
            raise ValueError("Snapshot stop timeout must be positive")
        normalized_region = region.strip().lower()
        if normalized_region != "screen":
            raise MicsigProtocolError(
                "Firmware 2.154.75 does not expose a reliable atomic four-channel "
                "memory transfer; only the screen snapshot is supported"
            )
        sources = tuple(f"CH{channel}" for channel in range(1, 5))

        async with self._operation_lock:
            initial_status = (await self._transport.query_text(":TRIGger:STATus?")).strip().upper()
            try:
                if initial_status != "STOP":
                    await self._write_menu_action_locked(":MENU:STOP")
                    await self._wait_until_stopped_locked(
                        timeout_s=stop_timeout_s,
                        action="snapshot stop",
                    )
                measurements: list[MicsigScalarMeasurement] = []
                for source in sources:
                    measurements.extend(
                        await self._read_scalar_measurements_locked(
                            source,
                            measurement_items,
                        )
                    )

                captured_screen = await self._capture_screenshot_locked()
            finally:
                if resume and initial_status != "STOP":
                    await self._write_menu_action_locked(":MENU:RUN")

        measurement_tuple = tuple(measurements)
        return MicsigSnapshot(
            region=normalized_region,
            measurements=measurement_tuple,
            measurements_csv=self._measurements_to_csv(measurement_tuple),
            screenshot=captured_screen if include_screenshot else None,
            screenshot_error=None,
        )

    async def close(self) -> None:
        async with self._operation_lock:
            await self._transport.close()

    async def storage_index(self, path: str = "/") -> tuple[str, ...]:
        return await self._transport.list_http_links(path)

    async def download_stored_file(self, path: str) -> bytes:
        """Download one file already saved in the scope's bounded HTTP storage."""
        async with self._operation_lock:
            return await self._transport.download_http_file(path)

    async def save_waveform_file(
        self,
        channel: int | str,
        *,
        file_type: str = "CSV",
        filename: str,
        timeout_s: float = 30.0,
    ) -> MicsigStoredWaveform:
        source = channel_source(channel)
        normalized_type = file_type.strip().upper()
        if normalized_type not in {"CSV", "BIN"}:
            raise ValueError("Micsig stored waveform type must be CSV or BIN")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,23}", filename):
            raise ValueError(
                "Micsig stored waveform filename must contain only letters, "
                "digits, underscores, or hyphens"
            )
        if not 0 < timeout_s <= 120:
            raise ValueError("Micsig stored waveform timeout must be between 0 and 120 seconds")
        async with self._operation_lock:
            storage_directory = "/files/csvwave" if normalized_type == "CSV" else "/files/binwave"
            # Firmware 2.154.75 accepts the documented storage writes, but its
            # corresponding SAVE queries do not return. These settings affect
            # only the scope's file-save dialog, so use bounded writes without
            # query/readback instead of blocking acquisition automation.
            await self._transport.write(":STORage:SAVE:LOCAtion LOCal")
            await asyncio.sleep(STORAGE_COMMAND_DELAY_S)
            await self._transport.write(f":STORage:SAVE:TYPE {normalized_type}")
            await asyncio.sleep(STORAGE_COMMAND_DELAY_S)
            # Firmware 2.154.75 preserves quote characters literally. An
            # unquoted safe token gives OpenBench a deterministic HTTP path.
            await self._transport.write(f":STORage:SAVE:FILename {filename}")
            await asyncio.sleep(STORAGE_COMMAND_DELAY_S)
            await self._transport.write(f":STORage:SAVE {source}")
            await asyncio.sleep(STORAGE_COMMAND_DELAY_S)
            await self._transport.write(":STORage:SAVE:STARt")

            deadline = time.monotonic() + timeout_s
            extension = normalized_type.casefold()
            newest_path = f"{storage_directory}/{filename}.{extension}"
            data: bytes | None = None
            while time.monotonic() < deadline:
                candidate: bytes | None = None
                try:
                    candidate = await self._transport.download_http_file(newest_path)
                except (OSError, RuntimeError, TimeoutError):
                    pass
                if candidate:
                    if normalized_type == "BIN":
                        if len(candidate) < 0x100:
                            candidate = None
                        else:
                            points = int.from_bytes(candidate[8:12], "little")
                            expected_bytes = 0x100 + points * 2
                            if points <= 0 or len(candidate) < expected_bytes:
                                candidate = None
                    if candidate:
                        data = candidate
                        break
            if data is None:
                raise TimeoutError(
                    f"Micsig did not publish stored {normalized_type} waveform "
                    f"{filename!r} within {timeout_s:g} seconds"
                )
            # The HTTP file can become complete a little before the save dialog
            # is ready for another channel. Give the firmware a short fixed
            # recovery window before the next storage transaction.
            await self._storage_readiness_barrier_locked()
            await asyncio.sleep(STORAGE_FILE_SETTLE_DELAY_S)
            return MicsigStoredWaveform(
                source=source,
                file_type=normalized_type,
                scope_filename=newest_path.rsplit("/", maxsplit=1)[-1].rsplit(".", maxsplit=1)[0],
                http_path=newest_path,
                data=data,
            )
