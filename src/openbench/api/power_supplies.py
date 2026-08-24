from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, HTTPException

from openbench.api.dependencies import ContextDep
from openbench.api.schemas import (
    PowerProgramStatusOut,
    PowerProgramStopIn,
    PowerSequenceStartIn,
    PowerSupplyDisplayUpdateIn,
    PowerSupplyMeteringUpdateIn,
    PowerSupplyOut,
    PowerSupplyOutputUpdateIn,
    PowerSupplyPresetApplyIn,
    PowerSupplyPresetUpdateIn,
    PowerSupplyProtectionUpdateIn,
    PowerSweepStartIn,
)
from openbench.drivers.fnirsi_dps150 import (
    FNIRSIDPS150,
    DPS150DisplayUpdate,
    DPS150OutputUpdate,
    DPS150ProtectionUpdate,
)
from openbench.services.dc_power_supply_service import PowerSequenceStep

router = APIRouter(prefix="/api/v1/power-supplies", tags=["DC power supplies"])


def _raise_http(exc: Exception) -> NoReturn:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError) and "already active" in str(exc):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError) and "No power-supply program" in str(exc):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, (OSError, TimeoutError)):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _power_supply_out(device_id: str, context: ContextDep) -> PowerSupplyOut:
    instrument = context.registry.instrument(device_id)
    if not isinstance(instrument, FNIRSIDPS150):
        raise ValueError(f"Device is not a supported DC power supply: {device_id}")
    state = await context.dc_power_supply_service.state(device_id)
    return PowerSupplyOut.from_driver(
        device_id=device_id,
        identity=instrument.identity,
        state=state,
        safety_state=context.matrix_service.safety_state().state,
    )


@router.get("/{device_id}", response_model=PowerSupplyOut)
async def get_power_supply(
    device_id: str,
    context: ContextDep,
) -> PowerSupplyOut:
    try:
        return await _power_supply_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/output", response_model=PowerSupplyOut)
async def update_power_supply_output(
    device_id: str,
    payload: PowerSupplyOutputUpdateIn,
    context: ContextDep,
) -> PowerSupplyOut:
    try:
        await context.dc_power_supply_service.update_output(
            device_id,
            DPS150OutputUpdate(**payload.model_dump()),
        )
        return await _power_supply_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/protections", response_model=PowerSupplyOut)
async def update_power_supply_protections(
    device_id: str,
    payload: PowerSupplyProtectionUpdateIn,
    context: ContextDep,
) -> PowerSupplyOut:
    try:
        await context.dc_power_supply_service.update_protections(
            device_id,
            DPS150ProtectionUpdate(**payload.model_dump()),
        )
        return await _power_supply_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/display", response_model=PowerSupplyOut)
async def update_power_supply_display(
    device_id: str,
    payload: PowerSupplyDisplayUpdateIn,
    context: ContextDep,
) -> PowerSupplyOut:
    try:
        await context.dc_power_supply_service.update_display(
            device_id,
            DPS150DisplayUpdate(**payload.model_dump()),
        )
        return await _power_supply_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/metering", response_model=PowerSupplyOut)
async def update_power_supply_metering(
    device_id: str,
    payload: PowerSupplyMeteringUpdateIn,
    context: ContextDep,
) -> PowerSupplyOut:
    try:
        await context.dc_power_supply_service.set_metering(device_id, payload.enabled)
        return await _power_supply_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.put("/{device_id}/presets/{slot}", response_model=PowerSupplyOut)
async def save_power_supply_preset(
    device_id: str,
    slot: int,
    payload: PowerSupplyPresetUpdateIn,
    context: ContextDep,
) -> PowerSupplyOut:
    try:
        await context.dc_power_supply_service.save_preset(
            device_id,
            slot,
            voltage_v=payload.voltage_v,
            current_a=payload.current_a,
        )
        return await _power_supply_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/presets/{slot}/apply", response_model=PowerSupplyOut)
async def apply_power_supply_preset(
    device_id: str,
    slot: int,
    context: ContextDep,
    payload: PowerSupplyPresetApplyIn | None = None,
) -> PowerSupplyOut:
    try:
        await context.dc_power_supply_service.apply_preset(
            device_id,
            slot,
            enabled=None if payload is None else payload.enabled,
        )
        return await _power_supply_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/programs/sequence", response_model=PowerProgramStatusOut)
async def start_power_sequence(
    device_id: str,
    payload: PowerSequenceStartIn,
    context: ContextDep,
) -> PowerProgramStatusOut:
    try:
        status = await context.dc_power_supply_service.start_sequence(
            device_id,
            steps=tuple(
                PowerSequenceStep(
                    voltage_v=step.voltage_v,
                    current_a=step.current_a,
                    dwell_s=step.dwell_s,
                )
                for step in payload.steps
            ),
            loops=payload.loops,
        )
        return PowerProgramStatusOut.from_service(status)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/programs/sweep", response_model=PowerProgramStatusOut)
async def start_power_sweep(
    device_id: str,
    payload: PowerSweepStartIn,
    context: ContextDep,
) -> PowerProgramStatusOut:
    try:
        status = await context.dc_power_supply_service.start_sweep(
            device_id,
            **payload.model_dump(),
        )
        return PowerProgramStatusOut.from_service(status)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.get("/{device_id}/programs/status", response_model=PowerProgramStatusOut)
def get_power_program_status(
    device_id: str,
    context: ContextDep,
) -> PowerProgramStatusOut:
    try:
        return PowerProgramStatusOut.from_service(
            context.dc_power_supply_service.program_status(device_id)
        )
    except (KeyError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/programs/pause", response_model=PowerProgramStatusOut)
async def pause_power_program(
    device_id: str,
    context: ContextDep,
) -> PowerProgramStatusOut:
    try:
        return PowerProgramStatusOut.from_service(
            await context.dc_power_supply_service.pause_program(device_id)
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/programs/resume", response_model=PowerProgramStatusOut)
async def resume_power_program(
    device_id: str,
    context: ContextDep,
) -> PowerProgramStatusOut:
    try:
        return PowerProgramStatusOut.from_service(
            await context.dc_power_supply_service.resume_program(device_id)
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/programs/stop", response_model=PowerProgramStatusOut)
async def stop_power_program(
    device_id: str,
    context: ContextDep,
    payload: PowerProgramStopIn | None = None,
) -> PowerProgramStatusOut:
    try:
        return PowerProgramStatusOut.from_service(
            await context.dc_power_supply_service.stop_program(
                device_id,
                output_off=True if payload is None else payload.output_off,
            )
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)
