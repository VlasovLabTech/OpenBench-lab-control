from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from openbench.core.events import MeasurementEventBus
from openbench.domain import Measurement
from openbench.drivers.micsig_mho1 import (
    MAX_SCALAR_MEASUREMENTS,
    MICSIG_DELAY_EDGES,
    SCALAR_MEASUREMENT_COMMANDS,
    MicsigScalarMeasurement,
    MicsigScalarMeasurementSpec,
    MicsigSnapshot,
)
from openbench.storage import InstrumentPreferenceStore

SCOPE_CHANNELS = ("CH1", "CH2", "CH3", "CH4")
MAX_SCOPE_MEASUREMENTS = MAX_SCALAR_MEASUREMENTS
SCOPE_POLL_INTERVAL_S = 2.0
SCOPE_FRESHNESS_TIMEOUT_S = 2.5
SCOPE_FRAME_RUN_DWELL_S = 0.25

SCOPE_MEASUREMENT_LABELS = {
    "period": "PERIOD",
    "frequency": "FREQ",
    "rise_time": "RISE",
    "fall_time": "FALL",
    "positive_duty": "DUTY+",
    "negative_duty": "DUTY-",
    "positive_width": "WIDTH+",
    "negative_width": "WIDTH-",
    "burst_width": "BURST",
    "positive_overshoot": "OVERSHOOT+",
    "negative_overshoot": "OVERSHOOT-",
    "phase": "PHASE",
    "delay": "DELAY",
    "peak_to_peak": "PK-PK",
    "amplitude": "AMP",
    "high": "HIGH",
    "low": "LOW",
    "maximum": "MAX",
    "minimum": "MIN",
    "rms": "RMS",
    "cycle_rms": "CYCLE RMS",
    "mean": "MEAN",
    "cycle_mean": "CYCLE MEAN",
    "ac_rms": "AC RMS",
    "positive_rate": "RATE+",
    "negative_rate": "RATE-",
}


class ScopeMeasurementInstrument(Protocol):
    device_id: str

    async def read_scalar_measurement_profile(
        self,
        measurements: Sequence[MicsigScalarMeasurementSpec],
    ) -> tuple[MicsigScalarMeasurement, ...]: ...

    async def replace_scalar_measurements(
        self,
        measurements: Sequence[MicsigScalarMeasurementSpec],
    ) -> tuple[MicsigScalarMeasurement, ...]: ...

    async def capture_frame(
        self,
        measurements: Sequence[MicsigScalarMeasurementSpec],
        *,
        channels: Sequence[int | str] = (1, 2, 3, 4),
        include_screenshot: bool = True,
        stop_before_capture: bool = True,
        resume_after: bool = True,
    ) -> MicsigSnapshot: ...

    async def single(self, *, wait_timeout_s: float | None = None) -> None: ...

    async def wait_for_trigger(
        self,
        *,
        timeout_s: float | None = None,
        poll_interval_s: float = 0.05,
    ) -> str: ...

    async def start(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ScopeCaptureOptions:
    screen: bool = True
    data: bool = True
    channels: tuple[str, ...] = SCOPE_CHANNELS
    wait_for_trigger: bool = False

    @property
    def active_channels(self) -> tuple[str, ...]:
        return self.channels if self.data else ()


@dataclass(frozen=True, slots=True)
class ScopeMeasurementSelection:
    channel: str
    item: str
    secondary_channel: str | None = None
    source_edge: str | None = None
    target_edge: str | None = None

    @property
    def label(self) -> str:
        return SCOPE_MEASUREMENT_LABELS.get(self.item, self.item.upper())

    @property
    def unit(self) -> str:
        return SCALAR_MEASUREMENT_COMMANDS[self.item][1]

    @property
    def source_label(self) -> str:
        if self.secondary_channel is None:
            return self.channel
        return f"{self.channel}→{self.secondary_channel}"

    @property
    def driver_spec(self) -> MicsigScalarMeasurementSpec:
        return MicsigScalarMeasurementSpec(
            channel=self.channel,
            item=self.item,
            secondary_channel=self.secondary_channel,
            source_edge=self.source_edge,
            target_edge=self.target_edge,
        )


@dataclass(frozen=True, slots=True)
class LiveScopeMeasurement:
    device_id: str
    timestamp_utc: datetime
    selection: ScopeMeasurementSelection
    scalar: MicsigScalarMeasurement

    @property
    def event_channel_id(self) -> str:
        return scope_event_channel_id(
            self.device_id,
            self.selection.channel,
            self.selection.item,
            secondary_channel=self.selection.secondary_channel,
            source_edge=self.selection.source_edge,
            target_edge=self.selection.target_edge,
        )


@dataclass(slots=True)
class _ScopeTarget:
    instrument: ScopeMeasurementInstrument
    poll_interval_s: float
    latest: dict[ScopeMeasurementSelection, LiveScopeMeasurement] = field(default_factory=dict)
    latest_frame: MicsigSnapshot | None = None
    last_waveform_frame_completed_monotonic_s: float | None = None
    connected: bool = True
    last_activity_monotonic_s: float = field(default_factory=time.monotonic)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    interval_changed: asyncio.Event = field(default_factory=asyncio.Event)
    polling_resumed: asyncio.Event = field(default_factory=asyncio.Event)
    polling_suspend_count: int = 0
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class TriggeredScopeFrame:
    triggered_at_utc: datetime
    readings: tuple[LiveScopeMeasurement, ...]
    frame: MicsigSnapshot


def scope_event_channel_id(
    device_id: str,
    channel: str,
    item: str,
    *,
    secondary_channel: str | None = None,
    source_edge: str | None = None,
    target_edge: str | None = None,
) -> str:
    parts = [device_id, "scope", channel.casefold()]
    if secondary_channel is not None:
        parts.append(secondary_channel.casefold())
    parts.append(item)
    if item == "delay":
        parts.extend(
            (
                (source_edge or "FRISe").casefold(),
                (target_edge or "FRISe").casefold(),
            )
        )
    return ".".join(parts)


class ScopeMeasurementService:
    """Poll an explicit, compact set of Micsig scalar measurements."""

    def __init__(
        self,
        event_bus: MeasurementEventBus,
        *,
        poll_interval_s: float = SCOPE_POLL_INTERVAL_S,
        preferences: InstrumentPreferenceStore | None = None,
    ) -> None:
        if poll_interval_s < SCOPE_POLL_INTERVAL_S:
            raise ValueError(
                f"Scope polling interval must be at least {SCOPE_POLL_INTERVAL_S:g} seconds"
            )
        self._event_bus = event_bus
        self._preferences = preferences
        self.poll_interval_s = poll_interval_s
        self._profiles: dict[str, list[ScopeMeasurementSelection]] = {}
        self._poll_intervals: dict[str, float] = {}
        self._capture_options: dict[str, ScopeCaptureOptions] = {}
        self._targets: dict[str, _ScopeTarget] = {}
        self._running = False
        self._stop_event = asyncio.Event()

    async def add_scope(self, instrument: ScopeMeasurementInstrument) -> None:
        if instrument.device_id in self._targets:
            return
        self._restore_preferences(instrument.device_id)
        target = _ScopeTarget(
            instrument=instrument,
            poll_interval_s=self._poll_intervals.setdefault(
                instrument.device_id,
                self.poll_interval_s,
            ),
        )
        target.polling_resumed.set()
        self._targets[instrument.device_id] = target
        self._profiles.setdefault(instrument.device_id, [])
        screenshot_supported = bool(getattr(instrument, "screenshot_supported", True))
        existing_options = self._capture_options.get(instrument.device_id)
        if existing_options is None:
            self._capture_options[instrument.device_id] = ScopeCaptureOptions(
                screen=screenshot_supported
            )
        elif existing_options.screen and not screenshot_supported:
            self._capture_options[instrument.device_id] = ScopeCaptureOptions(
                screen=False,
                data=existing_options.data,
                channels=existing_options.channels,
                wait_for_trigger=existing_options.wait_for_trigger,
            )
        if self._running:
            target.task = self._create_task(target)

    async def remove_scope(self, device_id: str) -> None:
        target = self._targets.pop(device_id, None)
        if target is None or target.task is None:
            return
        target.task.cancel()
        await asyncio.gather(target.task, return_exceptions=True)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        for target in self._targets.values():
            target.task = self._create_task(target)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        tasks = tuple(target.task for target in self._targets.values() if target.task is not None)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for target in self._targets.values():
            target.task = None

    def _create_task(self, target: _ScopeTarget) -> asyncio.Task[None]:
        return asyncio.create_task(
            self._run_target(target),
            name=f"openbench-scope-measurements-{target.instrument.device_id}",
        )

    def selections(self, device_id: str) -> tuple[ScopeMeasurementSelection, ...]:
        return tuple(self._profiles.get(device_id, ()))

    def latest(self, device_id: str) -> tuple[LiveScopeMeasurement, ...]:
        target = self._targets.get(device_id)
        if target is None:
            return ()
        return tuple(
            target.latest[selection]
            for selection in self.selections(device_id)
            if selection in target.latest
        )

    def latest_for(
        self,
        device_id: str,
        selection: ScopeMeasurementSelection,
    ) -> LiveScopeMeasurement | None:
        target = self._targets.get(device_id)
        return None if target is None else target.latest.get(selection)

    def latest_frame(self, device_id: str) -> MicsigSnapshot | None:
        target = self._targets.get(device_id)
        return None if target is None else target.latest_frame

    def capture_options(self, device_id: str) -> ScopeCaptureOptions:
        self._require_target(device_id)
        return self._capture_options.setdefault(device_id, ScopeCaptureOptions())

    def update_capture_options(
        self,
        device_id: str,
        *,
        screen: bool | None = None,
        data: bool | None = None,
        channels: Sequence[str] | None = None,
        wait_for_trigger: bool | None = None,
    ) -> ScopeCaptureOptions:
        target = self._require_target(device_id)
        if screen and not bool(getattr(target.instrument, "screenshot_supported", True)):
            raise ValueError(f"Direct screenshot capture is not supported by {device_id}")
        current = self.capture_options(device_id)
        selected_channels = (
            current.channels if channels is None else self._normalize_capture_channels(channels)
        )
        data_enabled = current.data if data is None else data
        if data_enabled and not selected_channels:
            if channels is None:
                selected_channels = SCOPE_CHANNELS
            else:
                raise ValueError(
                    "Select at least one oscilloscope channel when waveform data is enabled"
                )
        options = ScopeCaptureOptions(
            screen=current.screen if screen is None else screen,
            data=data_enabled,
            channels=selected_channels,
            wait_for_trigger=(
                current.wait_for_trigger if wait_for_trigger is None else wait_for_trigger
            ),
        )
        self._capture_options[device_id] = options
        if self._preferences is not None:
            self._preferences.update_section(
                device_id,
                "scope",
                screen=options.screen,
                data=options.data,
                channels=list(options.channels),
                wait_for_trigger=options.wait_for_trigger,
            )
        return options

    @staticmethod
    def _normalize_capture_channels(channels: Sequence[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        for channel in channels:
            selected = channel.strip().upper()
            if selected not in SCOPE_CHANNELS:
                raise ValueError(f"Unknown oscilloscope channel: {channel}")
            if selected not in normalized:
                normalized.append(selected)
        return tuple(normalized)

    def interval_for(self, device_id: str) -> float:
        target = self._targets.get(device_id)
        if target is not None:
            return target.poll_interval_s
        return self._poll_intervals.get(device_id, self.poll_interval_s)

    def update_interval(self, device_id: str, interval_s: float) -> float:
        target = self._require_target(device_id)
        if not SCOPE_POLL_INTERVAL_S <= interval_s <= 600:
            raise ValueError(
                f"Scope polling interval must be between {SCOPE_POLL_INTERVAL_S:g} and 600 seconds"
            )
        target.poll_interval_s = interval_s
        self._poll_intervals[device_id] = interval_s
        target.interval_changed.set()
        if self._preferences is not None:
            self._preferences.update(device_id, poll_interval_s=interval_s)
        return interval_s

    def freshness_timeout_for(self, device_id: str) -> float:
        return max(
            SCOPE_FRESHNESS_TIMEOUT_S,
            self.interval_for(device_id) * 1.5 + 1.0,
        )

    async def replace_selections(
        self,
        device_id: str,
        selections: Sequence[ScopeMeasurementSelection],
    ) -> tuple[LiveScopeMeasurement, ...]:
        target = self._require_target(device_id)
        normalized: list[ScopeMeasurementSelection] = []
        for selection in selections:
            checked = self._validate_selection(
                selection.channel,
                selection.item,
                secondary_channel=selection.secondary_channel,
                source_edge=selection.source_edge,
                target_edge=selection.target_edge,
            )
            if checked not in normalized:
                normalized.append(checked)
        if len(normalized) > MAX_SCOPE_MEASUREMENTS:
            raise ValueError(
                f"No more than {MAX_SCOPE_MEASUREMENTS} scope measurements can be selected"
            )

        async with target.operation_lock:
            scalars = await target.instrument.replace_scalar_measurements(
                tuple(selection.driver_spec for selection in normalized)
            )
            profile = self._profiles.setdefault(device_id, [])
            profile[:] = normalized
            if self._preferences is not None:
                self._preferences.update_section(
                    device_id,
                    "scope",
                    measurements=[
                        {
                            "channel": selection.channel,
                            "item": selection.item,
                            "secondary_channel": selection.secondary_channel,
                            "source_edge": selection.source_edge,
                            "target_edge": selection.target_edge,
                        }
                        for selection in normalized
                    ],
                )
            target.latest = {
                selection: reading
                for selection, reading in target.latest.items()
                if selection in normalized
            }
            readings = self._store_scalars(target, tuple(normalized), scalars)
            await self._publish(readings)
            return readings

    def _restore_preferences(self, device_id: str) -> None:
        if self._preferences is None:
            return
        preferences = self._preferences.get(device_id)

        if device_id not in self._poll_intervals:
            interval = preferences.get("poll_interval_s")
            if (
                isinstance(interval, (int, float))
                and not isinstance(interval, bool)
                and SCOPE_POLL_INTERVAL_S <= float(interval) <= 600
            ):
                self._poll_intervals[device_id] = float(interval)

        scope = preferences.get("scope")
        if not isinstance(scope, dict):
            return

        if device_id not in self._capture_options:
            screen = scope.get("screen", True)
            data = scope.get("data", True)
            wait_for_trigger = scope.get("wait_for_trigger", False)
            raw_channels = scope.get("channels", list(SCOPE_CHANNELS))
            try:
                channels = self._normalize_capture_channels(
                    raw_channels if isinstance(raw_channels, (list, tuple)) else SCOPE_CHANNELS
                )
                if (
                    not isinstance(screen, bool)
                    or not isinstance(data, bool)
                    or not isinstance(wait_for_trigger, bool)
                ):
                    raise ValueError("invalid scope option type")
                if data and not channels:
                    raise ValueError("ASCII data requires at least one channel")
            except (AttributeError, TypeError, ValueError):
                self._capture_options[device_id] = ScopeCaptureOptions()
            else:
                self._capture_options[device_id] = ScopeCaptureOptions(
                    screen=screen,
                    data=data,
                    channels=channels,
                    wait_for_trigger=wait_for_trigger,
                )

        if device_id not in self._profiles:
            restored: list[ScopeMeasurementSelection] = []
            raw_measurements = scope.get("measurements", [])
            if isinstance(raw_measurements, list):
                for raw in raw_measurements:
                    if not isinstance(raw, dict):
                        continue
                    channel = raw.get("channel")
                    item = raw.get("item")
                    secondary_channel = raw.get("secondary_channel")
                    source_edge = raw.get("source_edge")
                    target_edge = raw.get("target_edge")
                    if not isinstance(channel, str) or not isinstance(item, str):
                        continue
                    if secondary_channel is not None and not isinstance(secondary_channel, str):
                        continue
                    if source_edge is not None and not isinstance(source_edge, str):
                        continue
                    if target_edge is not None and not isinstance(target_edge, str):
                        continue
                    try:
                        selection = self._validate_selection(
                            channel,
                            item,
                            secondary_channel=secondary_channel,
                            source_edge=source_edge,
                            target_edge=target_edge,
                        )
                    except ValueError:
                        continue
                    if selection not in restored:
                        restored.append(selection)
                    if len(restored) == MAX_SCOPE_MEASUREMENTS:
                        break
            self._profiles[device_id] = restored

    async def add_selection(
        self,
        device_id: str,
        *,
        channel: str,
        item: str,
        secondary_channel: str | None = None,
        source_edge: str | None = None,
        target_edge: str | None = None,
    ) -> ScopeMeasurementSelection:
        self._require_target(device_id)
        selection = self._validate_selection(
            channel,
            item,
            secondary_channel=secondary_channel,
            source_edge=source_edge,
            target_edge=target_edge,
        )
        profile = self._profiles.setdefault(device_id, [])
        if selection in profile:
            return selection
        if len(profile) >= MAX_SCOPE_MEASUREMENTS:
            raise ValueError(
                f"No more than {MAX_SCOPE_MEASUREMENTS} scope measurements can be selected"
            )
        await self.replace_selections(device_id, (*profile, selection))
        target = self._targets[device_id]
        target.connected = True
        target.last_activity_monotonic_s = time.monotonic()
        return selection

    async def remove_selection(
        self,
        device_id: str,
        *,
        channel: str,
        item: str,
    ) -> None:
        target = self._require_target(device_id)
        selection = ScopeMeasurementSelection(channel.strip().upper(), item.strip().lower())
        profile = self._profiles.setdefault(device_id, [])
        if selection not in profile:
            raise KeyError(f"Scope measurement is not selected: {channel} {item}")
        await self.replace_selections(
            device_id,
            tuple(item for item in profile if item != selection),
        )
        target.latest.pop(selection, None)

    @staticmethod
    def _validate_selection(
        channel: str,
        item: str,
        *,
        secondary_channel: str | None = None,
        source_edge: str | None = None,
        target_edge: str | None = None,
    ) -> ScopeMeasurementSelection:
        normalized_channel = channel.strip().upper()
        normalized_item = item.strip().lower()
        if normalized_channel not in SCOPE_CHANNELS:
            raise ValueError(f"Unknown oscilloscope channel: {channel}")
        if normalized_item not in SCALAR_MEASUREMENT_COMMANDS:
            raise ValueError(f"Unknown oscilloscope measurement: {item}")
        normalized_secondary = (
            secondary_channel.strip().upper() if secondary_channel is not None else None
        )
        if normalized_item in {"phase", "delay"}:
            if normalized_secondary not in SCOPE_CHANNELS:
                raise ValueError(f"{normalized_item.upper()} requires a valid secondary channel")
            if normalized_secondary == normalized_channel:
                raise ValueError(f"{normalized_item.upper()} channels must be different")
        else:
            normalized_secondary = None

        if normalized_item == "delay":
            normalized_source_edge = ScopeMeasurementService._normalize_delay_edge(
                source_edge or "FRISe"
            )
            normalized_target_edge = ScopeMeasurementService._normalize_delay_edge(
                target_edge or "FRISe"
            )
        else:
            normalized_source_edge = None
            normalized_target_edge = None
        return ScopeMeasurementSelection(
            normalized_channel,
            normalized_item,
            normalized_secondary,
            normalized_source_edge,
            normalized_target_edge,
        )

    @staticmethod
    def _normalize_delay_edge(edge: str) -> str:
        normalized = edge.strip().casefold()
        for choice in MICSIG_DELAY_EDGES:
            if choice.casefold() == normalized:
                return choice
        raise ValueError(f"Unknown oscilloscope delay edge: {edge}")

    def device_connected(self, device_id: str, *, default: bool = True) -> bool:
        target = self._targets.get(device_id)
        if target is None:
            return default
        if not self.selections(device_id):
            return default
        age_s = time.monotonic() - target.last_activity_monotonic_s
        return target.connected and age_s <= self.freshness_timeout_for(device_id)

    async def sample_now(
        self,
        device_id: str,
        *,
        waveform_channels: Sequence[str] | None = None,
        include_screenshot: bool | None = None,
    ) -> tuple[LiveScopeMeasurement, ...]:
        target = self._require_target(device_id)
        return await self._sample_target(
            target,
            waveform_channels=waveform_channels,
            include_screenshot=include_screenshot,
        )

    async def suspend_live_polling(self, device_id: str) -> None:
        """Pause display-only polling after any in-flight read has completed."""
        target = self._require_target(device_id)
        target.polling_suspend_count += 1
        target.polling_resumed.clear()
        async with target.operation_lock:
            pass

    def resume_live_polling(self, device_id: str) -> None:
        target = self._require_target(device_id)
        if target.polling_suspend_count == 0:
            return
        target.polling_suspend_count -= 1
        if target.polling_suspend_count == 0:
            target.polling_resumed.set()

    async def start_acquisition(self, device_id: str) -> None:
        target = self._require_target(device_id)
        async with target.operation_lock:
            await target.instrument.start()

    async def capture_triggered_frame(
        self,
        device_id: str,
        *,
        timeout_s: float | None = None,
        poll_interval_s: float = 0.05,
        resume_after: bool = True,
    ) -> TriggeredScopeFrame:
        """Arm SINGLE, wait for STOP, and read the frozen acquisition."""
        target = self._require_target(device_id)
        selections = self.selections(device_id)
        options = self.capture_options(device_id)
        async with target.operation_lock:
            try:
                await target.instrument.single()
                await target.instrument.wait_for_trigger(
                    timeout_s=timeout_s,
                    poll_interval_s=poll_interval_s,
                )
                triggered_at = datetime.now(UTC)
                frame = await target.instrument.capture_frame(
                    tuple(selection.driver_spec for selection in selections),
                    channels=options.active_channels,
                    include_screenshot=options.screen,
                    stop_before_capture=False,
                    resume_after=False,
                )
                target.connected = True
                target.last_activity_monotonic_s = time.monotonic()
                target.latest_frame = frame
                readings = self._store_scalars(target, selections, frame.measurements)
                await self._publish(readings)
                if resume_after:
                    await target.instrument.start()
                return TriggeredScopeFrame(
                    triggered_at_utc=triggered_at,
                    readings=readings,
                    frame=frame,
                )
            except BaseException:
                # Cancellation, timeout, or a read error must never leave the
                # front panel frozen after OpenBench stops waiting.
                try:
                    await asyncio.shield(target.instrument.start())
                except Exception:
                    target.connected = False
                raise

    def _require_target(self, device_id: str) -> _ScopeTarget:
        try:
            return self._targets[device_id]
        except KeyError as exc:
            raise KeyError(f"Unknown scope measurement target: {device_id}") from exc

    async def _query_selected(
        self,
        target: _ScopeTarget,
        selections: tuple[ScopeMeasurementSelection, ...],
    ) -> tuple[MicsigScalarMeasurement, ...]:
        return await target.instrument.read_scalar_measurement_profile(
            tuple(selection.driver_spec for selection in selections)
        )

    async def _sample_target(
        self,
        target: _ScopeTarget,
        *,
        force_frame: bool = True,
        waveform_channels: Sequence[str] | None = None,
        include_screenshot: bool | None = None,
    ) -> tuple[LiveScopeMeasurement, ...]:
        device_id = target.instrument.device_id
        selections = self.selections(device_id)
        if not selections and not force_frame:
            return ()
        if not force_frame:
            # Dashboard refreshes are display-only. They must not stop the
            # acquisition or collect artifacts; only an explicit capture
            # requested by the common RUN workflow may use capture_frame().
            async with target.operation_lock:
                try:
                    scalars = await self._query_selected(target, selections)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    target.connected = False
                    await self._publish_disconnected(target, selections)
                    return self.latest(device_id)
                target.connected = True
                target.last_activity_monotonic_s = time.monotonic()
                readings = self._store_scalars(target, selections, scalars)
                await self._publish(readings)
                return readings

        options = self.capture_options(device_id)
        selected_waveform_channels = (
            options.active_channels
            if waveform_channels is None
            else self._normalize_capture_channels(waveform_channels)
        )
        screenshot_enabled = options.screen if include_screenshot is None else include_screenshot
        captures_waveforms = bool(selected_waveform_channels)
        async with target.operation_lock:
            if captures_waveforms and target.last_waveform_frame_completed_monotonic_s is not None:
                run_dwell_remaining_s = max(
                    0.0,
                    SCOPE_FRAME_RUN_DWELL_S
                    - (time.monotonic() - target.last_waveform_frame_completed_monotonic_s),
                )
                if run_dwell_remaining_s:
                    await asyncio.sleep(run_dwell_remaining_s)
            try:
                frame = await target.instrument.capture_frame(
                    tuple(selection.driver_spec for selection in selections),
                    channels=selected_waveform_channels,
                    include_screenshot=screenshot_enabled,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                target.connected = False
                await self._publish_disconnected(target, selections)
                return self.latest(device_id)
            finally:
                if captures_waveforms:
                    target.last_waveform_frame_completed_monotonic_s = time.monotonic()

            target.connected = True
            target.last_activity_monotonic_s = time.monotonic()
            target.latest_frame = frame
            readings = self._store_scalars(target, selections, frame.measurements)
            await self._publish(readings)
            return readings

    @staticmethod
    def _store_scalars(
        target: _ScopeTarget,
        selections: tuple[ScopeMeasurementSelection, ...],
        scalars: Sequence[MicsigScalarMeasurement],
    ) -> tuple[LiveScopeMeasurement, ...]:
        now = datetime.now(UTC)
        by_key = {
            (
                scalar.channel,
                scalar.item,
                scalar.secondary_channel,
                scalar.source_edge,
                scalar.target_edge,
            ): scalar
            for scalar in scalars
        }
        readings = []
        for selection in selections:
            scalar = by_key.get(
                (
                    selection.channel,
                    selection.item,
                    selection.secondary_channel,
                    selection.source_edge,
                    selection.target_edge,
                )
            )
            if scalar is None:
                scalar = MicsigScalarMeasurement(
                    item=selection.item,
                    channel=selection.channel,
                    secondary_channel=selection.secondary_channel,
                    source_edge=selection.source_edge,
                    target_edge=selection.target_edge,
                    value=None,
                    unit=selection.unit,
                    status="unavailable",
                )
            live = LiveScopeMeasurement(
                device_id=target.instrument.device_id,
                timestamp_utc=now,
                selection=selection,
                scalar=scalar,
            )
            target.latest[selection] = live
            readings.append(live)
        return tuple(readings)

    async def _publish(self, readings: Sequence[LiveScopeMeasurement]) -> None:
        loop = asyncio.get_running_loop()
        for reading in readings:
            await self._event_bus.publish(
                Measurement(
                    timestamp_utc=reading.timestamp_utc,
                    monotonic_s=loop.time(),
                    device_id=reading.device_id,
                    channel_id=reading.event_channel_id,
                    value=reading.scalar.value,
                    unit=reading.scalar.unit,
                    quality="instrument_scalar",
                    status=reading.scalar.status,
                )
            )

    async def _publish_disconnected(
        self,
        target: _ScopeTarget,
        selections: Sequence[ScopeMeasurementSelection],
    ) -> None:
        loop = asyncio.get_running_loop()
        now = datetime.now(UTC)
        for selection in selections:
            await self._event_bus.publish(
                Measurement(
                    timestamp_utc=now,
                    monotonic_s=loop.time(),
                    device_id=target.instrument.device_id,
                    channel_id=scope_event_channel_id(
                        target.instrument.device_id,
                        selection.channel,
                        selection.item,
                        secondary_channel=selection.secondary_channel,
                        source_edge=selection.source_edge,
                        target_edge=selection.target_edge,
                    ),
                    value=None,
                    unit=selection.unit,
                    quality="stale",
                    status="disconnected",
                )
            )

    async def _run_target(self, target: _ScopeTarget) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            if target.polling_suspend_count:
                await target.polling_resumed.wait()
                continue
            started = loop.time()
            await self._sample_target(target, force_frame=False)
            remaining = max(
                0.0,
                target.poll_interval_s - (loop.time() - started),
            )
            try:
                await asyncio.wait_for(
                    target.interval_changed.wait(),
                    timeout=remaining,
                )
                target.interval_changed.clear()
            except TimeoutError:
                continue

    @property
    def running(self) -> bool:
        return self._running
