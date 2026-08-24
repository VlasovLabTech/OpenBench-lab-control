from __future__ import annotations

from dataclasses import dataclass

from openbench.core.registry import DeviceRegistry
from openbench.core.scheduler import PollingScheduler
from openbench.drivers.micsig_common import is_micsig_scope_kind
from openbench.services.capture_service import CaptureService
from openbench.services.device_service import DeviceService
from openbench.services.scope_measurement_service import (
    SCOPE_POLL_INTERVAL_S,
    ScopeMeasurementService,
)
from openbench.storage import InstrumentPreferenceStore

MAX_POLL_INTERVAL_S = 600.0
MIN_POLL_INTERVAL_BY_KIND = {
    "simulated_meter": 0.1,
    "ut197": 0.3,
    "ut61e": 0.5,
    "ut61d": 0.5,
    "ut61eplus": 1.0,
    "feeltech_fy": 0.5,
    "fnirsi_dps150": 0.5,
    "owon_spm": 0.5,
    "itech_it6000c": 1.0,
    "micsig_mho1": SCOPE_POLL_INTERVAL_S,
    "micsig_eto": SCOPE_POLL_INTERVAL_S,
}


@dataclass(frozen=True, slots=True)
class InstrumentSettings:
    device_id: str
    context: str
    poll_interval_s: float | None
    minimum_poll_interval_s: float | None
    scope_screen: bool | None
    scope_data: bool | None
    scope_channels: tuple[str, ...] | None
    scope_wait_for_trigger: bool | None


class InstrumentSettingsService:
    def __init__(
        self,
        registry: DeviceRegistry,
        scheduler: PollingScheduler,
        device_service: DeviceService,
        scope_measurement_service: ScopeMeasurementService,
        capture_service: CaptureService,
        preferences: InstrumentPreferenceStore | None = None,
    ) -> None:
        self._registry = registry
        self._scheduler = scheduler
        self._device_service = device_service
        self._scope_measurement_service = scope_measurement_service
        self._capture_service = capture_service
        self._preferences = preferences

    def preferred_poll_interval(
        self,
        device_id: str,
        *,
        kind: str,
        default: float,
    ) -> float:
        if self._preferences is None:
            return default
        minimum = MIN_POLL_INTERVAL_BY_KIND.get(kind)
        stored = self._preferences.get(device_id).get("poll_interval_s")
        if (
            minimum is not None
            and isinstance(stored, (int, float))
            and not isinstance(stored, bool)
            and minimum <= float(stored) <= MAX_POLL_INTERVAL_S
        ):
            return float(stored)
        return default

    def get(self, device_id: str) -> InstrumentSettings:
        device = self._registry.device(device_id)
        minimum = MIN_POLL_INTERVAL_BY_KIND.get(device.kind)
        interval: float | None
        scope_screen: bool | None
        scope_data: bool | None
        scope_channels: tuple[str, ...] | None
        scope_wait_for_trigger: bool | None
        if is_micsig_scope_kind(device.kind):
            interval = self._scope_measurement_service.interval_for(device_id)
            scope_options = self._capture_service.scope_options(device_id)
            scope_screen = scope_options.screen
            scope_data = scope_options.data
            scope_channels = scope_options.channels
            scope_wait_for_trigger = scope_options.wait_for_trigger
        else:
            channels = tuple(
                channel for channel in self._registry.channels() if channel.device_id == device_id
            )
            interval = (
                self._scheduler.interval_for(channels[0].id)
                if channels and minimum is not None
                else None
            )
            scope_screen = None
            scope_data = None
            scope_channels = None
            scope_wait_for_trigger = None
        return InstrumentSettings(
            device_id=device_id,
            context=self._capture_service.instrument_context(device_id),
            poll_interval_s=interval,
            minimum_poll_interval_s=minimum,
            scope_screen=scope_screen,
            scope_data=scope_data,
            scope_channels=scope_channels,
            scope_wait_for_trigger=scope_wait_for_trigger,
        )

    def update_context(self, device_id: str, value: str) -> InstrumentSettings:
        self._capture_service.update_instrument_context(device_id, value)
        return self.get(device_id)

    async def update_poll_interval(
        self,
        device_id: str,
        interval_s: float,
    ) -> InstrumentSettings:
        device = self._registry.device(device_id)
        minimum = MIN_POLL_INTERVAL_BY_KIND.get(device.kind)
        if minimum is None:
            raise ValueError(f"Device does not support polling: {device_id}")
        if not minimum <= interval_s <= MAX_POLL_INTERVAL_S:
            raise ValueError(
                f"{device.name} polling interval must be between "
                f"{minimum:g} and {MAX_POLL_INTERVAL_S:g} seconds."
            )

        if is_micsig_scope_kind(device.kind):
            self._scope_measurement_service.update_interval(device_id, interval_s)
            return self.get(device_id)

        channels = tuple(
            channel for channel in self._registry.channels() if channel.device_id == device_id
        )
        if not channels:
            raise ValueError("Device has no polling channels")
        updated_channels = []
        for channel in channels:
            updated_channels.append(await self._scheduler.update_interval(channel.id, interval_s))
        for channel in updated_channels:
            self._registry.update_channel(channel)
        self._device_service.register(device, tuple(updated_channels))
        if self._preferences is not None:
            self._preferences.update(device_id, poll_interval_s=interval_s)
        return self.get(device_id)

    def update_scope_capture(
        self,
        device_id: str,
        *,
        screen: bool | None = None,
        data: bool | None = None,
        channels: tuple[str, ...] | None = None,
        wait_for_trigger: bool | None = None,
    ) -> InstrumentSettings:
        current = self.get(device_id)
        if current.scope_screen is None or current.scope_data is None:
            raise ValueError(f"Device is not an oscilloscope: {device_id}")
        self._capture_service.update_scope_options(
            device_id,
            screen=current.scope_screen if screen is None else screen,
            data=current.scope_data if data is None else data,
            channels=current.scope_channels if channels is None else channels,
            wait_for_trigger=(
                current.scope_wait_for_trigger if wait_for_trigger is None else wait_for_trigger
            ),
        )
        return self.get(device_id)
