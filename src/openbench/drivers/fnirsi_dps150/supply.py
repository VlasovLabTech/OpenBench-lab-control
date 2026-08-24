from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Protocol

from openbench.core.capabilities import MeterSample
from openbench.drivers.fnirsi_dps150.protocol import (
    DPS150_PARAMETERS,
    TYPE_ALL,
    TYPE_BRIGHTNESS,
    TYPE_FIRMWARE_VERSION,
    TYPE_HARDWARE_VERSION,
    TYPE_LVP,
    TYPE_METERING_ENABLE,
    TYPE_MODEL,
    TYPE_OCP,
    TYPE_OPP,
    TYPE_OTP,
    TYPE_OUTPUT_ENABLE,
    TYPE_OVP,
    TYPE_SET_CURRENT,
    TYPE_SET_VOLTAGE,
    TYPE_VOLUME,
    DPS150DisplayUpdate,
    DPS150Identity,
    DPS150MeterParameter,
    DPS150OutputUpdate,
    DPS150ProtectionSettings,
    DPS150ProtectionUpdate,
    DPS150State,
    parse_all_state,
    parse_identity,
    preset_type,
)
from openbench.drivers.fnirsi_dps150.transport import (
    DPS150Descriptor,
    DPS150SerialTransport,
)

STATE_CACHE_S = 0.25
VOLTAGE_STEP_V = 0.01
CURRENT_STEP_A = 0.001
MAX_OUTPUT_VOLTAGE_V = 30.0
MAX_OUTPUT_CURRENT_A = 5.0
MAX_OVER_CURRENT_A = 5.1
MAX_OUTPUT_POWER_W = 150.0
MAX_TEMPERATURE_C = 100.0
MAX_DISPLAY_LEVEL = 10


class DPS150Transport(Protocol):
    def query(self, data_type: int) -> bytes: ...

    def write_float(self, data_type: int, value: float) -> None: ...

    def write_byte(self, data_type: int, value: int) -> None: ...

    def close(self) -> None: ...


class FNIRSIDPS150:
    def __init__(
        self,
        descriptor: DPS150Descriptor,
        *,
        transport: DPS150Transport | None = None,
    ) -> None:
        identity = descriptor.serial_number or descriptor.location or descriptor.port
        normalized = re.sub(r"[^a-z0-9]+", "_", identity.casefold()).strip("_")
        self.device_id = f"fnirsi_dps150_{normalized or 'serial'}"
        self.descriptor = descriptor
        self._transport = transport or DPS150SerialTransport(descriptor)
        self._identity: DPS150Identity | None = None
        self._cached_state: DPS150State | None = None
        self._cached_at = 0.0
        self._operation_lock = asyncio.Lock()
        self._parameters_by_id = {
            f"{self.device_id}.{parameter.channel_suffix}": parameter
            for parameter in DPS150_PARAMETERS
        }

    @property
    def parameters(self) -> tuple[tuple[str, DPS150MeterParameter], ...]:
        return tuple(self._parameters_by_id.items())

    @property
    def identity(self) -> DPS150Identity | None:
        return self._identity

    @property
    def cached_state(self) -> DPS150State | None:
        return self._cached_state

    async def identify(self) -> str:
        async with self._operation_lock:
            identity = await asyncio.to_thread(self._identify_sync)
            self._identity = identity
        return (
            f"FNIRSI {identity.model} HW {identity.hardware_version} "
            f"FW {identity.firmware_version} on {self.descriptor.port}"
        )

    def _identify_sync(self) -> DPS150Identity:
        return parse_identity(
            self._transport.query(TYPE_MODEL),
            self._transport.query(TYPE_HARDWARE_VERSION),
            self._transport.query(TYPE_FIRMWARE_VERSION),
        )

    async def read_state(self, *, force: bool = False) -> DPS150State:
        async with self._operation_lock:
            return await self._read_state_locked(force=force)

    async def _read_state_locked(self, *, force: bool) -> DPS150State:
        now = time.monotonic()
        if not force and self._cached_state is not None and now - self._cached_at <= STATE_CACHE_S:
            return self._cached_state
        payload = await asyncio.to_thread(self._transport.query, TYPE_ALL)
        state = parse_all_state(payload)
        self._cached_state = state
        self._cached_at = time.monotonic()
        return state

    async def read_meter(self, channel_id: str) -> MeterSample:
        try:
            parameter = self._parameters_by_id[channel_id]
        except KeyError as exc:
            raise ValueError(f"Unknown DPS-150 channel: {channel_id}") from exc
        state = await self.read_state()
        return self._sample(parameter, state)

    @staticmethod
    def _sample(parameter: DPS150MeterParameter, state: DPS150State) -> MeterSample:
        values: dict[str, float | None] = {
            "input_voltage": state.input_voltage_v,
            "set_voltage": state.set_voltage_v,
            "set_current": state.set_current_a,
            "output_voltage": state.output_voltage_v,
            "output_current": state.output_current_a,
            "output_power": state.output_power_w,
            "temperature": state.temperature_c,
            "capacity": state.output_capacity_ah,
            "energy": state.output_energy_wh,
            "output": float(state.output_enabled),
            "mode": 0.0 if state.mode == "CC" else 1.0,
            "protection": float(state.protection_code),
            "metering": float(state.metering_enabled),
            "available_voltage": state.upper_voltage_v,
            "available_current": state.upper_current_a,
        }
        quality = {
            "output": "ON" if state.output_enabled else "OFF",
            "mode": state.mode,
            "protection": state.protection,
            "metering": "ON" if state.metering_enabled else "OFF",
        }.get(parameter.key, "device_reported")
        status = "ok"
        if parameter.key == "protection" and state.protection_code:
            status = "protection"
        elif parameter.key in {"capacity", "energy"} and not state.metering_enabled:
            status = "paused"
        return MeterSample(
            value=values[parameter.key],
            unit=parameter.unit,
            mode=quality,
            status=status,
        )

    @staticmethod
    def _validate_step(value: float, step: float, field: str) -> None:
        rounded = round(value / step) * step
        if not math.isclose(value, rounded, rel_tol=0, abs_tol=step / 100):
            raise ValueError(f"DPS-150 {field} must use {step:g} increments")

    @staticmethod
    def _require_range(value: float, minimum: float, maximum: float, field: str) -> None:
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"DPS-150 {field} must be between {minimum:g} and {maximum:g}")

    def _validate_output_update(
        self,
        current: DPS150State,
        update: DPS150OutputUpdate,
    ) -> tuple[float, float, bool]:
        voltage = current.set_voltage_v if update.voltage_v is None else update.voltage_v
        current_limit = current.set_current_a if update.current_a is None else update.current_a
        enabled = current.output_enabled if update.enabled is None else update.enabled
        voltage_max = min(MAX_OUTPUT_VOLTAGE_V, max(0.0, current.upper_voltage_v))
        current_max = min(MAX_OUTPUT_CURRENT_A, max(0.0, current.upper_current_a))
        self._require_range(voltage, 0.0, voltage_max, "voltage")
        self._require_range(current_limit, 0.0, current_max, "current")
        self._validate_step(voltage, VOLTAGE_STEP_V, "voltage")
        self._validate_step(current_limit, CURRENT_STEP_A, "current")
        if voltage * current_limit > MAX_OUTPUT_POWER_W + 1e-9:
            raise ValueError(f"DPS-150 voltage/current setpoints exceed {MAX_OUTPUT_POWER_W:g} W")
        return voltage, current_limit, enabled

    async def update_output(self, update: DPS150OutputUpdate) -> DPS150State:
        if update == DPS150OutputUpdate():
            raise ValueError("DPS-150 output update has no fields")
        async with self._operation_lock:
            current = await self._read_state_locked(force=True)
            voltage, current_limit, enabled = self._validate_output_update(current, update)

            # If this update turns an active output off, remove power before
            # changing either setpoint.
            if update.enabled is False and current.output_enabled:
                await asyncio.to_thread(
                    self._transport.write_byte,
                    TYPE_OUTPUT_ENABLE,
                    0,
                )

            # When both values change, reduce a limit before increasing the
            # other dimension. This avoids a transient less restrictive state.
            writes: list[tuple[int, float]] = []
            if update.current_a is not None and current_limit < current.set_current_a:
                writes.append((TYPE_SET_CURRENT, current_limit))
            if update.voltage_v is not None:
                writes.append((TYPE_SET_VOLTAGE, voltage))
            if update.current_a is not None and not any(
                data_type == TYPE_SET_CURRENT for data_type, _ in writes
            ):
                writes.append((TYPE_SET_CURRENT, current_limit))

            for data_type, value in writes:
                await asyncio.to_thread(self._transport.write_float, data_type, value)
            if update.enabled is not None and not (
                update.enabled is False and current.output_enabled
            ):
                await asyncio.to_thread(
                    self._transport.write_byte,
                    TYPE_OUTPUT_ENABLE,
                    int(enabled),
                )
            updated = await self._read_state_locked(force=True)
            self._verify_float("voltage", voltage, updated.set_voltage_v, VOLTAGE_STEP_V)
            self._verify_float("current", current_limit, updated.set_current_a, CURRENT_STEP_A)
            if updated.output_enabled != enabled:
                raise RuntimeError(
                    f"DPS-150 rejected output state: requested {'ON' if enabled else 'OFF'}, "
                    f"read back {'ON' if updated.output_enabled else 'OFF'}"
                )
            return updated

    @staticmethod
    def _verify_float(field: str, expected: float, actual: float, resolution: float) -> None:
        if not math.isclose(expected, actual, rel_tol=1e-5, abs_tol=resolution / 2):
            raise RuntimeError(
                f"DPS-150 rejected {field}: requested {expected:g}, read back {actual:g}"
            )

    def _validate_protections(
        self,
        current: DPS150State,
        update: DPS150ProtectionUpdate,
    ) -> DPS150ProtectionSettings:
        value = current.protections
        proposed = DPS150ProtectionSettings(
            over_voltage_v=(
                value.over_voltage_v if update.over_voltage_v is None else update.over_voltage_v
            ),
            over_current_a=(
                value.over_current_a if update.over_current_a is None else update.over_current_a
            ),
            over_power_w=(
                value.over_power_w if update.over_power_w is None else update.over_power_w
            ),
            over_temperature_c=(
                value.over_temperature_c
                if update.over_temperature_c is None
                else update.over_temperature_c
            ),
            low_input_voltage_v=(
                value.low_input_voltage_v
                if update.low_input_voltage_v is None
                else update.low_input_voltage_v
            ),
        )
        self._require_range(
            proposed.over_voltage_v,
            0,
            MAX_OUTPUT_VOLTAGE_V,
            "OVP",
        )
        self._require_range(
            proposed.over_current_a,
            0,
            MAX_OVER_CURRENT_A,
            "OCP",
        )
        self._require_range(proposed.over_power_w, 0, MAX_OUTPUT_POWER_W, "OPP")
        self._require_range(
            proposed.over_temperature_c,
            0,
            MAX_TEMPERATURE_C,
            "OTP",
        )
        self._require_range(
            proposed.low_input_voltage_v,
            0,
            MAX_OUTPUT_VOLTAGE_V,
            "LVP",
        )
        if proposed.over_voltage_v + 1e-6 < current.set_voltage_v:
            raise ValueError("DPS-150 OVP must not be below the voltage setpoint")
        if proposed.over_current_a + 1e-6 < current.set_current_a:
            raise ValueError("DPS-150 OCP must not be below the current setpoint")
        return proposed

    async def update_protections(
        self,
        update: DPS150ProtectionUpdate,
    ) -> DPS150State:
        if update == DPS150ProtectionUpdate():
            raise ValueError("DPS-150 protection update has no fields")
        async with self._operation_lock:
            current = await self._read_state_locked(force=True)
            proposed = self._validate_protections(current, update)
            requested = (
                (TYPE_OVP, update.over_voltage_v, proposed.over_voltage_v, "OVP", 0.01),
                (TYPE_OCP, update.over_current_a, proposed.over_current_a, "OCP", 0.001),
                (TYPE_OPP, update.over_power_w, proposed.over_power_w, "OPP", 0.1),
                (
                    TYPE_OTP,
                    update.over_temperature_c,
                    proposed.over_temperature_c,
                    "OTP",
                    0.1,
                ),
                (
                    TYPE_LVP,
                    update.low_input_voltage_v,
                    proposed.low_input_voltage_v,
                    "LVP",
                    0.01,
                ),
            )
            for data_type, supplied, target, _, _ in requested:
                if supplied is not None:
                    await asyncio.to_thread(self._transport.write_float, data_type, target)
            updated = await self._read_state_locked(force=True)
            actuals = (
                updated.protections.over_voltage_v,
                updated.protections.over_current_a,
                updated.protections.over_power_w,
                updated.protections.over_temperature_c,
                updated.protections.low_input_voltage_v,
            )
            for (_, supplied, target, field, resolution), actual in zip(
                requested,
                actuals,
                strict=True,
            ):
                if supplied is not None:
                    self._verify_float(field, target, actual, resolution)
            return updated

    async def update_display(self, update: DPS150DisplayUpdate) -> DPS150State:
        if update == DPS150DisplayUpdate():
            raise ValueError("DPS-150 display update has no fields")
        for name, value in (("brightness", update.brightness), ("volume", update.volume)):
            if value is not None and not 0 <= value <= MAX_DISPLAY_LEVEL:
                raise ValueError(f"DPS-150 {name} must be between 0 and {MAX_DISPLAY_LEVEL}")
        async with self._operation_lock:
            if update.brightness is not None:
                await asyncio.to_thread(
                    self._transport.write_byte,
                    TYPE_BRIGHTNESS,
                    update.brightness,
                )
            if update.volume is not None:
                await asyncio.to_thread(
                    self._transport.write_byte,
                    TYPE_VOLUME,
                    update.volume,
                )
            updated = await self._read_state_locked(force=True)
            if update.brightness is not None and updated.brightness != update.brightness:
                raise RuntimeError(
                    "DPS-150 rejected brightness: "
                    f"requested {update.brightness}, read back {updated.brightness}"
                )
            if update.volume is not None and updated.volume != update.volume:
                raise RuntimeError(
                    "DPS-150 rejected volume: "
                    f"requested {update.volume}, read back {updated.volume}"
                )
            return updated

    async def set_metering(self, enabled: bool) -> DPS150State:
        async with self._operation_lock:
            await asyncio.to_thread(
                self._transport.write_byte,
                TYPE_METERING_ENABLE,
                int(enabled),
            )
            updated = await self._read_state_locked(force=True)
            if updated.metering_enabled != enabled:
                raise RuntimeError(
                    f"DPS-150 rejected metering state: requested {'ON' if enabled else 'OFF'}"
                )
            return updated

    async def save_preset(
        self,
        slot: int,
        *,
        voltage_v: float,
        current_a: float,
    ) -> DPS150State:
        async with self._operation_lock:
            current = await self._read_state_locked(force=True)
            self._validate_output_update(
                current,
                DPS150OutputUpdate(voltage_v=voltage_v, current_a=current_a),
            )
            await asyncio.to_thread(
                self._transport.write_float,
                preset_type(slot, current=False),
                voltage_v,
            )
            await asyncio.to_thread(
                self._transport.write_float,
                preset_type(slot, current=True),
                current_a,
            )
            updated = await self._read_state_locked(force=True)
            preset = updated.presets[slot - 1]
            self._verify_float("preset voltage", voltage_v, preset.voltage_v, VOLTAGE_STEP_V)
            self._verify_float("preset current", current_a, preset.current_a, CURRENT_STEP_A)
            return updated

    async def apply_preset(
        self,
        slot: int,
        *,
        enabled: bool | None = None,
    ) -> DPS150State:
        if not 1 <= slot <= 6:
            raise ValueError("DPS-150 preset slot must be between 1 and 6")
        current = await self.read_state(force=True)
        preset = current.presets[slot - 1]
        return await self.update_output(
            DPS150OutputUpdate(
                voltage_v=preset.voltage_v,
                current_a=preset.current_a,
                enabled=enabled,
            )
        )

    async def close(self) -> None:
        async with self._operation_lock:
            await asyncio.to_thread(self._transport.close)
