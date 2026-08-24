from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from openbench.core.registry import DeviceRegistry
from openbench.drivers.micsig_common import (
    MICSIG_MAXIMUM_ASCII_CHUNK_POINTS,
    MicsigMaximumAsciiChunk,
    is_micsig_scope_kind,
)
from openbench.drivers.micsig_eto import MicsigETOScope
from openbench.drivers.micsig_mho1 import MicsigMHO1Scope
from openbench.drivers.micsig_mho1.protocol import channel_source
from openbench.services.capture_service import CaptureService
from openbench.services.scope_measurement_service import ScopeMeasurementService

ACTIVE_MAXIMUM_CAPTURE_STATES = frozenset(("starting", "capturing", "finalizing"))
ASCII_BYTES_PER_POINT_DISK_RESERVATION = 24
DISK_RESERVATION_MARGIN_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ScopeMaximumCaptureStatus:
    device_id: str
    state: str
    active: bool
    capture_id: str
    channels: tuple[str, ...]
    current_channel: str | None
    memory_depth_points: int
    points_total: int
    points_completed: int
    requested_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    artifact_directory: Path | None
    artifact_files: tuple[Path, ...]
    metadata_file: Path | None
    message: str
    error: str

    @property
    def progress_percent(self) -> float:
        if self.points_total <= 0:
            return 0.0
        return min(100.0, 100.0 * self.points_completed / self.points_total)


class ScopeMaximumCaptureService:
    """Own one asynchronous, STOP-only Micsig MAXIMUM ASCII export per scope."""

    def __init__(
        self,
        output_directory: Path,
        registry: DeviceRegistry,
        scope_measurement_service: ScopeMeasurementService,
        capture_service: CaptureService,
    ) -> None:
        self.output_directory = (output_directory / "scope-maximum").resolve()
        self._registry = registry
        self._scope_measurement_service = scope_measurement_service
        self._capture_service = capture_service
        self._statuses: dict[str, ScopeMaximumCaptureStatus] = {}
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _idle_status(device_id: str) -> ScopeMaximumCaptureStatus:
        return ScopeMaximumCaptureStatus(
            device_id=device_id,
            state="ready",
            active=False,
            capture_id="",
            channels=(),
            current_channel=None,
            memory_depth_points=0,
            points_total=0,
            points_completed=0,
            requested_at=None,
            started_at=None,
            completed_at=None,
            artifact_directory=None,
            artifact_files=(),
            metadata_file=None,
            message="Ready for one STOP-only MAXIMUM ASCII capture",
            error="",
        )

    def _instrument(self, device_id: str) -> MicsigMHO1Scope | MicsigETOScope:
        device = self._registry.device(device_id)
        instrument = self._registry.instrument(device_id)
        if not is_micsig_scope_kind(device.kind) or not isinstance(
            instrument,
            (MicsigMHO1Scope, MicsigETOScope),
        ):
            raise ValueError(f"Device is not a supported Micsig oscilloscope: {device_id}")
        return instrument

    def status(self, device_id: str) -> ScopeMaximumCaptureStatus:
        device = self._registry.device(device_id)
        if not is_micsig_scope_kind(device.kind):
            raise ValueError(f"Device is not a supported Micsig oscilloscope: {device_id}")
        return self._statuses.get(device_id, self._idle_status(device_id))

    def owns_device(self, device_id: str) -> bool:
        status = self._statuses.get(device_id)
        return status is not None and status.state in ACTIVE_MAXIMUM_CAPTURE_STATES

    def active_device_ids(self) -> frozenset[str]:
        return frozenset(device_id for device_id in self._statuses if self.owns_device(device_id))

    async def start_capture(
        self,
        device_id: str,
        *,
        channels: tuple[str, ...],
    ) -> ScopeMaximumCaptureStatus:
        normalized_channels = tuple(dict.fromkeys(channel_source(channel) for channel in channels))
        if not normalized_channels:
            raise ValueError("Select at least one Micsig MAXIMUM ASCII channel")
        instrument = self._instrument(device_id)
        async with self._lock:
            if self.owns_device(device_id):
                raise RuntimeError("Micsig MAXIMUM ASCII capture is already active")
            if self._capture_service.status().active:
                raise RuntimeError(
                    "Stop the common CSV recording before starting MAXIMUM ASCII capture"
                )
            requested_at = datetime.now(UTC)
            self._statuses[device_id] = replace(
                self._idle_status(device_id),
                state="starting",
                active=True,
                channels=normalized_channels,
                requested_at=requested_at,
                message="Checking STOP state and current memory depth",
            )
            await self._scope_measurement_service.suspend_live_polling(device_id)
            try:
                info = await instrument.maximum_ascii_capture_info()
                required_bytes = (
                    info.memory_depth_points
                    * len(normalized_channels)
                    * ASCII_BYTES_PER_POINT_DISK_RESERVATION
                    + DISK_RESERVATION_MARGIN_BYTES
                )
                await asyncio.to_thread(self.output_directory.mkdir, parents=True, exist_ok=True)
                free_bytes = await asyncio.to_thread(
                    lambda: shutil.disk_usage(self.output_directory).free
                )
                if free_bytes < required_bytes:
                    raise RuntimeError(
                        "Insufficient free disk space for bounded MAXIMUM ASCII capture: "
                        f"need at least {required_bytes:,} bytes, have {free_bytes:,}"
                    )
                capture_id = requested_at.strftime("%Y%m%dT%H%M%S%fZ")
                safe_device_id = re.sub(r"[^A-Za-z0-9_-]+", "_", device_id)
                artifact_directory = self.output_directory / safe_device_id / capture_id
                await asyncio.to_thread(
                    artifact_directory.mkdir,
                    parents=True,
                    exist_ok=False,
                )
                points_total = info.memory_depth_points * len(normalized_channels)
                self._statuses[device_id] = replace(
                    self._statuses[device_id],
                    state="capturing",
                    capture_id=capture_id,
                    memory_depth_points=info.memory_depth_points,
                    points_total=points_total,
                    started_at=datetime.now(UTC),
                    artifact_directory=artifact_directory,
                    message="Reading stopped acquisition memory",
                )
                task = asyncio.create_task(
                    self._run_capture(device_id, instrument),
                    name=f"openbench-scope-maximum-{device_id}",
                )
                self._jobs[device_id] = task
            except BaseException as exc:
                self._statuses[device_id] = replace(
                    self._statuses[device_id],
                    state="error",
                    active=False,
                    completed_at=datetime.now(UTC),
                    message="MAXIMUM ASCII capture did not start",
                    error=str(exc),
                )
                self._scope_measurement_service.resume_live_polling(device_id)
                raise
        return self.status(device_id)

    async def _run_capture(
        self,
        device_id: str,
        instrument: MicsigMHO1Scope | MicsigETOScope,
    ) -> None:
        status = self._statuses[device_id]
        artifact_directory = status.artifact_directory
        assert artifact_directory is not None
        streams: dict[str, BinaryIO] = {}
        paths: dict[str, Path] = {}
        preambles: dict[str, str] = {}
        device_kind = self._registry.device(device_id).kind
        artifact_prefix = "eto5004" if device_kind == "micsig_eto" else "mho1"

        async def receive(chunk: MicsigMaximumAsciiChunk) -> None:
            stream = streams.get(chunk.source)
            if stream is None:
                path = artifact_directory / (
                    f"{artifact_prefix}_{chunk.source.casefold()}_maximum_ascii.txt"
                )
                new_stream = await asyncio.to_thread(path.open, "wb")
                streams[chunk.source] = new_stream
                paths[chunk.source] = path
                preambles[chunk.source] = chunk.preamble_text
                stream = new_stream
            separator = b"," if stream.tell() else b""
            await asyncio.to_thread(stream.write, separator + chunk.data)
            if chunk.stop_point == chunk.total_points:
                await asyncio.to_thread(stream.write, b"\n")
                await asyncio.to_thread(stream.flush)
                await asyncio.to_thread(stream.close)
                streams.pop(chunk.source, None)
            latest = self._statuses[device_id]
            completed = latest.points_completed + chunk.points
            self._statuses[device_id] = replace(
                latest,
                current_channel=chunk.source,
                points_completed=completed,
                message=(
                    f"Reading {chunk.source}: {chunk.stop_point:,} / {chunk.total_points:,} points"
                ),
            )

        try:
            await instrument.stream_maximum_ascii(status.channels, on_chunk=receive)
            latest = self._statuses[device_id]
            self._statuses[device_id] = replace(
                latest,
                state="finalizing",
                current_channel=None,
                message="Finalizing MAXIMUM ASCII files",
            )
            metadata_path = artifact_directory / "capture.json"
            metadata = {
                "schema_version": 1,
                "capture_id": status.capture_id,
                "device_id": device_id,
                "device_kind": device_kind,
                "mode": "MAXIMUM",
                "format": "ASCII",
                "once_only": True,
                "required_acquisition_state": "STOP",
                "memory_depth_changed": False,
                "memory_depth_points": status.memory_depth_points,
                "chunk_points": MICSIG_MAXIMUM_ASCII_CHUNK_POINTS,
                "channels": list(status.channels),
                "requested_at_utc": (
                    status.requested_at.isoformat() if status.requested_at else None
                ),
                "started_at_utc": status.started_at.isoformat() if status.started_at else None,
                "completed_at_utc": datetime.now(UTC).isoformat(),
                "preambles": preambles,
                "files": {
                    source: {
                        "name": path.name,
                        "bytes": path.stat().st_size,
                    }
                    for source, path in paths.items()
                },
            }
            payload = (
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            await asyncio.to_thread(metadata_path.write_bytes, payload)
            completed_at = datetime.now(UTC)
            files = tuple(paths[source] for source in status.channels)
            self._statuses[device_id] = replace(
                self._statuses[device_id],
                state="completed",
                active=False,
                current_channel=None,
                points_completed=status.points_total,
                completed_at=completed_at,
                artifact_files=files,
                metadata_file=metadata_path,
                message="MAXIMUM ASCII capture complete",
            )
        except asyncio.CancelledError:
            self._statuses[device_id] = replace(
                self._statuses[device_id],
                state="stopped",
                active=False,
                completed_at=datetime.now(UTC),
                artifact_files=tuple(paths.values()),
                message="MAXIMUM ASCII capture stopped during OpenBench shutdown",
            )
            raise
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            self._statuses[device_id] = replace(
                self._statuses[device_id],
                state="error",
                active=False,
                completed_at=datetime.now(UTC),
                artifact_files=tuple(paths.values()),
                message="MAXIMUM ASCII capture failed",
                error=str(exc),
            )
        finally:
            for stream in streams.values():
                await asyncio.to_thread(stream.close)
            self._jobs.pop(device_id, None)
            self._scope_measurement_service.resume_live_polling(device_id)

    def artifact(self, device_id: str, filename: str) -> Path:
        status = self.status(device_id)
        if status.artifact_directory is None:
            raise FileNotFoundError("No Micsig MAXIMUM ASCII capture is available")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:txt|json)", filename):
            raise ValueError("Invalid MAXIMUM ASCII artifact filename")
        path = status.artifact_directory / filename
        if path.parent != status.artifact_directory or not path.is_file():
            raise FileNotFoundError(filename)
        return path

    async def close(self) -> None:
        tasks = tuple(self._jobs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
