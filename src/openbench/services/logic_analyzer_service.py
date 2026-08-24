from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from openbench.core.registry import DeviceRegistry
from openbench.drivers.kingst_la2016 import (
    KINGST_SAMPLE_RATES_HZ,
    KINGST_THRESHOLDS_V,
    KingstCaptureConfig,
    KingstLA2016,
    KingstTrigger,
)
from openbench.services.capture_service import (
    CaptureService,
    CaptureTimelineEvent,
)
from openbench.storage import InstrumentPreferenceStore

ACTIVE_LOGIC_STATES = {
    "scheduled",
    "starting",
    "pretrigger",
    "armed",
    "capturing",
    "downloading",
    "stopping",
}


class LogicAnalyzerInstrument(Protocol):
    @property
    def device_id(self) -> str: ...

    async def capture(
        self,
        config: KingstCaptureConfig,
        output_file: Path,
        *,
        on_state: Callable[[str], Awaitable[None]],
    ) -> None: ...

    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LogicAnalyzerSettings:
    device_id: str
    channels: tuple[int, ...]
    sample_rate_hz: int
    sample_count: int
    threshold_v: float
    capture_ratio_percent: int
    triggers: tuple[KingstTrigger, ...]
    auto_start_enabled: bool
    auto_start_delay_s: float

    @property
    def duration_s(self) -> float:
        return self.sample_count / self.sample_rate_hz

    @property
    def trigger_label(self) -> str:
        if not self.triggers:
            return "OFF"
        return " + ".join(f"CH{trigger.channel} {trigger.condition}" for trigger in self.triggers)

    @property
    def trigger_channels(self) -> tuple[int, ...]:
        return tuple(trigger.channel for trigger in self.triggers)

    @property
    def common_trigger_condition(self) -> str:
        if not self.triggers:
            return "off"
        conditions = {trigger.condition for trigger in self.triggers}
        return conditions.pop() if len(conditions) == 1 else "mixed"

    def capture_config(self, *, hardware_trigger: bool) -> KingstCaptureConfig:
        return KingstCaptureConfig(
            channels=self.channels,
            sample_rate_hz=self.sample_rate_hz,
            sample_count=self.sample_count,
            threshold_v=self.threshold_v,
            capture_ratio_percent=self.capture_ratio_percent,
            triggers=self.triggers if hardware_trigger else (),
        )


@dataclass(frozen=True, slots=True)
class LogicCaptureStatus:
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
    artifact_directory: Path | None
    artifact_file: Path | None
    recording_file: Path | None
    source: str
    message: str
    error: str


class LogicAnalyzerService:
    _CAPTURE_COMPLETION_OVERHEAD_S = 1.25
    _ESTIMATED_DOWNLOAD_BYTES_PER_SECOND = 20 * 1024 * 1024

    def __init__(
        self,
        output_directory: Path,
        registry: DeviceRegistry,
        capture_service: CaptureService,
        preferences: InstrumentPreferenceStore | None = None,
    ) -> None:
        self.output_directory = output_directory.resolve()
        self._registry = registry
        self._capture_service = capture_service
        self._preferences = preferences
        self._settings: dict[str, LogicAnalyzerSettings] = {}
        self._statuses: dict[str, LogicCaptureStatus] = {}
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._scheduled_jobs: dict[str, asyncio.Task[None]] = {}
        self._countdown_deadlines: dict[str, float] = {}
        self._scheduled_recordings: dict[str, Path] = {}
        self._history: dict[tuple[str, str], LogicCaptureStatus] = {}
        self._lock = asyncio.Lock()

    def add_device(self, analyzer: LogicAnalyzerInstrument) -> None:
        device_id = analyzer.device_id
        self._settings.setdefault(
            device_id,
            self._restored_settings(device_id),
        )
        self._statuses.setdefault(device_id, self._idle_status(device_id))

    async def remove_device(self, device_id: str) -> None:
        scheduled = self._scheduled_jobs.pop(device_id, None)
        if scheduled is not None:
            scheduled.cancel()
            await asyncio.gather(scheduled, return_exceptions=True)
        if self.status(device_id).active:
            await self.stop_capture(device_id)
        self._countdown_deadlines.pop(device_id, None)
        self._scheduled_recordings.pop(device_id, None)
        self._settings.pop(device_id, None)
        self._statuses.pop(device_id, None)

    def settings(self, device_id: str) -> LogicAnalyzerSettings:
        self._require_analyzer(device_id)
        try:
            return self._settings[device_id]
        except KeyError as exc:
            raise KeyError(f"Unknown logic analyzer: {device_id}") from exc

    def update_settings(
        self,
        device_id: str,
        *,
        channels: tuple[int, ...] | None = None,
        sample_rate_hz: int | None = None,
        sample_count: int | None = None,
        threshold_v: float | None = None,
        capture_ratio_percent: int | None = None,
        triggers: tuple[KingstTrigger, ...] | None = None,
        auto_start_enabled: bool | None = None,
        auto_start_delay_s: float | None = None,
    ) -> LogicAnalyzerSettings:
        current = self.settings(device_id)
        if self.status(device_id).active:
            raise RuntimeError("Logic analyzer settings cannot change during a capture")
        updated = replace(
            current,
            channels=current.channels if channels is None else channels,
            sample_rate_hz=(current.sample_rate_hz if sample_rate_hz is None else sample_rate_hz),
            sample_count=current.sample_count if sample_count is None else sample_count,
            threshold_v=current.threshold_v if threshold_v is None else threshold_v,
            capture_ratio_percent=(
                current.capture_ratio_percent
                if capture_ratio_percent is None
                else capture_ratio_percent
            ),
            triggers=current.triggers if triggers is None else triggers,
            auto_start_enabled=(
                current.auto_start_enabled if auto_start_enabled is None else auto_start_enabled
            ),
            auto_start_delay_s=(
                current.auto_start_delay_s if auto_start_delay_s is None else auto_start_delay_s
            ),
        )
        if not 0 <= updated.auto_start_delay_s <= 86400:
            raise ValueError("Automatic start delay must be between 0 and 86400 seconds")
        # The driver config performs the hardware-specific validation in one place.
        updated.capture_config(hardware_trigger=bool(updated.triggers))
        self._settings[device_id] = updated
        if self._preferences is not None:
            values = asdict(updated)
            values.pop("device_id", None)
            self._preferences.update_section(device_id, "logic_analyzer", **values)
        return updated

    def _restored_settings(self, device_id: str) -> LogicAnalyzerSettings:
        default = LogicAnalyzerSettings(
            device_id=device_id,
            channels=tuple(range(16)),
            sample_rate_hz=1_000_000,
            sample_count=1_000_000,
            threshold_v=1.4,
            capture_ratio_percent=50,
            triggers=(),
            auto_start_enabled=False,
            auto_start_delay_s=0.0,
        )
        if self._preferences is None:
            return default
        raw = self._preferences.get(device_id).get("logic_analyzer")
        if not isinstance(raw, dict):
            return default
        try:
            raw_channels = raw.get("channels", default.channels)
            raw_triggers = raw.get("triggers", [])
            if not isinstance(raw_channels, (list, tuple)) or not isinstance(
                raw_triggers, (list, tuple)
            ):
                raise ValueError("invalid stored logic-analyzer settings")
            triggers = tuple(
                KingstTrigger(channel=int(item["channel"]), condition=str(item["condition"]))
                for item in raw_triggers
                if isinstance(item, dict)
            )
            raw_auto_start_enabled = raw.get("auto_start_enabled")
            auto_start_enabled = (
                raw_auto_start_enabled
                if isinstance(raw_auto_start_enabled, bool)
                else default.auto_start_enabled
            )
            restored = LogicAnalyzerSettings(
                device_id=device_id,
                channels=tuple(int(channel) for channel in raw_channels),
                sample_rate_hz=int(raw.get("sample_rate_hz", default.sample_rate_hz)),
                sample_count=int(raw.get("sample_count", default.sample_count)),
                threshold_v=float(raw.get("threshold_v", default.threshold_v)),
                capture_ratio_percent=int(
                    raw.get("capture_ratio_percent", default.capture_ratio_percent)
                ),
                triggers=triggers,
                auto_start_enabled=auto_start_enabled,
                auto_start_delay_s=float(raw.get("auto_start_delay_s", default.auto_start_delay_s)),
            )
            if not 0 <= restored.auto_start_delay_s <= 86400:
                raise ValueError("invalid stored automatic start delay")
            restored.capture_config(hardware_trigger=bool(restored.triggers))
        except (KeyError, TypeError, ValueError):
            return default
        return restored

    def status(self, device_id: str) -> LogicCaptureStatus:
        self._require_analyzer(device_id)
        try:
            status = self._statuses[device_id]
        except KeyError as exc:
            raise KeyError(f"Unknown logic analyzer: {device_id}") from exc
        deadline = self._countdown_deadlines.get(device_id)
        remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
        return replace(
            status,
            active=status.state in ACTIVE_LOGIC_STATES,
            remaining_s=remaining,
        )

    def list_statuses(self) -> tuple[LogicCaptureStatus, ...]:
        return tuple(
            self.status(device.id)
            for device in self._registry.devices()
            if device.kind == "kingst_la2016"
        )

    def capture_status(self, device_id: str, capture_id: str) -> LogicCaptureStatus:
        current = self.status(device_id)
        if current.capture_id == capture_id:
            return current
        try:
            return self._history[(device_id, capture_id)]
        except KeyError as exc:
            raise KeyError(f"Unknown logic capture: {capture_id}") from exc

    async def start_capture(
        self,
        device_id: str,
        *,
        hardware_trigger: bool,
        title: str = "",
        comment: str = "",
        source: str = "api",
        recording_file: Path | None = None,
    ) -> LogicCaptureStatus:
        title, comment = self._validate_metadata(title, comment)
        settings = self.settings(device_id)
        config = settings.capture_config(hardware_trigger=hardware_trigger)
        if hardware_trigger and not config.triggers:
            raise ValueError("Configure at least one hardware trigger before arming")
        async with self._lock:
            if self.status(device_id).active:
                raise RuntimeError("A logic capture is already active")
            capture_directory = self._new_capture_directory(title)
            capture_directory.mkdir(parents=True, exist_ok=False)
            capture_id = capture_directory.name
            output_file = capture_directory / "capture.sr"
            requested_at = datetime.now(UTC)
            initial_state = "starting"
            estimated_duration_s = self._initial_phase_duration(
                config,
                hardware_trigger=hardware_trigger,
            )
            status = LogicCaptureStatus(
                device_id=device_id,
                state=initial_state,
                active=True,
                capture_id=capture_id,
                title=title,
                comment=comment,
                requested_at=requested_at,
                started_at=None,
                triggered_at=None,
                completed_at=None,
                scheduled_start_at=None,
                estimated_duration_s=estimated_duration_s,
                remaining_s=None,
                trigger_timestamp_quality="unavailable",
                trigger=settings.trigger_label if hardware_trigger else "OFF",
                artifact_directory=capture_directory,
                artifact_file=None,
                recording_file=recording_file,
                source=source,
                message="Starting sigrok capture",
                error="",
            )
            self._statuses[device_id] = status
            self._set_countdown(device_id, estimated_duration_s)
            await self._write_metadata(status, config)
            task = asyncio.create_task(
                self._run_capture(
                    device_id,
                    config,
                    output_file,
                    hardware_trigger=hardware_trigger,
                ),
                name=f"openbench-logic-capture-{device_id}",
            )
            self._jobs[device_id] = task
        event_name = "logic_capture_armed" if hardware_trigger else "logic_capture_started"
        await self._publish_event(device_id, event_name)
        return self.status(device_id)

    async def stop_capture(self, device_id: str) -> LogicCaptureStatus:
        current = self.status(device_id)
        scheduled = self._scheduled_jobs.pop(device_id, None)
        if scheduled is not None and current.state == "scheduled":
            scheduled.cancel()
            await asyncio.gather(scheduled, return_exceptions=True)
            self._countdown_deadlines.pop(device_id, None)
            self._scheduled_recordings.pop(device_id, None)
            self._statuses[device_id] = replace(
                current,
                state="stopped",
                active=False,
                completed_at=datetime.now(UTC),
                scheduled_start_at=None,
                message="Scheduled capture cancelled",
            )
            await self._publish_event(device_id, "logic_capture_stopped")
            return self.status(device_id)
        if not current.active:
            raise RuntimeError("Logic capture is not active")
        self._statuses[device_id] = replace(
            current,
            state="stopping",
            message="Stopping capture",
        )
        instrument = self._instrument(device_id)
        await instrument.stop()
        task = self._jobs.get(device_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return self.status(device_id)

    async def recording_started(
        self,
        started_at: datetime,
        recording_file: Path,
        title: str,
        comment: str,
    ) -> None:
        del comment
        for device in self._registry.devices():
            if device.kind != "kingst_la2016":
                continue
            settings = self.settings(device.id)
            if not settings.auto_start_enabled or self.status(device.id).active:
                continue
            delay_s = settings.auto_start_delay_s
            scheduled_at = started_at + timedelta(seconds=delay_s)
            capture_id = f"scheduled-{recording_file.stem}"
            self._statuses[device.id] = LogicCaptureStatus(
                device_id=device.id,
                state="scheduled",
                active=True,
                capture_id=capture_id,
                title=title,
                comment="",
                requested_at=datetime.now(UTC),
                started_at=None,
                triggered_at=None,
                completed_at=None,
                scheduled_start_at=scheduled_at,
                estimated_duration_s=delay_s,
                remaining_s=delay_s,
                trigger_timestamp_quality="unavailable",
                trigger=settings.trigger_label,
                artifact_directory=None,
                artifact_file=None,
                recording_file=recording_file,
                source="global_recording",
                message=f"Automatic start in {delay_s:g} s",
                error="",
            )
            self._countdown_deadlines[device.id] = time.monotonic() + delay_s
            self._scheduled_recordings[device.id] = recording_file
            task = asyncio.create_task(
                self._start_after_delay(
                    device.id,
                    delay_s=delay_s,
                    title=title,
                    recording_file=recording_file,
                ),
                name=f"openbench-logic-scheduled-{device.id}",
            )
            self._scheduled_jobs[device.id] = task

    async def recording_stopped(self, recording_file: Path) -> None:
        for device_id, scheduled_file in tuple(self._scheduled_recordings.items()):
            if scheduled_file != recording_file:
                continue
            task = self._scheduled_jobs.pop(device_id, None)
            if task is None or task.done():
                continue
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._countdown_deadlines.pop(device_id, None)
            self._scheduled_recordings.pop(device_id, None)
            current = self._statuses[device_id]
            self._statuses[device_id] = replace(
                current,
                state="stopped",
                active=False,
                completed_at=datetime.now(UTC),
                scheduled_start_at=None,
                message="Automatic start cancelled when CSV recording stopped",
            )

    async def _start_after_delay(
        self,
        device_id: str,
        *,
        delay_s: float,
        title: str,
        recording_file: Path,
    ) -> None:
        try:
            await asyncio.sleep(delay_s)
            self._scheduled_jobs.pop(device_id, None)
            self._scheduled_recordings.pop(device_id, None)
            self._countdown_deadlines.pop(device_id, None)
            # Replace the temporary scheduled state before the normal active-state guard.
            self._statuses[device_id] = self._idle_status(device_id)
            settings = self.settings(device_id)
            await self.start_capture(
                device_id,
                hardware_trigger=bool(settings.triggers),
                title=title,
                comment="Started automatically with the global CSV recording",
                source="global_recording",
                recording_file=recording_file,
            )
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            current = self._statuses[device_id]
            self._statuses[device_id] = replace(
                current,
                state="error",
                active=False,
                completed_at=datetime.now(UTC),
                message="Automatic logic capture failed",
                error=str(exc),
            )
            await self._publish_event(device_id, "logic_capture_error")

    async def _run_capture(
        self,
        device_id: str,
        config: KingstCaptureConfig,
        output_file: Path,
        *,
        hardware_trigger: bool,
    ) -> None:
        instrument = self._instrument(device_id)
        started_at = datetime.now(UTC)
        current = self._statuses[device_id]
        initial_state = "pretrigger" if hardware_trigger else "capturing"
        self._statuses[device_id] = replace(
            current,
            state=initial_state,
            started_at=started_at,
            message=(
                "Collecting pre-trigger samples" if hardware_trigger else "Capturing logic samples"
            ),
        )
        if hardware_trigger:
            pretrigger_s = config.duration_s * config.capture_ratio_percent / 100
            pretrigger_estimate = pretrigger_s if pretrigger_s > 0 else None
            self._statuses[device_id] = replace(
                self._statuses[device_id],
                estimated_duration_s=pretrigger_estimate,
            )
            self._set_countdown(device_id, pretrigger_estimate)
        else:
            total_estimate = self._estimated_bounded_duration(
                config,
                acquisition_duration_s=config.duration_s,
            )
            self._statuses[device_id] = replace(
                self._statuses[device_id],
                estimated_duration_s=total_estimate,
            )
            self._set_countdown(device_id, total_estimate)

        async def state_changed(state: str) -> None:
            latest = self._statuses[device_id]
            if state == "pretrigger":
                self._statuses[device_id] = replace(
                    latest,
                    state="pretrigger",
                    message="Collecting pre-trigger samples",
                )
            elif state == "armed":
                self._countdown_deadlines.pop(device_id, None)
                self._statuses[device_id] = replace(
                    latest,
                    state="armed",
                    estimated_duration_s=None,
                    message="Waiting for hardware trigger",
                )
            elif state == "posttrigger":
                if hardware_trigger:
                    triggered_at = datetime.now(UTC)
                    posttrigger_estimate = self._estimated_bounded_duration(
                        config,
                        acquisition_duration_s=config.post_trigger_duration_s,
                    )
                    self._statuses[device_id] = replace(
                        latest,
                        state="capturing",
                        triggered_at=triggered_at,
                        estimated_duration_s=posttrigger_estimate,
                        trigger_timestamp_quality="driver_log",
                        message="Trigger received; collecting post-trigger samples",
                    )
                    self._set_countdown(device_id, posttrigger_estimate)
                    await self._publish_event(device_id, "logic_capture_triggered")
                else:
                    self._statuses[device_id] = replace(
                        latest,
                        state="capturing",
                        message="Capturing logic samples",
                    )
            elif state == "downloading":
                download_estimate = latest.estimated_duration_s
                if device_id not in self._countdown_deadlines:
                    download_estimate = self._estimated_download_duration(config)
                    self._set_countdown(device_id, download_estimate)
                self._statuses[device_id] = replace(
                    latest,
                    state="downloading",
                    estimated_duration_s=download_estimate,
                    message="Downloading capture memory",
                )

        try:
            await instrument.capture(config, output_file, on_state=state_changed)
            completed_at = datetime.now(UTC)
            latest = self._statuses[device_id]
            state = "stopped" if latest.state == "stopping" else "completed"
            artifact = output_file if await asyncio.to_thread(output_file.is_file) else None
            self._statuses[device_id] = replace(
                latest,
                state=state,
                active=False,
                completed_at=completed_at,
                remaining_s=0.0,
                artifact_file=artifact,
                message=(
                    "Capture stopped by operator" if state == "stopped" else "Capture complete"
                ),
            )
            await self._publish_event(
                device_id,
                "logic_capture_stopped" if state == "stopped" else "logic_capture_completed",
            )
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            latest = self._statuses[device_id]
            stopped = latest.state == "stopping"
            self._statuses[device_id] = replace(
                latest,
                state="stopped" if stopped else "error",
                active=False,
                completed_at=datetime.now(UTC),
                message="Capture stopped by operator" if stopped else "Capture failed",
                error="" if stopped else str(exc),
                artifact_file=(
                    output_file if await asyncio.to_thread(output_file.is_file) else None
                ),
            )
            await self._publish_event(
                device_id,
                "logic_capture_stopped" if stopped else "logic_capture_error",
            )
        finally:
            self._countdown_deadlines.pop(device_id, None)
            self._jobs.pop(device_id, None)
            final = self._statuses[device_id]
            self._history[(device_id, final.capture_id)] = final
            await self._write_metadata(final, config)

    async def _publish_event(self, device_id: str, event: str) -> None:
        status = self._statuses[device_id]
        settings = self._settings[device_id]
        artifact_file = ""
        if status.artifact_file is not None:
            try:
                artifact_file = status.artifact_file.relative_to(
                    self._capture_service.output_directory
                ).as_posix()
            except ValueError:
                artifact_file = status.artifact_file.name
        await self._capture_service.publish_timeline_event(
            CaptureTimelineEvent(
                timestamp_utc=datetime.now(UTC),
                device_id=device_id,
                event=event,
                capture_id=status.capture_id,
                state=status.state,
                trigger=status.trigger,
                artifact_file=artifact_file,
                sample_rate_hz=settings.sample_rate_hz,
                sample_count=settings.sample_count,
                message=status.error or status.message,
            )
        )

    def resolve_artifact(
        self,
        device_id: str,
        capture_id: str,
        filename: str,
    ) -> Path:
        status = self.capture_status(device_id, capture_id)
        directory = status.artifact_directory
        if directory is None or not filename or Path(filename).name != filename:
            raise FileNotFoundError(filename)
        path = (directory / filename).resolve()
        if path.parent != directory.resolve() or not path.is_file():
            raise FileNotFoundError(filename)
        return path

    async def open_capture_directory(self, device_id: str) -> Path:
        status = self.status(device_id)
        directory = status.artifact_directory or self.output_directory
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(self._open_directory_sync, directory)
        return directory

    @staticmethod
    def _open_directory_sync(directory: Path) -> None:
        if os.name == "nt":
            command = ["explorer.exe", str(directory)]
        elif sys.platform == "darwin":
            command = ["open", str(directory)]
        else:
            command = ["xdg-open", str(directory)]
        subprocess.Popen(command)

    async def close(self) -> None:
        for task in tuple(self._scheduled_jobs.values()):
            task.cancel()
        if self._scheduled_jobs:
            await asyncio.gather(*self._scheduled_jobs.values(), return_exceptions=True)
        self._scheduled_jobs.clear()
        for device_id in tuple(self._jobs):
            try:
                await self.stop_capture(device_id)
            except (KeyError, RuntimeError):
                pass

    def _require_analyzer(self, device_id: str) -> None:
        device = self._registry.device(device_id)
        if device.kind != "kingst_la2016":
            raise ValueError(f"Device is not a Kingst logic analyzer: {device_id}")

    def _instrument(self, device_id: str) -> LogicAnalyzerInstrument:
        instrument = self._registry.instrument(device_id)
        if not isinstance(instrument, KingstLA2016) and not (
            hasattr(instrument, "capture") and hasattr(instrument, "stop")
        ):
            raise ValueError(f"Device does not support logic capture: {device_id}")
        return instrument  # type: ignore[return-value]

    def _set_countdown(self, device_id: str, duration_s: float | None) -> None:
        if duration_s is None:
            self._countdown_deadlines.pop(device_id, None)
            return
        self._countdown_deadlines[device_id] = time.monotonic() + max(0.0, duration_s)

    def _initial_phase_duration(
        self,
        config: KingstCaptureConfig,
        *,
        hardware_trigger: bool,
    ) -> float | None:
        if not hardware_trigger:
            return self._estimated_bounded_duration(
                config,
                acquisition_duration_s=config.duration_s,
            )
        pretrigger_s = config.duration_s * config.capture_ratio_percent / 100
        return pretrigger_s if pretrigger_s > 0 else None

    def _estimated_bounded_duration(
        self,
        config: KingstCaptureConfig,
        *,
        acquisition_duration_s: float,
    ) -> float:
        return acquisition_duration_s + self._estimated_download_duration(config)

    def _estimated_download_duration(self, config: KingstCaptureConfig) -> float:
        estimated_bytes = config.sample_count * len(config.channels) / 8
        return (
            self._CAPTURE_COMPLETION_OVERHEAD_S
            + estimated_bytes / self._ESTIMATED_DOWNLOAD_BYTES_PER_SECOND
        )

    def _new_capture_directory(self, title: str) -> Path:
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M")
        label = CaptureService._filename_label(title)
        base = f"{timestamp}_logic"
        if label:
            base = f"{base}_{label}"
        candidate = self.output_directory / base
        index = 2
        while candidate.exists():
            candidate = self.output_directory / f"{base}_{index:02d}"
            index += 1
        return candidate

    async def _write_metadata(
        self,
        status: LogicCaptureStatus,
        config: KingstCaptureConfig,
    ) -> None:
        directory = status.artifact_directory
        if directory is None:
            return
        payload = {
            "capture": {
                key: (
                    value.isoformat()
                    if isinstance(value, datetime)
                    else str(value)
                    if isinstance(value, Path)
                    else value
                )
                for key, value in asdict(status).items()
            },
            "configuration": {
                **asdict(config),
                "triggers": [asdict(trigger) for trigger in config.triggers],
            },
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        await asyncio.to_thread(
            (directory / "metadata.json").write_text,
            text,
            encoding="utf-8",
        )

    @staticmethod
    def _validate_metadata(title: str, comment: str) -> tuple[str, str]:
        title = title.strip()
        comment = comment.strip()
        if len(title) > 120:
            raise ValueError("Capture title must not exceed 120 characters")
        if len(comment) > 10000:
            raise ValueError("Capture comment must not exceed 10000 characters")
        return title, comment

    @staticmethod
    def _idle_status(device_id: str) -> LogicCaptureStatus:
        return LogicCaptureStatus(
            device_id=device_id,
            state="ready",
            active=False,
            capture_id="",
            title="",
            comment="",
            requested_at=None,
            started_at=None,
            triggered_at=None,
            completed_at=None,
            scheduled_start_at=None,
            estimated_duration_s=None,
            remaining_s=None,
            trigger_timestamp_quality="unavailable",
            trigger="OFF",
            artifact_directory=None,
            artifact_file=None,
            recording_file=None,
            source="",
            message="Ready",
            error="",
        )


__all__ = [
    "KINGST_SAMPLE_RATES_HZ",
    "KINGST_THRESHOLDS_V",
    "LogicAnalyzerService",
    "LogicAnalyzerSettings",
    "LogicCaptureStatus",
]
