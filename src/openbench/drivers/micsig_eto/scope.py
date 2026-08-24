from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace

from openbench.drivers.micsig_eto.protocol import (
    ETO_MAXIMUM_ASCII_CHUNK_POINTS,
    ETO_MAXIMUM_MEMORY_POINTS,
    MicsigETOMaximumAsciiChunk,
    MicsigETOMaximumCaptureInfo,
    parse_identification,
    parse_word_hex_waveform,
)
from openbench.drivers.micsig_eto.transport import MicsigETOTransport
from openbench.drivers.micsig_mho1.protocol import (
    MicsigDmmSupport,
    MicsigFastBinaryProbe,
    MicsigProtocolError,
    MicsigScopeStatus,
    MicsigScreenshot,
    MicsigWaveformCapture,
    MicsigWaveformPreamble,
    channel_source,
    normalize_screenshot_image,
    parse_ascii_waveform,
    parse_scpi_int,
    parse_waveform_preamble,
)
from openbench.drivers.micsig_mho1.scope import MicsigMHO1Scope, MicsigTransport
from openbench.drivers.micsig_mho1.transport import MicsigDescriptor

ETO_NORMAL_CAPTURE_POINTS = 1100
ETO_FRAME_SETTLE_S = 0.15
ETO_SOURCE_SETTLE_S = 1.0
ETO_BROKEN_TIMEBASE_MODE_FIRMWARES = frozenset(("3.392.132",))
ETO_DOUBLE_WORD_SCALE_FIRMWARES = frozenset(("3.392.132",))


class MicsigETOScope(MicsigMHO1Scope):
    """Bounded ETO5004 driver using the documented 8-bit waveform path."""

    def __init__(
        self,
        descriptor: MicsigDescriptor,
        *,
        transport: MicsigTransport | None = None,
    ) -> None:
        super().__init__(
            descriptor,
            transport=transport or MicsigETOTransport(descriptor),
        )

    @property
    def waveform_transfer_method(self) -> str:
        # Firmware 3.392.132 returns standard WORD data as four ASCII hex
        # characters per point. This is slow but materially more reliable than
        # its standard ASCII mode, which intermittently returns a zero block.
        return "standard_word_hex"

    @property
    def screenshot_supported(self) -> bool:
        # Direct SYS:SCR is broken on 3.392.132, but the documented scope-side
        # capture plus HTTP download path is available.
        return True

    @property
    def dmm_support(self) -> MicsigDmmSupport:
        return MicsigDmmSupport(
            hardware_present=False,
            direct_protocol_available=False,
            reason="ETO5004 has no integrated multimeter.",
        )

    async def identify(self) -> str:
        async with self._operation_lock:
            identification = parse_identification(await self._transport.query_text("*IDN?"))
        if identification.serial_number != self.descriptor.serial_number:
            raise MicsigProtocolError(
                f"Expected Micsig ETO serial {self.descriptor.serial_number}, "
                f"got {identification.serial_number}"
            )
        return (
            f"{identification.manufacturer} {identification.model} "
            f"SN {identification.serial_number} FW {identification.firmware_version}"
        )

    async def _capture_screenshot_locked(self) -> MicsigScreenshot:
        payload = await self._transport.capture_stored_screenshot(timeout_s=15.0)
        payload, image_format = normalize_screenshot_image(payload)
        return MicsigScreenshot(data=payload, image_format=image_format)

    async def _capture_direct_screenshot_locked(
        self,
        *,
        allow_cached: bool,
    ) -> MicsigScreenshot:
        del allow_cached
        return await self._capture_screenshot_locked()

    async def _read_state_locked(self) -> MicsigScopeStatus:
        state = await super()._read_state_locked()
        # Physical ETO5004 firmware 3.392.132 reports ``XY`` from the documented
        # query while its front panel is in the ordinary Y-T display mode. Do
        # not expose that known-bad readback as operator truth. OpenBench does
        # not use or configure ETO X-Y mode, so the bounded ETO profile reports
        # the verified normal display mode for this firmware revision.
        timebase_mode = self._normalize_timebase_mode_readback(state.timebase_mode)
        if timebase_mode != state.timebase_mode:
            return replace(state, timebase_mode=timebase_mode)
        return state

    def _normalize_timebase_mode_readback(self, reported: str) -> str:
        if (
            self.descriptor.firmware_version in ETO_BROKEN_TIMEBASE_MODE_FIRMWARES
            and reported == "XY"
        ):
            return "YT"
        return reported

    def _normalize_word_preamble(
        self,
        preamble: MicsigWaveformPreamble,
    ) -> MicsigWaveformPreamble:
        # ETO5004 3.392.132 returns correct 8-bit WORD codes but reports a
        # vertical increment/origin twice as large as the independently
        # verified scalar measurement and standard-ASCII voltage path.
        if self.descriptor.firmware_version in ETO_DOUBLE_WORD_SCALE_FIRMWARES:
            return replace(
                preamble,
                y_increment=preamble.y_increment / 2.0,
                y_origin=preamble.y_origin / 2.0,
            )
        return preamble

    async def capture_waveforms(
        self,
        channels: Sequence[int | str],
        *,
        mode: str = "NORMAL",
    ) -> tuple[MicsigWaveformCapture, ...]:
        sources = tuple(dict.fromkeys(channel_source(channel) for channel in channels))
        if not sources:
            raise ValueError("At least one Micsig ETO waveform channel is required")
        normalized_mode = mode.strip().upper()
        if normalized_mode != "NORMAL":
            raise ValueError("Micsig ETO waveform mode must be NORMAL")
        async with self._operation_lock:
            initial_status = (await self._transport.query_text(":TRIGger:STATus?")).strip().upper()
            try:
                if initial_status != "STOP":
                    await self._write_menu_action_locked(":MENU:STOP")
                    await self._wait_until_stopped_locked(
                        timeout_s=2.0,
                        action="ETO ASCII waveform capture stop",
                    )
                return await self._read_frame_waveforms_locked(sources)
            finally:
                if initial_status != "STOP":
                    await self._write_menu_action_locked(":MENU:RUN")

    async def probe_fast_binary_waveform(
        self,
        channel: int | str,
    ) -> MicsigFastBinaryProbe:
        del channel
        raise ValueError(
            "ETO5004 firmware does not support the fast DATA:BIN query; "
            "production capture uses the standard DATA path"
        )

    async def maximum_ascii_capture_info(self) -> MicsigETOMaximumCaptureInfo:
        """Validate STOP and report the current memory depth without changing it."""
        async with self._operation_lock:
            return await self._maximum_ascii_capture_info_locked()

    async def _maximum_ascii_capture_info_locked(self) -> MicsigETOMaximumCaptureInfo:
        status = self._enum(
            await self._transport.query_text(":TRIGger:STATus?"),
            command=":TRIGger:STATus?",
            allowed={"RUN", "WAIT", "AUTO", "STOP"},
        )
        if status != "STOP":
            raise RuntimeError(
                "ETO5004 MAXIMUM ASCII capture requires the oscilloscope to already be in STOP"
            )
        points = parse_scpi_int(await self._transport.query_text(":ACQuire:DEPTh?"))
        if not 1 <= points <= ETO_MAXIMUM_MEMORY_POINTS:
            raise MicsigProtocolError(f"ETO5004 reported unsupported memory depth: {points} points")
        return MicsigETOMaximumCaptureInfo(memory_depth_points=points)

    async def stream_maximum_ascii(
        self,
        channels: Sequence[int | str],
        *,
        on_chunk: Callable[[MicsigETOMaximumAsciiChunk], Awaitable[None]],
    ) -> MicsigETOMaximumCaptureInfo:
        """Stream every point in current stopped memory without changing depth."""
        sources = tuple(dict.fromkeys(channel_source(channel) for channel in channels))
        if not sources:
            raise ValueError("Select at least one ETO5004 MAXIMUM ASCII channel")
        async with self._operation_lock:
            info = await self._maximum_ascii_capture_info_locked()
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
            try:
                await self._write_readback_locked(
                    ":WAVeform:MODE MAXimum",
                    ":WAVeform:MODE?",
                )
                await self._write_readback_locked(
                    ":WAVeform:FORMat ASCII",
                    ":WAVeform:FORMat?",
                )
                for source in sources:
                    await self._write_readback_locked(
                        f":WAVeform:SOURce {source}",
                        ":WAVeform:SOURce?",
                    )
                    preamble_text = await self._transport.query_text(":WAVeform:PREamble?")
                    preamble = parse_waveform_preamble(preamble_text)
                    if preamble.format_code != 2 or preamble.mode_code != 1:
                        raise MicsigProtocolError(
                            "ETO5004 MAXIMUM ASCII preamble did not report ASCII/MAXIMUM"
                        )
                    for start in range(
                        1,
                        info.memory_depth_points + 1,
                        ETO_MAXIMUM_ASCII_CHUNK_POINTS,
                    ):
                        stop = min(
                            start + ETO_MAXIMUM_ASCII_CHUNK_POINTS - 1,
                            info.memory_depth_points,
                        )
                        await self._write_readback_locked(
                            f":WAVeform:STARt {start}",
                            ":WAVeform:STARt?",
                        )
                        await self._write_readback_locked(
                            f":WAVeform:STOP {stop}",
                            ":WAVeform:STOP?",
                        )
                        payload = await self._transport.query_ascii_block(":WAVeform:DATA?")
                        samples = parse_ascii_waveform(payload.decode("ascii", errors="strict"))
                        expected_points = stop - start + 1
                        if len(samples) != expected_points:
                            raise MicsigProtocolError(
                                f"ETO5004 {source} MAXIMUM block {start}:{stop} "
                                f"returned {len(samples)} points; expected "
                                f"{expected_points}"
                            )
                        await on_chunk(
                            MicsigETOMaximumAsciiChunk(
                                source=source,
                                start_point=start,
                                stop_point=stop,
                                total_points=info.memory_depth_points,
                                data=payload,
                                preamble_text=preamble_text,
                            )
                        )
            finally:
                # This transaction never starts acquisition and never writes
                # ACQuire:DEPSelect. Restore only the waveform-reader context.
                await self._write_readback_locked(
                    ":WAVeform:STARt 1",
                    ":WAVeform:STARt?",
                )
                await self._write_readback_locked(
                    f":WAVeform:MODE {initial_mode}",
                    ":WAVeform:MODE?",
                )
                await self._write_readback_locked(
                    f":WAVeform:FORMat {initial_format}",
                    ":WAVeform:FORMat?",
                )
                await self._write_readback_locked(
                    f":WAVeform:STOP {initial_stop}",
                    ":WAVeform:STOP?",
                )
                await self._write_readback_locked(
                    f":WAVeform:STARt {initial_start}",
                    ":WAVeform:STARt?",
                )
                await self._write_readback_locked(
                    f":WAVeform:SOURce {initial_source}",
                    ":WAVeform:SOURce?",
                )
            return info

    async def _read_frame_waveforms_locked(
        self,
        sources: tuple[str, ...],
    ) -> tuple[MicsigWaveformCapture, ...]:
        # The shared frame transaction sends STOP immediately before entering
        # this model hook. ETO5004 can acknowledge the menu action before the
        # display buffer is ready and then return a zero-length DATA block.
        await self._wait_until_stopped_locked(
            timeout_s=2.0,
            action="ETO frame capture",
        )
        await asyncio.sleep(ETO_FRAME_SETTLE_S)
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
            await self._write_readback_locked(
                ":WAVeform:MODE NORMAL",
                ":WAVeform:MODE?",
            )
            await self._write_readback_locked(
                ":WAVeform:FORMat WORD",
                ":WAVeform:FORMat?",
            )
            await self._write_readback_locked(
                ":WAVeform:STARt 1",
                ":WAVeform:STARt?",
            )
            await self._write_readback_locked(
                f":WAVeform:STOP {ETO_NORMAL_CAPTURE_POINTS}",
                ":WAVeform:STOP?",
            )
            for source in sources:
                await self._write_readback_locked(
                    f":WAVeform:SOURce {source}",
                    ":WAVeform:SOURce?",
                )
                await asyncio.sleep(ETO_SOURCE_SETTLE_S)
                payload = b""
                for attempt in range(2):
                    try:
                        payload = await self._transport.query_block(
                            ":WAVeform:DATA?",
                            length_multiplier=4,
                        )
                    finally:
                        # ETO5004 3.392.132 serves at most one reliable
                        # standard DATA transaction per SCPI session.
                        await self._transport.close()
                    if payload:
                        break
                    if attempt == 0:
                        await asyncio.sleep(ETO_FRAME_SETTLE_S)
                preamble_text = await self._transport.query_text(":WAVeform:PREamble?")
                reported_preamble = parse_waveform_preamble(preamble_text)
                if reported_preamble.format_code != 0:
                    raise MicsigProtocolError(
                        "Micsig ETO WORD waveform preamble did not report WORD format"
                    )
                preamble = self._normalize_word_preamble(reported_preamble)
                captures.append(
                    MicsigWaveformCapture(
                        source=source,
                        mode="NORMAL",
                        samples=parse_word_hex_waveform(payload),
                        preamble=preamble,
                        preamble_text=preamble_text,
                        reported_preamble=reported_preamble,
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
        return tuple(captures)
