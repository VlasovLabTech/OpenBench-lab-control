from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, HTTPException

from openbench.api.dependencies import ContextDep
from openbench.api.schemas import (
    GeneratorBurstUpdateIn,
    GeneratorChannelUpdateIn,
    GeneratorCounterUpdateIn,
    GeneratorKeyingUpdateIn,
    GeneratorOutputsUpdateIn,
    GeneratorPresetActionOut,
    GeneratorSweepUpdateIn,
    GeneratorSynchronizationUpdateIn,
    GeneratorTriggerIn,
    SignalGeneratorOut,
)
from openbench.drivers.feeltech_fy import FeelTechChannelUpdate, FeelTechGeneratorState

router = APIRouter(
    prefix="/api/v1/generators",
    tags=["signal generators"],
)


async def _generator_out(
    device_id: str,
    context: ContextDep,
    *,
    state: FeelTechGeneratorState | None = None,
) -> SignalGeneratorOut:
    service = context.signal_generator_service
    if state is None:
        state = await service.state(device_id)
    synchronization = await service.synchronization(device_id)
    advanced = await service.advanced_state(device_id)
    return SignalGeneratorOut.from_driver(
        device_id=device_id,
        state=state,
        safety_state=context.matrix_service.safety_state().state,
        waveforms=service.waveform_options(device_id),
        synchronization=synchronization,
        advanced=advanced,
    )


def _raise_http(exc: Exception) -> NoReturn:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{device_id}", response_model=SignalGeneratorOut)
async def generator_state(device_id: str, context: ContextDep) -> SignalGeneratorOut:
    try:
        return await _generator_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch(
    "/{device_id}/channels/{channel}",
    response_model=SignalGeneratorOut,
)
async def update_generator_channel(
    device_id: str,
    channel: int,
    payload: GeneratorChannelUpdateIn,
    context: ContextDep,
) -> SignalGeneratorOut:
    if not payload.model_fields_set:
        raise HTTPException(status_code=400, detail="At least one channel field is required")
    try:
        state = await context.signal_generator_service.update_channel(
            device_id,
            channel,
            FeelTechChannelUpdate(**payload.model_dump()),
        )
        return await _generator_out(device_id, context, state=state)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.put("/{device_id}/outputs", response_model=SignalGeneratorOut)
async def update_generator_outputs(
    device_id: str,
    payload: GeneratorOutputsUpdateIn,
    context: ContextDep,
) -> SignalGeneratorOut:
    try:
        state = await context.signal_generator_service.set_outputs(
            device_id,
            channel_1=payload.channel_1,
            channel_2=payload.channel_2,
        )
        return await _generator_out(device_id, context, state=state)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/synchronization", response_model=SignalGeneratorOut)
async def update_generator_synchronization(
    device_id: str,
    payload: GeneratorSynchronizationUpdateIn,
    context: ContextDep,
) -> SignalGeneratorOut:
    try:
        await context.signal_generator_service.set_synchronization(
            device_id,
            payload.parameter,
            payload.enabled,
        )
        return await _generator_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/burst", response_model=SignalGeneratorOut)
async def update_generator_burst(
    device_id: str,
    payload: GeneratorBurstUpdateIn,
    context: ContextDep,
) -> SignalGeneratorOut:
    trigger_modes = {"off": 0, "ch2": 1, "external": 2}
    try:
        await context.signal_generator_service.configure_burst(
            device_id,
            trigger_mode=trigger_modes[payload.source],
            cycles=payload.cycles,
        )
        return await _generator_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/burst/trigger", response_model=SignalGeneratorOut)
async def trigger_generator_burst(
    device_id: str,
    payload: GeneratorTriggerIn,
    context: ContextDep,
) -> SignalGeneratorOut:
    try:
        await context.signal_generator_service.trigger_once(
            device_id,
            cycles=payload.cycles,
        )
        return await _generator_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/keying", response_model=SignalGeneratorOut)
async def update_generator_keying(
    device_id: str,
    payload: GeneratorKeyingUpdateIn,
    context: ContextDep,
) -> SignalGeneratorOut:
    modes = {"off": 0, "external": 1, "manual": 2}
    try:
        await context.signal_generator_service.configure_keying(
            device_id,
            kind=payload.kind,
            mode=modes[payload.source],
            secondary_frequency_hz=payload.secondary_frequency_hz,
        )
        return await _generator_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/counter", response_model=SignalGeneratorOut)
async def update_generator_counter(
    device_id: str,
    payload: GeneratorCounterUpdateIn,
    context: ContextDep,
) -> SignalGeneratorOut:
    gate_codes = {1: 0, 10: 1, 100: 2}
    try:
        await context.signal_generator_service.configure_counter(
            device_id,
            gate_code=gate_codes[payload.gate_time_s],
            coupling=payload.coupling,
            mode=payload.mode,
        )
        return await _generator_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/counter/pause", response_model=SignalGeneratorOut)
async def pause_generator_counter(
    device_id: str,
    context: ContextDep,
) -> SignalGeneratorOut:
    try:
        await context.signal_generator_service.pause_counter(device_id)
        return await _generator_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/counter/reset", response_model=SignalGeneratorOut)
async def reset_generator_counter(
    device_id: str,
    context: ContextDep,
) -> SignalGeneratorOut:
    try:
        await context.signal_generator_service.reset_counter(device_id)
        return await _generator_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/sweep", response_model=SignalGeneratorOut)
async def update_generator_sweep(
    device_id: str,
    payload: GeneratorSweepUpdateIn,
    context: ContextDep,
) -> SignalGeneratorOut:
    try:
        await context.signal_generator_service.configure_sweep(
            device_id,
            **payload.model_dump(),
        )
        return await _generator_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post(
    "/{device_id}/presets/{slot}/save",
    response_model=GeneratorPresetActionOut,
)
async def save_generator_preset(
    device_id: str,
    slot: int,
    context: ContextDep,
) -> GeneratorPresetActionOut:
    try:
        await context.signal_generator_service.save_preset(device_id, slot)
        return GeneratorPresetActionOut(device_id=device_id, slot=slot, action="saved")
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post(
    "/{device_id}/presets/{slot}/load",
    response_model=SignalGeneratorOut,
)
async def load_generator_preset(
    device_id: str,
    slot: int,
    context: ContextDep,
) -> SignalGeneratorOut:
    try:
        state = await context.signal_generator_service.load_preset(device_id, slot)
        return await _generator_out(device_id, context, state=state)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)
