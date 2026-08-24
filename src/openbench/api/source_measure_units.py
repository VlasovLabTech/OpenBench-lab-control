from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, HTTPException

from openbench.api.dependencies import ContextDep
from openbench.api.schemas import (
    SourceMeasureMultimeterUpdateIn,
    SourceMeasureOutputUpdateIn,
    SourceMeasureProtectionUpdateIn,
    SourceMeasureUnitOut,
)
from openbench.drivers.owon_spm import (
    OwonSPMDMMUpdate,
    OwonSPMInstrument,
    OwonSPMOutputUpdate,
    OwonSPMProtectionUpdate,
)

router = APIRouter(prefix="/api/v1/source-measure-units", tags=["Source-measure units"])


def _raise_http(exc: Exception) -> NoReturn:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (OSError, TimeoutError)):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _state_out(device_id: str, context: ContextDep) -> SourceMeasureUnitOut:
    instrument = context.registry.instrument(device_id)
    if not isinstance(instrument, OwonSPMInstrument):
        raise ValueError(f"Device is not a supported source-measure unit: {device_id}")
    state = await context.source_measure_unit_service.state(device_id)
    return SourceMeasureUnitOut.from_driver(
        device_id=device_id,
        identity=instrument.identity,
        state=state,
        safety_state=context.matrix_service.safety_state().state,
    )


@router.get("", response_model=list[SourceMeasureUnitOut])
async def list_source_measure_units(context: ContextDep) -> list[SourceMeasureUnitOut]:
    result: list[SourceMeasureUnitOut] = []
    for device_id in context.source_measure_unit_service.device_ids():
        result.append(await _state_out(device_id, context))
    return result


@router.get("/{device_id}", response_model=SourceMeasureUnitOut)
async def get_source_measure_unit(device_id: str, context: ContextDep) -> SourceMeasureUnitOut:
    try:
        return await _state_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/output", response_model=SourceMeasureUnitOut)
async def update_source_measure_output(
    device_id: str, payload: SourceMeasureOutputUpdateIn, context: ContextDep
) -> SourceMeasureUnitOut:
    try:
        await context.source_measure_unit_service.update_output(
            device_id, OwonSPMOutputUpdate(**payload.model_dump())
        )
        return await _state_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/protections", response_model=SourceMeasureUnitOut)
async def update_source_measure_protections(
    device_id: str, payload: SourceMeasureProtectionUpdateIn, context: ContextDep
) -> SourceMeasureUnitOut:
    try:
        await context.source_measure_unit_service.update_protections(
            device_id, OwonSPMProtectionUpdate(**payload.model_dump())
        )
        return await _state_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/multimeter", response_model=SourceMeasureUnitOut)
async def update_source_measure_multimeter(
    device_id: str, payload: SourceMeasureMultimeterUpdateIn, context: ContextDep
) -> SourceMeasureUnitOut:
    try:
        await context.source_measure_unit_service.update_multimeter(
            device_id, OwonSPMDMMUpdate(**payload.model_dump())
        )
        return await _state_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)
