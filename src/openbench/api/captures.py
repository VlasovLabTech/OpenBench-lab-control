from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from openbench.api.dependencies import ContextDep
from openbench.api.schemas import (
    CaptureMetadataIn,
    CaptureStatusOut,
    RecordingScopeFrameIn,
    RecordingScopeFrameOut,
    RecordingStartIn,
    SnapshotOut,
)

router = APIRouter(prefix="/api/v1/captures", tags=["captures"])


@router.get("/status", response_model=CaptureStatusOut)
def capture_status(context: ContextDep) -> CaptureStatusOut:
    return CaptureStatusOut.from_service(context.capture_service.status())


@router.post("/snapshot", response_model=SnapshotOut, status_code=201)
async def take_snapshot(
    payload: CaptureMetadataIn,
    context: ContextDep,
) -> SnapshotOut:
    try:
        path, measurements = await context.capture_service.snapshot(
            title=payload.title,
            comment=payload.comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SnapshotOut(
        file_name=path.name,
        download_url=f"/api/v1/captures/files/{path.name}",
        measurement_count=len(measurements),
    )


@router.post("/recording/start", response_model=CaptureStatusOut, status_code=201)
async def start_recording(
    payload: RecordingStartIn,
    context: ContextDep,
) -> CaptureStatusOut:
    try:
        status = await context.capture_service.start_recording(
            title=payload.title,
            comment=payload.comment,
            duration_s=payload.duration_s,
            scope_capture_mode=payload.scope_capture_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CaptureStatusOut.from_service(status)


@router.post(
    "/recording/scopes/{device_id}/frame",
    response_model=RecordingScopeFrameOut,
    status_code=201,
)
async def capture_recording_scope_frame(
    device_id: str,
    payload: RecordingScopeFrameIn,
    context: ContextDep,
) -> RecordingScopeFrameOut:
    try:
        capture_id, result = await context.capture_service.capture_recording_scope_frame(
            device_id,
            label=payload.label,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RecordingScopeFrameOut(
        device_id=device_id,
        capture_id=capture_id,
        timestamp_utc=result.timestamp_utc,
        status=result.status,
        screen_file=result.screen_file,
        data_file=result.data_file,
        error=result.error,
    )


@router.post("/recording/stop", response_model=CaptureStatusOut)
async def stop_recording(context: ContextDep) -> CaptureStatusOut:
    try:
        status = await context.capture_service.stop_recording()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CaptureStatusOut.from_service(status)


@router.get("/files/{filename}", response_class=FileResponse)
def download_capture(filename: str, context: ContextDep) -> FileResponse:
    try:
        path = context.capture_service.resolve_artifact(filename)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Capture file not found") from exc
    return FileResponse(path, media_type="text/csv", filename=path.name)
