from __future__ import annotations

import csv
import io
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from openbench.api.dependencies import ContextDep
from openbench.api.schemas import (
    OscilloscopeOut,
    ScopeAcquisitionActionOut,
    ScopeFastBinaryProbeIn,
    ScopeFastBinaryProbeOut,
    ScopeMaximumCaptureFileOut,
    ScopeMaximumCaptureIn,
    ScopeMaximumCaptureStatusOut,
    ScopeNumericCsvCaptureIn,
    ScopeNumericCsvOut,
    ScopeScalarMeasurementOut,
    ScopeScalarMeasurementProfileIn,
    ScopeScalarMeasurementProfileOut,
    ScopeScreenshotProbeIn,
    ScopeScreenshotProbeOut,
    ScopeSettingsUpdateIn,
    ScopeSingleIn,
    ScopeStoredFileImportIn,
    ScopeStoredWaveformCaptureIn,
    ScopeStoredWaveformFileOut,
    ScopeStoredWaveformOut,
    ScopeWaveformCaptureIn,
    ScopeWaveformChannelOut,
    ScopeWaveformOut,
)
from openbench.drivers.micsig_common import is_micsig_scope_kind
from openbench.drivers.micsig_mho1 import (
    MicsigChannelUpdate,
    MicsigMHO1Scope,
    MicsigScalarMeasurement,
    MicsigScopeStatus,
    MicsigScopeUpdate,
    MicsigTriggerUpdate,
)
from openbench.services.scope_maximum_capture_service import ScopeMaximumCaptureStatus
from openbench.services.scope_measurement_service import ScopeMeasurementSelection

router = APIRouter(
    prefix="/api/v1/oscilloscopes",
    tags=["oscilloscopes"],
)


def _scope(device_id: str, context: ContextDep) -> MicsigMHO1Scope:
    device = context.registry.device(device_id)
    instrument = context.registry.instrument(device_id)
    if not is_micsig_scope_kind(device.kind) or not isinstance(
        instrument,
        MicsigMHO1Scope,
    ):
        raise ValueError(f"Device is not a supported oscilloscope: {device_id}")
    return instrument


def _out(scope: MicsigMHO1Scope, state: MicsigScopeStatus) -> OscilloscopeOut:
    descriptor = scope.descriptor
    return OscilloscopeOut.from_driver(
        device_id=scope.device_id,
        model=descriptor.model,
        serial_number=descriptor.serial_number,
        firmware_version=descriptor.firmware_version,
        calibrated_waveform_transfer_available=(scope.calibrated_waveform_transfer_available),
        waveform_transfer_method=scope.waveform_transfer_method,
        state=state,
    )


def _raise_http(exc: Exception) -> NoReturn:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=503, detail=str(exc)) from exc


def _measurement_out(item: MicsigScalarMeasurement) -> ScopeScalarMeasurementOut:
    return ScopeScalarMeasurementOut(
        channel=item.channel,
        item=item.item,
        value=item.value,
        unit=item.unit,
        status=item.status,
        secondary_channel=item.secondary_channel,
        source_edge=item.source_edge,
        target_edge=item.target_edge,
    )


def _maximum_capture_out(
    status: ScopeMaximumCaptureStatus,
) -> ScopeMaximumCaptureStatusOut:
    base = f"/api/v1/oscilloscopes/{status.device_id}/maximum-capture/files"
    return ScopeMaximumCaptureStatusOut(
        device_id=status.device_id,
        state=status.state,
        active=status.active,
        capture_id=status.capture_id,
        channels=list(status.channels),
        current_channel=status.current_channel,
        memory_depth_points=status.memory_depth_points,
        points_total=status.points_total,
        points_completed=status.points_completed,
        progress_percent=status.progress_percent,
        requested_at=status.requested_at,
        started_at=status.started_at,
        completed_at=status.completed_at,
        artifact_directory=(
            status.artifact_directory.name if status.artifact_directory is not None else None
        ),
        files=[
            ScopeMaximumCaptureFileOut(
                filename=path.name,
                bytes=path.stat().st_size,
                download_url=f"{base}/{path.name}",
            )
            for path in status.artifact_files
            if path.is_file()
        ],
        metadata_download_url=(
            f"{base}/{status.metadata_file.name}" if status.metadata_file is not None else None
        ),
        message=status.message,
        error=status.error,
    )


@router.get(
    "/{device_id}/measurements",
    response_model=ScopeScalarMeasurementProfileOut,
)
async def get_oscilloscope_measurements(
    device_id: str,
    context: ContextDep,
) -> ScopeScalarMeasurementProfileOut:
    """Return the configured Dashboard/front-panel profile without hardware writes."""
    try:
        device = context.registry.device(device_id)
        if not is_micsig_scope_kind(device.kind):
            raise ValueError(f"Device is not a supported oscilloscope: {device_id}")
        measurements = []
        for selection in context.scope_measurement_service.selections(device_id):
            latest = context.scope_measurement_service.latest_for(device_id, selection)
            scalar = (
                latest.scalar
                if latest is not None
                else MicsigScalarMeasurement(
                    item=selection.item,
                    channel=selection.channel,
                    secondary_channel=selection.secondary_channel,
                    source_edge=selection.source_edge,
                    target_edge=selection.target_edge,
                    value=None,
                    unit=selection.unit,
                    status="waiting",
                )
            )
            measurements.append(_measurement_out(scalar))
        return ScopeScalarMeasurementProfileOut(
            device_id=device_id,
            measurements=measurements,
            elapsed_s=0.0,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.get("/{device_id}", response_model=OscilloscopeOut)
async def oscilloscope_state(device_id: str, context: ContextDep) -> OscilloscopeOut:
    try:
        scope = _scope(device_id, context)
        return _out(scope, await scope.read_state())
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/settings", response_model=OscilloscopeOut)
async def update_oscilloscope_settings(
    device_id: str,
    payload: ScopeSettingsUpdateIn,
    context: ContextDep,
) -> OscilloscopeOut:
    if not payload.model_fields_set:
        raise HTTPException(status_code=400, detail="At least one scope setting is required")
    try:
        scope = _scope(device_id, context)
        trigger = payload.trigger
        update = MicsigScopeUpdate(
            channels=tuple(
                MicsigChannelUpdate(
                    channel=item.channel,
                    displayed=item.displayed,
                    scale_v_per_div=item.scale_v_per_div,
                    position=item.position_v,
                    coupling=item.coupling,
                    probe_attenuation=item.probe_attenuation,
                    input_impedance=item.input_impedance,
                )
                for item in payload.channels
            ),
            acquisition_type=payload.acquisition_type,
            averaging_count=payload.averaging_count,
            memory_depth_setting=payload.memory_depth_setting,
            timebase_s_per_div=payload.timebase_s_per_div,
            timebase_position_s=payload.timebase_position_s,
            timebase_mode=payload.timebase_mode,
            trigger=(MicsigTriggerUpdate(**trigger.model_dump()) if trigger is not None else None),
        )
        return _out(scope, await scope.apply_update(update))
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/run", response_model=ScopeAcquisitionActionOut)
async def run_oscilloscope(device_id: str, context: ContextDep) -> ScopeAcquisitionActionOut:
    try:
        scope = _scope(device_id, context)
        started = time.monotonic()
        await scope.run()
        return ScopeAcquisitionActionOut(
            device_id=device_id,
            status="RUN",
            elapsed_s=time.monotonic() - started,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/stop", response_model=ScopeAcquisitionActionOut)
async def stop_oscilloscope(device_id: str, context: ContextDep) -> ScopeAcquisitionActionOut:
    try:
        scope = _scope(device_id, context)
        started = time.monotonic()
        await scope.stop()
        return ScopeAcquisitionActionOut(
            device_id=device_id,
            status="STOP",
            elapsed_s=time.monotonic() - started,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/single", response_model=OscilloscopeOut)
async def single_oscilloscope(
    device_id: str,
    payload: ScopeSingleIn,
    context: ContextDep,
) -> OscilloscopeOut:
    try:
        scope = _scope(device_id, context)
        await scope.single(wait_timeout_s=payload.timeout_s)
        return _out(scope, await scope.read_state())
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/waveforms", response_model=ScopeWaveformOut)
async def capture_oscilloscope_waveforms(
    device_id: str,
    payload: ScopeWaveformCaptureIn,
    context: ContextDep,
) -> ScopeWaveformOut:
    try:
        _scope(device_id, context)
        if payload.mode != "NORMAL":
            raise ValueError("The verified Micsig frame supports NORMAL mode only")
        await context.scope_measurement_service.sample_now(
            device_id,
            waveform_channels=payload.channels,
            include_screenshot=False,
        )
        frame = context.scope_measurement_service.latest_frame(device_id)
        if frame is None:
            raise RuntimeError("Micsig frame capture did not return a frame")
        if frame.waveform_error:
            raise RuntimeError(frame.waveform_error)
        requested = set(payload.channels)
        captures = tuple(item for item in frame.waveforms if item.source in requested)
        return ScopeWaveformOut(
            device_id=device_id,
            channels=[ScopeWaveformChannelOut.from_driver(item) for item in captures],
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.put(
    "/{device_id}/measurements",
    response_model=ScopeScalarMeasurementProfileOut,
)
async def replace_oscilloscope_measurements(
    device_id: str,
    payload: ScopeScalarMeasurementProfileIn,
    context: ContextDep,
) -> ScopeScalarMeasurementProfileOut:
    """Replace the scalar measurements displayed by the oscilloscope."""
    try:
        _scope(device_id, context)
        started = time.monotonic()
        readings = await context.scope_measurement_service.replace_selections(
            device_id,
            tuple(
                ScopeMeasurementSelection(
                    channel=item.channel,
                    item=item.item,
                    secondary_channel=item.secondary_channel,
                    source_edge=item.source_edge,
                    target_edge=item.target_edge,
                )
                for item in payload.measurements
            ),
        )
        return ScopeScalarMeasurementProfileOut(
            device_id=device_id,
            measurements=[_measurement_out(reading.scalar) for reading in readings],
            elapsed_s=time.monotonic() - started,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post(
    "/{device_id}/measurements/read",
    response_model=ScopeScalarMeasurementProfileOut,
)
async def read_oscilloscope_measurements(
    device_id: str,
    payload: ScopeScalarMeasurementProfileIn,
    context: ContextDep,
) -> ScopeScalarMeasurementProfileOut:
    """Read a configured scalar profile without changing the front panel."""
    try:
        scope = _scope(device_id, context)
        started = time.monotonic()
        values = await scope.read_scalar_measurement_profile(
            tuple(
                ScopeMeasurementSelection(
                    channel=item.channel,
                    item=item.item,
                    secondary_channel=item.secondary_channel,
                    source_edge=item.source_edge,
                    target_edge=item.target_edge,
                ).driver_spec
                for item in payload.measurements
            )
        )
        return ScopeScalarMeasurementProfileOut(
            device_id=device_id,
            measurements=[_measurement_out(item) for item in values],
            elapsed_s=time.monotonic() - started,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post(
    "/{device_id}/screenshot-probe",
    response_model=ScopeScreenshotProbeOut,
)
async def probe_oscilloscope_screenshot(
    device_id: str,
    payload: ScopeScreenshotProbeIn,
    context: ContextDep,
) -> ScopeScreenshotProbeOut:
    """Probe a direct screenshot transport without creating a scope file."""
    try:
        result = await _scope(device_id, context).probe_direct_screenshot(payload.transport)
        return ScopeScreenshotProbeOut(
            device_id=device_id,
            transport=result.transport,
            raw_bytes=result.raw_bytes,
            declared_payload_bytes=result.declared_payload_bytes,
            payload_bytes=result.payload_bytes,
            prefix_hex=result.prefix_hex,
            image_format=result.image_format,
            error=result.error,
            elapsed_s=result.elapsed_s,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post(
    "/{device_id}/fast-binary-probe",
    response_model=ScopeFastBinaryProbeOut,
)
async def probe_fast_binary_waveform(
    device_id: str,
    payload: ScopeFastBinaryProbeIn,
    context: ContextDep,
) -> ScopeFastBinaryProbeOut:
    """Probe one undocumented fast binary waveform query without fallback."""
    try:
        result = await _scope(device_id, context).probe_fast_binary_waveform(payload.channel)
        filename: str | None = None
        download_url: str | None = None
        if result.data:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            filename = f"mho1_fast_binary_{result.source.lower()}_{timestamp}.bin"
            output_directory = _stored_waveform_directory(context, device_id)
            output_directory.mkdir(parents=True, exist_ok=True)
            (output_directory / filename).write_bytes(result.data)
            download_url = f"/api/v1/oscilloscopes/{device_id}/storage-waveforms/{filename}"
        return ScopeFastBinaryProbeOut(
            device_id=device_id,
            source=result.source,
            payload_bytes=result.payload_bytes,
            points=result.points,
            prefix_hex=result.prefix_hex,
            error=result.error,
            elapsed_s=result.elapsed_s,
            filename=filename,
            download_url=download_url,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.get("/{device_id}/storage-index", response_model=list[str])
async def oscilloscope_storage_index(
    device_id: str,
    context: ContextDep,
    path: str = Query(default="/", min_length=1, max_length=500),
) -> list[str]:
    try:
        return list(await _scope(device_id, context).storage_index(path))
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


def _stored_waveform_directory(context: ContextDep, device_id: str) -> Path:
    safe_device_id = re.sub(r"[^A-Za-z0-9_-]+", "_", device_id)
    return Path(context.settings.capture_directory) / "scope-waveforms" / safe_device_id


@router.post(
    "/{device_id}/numeric-waveforms/csv",
    response_model=ScopeNumericCsvOut,
)
async def capture_numeric_oscilloscope_csv(
    device_id: str,
    payload: ScopeNumericCsvCaptureIn,
    context: ContextDep,
) -> ScopeNumericCsvOut:
    try:
        scope = _scope(device_id, context)
        if payload.mode != "NORMAL":
            raise ValueError("The verified Micsig frame supports NORMAL mode only")
        await context.scope_measurement_service.sample_now(
            device_id,
            waveform_channels=payload.channels,
            include_screenshot=False,
        )
        frame = context.scope_measurement_service.latest_frame(device_id)
        if frame is None:
            raise RuntimeError("Micsig frame capture did not return a frame")
        if frame.waveform_error:
            raise RuntimeError(frame.waveform_error)
        requested = set(payload.channels)
        captures = tuple(item for item in frame.waveforms if item.source in requested)
        if not captures:
            raise RuntimeError("Micsig frame contained none of the requested channels")
        points = min(item.points for item in captures)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            ["sample_index", "time_s", *(f"{item.source.lower()}_v" for item in captures)]
        )
        reference = captures[0]
        for index in range(points):
            writer.writerow(
                [
                    index,
                    format(reference.time_at(index), ".17g"),
                    *(format(item.voltage_at(index), ".17g") for item in captures),
                ]
            )
        data = output.getvalue().encode("utf-8")
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"{payload.filename_prefix}_{timestamp}.csv"
        output_directory = _stored_waveform_directory(context, device_id)
        output_directory.mkdir(parents=True, exist_ok=True)
        (output_directory / filename).write_bytes(data)
        return ScopeNumericCsvOut(
            device_id=device_id,
            channels=[item.source for item in captures],
            points=points,
            transfer_method=scope.waveform_transfer_method,
            filename=filename,
            bytes=len(data),
            download_url=(f"/api/v1/oscilloscopes/{device_id}/storage-waveforms/{filename}"),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.get(
    "/{device_id}/maximum-capture",
    response_model=ScopeMaximumCaptureStatusOut,
)
def maximum_oscilloscope_capture_status(
    device_id: str,
    context: ContextDep,
) -> ScopeMaximumCaptureStatusOut:
    try:
        return _maximum_capture_out(context.scope_maximum_capture_service.status(device_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/{device_id}/maximum-capture",
    response_model=ScopeMaximumCaptureStatusOut,
    status_code=202,
)
async def start_maximum_oscilloscope_capture(
    device_id: str,
    payload: ScopeMaximumCaptureIn,
    context: ContextDep,
) -> ScopeMaximumCaptureStatusOut:
    try:
        status = await context.scope_maximum_capture_service.start_capture(
            device_id,
            channels=tuple(payload.channels),
        )
        return _maximum_capture_out(status)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/{device_id}/maximum-capture/files/{filename}",
    response_class=FileResponse,
)
def download_maximum_oscilloscope_capture(
    device_id: str,
    filename: str,
    context: ContextDep,
) -> FileResponse:
    try:
        path = context.scope_maximum_capture_service.artifact(device_id, filename)
        media_type = "application/json" if path.suffix.casefold() == ".json" else "text/plain"
        return FileResponse(path, media_type=media_type, filename=path.name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/{device_id}/storage-waveforms",
    response_model=ScopeStoredWaveformOut,
)
async def capture_stored_oscilloscope_waveforms(
    device_id: str,
    payload: ScopeStoredWaveformCaptureIn,
    context: ContextDep,
) -> ScopeStoredWaveformOut:
    try:
        scope = _scope(device_id, context)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        output_directory = _stored_waveform_directory(context, device_id)
        output_directory.mkdir(parents=True, exist_ok=True)
        files: list[ScopeStoredWaveformFileOut] = []
        for channel in dict.fromkeys(payload.channels):
            local_stem = f"{payload.filename_prefix}_{timestamp}_{channel.lower()}"
            scope_stem = f"{datetime.now(UTC).strftime('%y%m%d%H%M%S%f')}{channel[-1]}1"
            stored = await scope.save_waveform_file(
                channel,
                file_type=payload.format,
                filename=scope_stem,
                timeout_s=payload.timeout_s,
            )
            extension = payload.format.lower()
            local_filename = f"{local_stem}.{extension}"
            local_path = output_directory / local_filename
            local_path.write_bytes(stored.data)
            files.append(
                ScopeStoredWaveformFileOut(
                    source=stored.source,
                    format=stored.file_type,
                    filename=local_filename,
                    bytes=len(stored.data),
                    scope_path=stored.http_path,
                    download_url=(
                        f"/api/v1/oscilloscopes/{device_id}/storage-waveforms/{local_filename}"
                    ),
                    attempts=1,
                )
            )
        return ScopeStoredWaveformOut(device_id=device_id, files=files)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post(
    "/{device_id}/storage-waveforms/import",
    response_model=ScopeStoredWaveformOut,
)
async def import_stored_oscilloscope_waveforms(
    device_id: str,
    payload: ScopeStoredFileImportIn,
    context: ContextDep,
) -> ScopeStoredWaveformOut:
    """Copy files already saved by the operator from scope storage to the PC."""
    try:
        scope = _scope(device_id, context)
        output_directory = _stored_waveform_directory(context, device_id)
        output_directory.mkdir(parents=True, exist_ok=True)
        files: list[ScopeStoredWaveformFileOut] = []
        for scope_path in dict.fromkeys(payload.scope_paths):
            match = re.fullmatch(
                r"/files/(?:csvwave|binwave|refwave)/"
                r"([A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.(csv|bin|wav))",
                scope_path,
                flags=re.IGNORECASE,
            )
            if match is None:
                raise ValueError(
                    "Stored scope path must name a CSV, BIN, or WAV file under "
                    "/files/csvwave, /files/binwave, or /files/refwave"
                )
            filename = match.group(1)
            extension = match.group(2).lower()
            data = await scope.download_stored_file(scope_path)
            local_path = output_directory / filename
            if local_path.exists() and local_path.read_bytes() != data:
                timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                local_path = output_directory / f"{local_path.stem}_{timestamp}{local_path.suffix}"
            local_path.write_bytes(data)
            local_filename = local_path.name
            files.append(
                ScopeStoredWaveformFileOut(
                    source="stored",
                    format=extension.upper(),
                    filename=local_filename,
                    bytes=len(data),
                    scope_path=scope_path,
                    download_url=(
                        f"/api/v1/oscilloscopes/{device_id}/storage-waveforms/{local_filename}"
                    ),
                )
            )
        return ScopeStoredWaveformOut(device_id=device_id, files=files)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.get("/{device_id}/storage-waveforms/{filename}", response_class=FileResponse)
def download_stored_oscilloscope_waveform(
    device_id: str,
    filename: str,
    context: ContextDep,
) -> FileResponse:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:csv|bin|wav)", filename):
        raise HTTPException(status_code=400, detail="Invalid stored waveform filename")
    try:
        _scope(device_id, context)
        path = _stored_waveform_directory(context, device_id) / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Stored waveform file not found")
        media_type = "text/csv" if path.suffix.casefold() == ".csv" else "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=filename)
    except HTTPException:
        raise
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)
