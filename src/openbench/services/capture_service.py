from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TextIO, cast

from openbench.core.events import MeasurementEventBus
from openbench.core.registry import DeviceRegistry
from openbench.core.scheduler import PollingScheduler
from openbench.domain import Device, Measurement
from openbench.drivers.itech_it6000c import ITechIT6000C
from openbench.drivers.micsig_common import is_micsig_scope_kind
from openbench.drivers.micsig_mho1 import (
    MicsigScalarMeasurement,
    MicsigScreenshot,
    MicsigSnapshot,
)
from openbench.services.scope_measurement_service import (
    ScopeCaptureOptions,
    ScopeMeasurementService,
    TriggeredScopeFrame,
    scope_event_channel_id,
)
from openbench.storage import InstrumentPreferenceStore

DEVICE_DISPLAY_NAMES = {
    "simulated_meter": "Simulated Meter",
    "ut197": "UT197",
    "ut61d": "UT61D",
    "ut61e": "UT61E",
    "ut61eplus": "UT61E+",
    "micsig_mho1": "MHO1",
    "micsig_eto": "ETO",
    "fnirsi_dps150": "DPS-150",
    "kingst_la2016": "LA2016",
}

RECORDING_STREAM_CHANNEL_SUFFIXES_BY_KIND = {
    "itech_it6000c": frozenset(
        {
            "voltage",
            "current",
            "power",
            "set_voltage",
            "set_current",
        }
    ),
}


class ScopeCaptureInstrument(Protocol):
    device_id: str

    async def read_scalar_measurements(
        self,
        channel: int | str,
        items: tuple[str, ...],
    ) -> tuple[MicsigScalarMeasurement, ...]: ...

    async def capture_snapshot(
        self,
        *,
        region: str = "screen",
        measurement_items: tuple[str, ...] = (),
        include_screenshot: bool = True,
        resume: bool = False,
        stop_timeout_s: float = 2.0,
    ) -> MicsigSnapshot: ...


class CsvWriter(Protocol):
    def writerow(self, row: Iterable[object]) -> object: ...


@dataclass(frozen=True, slots=True)
class ScopeCaptureResult:
    device_id: str
    timestamp_utc: datetime
    measurements: tuple[MicsigScalarMeasurement, ...]
    screen_file: str
    data_file: str
    status: str
    error: str


@dataclass(frozen=True, slots=True)
class WideColumn:
    device_id: str
    key: str
    header: str


@dataclass(frozen=True, slots=True)
class CaptureTimelineEvent:
    timestamp_utc: datetime
    device_id: str
    event: str
    capture_id: str
    state: str
    trigger: str = ""
    artifact_file: str = ""
    sample_rate_hz: int | None = None
    sample_count: int | None = None
    message: str = ""
    category: str = "logic"


def _scope_scalar_identity(scalar: MicsigScalarMeasurement) -> str:
    parts = [scalar.channel]
    if scalar.secondary_channel is not None:
        parts.append(scalar.secondary_channel)
    parts.append(scalar.item)
    if scalar.item == "delay":
        parts.extend((scalar.source_edge or "FRISe", scalar.target_edge or "FRISe"))
    return ":".join(parts)


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    active: bool
    started_at: datetime | None
    current_file: Path | None
    last_recording_file: Path | None
    last_snapshot_file: Path | None
    samples_written: int
    current_title: str
    current_comment: str
    draft_title: str
    draft_comment: str
    duration_s: float | None
    elapsed_s: float
    remaining_s: float | None
    scope_capture_mode: str


class CaptureService:
    def __init__(
        self,
        output_directory: Path,
        event_bus: MeasurementEventBus,
        scheduler: PollingScheduler,
        registry: DeviceRegistry,
        scope_measurement_service: ScopeMeasurementService,
        preferences: InstrumentPreferenceStore | None = None,
    ) -> None:
        self.output_directory = output_directory.resolve()
        self._event_bus = event_bus
        self._scheduler = scheduler
        self._registry = registry
        self._scope_measurement_service = scope_measurement_service
        self._preferences = preferences
        self._instrument_contexts: dict[str, str] = {}
        self._recording_task: asyncio.Task[None] | None = None
        self._scope_recording_tasks: tuple[asyncio.Task[None], ...] = ()
        self._manual_scope_ids: tuple[str, ...] = ()
        self._manual_scope_sequences: dict[str, int] = {}
        self._manual_scope_capture_lock = asyncio.Lock()
        self._scope_capture_mode = "periodic"
        self._recording_initial_settings: dict[str, str] = {}
        self._auto_stop_task: asyncio.Task[None] | None = None
        self._started_at: datetime | None = None
        self._duration_s: float | None = None
        self._current_file: Path | None = None
        self._last_recording_file: Path | None = None
        self._last_snapshot_file: Path | None = None
        self._samples_written = 0
        self._current_title = ""
        self._current_comment = ""
        self._draft_title = ""
        self._draft_comment = ""
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()
        self._timeline_events: asyncio.Queue[CaptureTimelineEvent] = asyncio.Queue(maxsize=128)
        self._recording_started_listeners: list[
            Callable[[datetime, Path, str, str], Awaitable[None]]
        ] = []
        self._recording_stopped_listeners: list[Callable[[Path], Awaitable[None]]] = []
        self._device_exclusion_provider: Callable[[], frozenset[str]] = frozenset

    def set_device_exclusion_provider(
        self,
        provider: Callable[[], frozenset[str]],
    ) -> None:
        """Exclude devices owned by a dedicated mode from common captures."""
        self._device_exclusion_provider = provider

    def _device_excluded(self, device_id: str) -> bool:
        return device_id in self._device_exclusion_provider()

    def add_recording_listener(
        self,
        *,
        started: Callable[[datetime, Path, str, str], Awaitable[None]],
        stopped: Callable[[Path], Awaitable[None]],
    ) -> None:
        self._recording_started_listeners.append(started)
        self._recording_stopped_listeners.append(stopped)

    async def publish_timeline_event(self, event: CaptureTimelineEvent) -> bool:
        if not self.status().active:
            return False
        if self._timeline_events.full():
            try:
                self._timeline_events.get_nowait()
                self._timeline_events.task_done()
            except asyncio.QueueEmpty:
                pass
        try:
            self._timeline_events.put_nowait(event)
        except asyncio.QueueFull:
            return False
        return True

    def scope_options(self, device_id: str) -> ScopeCaptureOptions:
        return self._scope_measurement_service.capture_options(device_id)

    def update_scope_options(
        self,
        device_id: str,
        *,
        screen: bool,
        data: bool,
        channels: tuple[str, ...] | None = None,
        wait_for_trigger: bool | None = None,
    ) -> ScopeCaptureOptions:
        device = self._registry.device(device_id)
        if not is_micsig_scope_kind(device.kind):
            raise ValueError(f"Device is not a Micsig oscilloscope: {device_id}")
        return self._scope_measurement_service.update_capture_options(
            device_id,
            screen=screen,
            data=data,
            channels=channels,
            wait_for_trigger=wait_for_trigger,
        )

    def instrument_context(self, device_id: str) -> str:
        self._registry.device(device_id)
        if device_id not in self._instrument_contexts and self._preferences is not None:
            stored = self._preferences.get(device_id).get("context")
            if isinstance(stored, str):
                self._instrument_contexts[device_id] = stored
        return self._instrument_contexts.get(device_id, "")

    def update_instrument_context(self, device_id: str, value: str) -> str:
        self._registry.device(device_id)
        normalized = value.strip()
        if len(normalized) > 10000:
            raise ValueError("Instrument context must be 10000 characters or fewer")
        self._instrument_contexts[device_id] = normalized
        if self._preferences is not None:
            self._preferences.update(device_id, context=normalized)
        return normalized

    def status(self) -> CaptureStatus:
        task = self._recording_task
        active = task is not None and not task.done()
        elapsed_s = (
            max(0.0, (datetime.now(UTC) - self._started_at).total_seconds())
            if active and self._started_at is not None
            else 0.0
        )
        remaining_s = (
            max(0.0, self._duration_s - elapsed_s)
            if active and self._duration_s is not None
            else None
        )
        return CaptureStatus(
            active=active,
            started_at=self._started_at,
            current_file=self._current_file,
            last_recording_file=self._last_recording_file,
            last_snapshot_file=self._last_snapshot_file,
            samples_written=self._samples_written,
            current_title=self._current_title,
            current_comment=self._current_comment,
            draft_title=self._draft_title,
            draft_comment=self._draft_comment,
            duration_s=self._duration_s,
            elapsed_s=elapsed_s,
            remaining_s=remaining_s,
            scope_capture_mode=self._scope_capture_mode,
        )

    def resolve_artifact(self, filename: str) -> Path:
        if not filename or Path(filename).name != filename:
            raise ValueError("Invalid capture filename")
        path = (self.output_directory / filename).resolve()
        if path.parent != self.output_directory or not path.is_file():
            raise FileNotFoundError(filename)
        return path

    def _artifact_reference(self, path: Path) -> str:
        return path.resolve().relative_to(self.output_directory).as_posix()

    async def snapshot(
        self,
        *,
        title: str = "",
        comment: str = "",
    ) -> tuple[Path, tuple[Measurement, ...]]:
        title, comment = self._validate_metadata(title, comment)
        self._draft_title = title
        self._draft_comment = comment
        created_at = datetime.now(UTC)
        measurements = await self._scheduler.sample_all()
        path = self._new_path("snap", title)
        scope_results = await self._capture_scopes(snapshot_path=path)
        await asyncio.to_thread(
            self._write_snapshot,
            path,
            measurements,
            scope_results,
            title,
            comment,
            created_at,
        )
        self._last_snapshot_file = path
        return path, measurements

    async def start_recording(
        self,
        *,
        title: str = "",
        comment: str = "",
        duration_s: float | None = None,
        scope_capture_mode: str = "periodic",
    ) -> CaptureStatus:
        title, comment = self._validate_metadata(title, comment)
        if duration_s is not None and not 1 <= duration_s <= 86400:
            raise ValueError("Recording duration must be between 1 second and 24 hours")
        scope_capture_mode = scope_capture_mode.strip().casefold()
        if scope_capture_mode not in {"periodic", "manual"}:
            raise ValueError("Scope capture mode must be periodic or manual")
        recording_initial_settings = await self._read_recording_initial_settings()
        async with self._lock:
            if self._recording_task is not None and not self._recording_task.done():
                raise RuntimeError("CSV recording is already active")
            self._started_at = datetime.now(UTC)
            self._duration_s = duration_s
            self._current_title = title
            self._current_comment = comment
            self._scope_capture_mode = scope_capture_mode
            self._recording_initial_settings = recording_initial_settings
            self._manual_scope_ids = ()
            self._manual_scope_sequences = {}
            self._draft_title = title
            self._draft_comment = comment
            self._current_file = self._new_path("rec", title)
            self._samples_written = 0
            while not self._timeline_events.empty():
                try:
                    self._timeline_events.get_nowait()
                    self._timeline_events.task_done()
                except asyncio.QueueEmpty:
                    break
            self._ready.clear()
            self._recording_task = asyncio.create_task(
                self._record_loop(
                    self._current_file,
                    title,
                    comment,
                    self._started_at,
                ),
                name="openbench-csv-recorder",
            )
            self._auto_stop_task = (
                asyncio.create_task(
                    self._auto_stop_after(duration_s),
                    name="openbench-csv-auto-stop",
                )
                if duration_s is not None
                else None
            )
        await self._ready.wait()
        assert self._started_at is not None
        assert self._current_file is not None
        scope_devices = tuple(
            device
            for device in self._registry.devices()
            if is_micsig_scope_kind(device.kind) and not self._device_excluded(device.id)
        )
        suspended_scope_ids: list[str] = []
        try:
            for device in scope_devices:
                await self._scope_measurement_service.suspend_live_polling(device.id)
                suspended_scope_ids.append(device.id)
            if scope_capture_mode == "periodic":
                self._scope_recording_tasks = tuple(
                    asyncio.create_task(
                        self._scope_recording_loop(device, self._current_file),
                        name=f"openbench-scope-recorder-{device.id}",
                    )
                    for device in scope_devices
                )
            else:
                self._manual_scope_ids = tuple(device.id for device in scope_devices)
        except BaseException:
            for device_id in suspended_scope_ids:
                self._scope_measurement_service.resume_live_polling(device_id)
            self._manual_scope_ids = ()
            raise
        await self._notify_recording_started(
            self._started_at,
            self._current_file,
            title,
            comment,
        )
        return self.status()

    async def stop_recording(self) -> CaptureStatus:
        return await self._finish_recording(cancel_auto_stop=True)

    async def _finish_recording(self, *, cancel_auto_stop: bool) -> CaptureStatus:
        async with self._manual_scope_capture_lock:
            async with self._lock:
                task = self._recording_task
                if task is None or task.done():
                    raise RuntimeError("CSV recording is not active")
                auto_stop_task = self._auto_stop_task
                scope_tasks = self._scope_recording_tasks
                manual_scope_ids = self._manual_scope_ids
                self._scope_recording_tasks = ()
                self._manual_scope_ids = ()
                for scope_task in scope_tasks:
                    scope_task.cancel()
        if scope_tasks or manual_scope_ids:
            if scope_tasks:
                await asyncio.gather(*scope_tasks, return_exceptions=True)
            for device_id in manual_scope_ids:
                try:
                    await self._scope_measurement_service.start_acquisition(device_id)
                finally:
                    self._scope_measurement_service.resume_live_polling(device_id)
            try:
                await asyncio.wait_for(self._timeline_events.join(), timeout=1.0)
            except TimeoutError:
                pass
        async with self._lock:
            task.cancel()
            if (
                cancel_auto_stop
                and auto_stop_task is not None
                and auto_stop_task is not asyncio.current_task()
            ):
                auto_stop_task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if (
            cancel_auto_stop
            and auto_stop_task is not None
            and auto_stop_task is not asyncio.current_task()
        ):
            await asyncio.gather(auto_stop_task, return_exceptions=True)
        async with self._lock:
            completed_file = self._current_file
            assert completed_file is not None
            self._last_recording_file = self._current_file
            self._recording_task = None
            self._auto_stop_task = None
            self._started_at = None
            self._duration_s = None
            self._current_file = None
            self._current_title = ""
            self._current_comment = ""
            self._scope_capture_mode = "periodic"
            self._recording_initial_settings = {}
            self._manual_scope_sequences = {}
        await self._notify_recording_stopped(completed_file)
        return self.status()

    async def capture_recording_scope_frame(
        self,
        device_id: str,
        *,
        label: str = "",
    ) -> tuple[str, ScopeCaptureResult]:
        """Capture one explicitly requested scope frame into an active recording."""
        normalized_label = label.strip()
        if len(normalized_label) > 120:
            raise ValueError("Scope frame label must be 120 characters or fewer")
        async with self._manual_scope_capture_lock:
            async with self._lock:
                task = self._recording_task
                if task is None or task.done():
                    raise RuntimeError("CSV recording is not active")
                if self._scope_capture_mode != "manual":
                    raise RuntimeError("Scope frames are not in manual capture mode")
                if device_id not in self._manual_scope_ids:
                    raise KeyError(f"Unknown recording oscilloscope: {device_id}")
                recording_path = self._current_file
                assert recording_path is not None
                sequence = self._manual_scope_sequences.get(device_id, 0) + 1
                self._manual_scope_sequences[device_id] = sequence

            device = self._registry.device(device_id)
            instrument = cast(ScopeCaptureInstrument, self._registry.instrument(device_id))
            options = self.scope_options(device_id)
            capture_kind = "trigger" if options.wait_for_trigger else "frame"
            artifact_directory = recording_path.with_suffix("") / (
                f"{self._filename_label(self._display_name(device)) or 'mho1'}_"
                f"{capture_kind}_{sequence:06d}"
            )
            if options.wait_for_trigger:
                triggered = await self._scope_measurement_service.capture_triggered_frame(
                    device_id,
                    resume_after=False,
                )
            else:
                triggered = None
            result = await self._capture_scope(
                instrument=instrument,
                device=device,
                options=options,
                artifact_directory=artifact_directory,
                triggered=triggered,
            )
            event = "trigger" if triggered is not None else "frame"
            event_time = (
                triggered.triggered_at_utc if triggered is not None else result.timestamp_utc
            )
            await self.publish_timeline_event(
                CaptureTimelineEvent(
                    timestamp_utc=event_time,
                    device_id=device_id,
                    event=event,
                    capture_id=str(sequence),
                    state=result.status,
                    trigger="SINGLE" if triggered is not None else "manual",
                    artifact_file=result.data_file,
                    message=normalized_label
                    or ("Trigger status reached STOP" if triggered else ""),
                    category="scope",
                )
            )
            return str(sequence), result

    async def _notify_recording_started(
        self,
        started_at: datetime,
        path: Path,
        title: str,
        comment: str,
    ) -> None:
        if not self._recording_started_listeners:
            return
        await asyncio.gather(
            *(
                listener(started_at, path, title, comment)
                for listener in tuple(self._recording_started_listeners)
            ),
            return_exceptions=True,
        )

    async def _notify_recording_stopped(self, path: Path) -> None:
        if not self._recording_stopped_listeners:
            return
        await asyncio.gather(
            *(listener(path) for listener in tuple(self._recording_stopped_listeners)),
            return_exceptions=True,
        )

    async def _auto_stop_after(self, duration_s: float) -> None:
        try:
            await asyncio.sleep(duration_s)
            await self._finish_recording(cancel_auto_stop=False)
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        task = self._recording_task
        if task is None or task.done():
            return
        await self.stop_recording()

    async def open_output_directory(self) -> Path:
        await asyncio.to_thread(self._open_output_directory_sync)
        return self.output_directory

    def _open_output_directory_sync(self) -> None:
        self.output_directory.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            command = ["explorer.exe", str(self.output_directory)]
        elif sys.platform == "darwin":
            command = ["open", str(self.output_directory)]
        else:
            command = ["xdg-open", str(self.output_directory)]
        subprocess.Popen(command)

    @staticmethod
    def _validate_metadata(title: str, comment: str) -> tuple[str, str]:
        normalized_title = title.strip()
        normalized_comment = comment.strip()
        if len(normalized_title) > 120:
            raise ValueError("Capture title must not exceed 120 characters")
        if len(normalized_comment) > 10000:
            raise ValueError("Capture comment must not exceed 10000 characters")
        return normalized_title, normalized_comment

    @staticmethod
    def _filename_label(title: str) -> str:
        normalized = "".join(
            character.casefold() if character.isalnum() else "_" for character in title
        )
        collapsed = "_".join(part for part in normalized.split("_") if part)
        return collapsed[:10].rstrip("_")

    def _new_path(self, capture_kind: str, title: str) -> Path:
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M")
        label = self._filename_label(title)
        base = f"{timestamp}_{capture_kind}"
        if label:
            base = f"{base}_{label}"
        candidate = self.output_directory / f"{base}.csv"
        index = 2
        while candidate.exists() or candidate.with_suffix("").exists():
            candidate = self.output_directory / f"{base}_{index:02d}.csv"
            index += 1
        return candidate

    @staticmethod
    def _display_name(device: Device) -> str:
        return DEVICE_DISPLAY_NAMES.get(device.kind, device.name)

    def _instrument_header(self, device_id: str, *, initial_settings: str = "") -> str:
        device = self._registry.device(device_id)
        if is_micsig_scope_kind(device.kind):
            detail = f"poll_interval_s: {self._scope_measurement_service.interval_for(device_id):g}"
        else:
            intervals = sorted(
                {
                    self._scheduler.interval_for(channel.id)
                    for channel in self._registry.channels()
                    if channel.device_id == device_id
                }
            )
            values = "/".join(f"{interval:g}" for interval in intervals)
            detail = f"poll_interval_s: {values or 'n/a'}"
        header = f"{self._display_name(device)} | ID: {device.id} | {detail}"
        instrument_context = self._instrument_contexts.get(device_id, "")
        if instrument_context:
            header = f"{header} | context: {instrument_context}"
        if initial_settings:
            header = f"{header} | initial_settings: {initial_settings}"
        return header

    @staticmethod
    def _format_setting(value: float) -> str:
        return format(value, ".9g")

    async def _read_recording_initial_settings(self) -> dict[str, str]:
        settings: dict[str, str] = {}
        for device in self._registry.devices():
            if device.kind != "itech_it6000c" or self._device_excluded(device.id):
                continue
            instrument = self._registry.instrument(device.id)
            if not isinstance(instrument, ITechIT6000C):
                continue
            try:
                if self._scheduler.device_suspended(device.id):
                    state = instrument.cached_state
                    if state is None:
                        settings[device.id] = "unavailable"
                        continue
                else:
                    state = await instrument.read_state(force=True, full=True)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                settings[device.id] = "unavailable"
                continue
            settings[device.id] = "; ".join(
                (
                    "current_limit_positive="
                    f"{self._format_setting(state.current_limit_positive_a)} A",
                    "current_limit_negative="
                    f"{self._format_setting(state.current_limit_negative_a)} A",
                    "voltage_limit_positive="
                    f"{self._format_setting(state.voltage_limit_positive_v)} V",
                    "voltage_limit_negative="
                    f"{self._format_setting(state.voltage_limit_negative_v)} V",
                    f"power_limit_positive={self._format_setting(state.power_limit_positive_w)} W",
                    f"power_limit_negative={self._format_setting(state.power_limit_negative_w)} W",
                    f"output={'ON' if state.output_enabled else 'OFF'}",
                    f"priority={state.priority}",
                    f"direction={state.direction}",
                )
            )
        return settings

    def _recording_channel_is_streamed(self, channel_id: str, device_id: str) -> bool:
        device = self._registry.device(device_id)
        selected_suffixes = RECORDING_STREAM_CHANNEL_SUFFIXES_BY_KIND.get(device.kind)
        if selected_suffixes is None:
            return True
        suffix = channel_id.rsplit(".", 1)[-1]
        return suffix in selected_suffixes

    @staticmethod
    def _metadata_row(
        *,
        capture_kind: str,
        title: str,
        comment: str,
        created_at: datetime,
    ) -> list[object]:
        return [
            "capture_type",
            capture_kind,
            "title",
            title,
            "comment",
            comment,
            "created_at_local",
            created_at.astimezone().isoformat(),
        ]

    def _group_header_row(
        self,
        columns: tuple[WideColumn, ...],
        *,
        initial_settings: dict[str, str] | None = None,
    ) -> list[str]:
        row = ["timestamp_utc"]
        previous_device_id = ""
        settings = initial_settings or {}
        for column in columns:
            if column.device_id != previous_device_id:
                row.append(
                    self._instrument_header(
                        column.device_id,
                        initial_settings=settings.get(column.device_id, ""),
                    )
                )
                previous_device_id = column.device_id
            else:
                row.append("")
        return row

    @staticmethod
    def _field_header_row(columns: tuple[WideColumn, ...]) -> list[str]:
        return ["timestamp_utc", *(column.header for column in columns)]

    @staticmethod
    def _write_table_header(
        writer: CsvWriter,
        *,
        capture_kind: str,
        title: str,
        comment: str,
        created_at: datetime,
        group_header: list[str],
        field_header: list[str],
    ) -> None:
        writer.writerow(
            CaptureService._metadata_row(
                capture_kind=capture_kind,
                title=title,
                comment=comment,
                created_at=created_at,
            )
        )
        writer.writerow(())
        writer.writerow(group_header)
        writer.writerow(field_header)

    def _meter_columns(
        self,
        measurements: tuple[Measurement, ...],
    ) -> tuple[WideColumn, ...]:
        columns: list[WideColumn] = []
        device_ids = tuple(dict.fromkeys(measurement.device_id for measurement in measurements))
        for device_id in device_ids:
            device_measurements = tuple(
                measurement for measurement in measurements if measurement.device_id == device_id
            )
            multiple = len(device_measurements) > 1
            for measurement in device_measurements:
                prefix = f"{measurement.channel_id}_" if multiple else ""
                for field in ("value", "unit", "status", "quality"):
                    columns.append(
                        WideColumn(
                            device_id=device_id,
                            key=f"meter:{measurement.channel_id}:{field}",
                            header=f"{prefix}{field}",
                        )
                    )
        return tuple(columns)

    @staticmethod
    def _scope_columns(
        results: tuple[ScopeCaptureResult, ...],
    ) -> tuple[WideColumn, ...]:
        columns: list[WideColumn] = []
        for result in results:
            columns.extend(
                (
                    WideColumn(result.device_id, "scope:screen_file", "screen_file"),
                    WideColumn(result.device_id, "scope:data_file", "data_file"),
                    WideColumn(result.device_id, "scope:capture_status", "capture_status"),
                )
            )
            seen: set[str] = set()
            for scalar in result.measurements:
                identity = _scope_scalar_identity(scalar)
                if identity in seen:
                    continue
                seen.add(identity)
                prefix = identity.replace(":", "_")
                for field in ("value", "unit", "status", "quality"):
                    columns.append(
                        WideColumn(
                            result.device_id,
                            f"scope:{identity}:{field}",
                            f"{prefix}_{field}",
                        )
                    )
        return tuple(columns)

    @staticmethod
    def _snapshot_values(
        measurements: tuple[Measurement, ...],
        scope_results: tuple[ScopeCaptureResult, ...],
    ) -> dict[tuple[str, str], object]:
        values: dict[tuple[str, str], object] = {}
        for measurement in measurements:
            key_prefix = f"meter:{measurement.channel_id}"
            values[(measurement.device_id, f"{key_prefix}:value")] = (
                "" if measurement.value is None else measurement.value
            )
            values[(measurement.device_id, f"{key_prefix}:unit")] = measurement.unit
            values[(measurement.device_id, f"{key_prefix}:status")] = measurement.status
            values[(measurement.device_id, f"{key_prefix}:quality")] = measurement.quality
        for result in scope_results:
            values[(result.device_id, "scope:screen_file")] = result.screen_file
            values[(result.device_id, "scope:data_file")] = result.data_file
            values[(result.device_id, "scope:capture_status")] = result.status
            for scalar in result.measurements:
                prefix = f"scope:{_scope_scalar_identity(scalar)}"
                values[(result.device_id, f"{prefix}:value")] = (
                    "" if scalar.value is None else scalar.value
                )
                values[(result.device_id, f"{prefix}:unit")] = scalar.unit
                values[(result.device_id, f"{prefix}:status")] = scalar.status
                values[(result.device_id, f"{prefix}:quality")] = "instrument_scalar"
        return values

    def _write_snapshot(
        self,
        path: Path,
        measurements: tuple[Measurement, ...],
        scope_results: tuple[ScopeCaptureResult, ...],
        title: str,
        comment: str,
        created_at: datetime,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = (*self._meter_columns(measurements), *self._scope_columns(scope_results))
        values = self._snapshot_values(measurements, scope_results)
        data_row: list[object] = [
            created_at.isoformat(),
            *(values.get((column.device_id, column.key), "") for column in columns),
        ]
        with path.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            self._write_table_header(
                writer,
                capture_kind="snapshot",
                title=title,
                comment=comment,
                created_at=created_at,
                group_header=self._group_header_row(columns),
                field_header=self._field_header_row(columns),
            )
            writer.writerow(data_row)

    def _recording_columns(self) -> tuple[WideColumn, ...]:
        columns: list[WideColumn] = []
        channels = tuple(
            channel
            for channel in self._registry.channels()
            if not self._device_excluded(channel.device_id)
            and self._recording_channel_is_streamed(channel.id, channel.device_id)
        )
        device_ids = tuple(dict.fromkeys(channel.device_id for channel in channels))
        for device_id in device_ids:
            device_channels = tuple(
                channel for channel in channels if channel.device_id == device_id
            )
            multiple = len(device_channels) > 1
            for channel in device_channels:
                prefix = f"{channel.id}_" if multiple else ""
                for field in ("value", "unit", "status", "quality"):
                    columns.append(
                        WideColumn(
                            device_id,
                            f"event:{channel.id}:{field}",
                            f"{prefix}{field}",
                        )
                    )
        for device in self._registry.devices():
            if not is_micsig_scope_kind(device.kind) or self._device_excluded(device.id):
                continue
            for selection in self._scope_measurement_service.selections(device.id):
                event_channel_id = scope_event_channel_id(
                    device.id,
                    selection.channel,
                    selection.item,
                    secondary_channel=selection.secondary_channel,
                    source_edge=selection.source_edge,
                    target_edge=selection.target_edge,
                )
                prefix_parts = [selection.channel]
                if selection.secondary_channel is not None:
                    prefix_parts.append(selection.secondary_channel)
                prefix_parts.append(selection.item)
                if selection.item == "delay":
                    prefix_parts.extend(
                        (
                            selection.source_edge or "FRISe",
                            selection.target_edge or "FRISe",
                        )
                    )
                prefix = "_".join(prefix_parts)
                for field in ("value", "unit", "status", "quality"):
                    columns.append(
                        WideColumn(
                            device.id,
                            f"event:{event_channel_id}:{field}",
                            f"{prefix}_{field}",
                        )
                    )
            columns.extend(
                WideColumn(device.id, f"scope:{field}", header)
                for field, header in (
                    ("event", "trigger_event"),
                    ("capture_id", "trigger_sequence"),
                    ("state", "trigger_state"),
                    ("trigger", "trigger_mode"),
                    ("artifact_file", "trigger_artifact_file"),
                    ("message", "trigger_message"),
                )
            )
        for device in self._registry.devices():
            if device.kind != "kingst_la2016":
                continue
            columns.extend(
                WideColumn(device.id, f"logic:{field}", header)
                for field, header in (
                    ("event", "event"),
                    ("capture_id", "capture_id"),
                    ("state", "state"),
                    ("trigger", "trigger"),
                    ("artifact_file", "artifact_file"),
                    ("sample_rate_hz", "sample_rate_hz"),
                    ("sample_count", "sample_count"),
                    ("message", "message"),
                )
            )
        return tuple(columns)

    def _open_recording(
        self,
        path: Path,
        title: str,
        comment: str,
        created_at: datetime,
        columns: tuple[WideColumn, ...],
    ) -> tuple[TextIO, CsvWriter]:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("w", newline="", encoding="utf-8-sig")
        writer = csv.writer(stream)
        self._write_table_header(
            writer,
            capture_kind="recording",
            title=title,
            comment=comment,
            created_at=created_at,
            group_header=self._group_header_row(
                columns,
                initial_settings=self._recording_initial_settings,
            ),
            field_header=self._field_header_row(columns),
        )
        stream.flush()
        return stream, writer

    @staticmethod
    def _recording_row(
        measurement: Measurement,
        columns: tuple[WideColumn, ...],
    ) -> list[object] | None:
        key_prefix = f"event:{measurement.channel_id}"
        if not any(
            column.device_id == measurement.device_id and column.key.startswith(f"{key_prefix}:")
            for column in columns
        ):
            return None
        values = {
            f"{key_prefix}:value": "" if measurement.value is None else measurement.value,
            f"{key_prefix}:unit": measurement.unit,
            f"{key_prefix}:status": measurement.status,
            f"{key_prefix}:quality": measurement.quality,
        }
        return [
            measurement.timestamp_utc.isoformat(),
            *(
                values.get(column.key, "") if column.device_id == measurement.device_id else ""
                for column in columns
            ),
        ]

    @staticmethod
    def _timeline_row(
        event: CaptureTimelineEvent,
        columns: tuple[WideColumn, ...],
    ) -> list[object] | None:
        prefix = event.category
        if not any(
            column.device_id == event.device_id and column.key.startswith(f"{prefix}:")
            for column in columns
        ):
            return None
        values: dict[str, object] = {
            f"{prefix}:event": event.event,
            f"{prefix}:capture_id": event.capture_id,
            f"{prefix}:state": event.state,
            f"{prefix}:trigger": event.trigger,
            f"{prefix}:artifact_file": event.artifact_file,
            f"{prefix}:sample_rate_hz": (
                "" if event.sample_rate_hz is None else event.sample_rate_hz
            ),
            f"{prefix}:sample_count": "" if event.sample_count is None else event.sample_count,
            f"{prefix}:message": event.message,
        }
        return [
            event.timestamp_utc.isoformat(),
            *(
                values.get(column.key, "") if column.device_id == event.device_id else ""
                for column in columns
            ),
        ]

    @staticmethod
    def _write_recording_row(
        stream: TextIO,
        writer: CsvWriter,
        row: list[object],
    ) -> None:
        writer.writerow(row)
        stream.flush()

    async def _record_loop(
        self,
        path: Path,
        title: str,
        comment: str,
        created_at: datetime,
    ) -> None:
        columns = self._recording_columns()
        stream, writer = await asyncio.to_thread(
            self._open_recording,
            path,
            title,
            comment,
            created_at,
            columns,
        )
        measurement_task: asyncio.Task[Measurement] | None = None
        timeline_task: asyncio.Task[CaptureTimelineEvent] | None = None
        try:
            async with self._event_bus.subscribe() as queue:
                self._ready.set()
                measurement_task = asyncio.create_task(queue.get())
                timeline_task = asyncio.create_task(self._timeline_events.get())
                while True:
                    completed, _ = await asyncio.wait(
                        (measurement_task, timeline_task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in completed:
                        is_timeline_event = task is timeline_task
                        if not is_timeline_event:
                            measurement = measurement_task.result()
                            row = self._recording_row(measurement, columns)
                            measurement_task = asyncio.create_task(queue.get())
                        else:
                            event = timeline_task.result()
                            row = self._timeline_row(event, columns)
                            timeline_task = asyncio.create_task(self._timeline_events.get())
                        if row is None:
                            if is_timeline_event:
                                self._timeline_events.task_done()
                            continue
                        await asyncio.to_thread(
                            self._write_recording_row,
                            stream,
                            writer,
                            row,
                        )
                        self._samples_written += 1
                        if is_timeline_event:
                            self._timeline_events.task_done()
        finally:
            self._ready.set()
            pending_tasks = tuple(
                item for item in (measurement_task, timeline_task) if item is not None
            )
            for pending_task in pending_tasks:
                pending_task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            await asyncio.to_thread(stream.close)

    async def _scope_recording_loop(self, device: Device, recording_path: Path) -> None:
        instrument = cast(ScopeCaptureInstrument, self._registry.instrument(device.id))
        sequence = 0
        try:
            loop = asyncio.get_running_loop()
            while True:
                started = loop.time()
                options = self.scope_options(device.id)
                sequence += 1
                capture_kind = "trigger" if options.wait_for_trigger else "frame"
                artifact_directory = recording_path.with_suffix("") / (
                    f"{self._filename_label(self._display_name(device)) or 'mho1'}_"
                    f"{capture_kind}_{sequence:06d}"
                )
                if options.wait_for_trigger:
                    triggered = await self._scope_measurement_service.capture_triggered_frame(
                        device.id,
                        resume_after=False,
                    )
                else:
                    triggered = None
                save_task = asyncio.create_task(
                    self._capture_scope(
                        instrument=instrument,
                        device=device,
                        options=options,
                        artifact_directory=artifact_directory,
                        triggered=triggered,
                    )
                )
                stop_after_save = False
                try:
                    result = await asyncio.shield(save_task)
                except asyncio.CancelledError:
                    # Once a complete frozen frame has reached memory, STOP
                    # must not leave a half-written artifact directory. Finish
                    # that one frame and its timeline row, then terminate.
                    stop_after_save = True
                    result = await save_task
                if options.wait_for_trigger:
                    assert triggered is not None
                    await self.publish_timeline_event(
                        CaptureTimelineEvent(
                            timestamp_utc=triggered.triggered_at_utc,
                            device_id=device.id,
                            event="trigger",
                            capture_id=str(sequence),
                            state="captured",
                            trigger="SINGLE",
                            artifact_file=result.data_file,
                            message="Trigger status reached STOP",
                            category="scope",
                        )
                    )
                    if stop_after_save:
                        raise asyncio.CancelledError
                    # The next iteration immediately re-arms SINGLE after all
                    # selected data from the frozen frame has been saved.
                    continue
                await self.publish_timeline_event(
                    CaptureTimelineEvent(
                        timestamp_utc=result.timestamp_utc,
                        device_id=device.id,
                        event="frame",
                        capture_id=str(sequence),
                        state=result.status,
                        trigger="periodic",
                        artifact_file=result.data_file,
                        message=result.error,
                        category="scope",
                    )
                )
                if stop_after_save:
                    raise asyncio.CancelledError
                remaining_s = max(
                    0.0,
                    self._scope_measurement_service.interval_for(device.id)
                    - (loop.time() - started),
                )
                await asyncio.sleep(remaining_s)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await self._scope_measurement_service.start_acquisition(device.id)
            finally:
                self._scope_measurement_service.resume_live_polling(device.id)

    async def _capture_scopes(
        self,
        *,
        snapshot_path: Path,
    ) -> tuple[ScopeCaptureResult, ...]:
        scopes = tuple(
            device
            for device in self._registry.devices()
            if is_micsig_scope_kind(device.kind) and not self._device_excluded(device.id)
        )
        artifact_directory = snapshot_path.with_suffix("")
        results: list[ScopeCaptureResult] = []
        for device in scopes:
            options = self.scope_options(device.id)
            instrument = cast(
                ScopeCaptureInstrument,
                self._registry.instrument(device.id),
            )
            try:
                if options.wait_for_trigger:
                    await self._scope_measurement_service.suspend_live_polling(device.id)
                    try:
                        triggered = await self._scope_measurement_service.capture_triggered_frame(
                            device.id,
                            resume_after=True,
                        )
                    finally:
                        self._scope_measurement_service.resume_live_polling(device.id)
                else:
                    triggered = None
                results.append(
                    await self._capture_scope(
                        instrument=instrument,
                        device=device,
                        options=options,
                        artifact_directory=artifact_directory,
                        triggered=triggered,
                    )
                )
            except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                results.append(
                    ScopeCaptureResult(
                        device_id=device.id,
                        timestamp_utc=datetime.now(UTC),
                        measurements=(),
                        screen_file="",
                        data_file="",
                        status="unavailable",
                        error=str(exc),
                    )
                )
        return tuple(results)

    async def _capture_scope(
        self,
        *,
        instrument: ScopeCaptureInstrument,
        device: Device,
        options: ScopeCaptureOptions,
        artifact_directory: Path,
        triggered: TriggeredScopeFrame | None = None,
    ) -> ScopeCaptureResult:
        screenshot: MicsigScreenshot | None = None
        scalar_measurements: tuple[MicsigScalarMeasurement, ...]
        del instrument
        if triggered is None:
            live_measurements = await self._scope_measurement_service.sample_now(device.id)
            frame = self._scope_measurement_service.latest_frame(device.id)
            if frame is None:
                raise RuntimeError("Micsig frame capture did not return a frame")
            captured_at = datetime.now(UTC)
        else:
            live_measurements = triggered.readings
            frame = triggered.frame
            captured_at = triggered.triggered_at_utc
        scalar_measurements = tuple(reading.scalar for reading in live_measurements)
        if options.screen:
            screenshot = frame.screenshot

        screen_file = ""
        await asyncio.to_thread(artifact_directory.mkdir, parents=True, exist_ok=True)
        file_stem = self._filename_label(self._display_name(device)) or "mho1"
        if screenshot is not None:
            extension = (
                "jpg"
                if screenshot.image_format.casefold() == "jpeg"
                else screenshot.image_format.casefold()
            )
            screen_path = artifact_directory / f"{file_stem}_screen.{extension}"
            await asyncio.to_thread(screen_path.write_bytes, screenshot.data)
            screen_file = self._artifact_reference(screen_path)

        measurements_path = artifact_directory / f"{file_stem}_measurements.csv"
        measurements_csv = frame.measurements_csv or self._scope_measurements_to_csv(
            scalar_measurements
        )
        await asyncio.to_thread(measurements_path.write_bytes, measurements_csv)

        waveform_files: dict[str, str] = {}
        waveform_csv_name: str | None = None
        if options.data:
            for waveform in frame.waveforms:
                if not waveform.ascii_data:
                    continue
                waveform_path = artifact_directory / (
                    f"{file_stem}_{waveform.source.casefold()}_ascii.txt"
                )
                await asyncio.to_thread(waveform_path.write_bytes, waveform.ascii_data)
                waveform_files[waveform.source] = waveform_path.name
            if frame.waveform_csv:
                waveform_csv_path = artifact_directory / f"{file_stem}_waveforms.csv"
                await asyncio.to_thread(
                    waveform_csv_path.write_bytes,
                    frame.waveform_csv,
                )
                waveform_csv_name = waveform_csv_path.name

        frame_errors = tuple(
            error for error in (frame.screenshot_error, frame.waveform_error) if error
        )
        metadata_path = artifact_directory / f"{file_stem}_capture.json"
        metadata = self._scope_capture_metadata(
            device=device,
            options=options,
            frame=frame,
            captured_at=captured_at,
            screen_name=Path(screen_file).name if screen_file else None,
            measurements_name=measurements_path.name,
            waveform_files=waveform_files,
            waveform_csv_name=waveform_csv_name,
            status="ok" if not frame_errors else "partial",
        )
        metadata_json = (
            json.dumps(
                metadata,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        await asyncio.to_thread(metadata_path.write_bytes, metadata_json)
        data_file = self._artifact_reference(metadata_path)
        return ScopeCaptureResult(
            device_id=device.id,
            timestamp_utc=captured_at,
            measurements=scalar_measurements,
            screen_file=screen_file,
            data_file=data_file,
            status="ok" if not frame_errors else "partial",
            error="; ".join(frame_errors),
        )

    @staticmethod
    def _scope_measurements_to_csv(
        measurements: tuple[MicsigScalarMeasurement, ...],
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
            writer.writerow(
                (
                    measurement.channel,
                    measurement.item,
                    "" if measurement.value is None else format(measurement.value, ".17g"),
                    measurement.unit,
                    measurement.status,
                    measurement.secondary_channel or "",
                    measurement.source_edge or "",
                    measurement.target_edge or "",
                )
            )
        return output.getvalue().encode("utf-8")

    @staticmethod
    def _scope_capture_metadata(
        *,
        device: Device,
        options: ScopeCaptureOptions,
        frame: MicsigSnapshot,
        captured_at: datetime,
        screen_name: str | None,
        measurements_name: str,
        waveform_files: dict[str, str],
        waveform_csv_name: str | None,
        status: str,
    ) -> dict[str, object]:
        waveforms: list[dict[str, object]] = []
        for waveform in frame.waveforms:
            first_time_s = waveform.time_at(0) if waveform.points else None
            last_time_s = waveform.time_at(waveform.points - 1) if waveform.points else None
            waveforms.append(
                {
                    "source": waveform.source,
                    "mode": waveform.mode,
                    "points": waveform.points,
                    "ascii_file": waveform_files.get(waveform.source),
                    "first_sample_time_s": first_time_s,
                    "last_sample_time_s": last_time_s,
                    "captured_span_s": (
                        last_time_s - first_time_s
                        if first_time_s is not None and last_time_s is not None
                        else None
                    ),
                }
            )
        preamble = (
            frame.waveforms[0].reported_preamble or frame.waveforms[0].preamble
            if frame.waveforms
            else None
        )
        preamble_text = frame.waveforms[0].preamble_text if frame.waveforms else ""
        reference_waveform = frame.waveforms[0] if frame.waveforms else None
        first_sample_time_s = (
            reference_waveform.time_at(0)
            if reference_waveform is not None and reference_waveform.points
            else None
        )
        last_sample_time_s = (
            reference_waveform.time_at(reference_waveform.points - 1)
            if reference_waveform is not None and reference_waveform.points
            else None
        )
        return {
            "schema_version": 1,
            "captured_at_utc": captured_at.isoformat(),
            "device": {
                "id": device.id,
                "name": device.name,
                "kind": device.kind,
            },
            "capture": {
                "status": status,
                "elapsed_s": frame.elapsed_s,
                "screenshot_enabled": options.screen,
                "ascii_enabled": options.data,
                "selected_channels": list(options.channels),
                "wait_for_trigger": options.wait_for_trigger,
                "screenshot_error": frame.screenshot_error,
                "waveform_error": frame.waveform_error,
            },
            "files": {
                "measurements": measurements_name,
                "screenshot": screen_name,
                "waveforms_csv": waveform_csv_name,
            },
            "waveform": {
                "format": "ASCII" if frame.waveforms else None,
                "mode": "NORMAL",
                "common_preamble_raw": preamble_text or None,
                "common_preamble": (
                    {
                        "format_code": preamble.format_code,
                        "mode_code": preamble.mode_code,
                        "averaging_count": preamble.averaging_count,
                        "x_increment_s": preamble.x_increment_s,
                        "x_origin_s": preamble.x_origin_s,
                        "x_reference": preamble.x_reference,
                        "y_increment": preamble.y_increment,
                        "y_origin": preamble.y_origin,
                        "y_reference": preamble.y_reference,
                    }
                    if preamble is not None
                    else None
                ),
                "horizontal_timing": (
                    {
                        "source": "waveform_preamble",
                        "sample_interval_s": preamble.x_increment_s,
                        "time_origin_s": preamble.x_origin_s,
                        "reference_sample": preamble.x_reference,
                        "first_sample_time_s": first_sample_time_s,
                        "last_sample_time_s": last_sample_time_s,
                        "captured_span_s": (
                            last_sample_time_s - first_sample_time_s
                            if first_sample_time_s is not None and last_sample_time_s is not None
                            else None
                        ),
                    }
                    if preamble is not None
                    else None
                ),
                "channels": waveforms,
            },
        }
