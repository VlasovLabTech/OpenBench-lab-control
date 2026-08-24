from __future__ import annotations

from openbench.core.registry import DeviceRegistry
from openbench.drivers.owon_spm import (
    OwonSPMDMMUpdate,
    OwonSPMInstrument,
    OwonSPMOutputUpdate,
    OwonSPMProtectionUpdate,
    OwonSPMState,
)
from openbench.services.matrix_service import MatrixService


class SourceMeasureUnitService:
    def __init__(self, registry: DeviceRegistry, matrix_service: MatrixService) -> None:
        self._registry = registry
        self._matrix_service = matrix_service

    def _instrument(self, device_id: str) -> OwonSPMInstrument:
        device = self._registry.device(device_id)
        instrument = self._registry.instrument(device_id)
        if device.kind != "owon_spm" or not isinstance(instrument, OwonSPMInstrument):
            raise ValueError(f"Device is not a supported source-measure unit: {device_id}")
        return instrument

    def _require_safe(self) -> None:
        safety = self._matrix_service.safety_state()
        if safety.state != "safe":
            raise ValueError(f"Source output is blocked while safety state is {safety.state}")

    def device_ids(self) -> tuple[str, ...]:
        return tuple(device.id for device in self._registry.devices() if device.kind == "owon_spm")

    async def state(self, device_id: str) -> OwonSPMState:
        return await self._instrument(device_id).read_state(force=True)

    async def update_output(self, device_id: str, update: OwonSPMOutputUpdate) -> OwonSPMState:
        instrument = self._instrument(device_id)
        current = await instrument.read_state(force=True)
        target_enabled = current.source.output_enabled if update.enabled is None else update.enabled
        if target_enabled:
            self._require_safe()
        return await instrument.update_output(update)

    async def update_protections(
        self, device_id: str, update: OwonSPMProtectionUpdate
    ) -> OwonSPMState:
        return await self._instrument(device_id).update_protections(update)

    async def update_multimeter(self, device_id: str, update: OwonSPMDMMUpdate) -> OwonSPMState:
        return await self._instrument(device_id).update_multimeter(update)

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
