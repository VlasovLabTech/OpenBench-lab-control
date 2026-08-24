from __future__ import annotations

import asyncio

from openbench.core.registry import DeviceRegistry
from openbench.core.scheduler import PollingScheduler
from openbench.domain import Measurement
from openbench.drivers.itech_it6000c import (
    ITechAdvancedUpdate,
    ITechIT6000C,
    ITechIT6000CState,
    ITechOperatingPointUpdate,
    ITechProtectionUpdate,
)
from openbench.services.matrix_service import MatrixService


class BidirectionalPowerSupplyService:
    def __init__(
        self,
        registry: DeviceRegistry,
        matrix_service: MatrixService,
        scheduler: PollingScheduler,
    ) -> None:
        self._registry = registry
        self._matrix_service = matrix_service
        self._scheduler = scheduler
        self._experiment_reservations: set[str] = set()
        self._reservation_lock = asyncio.Lock()

    def _instrument(self, device_id: str) -> ITechIT6000C:
        device = self._registry.device(device_id)
        instrument = self._registry.instrument(device_id)
        if device.kind != "itech_it6000c" or not isinstance(instrument, ITechIT6000C):
            raise ValueError(f"Device is not an ITECH IT6000C: {device_id}")
        return instrument

    def _require_safe(self) -> None:
        safety = self._matrix_service.safety_state()
        if safety.state != "safe":
            raise ValueError(f"ITECH output is blocked while safety state is {safety.state}")

    def device_ids(self) -> tuple[str, ...]:
        return tuple(
            device.id for device in self._registry.devices() if device.kind == "itech_it6000c"
        )

    async def state(self, device_id: str) -> ITechIT6000CState:
        return await self._instrument(device_id).read_state(force=True, full=True)

    async def update_operating_point(
        self, device_id: str, update: ITechOperatingPointUpdate
    ) -> ITechIT6000CState:
        instrument = self._instrument(device_id)
        current = instrument.cached_state
        target_enabled = update.output_enabled
        if target_enabled is None:
            # Conservatively require a safe matrix if there is no state cache.
            target_enabled = True if current is None else current.output_enabled
        if target_enabled:
            self._require_safe()
        return await instrument.update_operating_point(update)

    async def measurements(self, device_id: str) -> tuple[Measurement, ...]:
        instrument = self._instrument(device_id)
        channel_ids = tuple(
            channel_id
            for channel_id, parameter in instrument.parameters
            if parameter.key in {"measured_voltage", "measured_current", "measured_power"}
        )
        sample = (
            self._scheduler.sample_now_reserved
            if device_id in self._experiment_reservations
            else self._scheduler.sample_now
        )
        return tuple(await asyncio.gather(*(sample(channel_id) for channel_id in channel_ids)))

    async def reserve_experiment(self, device_id: str) -> int:
        """Give an external experiment exclusive ownership of scheduled reads."""
        self._instrument(device_id)
        async with self._reservation_lock:
            if device_id in self._experiment_reservations:
                raise RuntimeError(f"ITECH experiment reservation is already active: {device_id}")
            suspended = await self._scheduler.suspend_device(device_id)
            if suspended == 0:
                raise RuntimeError(f"ITECH has no Dashboard polling targets: {device_id}")
            self._experiment_reservations.add(device_id)
            return suspended

    async def release_experiment(self, device_id: str) -> int:
        """Release exclusive scheduled-read ownership after cleanup is complete."""
        self._instrument(device_id)
        async with self._reservation_lock:
            if device_id not in self._experiment_reservations:
                return 0
            self._experiment_reservations.remove(device_id)
            return self._scheduler.resume_device(device_id)

    def experiment_reserved(self, device_id: str) -> bool:
        self._instrument(device_id)
        return device_id in self._experiment_reservations

    async def update_protections(
        self, device_id: str, update: ITechProtectionUpdate
    ) -> ITechIT6000CState:
        return await self._instrument(device_id).update_protections(update)

    async def clear_protection(self, device_id: str) -> ITechIT6000CState:
        return await self._instrument(device_id).clear_protection()

    async def update_advanced(
        self, device_id: str, update: ITechAdvancedUpdate
    ) -> ITechIT6000CState:
        return await self._instrument(device_id).update_advanced(update)

    async def remove_device(self, device_id: str) -> None:
        await self._instrument(device_id).force_output_off()

    async def all_outputs_off(self) -> tuple[str, ...]:
        errors: list[str] = []
        for device_id in self.device_ids():
            try:
                await self._instrument(device_id).force_output_off()
            except Exception as exc:
                errors.append(f"{device_id}: {exc}")
        return tuple(errors)

    async def close(self) -> None:
        await self.all_outputs_off()
