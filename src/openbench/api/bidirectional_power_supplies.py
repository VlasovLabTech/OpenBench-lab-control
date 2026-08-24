from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, HTTPException

from openbench.api.dependencies import ContextDep
from openbench.api.schemas import (
    BidirectionalAdvancedUpdateIn,
    BidirectionalExperimentReservationOut,
    BidirectionalMeasurementsOut,
    BidirectionalOperatingPointUpdateIn,
    BidirectionalPowerSupplyOut,
    BidirectionalProtectionUpdateIn,
)
from openbench.drivers.itech_it6000c import (
    ITechAdvancedUpdate,
    ITechIT6000C,
    ITechIT6000CState,
    ITechOperatingPointUpdate,
    ITechProtectionUpdate,
)

router = APIRouter(
    prefix="/api/v1/bidirectional-power-supplies",
    tags=["Bidirectional power supplies"],
)


def _raise_http(exc: Exception) -> NoReturn:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (OSError, TimeoutError)):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _state_out(device_id: str, context: ContextDep) -> BidirectionalPowerSupplyOut:
    instrument = context.registry.instrument(device_id)
    if not isinstance(instrument, ITechIT6000C):
        raise ValueError(f"Device is not an ITECH IT6000C: {device_id}")
    state = await context.bidirectional_power_supply_service.state(device_id)
    return _state_value_out(device_id, instrument, state, context)


def _state_value_out(
    device_id: str,
    instrument: ITechIT6000C,
    state: ITechIT6000CState,
    context: ContextDep,
) -> BidirectionalPowerSupplyOut:
    return BidirectionalPowerSupplyOut.from_driver(
        device_id=device_id,
        instrument=instrument,
        state=state,
        safety_state=context.matrix_service.safety_state().state,
    )


@router.get("", response_model=list[BidirectionalPowerSupplyOut])
async def list_bidirectional_power_supplies(
    context: ContextDep,
) -> list[BidirectionalPowerSupplyOut]:
    result: list[BidirectionalPowerSupplyOut] = []
    for device_id in context.bidirectional_power_supply_service.device_ids():
        result.append(await _state_out(device_id, context))
    return result


@router.get("/{device_id}", response_model=BidirectionalPowerSupplyOut)
async def get_bidirectional_power_supply(
    device_id: str, context: ContextDep
) -> BidirectionalPowerSupplyOut:
    try:
        return await _state_out(device_id, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.get("/{device_id}/measurements", response_model=BidirectionalMeasurementsOut)
async def get_bidirectional_measurements(
    device_id: str, context: ContextDep
) -> BidirectionalMeasurementsOut:
    try:
        measurements = await context.bidirectional_power_supply_service.measurements(device_id)
        values = {
            measurement.channel_id.rsplit(".", 1)[-1]: measurement.value
            for measurement in measurements
        }
        return BidirectionalMeasurementsOut(
            device_id=device_id,
            timestamp_utc=max(measurement.timestamp_utc for measurement in measurements),
            measured_voltage_v=values["voltage"],
            measured_current_a=values["current"],
            measured_power_w=values["power"],
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post(
    "/{device_id}/experiment-reservation",
    response_model=BidirectionalExperimentReservationOut,
)
async def reserve_bidirectional_experiment(
    device_id: str,
    context: ContextDep,
) -> BidirectionalExperimentReservationOut:
    """Suspend ordinary Dashboard polling for an external experiment."""
    try:
        suspended = await context.bidirectional_power_supply_service.reserve_experiment(
            device_id
        )
        return BidirectionalExperimentReservationOut(
            device_id=device_id,
            active=True,
            polling_targets_suspended=suspended,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.delete(
    "/{device_id}/experiment-reservation",
    response_model=BidirectionalExperimentReservationOut,
)
async def release_bidirectional_experiment(
    device_id: str,
    context: ContextDep,
) -> BidirectionalExperimentReservationOut:
    """Resume ordinary polling after external-experiment cleanup."""
    try:
        resumed = await context.bidirectional_power_supply_service.release_experiment(device_id)
        return BidirectionalExperimentReservationOut(
            device_id=device_id,
            active=False,
            polling_targets_suspended=resumed,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/operating-point", response_model=BidirectionalPowerSupplyOut)
async def update_bidirectional_operating_point(
    device_id: str,
    payload: BidirectionalOperatingPointUpdateIn,
    context: ContextDep,
) -> BidirectionalPowerSupplyOut:
    try:
        state = await context.bidirectional_power_supply_service.update_operating_point(
            device_id,
            ITechOperatingPointUpdate(**payload.model_dump()),
        )
        instrument = context.registry.instrument(device_id)
        if not isinstance(instrument, ITechIT6000C):
            raise ValueError(f"Device is not an ITECH IT6000C: {device_id}")
        return _state_value_out(device_id, instrument, state, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/protections", response_model=BidirectionalPowerSupplyOut)
async def update_bidirectional_protections(
    device_id: str,
    payload: BidirectionalProtectionUpdateIn,
    context: ContextDep,
) -> BidirectionalPowerSupplyOut:
    try:
        state = await context.bidirectional_power_supply_service.update_protections(
            device_id,
            ITechProtectionUpdate(**payload.model_dump()),
        )
        instrument = context.registry.instrument(device_id)
        if not isinstance(instrument, ITechIT6000C):
            raise ValueError(f"Device is not an ITECH IT6000C: {device_id}")
        return _state_value_out(device_id, instrument, state, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.post("/{device_id}/protections/clear", response_model=BidirectionalPowerSupplyOut)
async def clear_bidirectional_protection(
    device_id: str,
    context: ContextDep,
) -> BidirectionalPowerSupplyOut:
    try:
        state = await context.bidirectional_power_supply_service.clear_protection(device_id)
        instrument = context.registry.instrument(device_id)
        if not isinstance(instrument, ITechIT6000C):
            raise ValueError(f"Device is not an ITECH IT6000C: {device_id}")
        return _state_value_out(device_id, instrument, state, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)


@router.patch("/{device_id}/advanced", response_model=BidirectionalPowerSupplyOut)
async def update_bidirectional_advanced(
    device_id: str,
    payload: BidirectionalAdvancedUpdateIn,
    context: ContextDep,
) -> BidirectionalPowerSupplyOut:
    try:
        state = await context.bidirectional_power_supply_service.update_advanced(
            device_id,
            ITechAdvancedUpdate(**payload.model_dump()),
        )
        instrument = context.registry.instrument(device_id)
        if not isinstance(instrument, ITechIT6000C):
            raise ValueError(f"Device is not an ITECH IT6000C: {device_id}")
        return _state_value_out(device_id, instrument, state, context)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _raise_http(exc)
