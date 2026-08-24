from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from openbench.api.dependencies import ContextDep
from openbench.api.schemas import (
    LogicAnalyzerSettingsOut,
    LogicAnalyzerSettingsUpdate,
    LogicCaptureStartIn,
    LogicCaptureStatusOut,
)
from openbench.drivers.kingst_la2016 import (
    KINGST_SAMPLE_RATES_HZ,
    KINGST_THRESHOLDS_V,
)

router = APIRouter(prefix="/api/v1/logic-analyzers", tags=["logic analyzers"])


def _settings_out(context: ContextDep, device_id: str) -> LogicAnalyzerSettingsOut:
    return LogicAnalyzerSettingsOut.from_service(
        context.logic_analyzer_service.settings(device_id),
        supported_sample_rates_hz=KINGST_SAMPLE_RATES_HZ,
        supported_thresholds_v=KINGST_THRESHOLDS_V,
    )


@router.get("", response_model=list[LogicCaptureStatusOut])
def list_logic_analyzers(context: ContextDep) -> list[LogicCaptureStatusOut]:
    return [
        LogicCaptureStatusOut.from_service(status)
        for status in context.logic_analyzer_service.list_statuses()
    ]


@router.get("/{device_id}/settings", response_model=LogicAnalyzerSettingsOut)
def logic_analyzer_settings(
    device_id: str,
    context: ContextDep,
) -> LogicAnalyzerSettingsOut:
    try:
        return _settings_out(context, device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{device_id}/settings", response_model=LogicAnalyzerSettingsOut)
def update_logic_analyzer_settings(
    device_id: str,
    payload: LogicAnalyzerSettingsUpdate,
    context: ContextDep,
) -> LogicAnalyzerSettingsOut:
    try:
        context.logic_analyzer_service.update_settings(
            device_id,
            channels=(
                tuple(payload.channels)
                if "channels" in payload.model_fields_set and payload.channels is not None
                else None
            ),
            sample_rate_hz=payload.sample_rate_hz,
            sample_count=payload.sample_count,
            threshold_v=payload.threshold_v,
            capture_ratio_percent=payload.capture_ratio_percent,
            triggers=(
                tuple(item.to_domain() for item in payload.triggers)
                if "triggers" in payload.model_fields_set and payload.triggers is not None
                else None
            ),
            auto_start_enabled=payload.auto_start_enabled,
            auto_start_delay_s=payload.auto_start_delay_s,
        )
        return _settings_out(context, device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{device_id}/captures/status", response_model=LogicCaptureStatusOut)
def logic_capture_status(
    device_id: str,
    context: ContextDep,
) -> LogicCaptureStatusOut:
    try:
        return LogicCaptureStatusOut.from_service(context.logic_analyzer_service.status(device_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/{device_id}/captures/{capture_id}",
    response_model=LogicCaptureStatusOut,
)
def logic_capture_by_id(
    device_id: str,
    capture_id: str,
    context: ContextDep,
) -> LogicCaptureStatusOut:
    try:
        return LogicCaptureStatusOut.from_service(
            context.logic_analyzer_service.capture_status(device_id, capture_id)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{device_id}/captures/start",
    response_model=LogicCaptureStatusOut,
    status_code=201,
    summary="Start an immediate triggerless logic capture",
)
async def start_logic_capture(
    device_id: str,
    payload: LogicCaptureStartIn,
    context: ContextDep,
) -> LogicCaptureStatusOut:
    try:
        status = await context.logic_analyzer_service.start_capture(
            device_id,
            hardware_trigger=False,
            title=payload.title,
            comment=payload.comment,
            source="api",
            recording_file=context.capture_service.status().current_file,
        )
        return LogicCaptureStatusOut.from_service(status)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/{device_id}/captures/arm",
    response_model=LogicCaptureStatusOut,
    status_code=201,
    summary="Arm a logic capture using the configured hardware trigger",
)
async def arm_logic_capture(
    device_id: str,
    payload: LogicCaptureStartIn,
    context: ContextDep,
) -> LogicCaptureStatusOut:
    try:
        status = await context.logic_analyzer_service.start_capture(
            device_id,
            hardware_trigger=True,
            title=payload.title,
            comment=payload.comment,
            source="api",
            recording_file=context.capture_service.status().current_file,
        )
        return LogicCaptureStatusOut.from_service(status)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{device_id}/captures/stop", response_model=LogicCaptureStatusOut)
async def stop_logic_capture(
    device_id: str,
    context: ContextDep,
) -> LogicCaptureStatusOut:
    try:
        return LogicCaptureStatusOut.from_service(
            await context.logic_analyzer_service.stop_capture(device_id)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/{device_id}/captures/{capture_id}/files/{filename}",
    response_class=FileResponse,
)
def download_logic_capture(
    device_id: str,
    capture_id: str,
    filename: str,
    context: ContextDep,
) -> FileResponse:
    try:
        path = context.logic_analyzer_service.resolve_artifact(
            device_id,
            capture_id,
            filename,
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Logic capture file not found") from exc
    media_type = (
        "application/zip"
        if path.suffix.casefold() == ".sr"
        else "application/json"
        if path.suffix.casefold() == ".json"
        else "application/octet-stream"
    )
    return FileResponse(path, media_type=media_type, filename=path.name)
