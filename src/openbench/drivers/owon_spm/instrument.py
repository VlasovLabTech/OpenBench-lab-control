from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from openbench.core.capabilities import MeterSample
from openbench.drivers.owon_spm.protocol import (
    DMM_AUTO_RANGE_FUNCTIONS,
    DMM_FUNCTIONS,
    DMM_RANGES,
    DMM_RELATIVE_FUNCTIONS,
    OWON_SPM_PARAMETERS,
    OwonSPMDMMState,
    OwonSPMDMMUpdate,
    OwonSPMIdentity,
    OwonSPMMeterParameter,
    OwonSPMOutputUpdate,
    OwonSPMProtectionUpdate,
    OwonSPMSourceState,
    OwonSPMState,
    parse_dmm_state,
    parse_float,
    parse_identity,
    parse_measurement_info,
    parse_on_off,
)
from openbench.drivers.owon_spm.transport import OwonSPMDescriptor, OwonSPMSerialTransport

STATE_CACHE_S = 0.25
MAX_OUTPUT_VOLTAGE_V = 60.0
MAX_OUTPUT_CURRENT_A = 10.0
MAX_OUTPUT_POWER_W = 300.0
MAX_OVP_V = 62.0
MAX_OCP_A = 10.0
VOLTAGE_STEP_V = 0.01
CURRENT_STEP_A = 0.001


class OwonSPMTransport(Protocol):
    def query(self, command: str) -> str: ...

    def write(self, command: str) -> None: ...

    def close(self) -> None: ...


class OwonSPMInstrument:
    def __init__(
        self,
        descriptor: OwonSPMDescriptor,
        *,
        transport: OwonSPMTransport | None = None,
    ) -> None:
        identity_key = descriptor.identity.serial_number or descriptor.location or descriptor.port
        normalized = re.sub(r"[^a-z0-9]+", "_", identity_key.casefold()).strip("_")
        model = re.sub(r"[^a-z0-9]+", "_", descriptor.identity.model.casefold()).strip("_")
        self.device_id = f"owon_{model}_{normalized or 'serial'}"
        self.descriptor = descriptor
        self._transport = transport or OwonSPMSerialTransport(descriptor)
        self._identity: OwonSPMIdentity = descriptor.identity
        self._cached_state: OwonSPMState | None = None
        self._cached_at = 0.0
        self._operation_lock = asyncio.Lock()
        self._parameters_by_id = {
            f"{self.device_id}.{parameter.channel_suffix}": parameter
            for parameter in OWON_SPM_PARAMETERS
        }

    @property
    def identity(self) -> OwonSPMIdentity:
        return self._identity

    @property
    def parameters(self) -> tuple[tuple[str, OwonSPMMeterParameter], ...]:
        return tuple(self._parameters_by_id.items())

    @property
    def cached_state(self) -> OwonSPMState | None:
        return self._cached_state

    async def identify(self) -> str:
        async with self._operation_lock:
            identity = parse_identity(await asyncio.to_thread(self._transport.query, "*IDN?"))
            if identity.serial_number != self.descriptor.identity.serial_number:
                raise RuntimeError("OWON SPM serial number changed after discovery")
            self._identity = identity
        return (
            f"{identity.manufacturer} {identity.model} SN {identity.serial_number} "
            f"{identity.firmware_version} on {self.descriptor.port}"
        )

    async def read_state(self, *, force: bool = False) -> OwonSPMState:
        async with self._operation_lock:
            return await self._read_state_locked(force=force)

    async def _read_state_locked(self, *, force: bool) -> OwonSPMState:
        now = time.monotonic()
        if not force and self._cached_state is not None and now - self._cached_at <= STATE_CACHE_S:
            return self._cached_state
        source = await asyncio.to_thread(self._read_source_sync)
        multimeter = await asyncio.to_thread(self._read_dmm_sync)
        state = OwonSPMState(source=source, multimeter=multimeter)
        self._cached_state = state
        self._cached_at = time.monotonic()
        return state

    def _read_source_sync(self) -> OwonSPMSourceState:
        measured = parse_measurement_info(self._transport.query("MEAS:ALL:INFO?"))
        return OwonSPMSourceState(
            set_voltage_v=parse_float(self._transport.query("VOLT?"), "voltage setpoint"),
            set_current_a=parse_float(self._transport.query("CURR?"), "current setpoint"),
            output_voltage_v=measured[0],
            output_current_a=measured[1],
            output_power_w=measured[2],
            output_enabled=parse_on_off(self._transport.query("OUTP?"), "output state"),
            over_voltage_v=parse_float(self._transport.query("VOLT:LIM?"), "OVP threshold"),
            over_current_a=parse_float(self._transport.query("CURR:LIM?"), "OCP threshold"),
            over_voltage_fault=measured[3],
            over_current_fault=measured[4],
            over_temperature_fault=measured[5],
            mode=measured[6],
        )

    def _read_dmm_sync(self) -> OwonSPMDMMState:
        state = parse_dmm_state(self._transport.query("CONF:ALL?"))
        scpi, _ = DMM_FUNCTIONS[state.function]
        range_value = None
        if state.function in DMM_RANGES or state.function == "capacitance":
            range_value = parse_float(self._transport.query(f"{scpi}:RANG?"), "DMM range")
        range_mode = state.range_mode
        if state.function in DMM_AUTO_RANGE_FUNCTIONS:
            range_mode = (
                "AUTO"
                if parse_on_off(
                    self._transport.query(f"{scpi}:RANG:AUTO?"),
                    "DMM auto-range state",
                )
                else "MANUAL"
            )
        relative_enabled = False
        if state.function in DMM_RELATIVE_FUNCTIONS:
            relative_enabled = parse_on_off(
                self._transport.query(f"{scpi}:NULL?"),
                "DMM relative state",
            )
        hold_enabled = parse_on_off(self._transport.query("MULT:HOLD?"), "DMM hold state")
        return replace(
            state,
            range_mode=range_mode,
            range_value=range_value,
            relative_enabled=relative_enabled,
            hold_enabled=hold_enabled,
        )

    async def read_meter(self, channel_id: str) -> MeterSample:
        try:
            parameter = self._parameters_by_id[channel_id]
        except KeyError as exc:
            raise ValueError(f"Unknown OWON SPM channel: {channel_id}") from exc
        state = await self.read_state()
        return self._sample(parameter, state)

    @staticmethod
    def _sample(parameter: OwonSPMMeterParameter, state: OwonSPMState) -> MeterSample:
        source = state.source
        if parameter.key == "dmm":
            dmm = state.multimeter
            return MeterSample(dmm.value, dmm.unit, dmm.function, dmm.status)
        values: dict[str, float] = {
            "set_voltage": source.set_voltage_v,
            "set_current": source.set_current_a,
            "output_voltage": source.output_voltage_v,
            "output_current": source.output_current_a,
            "output_power": source.output_power_w,
            "output": float(source.output_enabled),
            "mode": {"standby": 0.0, "CV": 1.0, "CC": 2.0, "fault": 3.0}[source.mode],
            "protection": float(
                source.over_voltage_fault
                or source.over_current_fault
                or source.over_temperature_fault
            ),
        }
        mode = "device_reported"
        status = "ok"
        if parameter.key == "output":
            mode = "ON" if source.output_enabled else "OFF"
        elif parameter.key == "mode":
            mode = source.mode
        elif parameter.key == "protection":
            faults = []
            if source.over_voltage_fault:
                faults.append("OVP")
            if source.over_current_fault:
                faults.append("OCP")
            if source.over_temperature_fault:
                faults.append("OTP")
            mode = "+".join(faults) if faults else "clear"
            status = "protection" if faults else "ok"
        return MeterSample(values[parameter.key], parameter.unit, mode, status)

    @staticmethod
    def _require_range(value: float, minimum: float, maximum: float, field: str) -> None:
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"OWON SPM {field} must be between {minimum:g} and {maximum:g}")

    @staticmethod
    def _require_step(value: float, step: float, field: str) -> None:
        if not math.isclose(value, round(value / step) * step, rel_tol=0, abs_tol=step / 100):
            raise ValueError(f"OWON SPM {field} must use {step:g} increments")

    def _validate_output(
        self, current: OwonSPMSourceState, update: OwonSPMOutputUpdate
    ) -> tuple[float, float, bool]:
        voltage = current.set_voltage_v if update.voltage_v is None else update.voltage_v
        current_limit = current.set_current_a if update.current_a is None else update.current_a
        enabled = current.output_enabled if update.enabled is None else update.enabled
        self._require_range(voltage, 0, MAX_OUTPUT_VOLTAGE_V, "voltage")
        self._require_range(current_limit, 0, MAX_OUTPUT_CURRENT_A, "current")
        self._require_step(voltage, VOLTAGE_STEP_V, "voltage")
        self._require_step(current_limit, CURRENT_STEP_A, "current")
        if voltage * current_limit > MAX_OUTPUT_POWER_W + 1e-9:
            raise ValueError(f"OWON SPM setpoints exceed {MAX_OUTPUT_POWER_W:g} W")
        return voltage, current_limit, enabled

    async def update_output(self, update: OwonSPMOutputUpdate) -> OwonSPMState:
        if update == OwonSPMOutputUpdate():
            raise ValueError("OWON SPM output update has no fields")
        async with self._operation_lock:
            current = await self._read_state_locked(force=True)
            voltage, current_limit, enabled = self._validate_output(current.source, update)
            await asyncio.to_thread(self._transport.write, "SYST:REM")
            try:
                if current.source.output_enabled and (
                    update.enabled is False
                    or update.voltage_v is not None
                    or update.current_a is not None
                ):
                    await asyncio.to_thread(self._transport.write, "OUTP OFF")
                writes: list[str] = []
                if update.current_a is not None and current_limit < current.source.set_current_a:
                    writes.append(f"CURR {current_limit:.3f}")
                if update.voltage_v is not None:
                    writes.append(f"VOLT {voltage:.2f}")
                if update.current_a is not None and not any(
                    item.startswith("CURR ") for item in writes
                ):
                    writes.append(f"CURR {current_limit:.3f}")
                for command in writes:
                    await asyncio.to_thread(self._transport.write, command)
                if enabled and (
                    update.enabled is True or (current.source.output_enabled and bool(writes))
                ):
                    await asyncio.to_thread(self._transport.write, "OUTP ON")
                    await asyncio.sleep(0.55)
                elif update.enabled is False and not current.source.output_enabled:
                    await asyncio.to_thread(self._transport.write, "OUTP OFF")
            finally:
                await asyncio.to_thread(self._transport.write, "SYST:LOC")
            updated = await self._read_state_locked(force=True)
            self._verify_float("voltage", voltage, updated.source.set_voltage_v, VOLTAGE_STEP_V)
            self._verify_float(
                "current", current_limit, updated.source.set_current_a, CURRENT_STEP_A
            )
            if updated.source.output_enabled != enabled:
                raise RuntimeError(
                    f"OWON SPM rejected output state: requested {'ON' if enabled else 'OFF'}, "
                    f"read back {'ON' if updated.source.output_enabled else 'OFF'}"
                )
            return updated

    async def update_protections(self, update: OwonSPMProtectionUpdate) -> OwonSPMState:
        if update == OwonSPMProtectionUpdate():
            raise ValueError("OWON SPM protection update has no fields")
        async with self._operation_lock:
            current = await self._read_state_locked(force=True)
            ovp = (
                current.source.over_voltage_v
                if update.over_voltage_v is None
                else update.over_voltage_v
            )
            ocp = (
                current.source.over_current_a
                if update.over_current_a is None
                else update.over_current_a
            )
            self._require_range(ovp, 0, MAX_OVP_V, "over-voltage limit")
            self._require_range(ocp, 0, MAX_OCP_A, "over-current limit")
            self._require_step(ovp, VOLTAGE_STEP_V, "over-voltage limit")
            self._require_step(ocp, CURRENT_STEP_A, "over-current limit")
            await asyncio.to_thread(self._transport.write, "SYST:REM")
            try:
                if update.over_voltage_v is not None:
                    await asyncio.to_thread(self._transport.write, f"VOLT:LIM {ovp:.2f}")
                if update.over_current_a is not None:
                    await asyncio.to_thread(self._transport.write, f"CURR:LIM {ocp:.3f}")
            finally:
                await asyncio.to_thread(self._transport.write, "SYST:LOC")
            updated = await self._read_state_locked(force=True)
            self._verify_float("OVP", ovp, updated.source.over_voltage_v, VOLTAGE_STEP_V)
            self._verify_float("OCP", ocp, updated.source.over_current_a, CURRENT_STEP_A)
            return updated

    async def update_multimeter(self, update: OwonSPMDMMUpdate) -> OwonSPMState:
        if update == OwonSPMDMMUpdate():
            raise ValueError("OWON SPM multimeter update has no fields")
        async with self._operation_lock:
            current = await self._read_state_locked(force=True)
            function = current.multimeter.function if update.function is None else update.function
            try:
                scpi, _ = DMM_FUNCTIONS[function]
            except KeyError as exc:
                raise ValueError(f"Unsupported OWON SPM DMM function: {function}") from exc
            range_mode = update.range_mode
            if range_mode is not None and range_mode not in {"auto", "manual"}:
                raise ValueError("OWON SPM DMM range mode must be auto or manual")
            if update.range_value is not None and range_mode is None:
                range_mode = "manual"
            if range_mode == "auto" and function not in DMM_AUTO_RANGE_FUNCTIONS:
                raise ValueError(f"OWON SPM {function} does not expose automatic range control")
            if range_mode == "manual":
                if update.range_value is None:
                    raise ValueError("OWON SPM manual DMM range requires range_value")
                ranges = DMM_RANGES.get(function)
                if ranges is None or not any(
                    math.isclose(update.range_value, item, rel_tol=1e-9, abs_tol=1e-15)
                    for item in ranges
                ):
                    raise ValueError(f"Unsupported OWON SPM {function} range")
            if update.relative_enabled is not None and function not in DMM_RELATIVE_FUNCTIONS:
                raise ValueError(f"OWON SPM {function} does not support relative mode")

            await asyncio.to_thread(self._transport.write, "SYST:REM")
            confirmed: OwonSPMDMMState | None = None

            async def wait_for_settings(
                predicate: Callable[[OwonSPMDMMState], bool], label: str
            ) -> OwonSPMDMMState:
                last_state: OwonSPMDMMState | None = None
                for _ in range(20):
                    await asyncio.sleep(0.12)
                    last_state = await asyncio.to_thread(self._read_dmm_sync)
                    if last_state.function == function and predicate(last_state):
                        return last_state
                detail = "" if last_state is None else f"; last state: {last_state}"
                raise RuntimeError(f"OWON SPM did not confirm {label}{detail}")

            try:
                if update.function is not None:
                    await asyncio.to_thread(self._transport.write, f"SENS:FUNC:{scpi}")
                    # The SPM6103 changes its DMM mode asynchronously. Immediate
                    # CONF:ALL? often still reports the previous function.
                    for _ in range(8):
                        await asyncio.sleep(0.12)
                        candidate = await asyncio.to_thread(
                            parse_dmm_state,
                            self._transport.query("CONF:ALL?"),
                        )
                        if candidate.function == function:
                            break
                    else:
                        raise RuntimeError(f"OWON SPM did not confirm DMM function {function}")
                if range_mode == "auto":
                    await asyncio.to_thread(self._transport.write, f"{scpi}:RANG:AUTO ON")
                elif range_mode == "manual":
                    if function in DMM_AUTO_RANGE_FUNCTIONS:
                        await asyncio.to_thread(
                            self._transport.write,
                            f"{scpi}:RANG:AUTO OFF",
                        )
                    await asyncio.to_thread(
                        self._transport.write,
                        f"{scpi}:RANG {update.range_value:g}",
                    )
                if range_mode is not None:

                    def range_matches(candidate: OwonSPMDMMState) -> bool:
                        if range_mode == "auto":
                            return candidate.range_mode == "AUTO"
                        return (
                            candidate.range_mode == "MANUAL"
                            and candidate.range_value is not None
                            and update.range_value is not None
                            and math.isclose(
                                candidate.range_value,
                                update.range_value,
                                rel_tol=1e-9,
                                abs_tol=1e-15,
                            )
                        )

                    confirmed = await wait_for_settings(range_matches, "DMM range")
                if update.relative_enabled is not None:
                    await asyncio.to_thread(
                        self._transport.write,
                        f"{scpi}:NULL {'ON' if update.relative_enabled else 'OFF'}",
                    )
                    confirmed = await wait_for_settings(
                        lambda candidate: candidate.relative_enabled == update.relative_enabled,
                        "DMM relative state",
                    )
                if update.hold_enabled is not None:
                    await asyncio.to_thread(
                        self._transport.write,
                        f"MULT:HOLD {'ON' if update.hold_enabled else 'OFF'}",
                    )
                    confirmed = await wait_for_settings(
                        lambda candidate: candidate.hold_enabled == update.hold_enabled,
                        "DMM Hold state",
                    )
                if confirmed is None:
                    confirmed = await wait_for_settings(lambda _candidate: True, "DMM function")
            finally:
                await asyncio.to_thread(self._transport.write, "SYST:LOC")
            source = await asyncio.to_thread(self._read_source_sync)
            updated = OwonSPMState(source=source, multimeter=confirmed)
            self._cached_state = updated
            self._cached_at = time.monotonic()
            return updated

    @staticmethod
    def _verify_float(field: str, expected: float, actual: float, resolution: float) -> None:
        if not math.isclose(expected, actual, rel_tol=1e-5, abs_tol=resolution / 2):
            raise RuntimeError(
                f"OWON SPM rejected {field}: requested {expected:g}, read back {actual:g}"
            )

    async def force_output_off(self) -> None:
        async with self._operation_lock:
            await asyncio.to_thread(self._transport.write, "SYST:REM")
            try:
                await asyncio.to_thread(self._transport.write, "OUTP OFF")
                if parse_on_off(await asyncio.to_thread(self._transport.query, "OUTP?")):
                    raise RuntimeError("OWON SPM output remained ON")
            finally:
                await asyncio.to_thread(self._transport.write, "SYST:LOC")
            self._cached_state = None

    async def close(self) -> None:
        try:
            await self.force_output_off()
        finally:
            await asyncio.to_thread(self._transport.close)
