from __future__ import annotations

import asyncio

from openbench.core.registry import DeviceRegistry
from openbench.drivers.feeltech_fy import (
    WAVEFORM_OPTIONS,
    FeelTechAdvancedState,
    FeelTechChannelUpdate,
    FeelTechFYGenerator,
    FeelTechGeneratorState,
    FeelTechSweepState,
)
from openbench.services.matrix_service import MatrixService


class SignalGeneratorService:
    def __init__(
        self,
        registry: DeviceRegistry,
        matrix_service: MatrixService,
    ) -> None:
        self._registry = registry
        self._matrix_service = matrix_service

    def _generator(self, device_id: str) -> FeelTechFYGenerator:
        device = self._registry.device(device_id)
        instrument = self._registry.instrument(device_id)
        if device.kind != "feeltech_fy" or not isinstance(instrument, FeelTechFYGenerator):
            raise ValueError(f"Device is not a supported signal generator: {device_id}")
        return instrument

    def _require_safe(self) -> None:
        safety = self._matrix_service.safety_state()
        if safety.state != "safe":
            raise ValueError(f"Generator output is blocked while safety state is {safety.state}")

    async def state(self, device_id: str) -> FeelTechGeneratorState:
        return await self._generator(device_id).read_state(force=True)

    def waveform_options(self, device_id: str) -> tuple[tuple[int, str, float], ...]:
        generator = self._generator(device_id)
        return tuple(
            (code, name, generator.frequency_limit_hz(code)) for code, name in WAVEFORM_OPTIONS
        )

    async def update_channel(
        self,
        device_id: str,
        channel: int,
        update: FeelTechChannelUpdate,
    ) -> FeelTechGeneratorState:
        generator = self._generator(device_id)
        current = (await generator.read_state(force=True)).channel(channel)
        target_enabled = (
            current.output_enabled if update.output_enabled is None else update.output_enabled
        )
        if target_enabled:
            self._require_safe()
        return await generator.update_channel(channel, update)

    async def set_outputs(
        self,
        device_id: str,
        *,
        channel_1: bool,
        channel_2: bool,
    ) -> FeelTechGeneratorState:
        if channel_1 or channel_2:
            self._require_safe()
        return await self._generator(device_id).set_outputs(
            channel_1=channel_1,
            channel_2=channel_2,
        )

    async def set_channel_output(
        self,
        device_id: str,
        channel: int,
        enabled: bool,
    ) -> bool:
        if enabled:
            self._require_safe()
        return await self._generator(device_id).set_channel_output(channel, enabled)

    async def synchronization(self, device_id: str) -> dict[str, bool]:
        return await self._generator(device_id).synchronization()

    async def set_synchronization(
        self,
        device_id: str,
        key: str,
        enabled: bool,
    ) -> dict[str, bool]:
        state = await self.state(device_id)
        if any(channel.output_enabled for channel in state.channels):
            self._require_safe()
        return await self._generator(device_id).set_synchronization(key, enabled)

    async def advanced_state(self, device_id: str) -> FeelTechAdvancedState:
        return await self._generator(device_id).read_advanced_state()

    async def configure_burst(
        self,
        device_id: str,
        *,
        trigger_mode: int,
        cycles: int,
    ) -> FeelTechAdvancedState:
        state = await self.state(device_id)
        if any(channel.output_enabled for channel in state.channels):
            self._require_safe()
        return await self._generator(device_id).configure_burst(
            trigger_mode=trigger_mode,
            cycles=cycles,
        )

    async def trigger_once(
        self,
        device_id: str,
        *,
        cycles: int | None = None,
    ) -> FeelTechAdvancedState:
        self._require_safe()
        return await self._generator(device_id).trigger_once(cycles=cycles)

    async def configure_keying(
        self,
        device_id: str,
        *,
        kind: str,
        mode: int,
        secondary_frequency_hz: float | None = None,
    ) -> FeelTechAdvancedState:
        state = await self.state(device_id)
        if any(channel.output_enabled for channel in state.channels):
            self._require_safe()
        return await self._generator(device_id).configure_keying(
            kind=kind,
            mode=mode,
            secondary_frequency_hz=secondary_frequency_hz,
        )

    async def configure_counter(
        self,
        device_id: str,
        *,
        gate_code: int,
        coupling: str,
        mode: str = "frequency",
    ) -> FeelTechAdvancedState:
        return await self._generator(device_id).configure_counter(
            gate_code=gate_code,
            coupling=coupling,
            mode=mode,
        )

    async def pause_counter(self, device_id: str) -> FeelTechAdvancedState:
        return await self._generator(device_id).pause_counter()

    async def reset_counter(self, device_id: str) -> FeelTechAdvancedState:
        return await self._generator(device_id).reset_counter()

    async def configure_sweep(
        self,
        device_id: str,
        *,
        target: str,
        start: float,
        end: float,
        duration_s: float,
        mode: str,
        source: str,
        enabled: bool,
    ) -> FeelTechSweepState:
        if enabled:
            self._require_safe()
        return await self._generator(device_id).configure_sweep(
            target=target,
            start=start,
            end=end,
            duration_s=duration_s,
            mode=mode,
            source=source,
            enabled=enabled,
        )

    async def save_preset(self, device_id: str, slot: int) -> None:
        await self._generator(device_id).save_preset(slot)

    async def load_preset(
        self,
        device_id: str,
        slot: int,
    ) -> FeelTechGeneratorState:
        self._require_safe()
        return await self._generator(device_id).load_preset(slot)

    async def all_outputs_off(self) -> tuple[str, ...]:
        generators = tuple(
            device.id for device in self._registry.devices() if device.kind == "feeltech_fy"
        )
        if not generators:
            return ()

        results = await asyncio.gather(
            *(
                self.set_outputs(device_id, channel_1=False, channel_2=False)
                for device_id in generators
            ),
            return_exceptions=True,
        )
        return tuple(
            f"{device_id}: {result}"
            for device_id, result in zip(generators, results, strict=True)
            if isinstance(result, BaseException)
        )
