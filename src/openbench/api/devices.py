from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi import APIRouter, HTTPException

from openbench.api.dependencies import ContextDep
from openbench.api.schemas import (
    ChannelOut,
    DeviceOut,
    DeviceSettingsOut,
    DeviceSettingsUpdate,
    MeasurementOut,
)
from openbench.bootstrap import (
    disconnect_device,
    register_dps150_devices,
    register_feeltech_devices,
    register_itech_it6000c_devices,
    register_kingst_devices,
    register_micsig_devices,
    register_micsig_eto_devices,
    register_owon_spm_devices,
    register_simulated_meter,
    register_ut61d_devices,
    register_ut61e_devices,
    register_ut61eplus_devices,
    register_ut197_devices,
)
from openbench.domain import Device
from openbench.drivers.micsig_common import is_micsig_scope_kind

router = APIRouter(prefix="/api/v1", tags=["devices"])


def _device_out(device: Device, context: ContextDep) -> DeviceOut:
    connected = (
        context.scope_measurement_service.device_connected(
            device.id,
            default=device.connected,
        )
        if is_micsig_scope_kind(device.kind)
        else context.scheduler.device_connected(
            device.id,
            default=device.connected,
        )
    )
    return DeviceOut.from_domain(replace(device, connected=connected))


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(context: ContextDep) -> list[DeviceOut]:
    return [_device_out(item, context) for item in context.registry.devices()]


@router.post("/devices/discover/{driver_id}", response_model=list[DeviceOut])
async def discover_devices(driver_id: str, context: ContextDep) -> list[DeviceOut]:
    try:
        if driver_id == "all":
            itech = await register_itech_it6000c_devices(context)
            owon = await register_owon_spm_devices(context)
            physical = await asyncio.gather(
                register_dps150_devices(context),
                register_feeltech_devices(context),
                register_ut197_devices(context),
                register_ut61d_devices(context),
                register_ut61eplus_devices(context),
                register_micsig_devices(context),
                register_micsig_eto_devices(context),
                register_kingst_devices(context),
            )
            devices = itech + owon + tuple(device for result in physical for device in result)
        elif driver_id == "simulated":
            devices = register_simulated_meter(context)
        else:
            discovery = {
                "ut197": register_ut197_devices,
                "ut61d": register_ut61d_devices,
                "ut61e": register_ut61e_devices,
                "ut61eplus": register_ut61eplus_devices,
                "feeltech": register_feeltech_devices,
                "dps150": register_dps150_devices,
                "owon_spm": register_owon_spm_devices,
                "itech_it6000c": register_itech_it6000c_devices,
                "micsig": register_micsig_devices,
                "micsig_eto": register_micsig_eto_devices,
                "kingst": register_kingst_devices,
            }.get(driver_id)
            if discovery is None:
                raise HTTPException(status_code=404, detail="Unknown device driver")
            devices = await discovery(context)
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not devices:
        raise HTTPException(status_code=404, detail="No matching instrument was found")
    return [_device_out(device, context) for device in devices]


@router.delete("/devices/{device_id}", response_model=DeviceOut)
async def disconnect_instrument(device_id: str, context: ContextDep) -> DeviceOut:
    try:
        device = await disconnect_device(context, device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return DeviceOut.from_domain(replace(device, connected=False))


@router.get("/devices/{device_id}/settings", response_model=DeviceSettingsOut)
def device_settings(device_id: str, context: ContextDep) -> DeviceSettingsOut:
    try:
        settings = context.instrument_settings_service.get(device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return DeviceSettingsOut.from_service(settings)


@router.patch("/devices/{device_id}/settings", response_model=DeviceSettingsOut)
async def update_device_settings(
    device_id: str,
    payload: DeviceSettingsUpdate,
    context: ContextDep,
) -> DeviceSettingsOut:
    service = context.instrument_settings_service
    try:
        service.get(device_id)
        if "context" in payload.model_fields_set and payload.context is not None:
            service.update_context(device_id, payload.context)
        if payload.poll_interval_s is not None:
            await service.update_poll_interval(device_id, payload.poll_interval_s)
        if {
            "scope_screen",
            "scope_data",
            "scope_channels",
            "scope_wait_for_trigger",
        } & payload.model_fields_set:
            service.update_scope_capture(
                device_id,
                screen=payload.scope_screen,
                data=payload.scope_data,
                channels=(
                    tuple(payload.scope_channels) if payload.scope_channels is not None else None
                ),
                wait_for_trigger=payload.scope_wait_for_trigger,
            )
        return DeviceSettingsOut.from_service(service.get(device_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/channels", response_model=list[ChannelOut])
def list_channels(context: ContextDep) -> list[ChannelOut]:
    return [ChannelOut.from_domain(item) for item in context.registry.channels()]


@router.get("/channels/{channel_id}/latest", response_model=MeasurementOut)
def latest_measurement(
    channel_id: str,
    context: ContextDep,
) -> MeasurementOut:
    measurement = context.measurement_service.latest(channel_id)
    if measurement is None:
        raise HTTPException(status_code=404, detail="No measurement is available for this channel")
    return MeasurementOut.from_domain(measurement)
