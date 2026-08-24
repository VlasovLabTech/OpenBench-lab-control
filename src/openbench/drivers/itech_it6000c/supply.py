from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import replace
from typing import Protocol

from openbench.core.capabilities import MeterSample
from openbench.drivers.itech_it6000c.protocol import (
    ITECH_PARAMETERS,
    SUPPORTED_PROFILES,
    ITechAdvancedUpdate,
    ITechIT6000CIdentity,
    ITechIT6000CProfile,
    ITechIT6000CState,
    ITechMeterParameter,
    ITechOperatingPointUpdate,
    ITechProtectionUpdate,
    parse_bool,
    parse_float,
    parse_function_mode,
    parse_identity,
    parse_priority,
)
from openbench.drivers.itech_it6000c.transport import (
    ITechIT6000CDescriptor,
    ITechIT6000CSerialTransport,
)

STATE_CACHE_S = 0.3
READ_FAILURE_COOLDOWN_S = 1.0


class ITechTransport(Protocol):
    def query(self, command: str) -> str: ...

    def write(self, command: str) -> None: ...

    def close(self) -> None: ...


class ITechIT6000C:
    def __init__(
        self,
        descriptor: ITechIT6000CDescriptor,
        *,
        transport: ITechTransport | None = None,
    ) -> None:
        identity_key = descriptor.identity.serial_number or descriptor.location or descriptor.port
        normalized = re.sub(r"[^a-z0-9]+", "_", identity_key.casefold()).strip("_")
        self.device_id = f"itech_it6000c_{normalized or 'serial'}"
        self.descriptor = descriptor
        self._transport = transport or ITechIT6000CSerialTransport(descriptor)
        self._identity = descriptor.identity
        self._profile = SUPPORTED_PROFILES[descriptor.identity.model]
        self._cached_state: ITechIT6000CState | None = None
        self._cached_at = 0.0
        self._read_failed_at = 0.0
        self._read_failure_message = ""
        self._operation_lock = asyncio.Lock()
        self._parameters_by_id = {
            f"{self.device_id}.{parameter.suffix}": parameter for parameter in ITECH_PARAMETERS
        }

    @property
    def identity(self) -> ITechIT6000CIdentity:
        return self._identity

    @property
    def profile(self) -> ITechIT6000CProfile:
        return self._profile

    @property
    def cached_state(self) -> ITechIT6000CState | None:
        return self._cached_state

    @property
    def parameters(self) -> tuple[tuple[str, ITechMeterParameter], ...]:
        return tuple(self._parameters_by_id.items())

    async def identify(self) -> str:
        async with self._operation_lock:
            identity = parse_identity(await asyncio.to_thread(self._transport.query, "*IDN?"))
            if identity.serial_number != self.descriptor.identity.serial_number:
                raise RuntimeError("ITECH serial number changed after discovery")
            self._identity = identity
        return (
            f"{identity.manufacturer} {identity.model} SN {identity.serial_number} "
            f"FW {identity.main_firmware} on {self.descriptor.port} "
            f"at {self.descriptor.baud_rate} baud"
        )

    async def read_state(self, *, force: bool = False, full: bool = True) -> ITechIT6000CState:
        async with self._operation_lock:
            return await self._read_state_locked(force=force, full=full)

    async def _read_state_locked(self, *, force: bool, full: bool) -> ITechIT6000CState:
        now = time.monotonic()
        if not force and self._cached_state is not None and now - self._cached_at <= STATE_CACHE_S:
            return self._cached_state
        if not force and now - self._read_failed_at <= READ_FAILURE_COOLDOWN_S:
            raise ConnectionError(self._read_failure_message or "ITECH read retry is cooling down")
        try:
            if full or self._cached_state is None:
                state = await asyncio.to_thread(self._read_full_sync)
            else:
                state = await asyncio.to_thread(self._read_live_sync, self._cached_state)
        except Exception as exc:
            self._cached_state = None
            self._cached_at = 0.0
            self._read_failed_at = time.monotonic()
            self._read_failure_message = str(exc)
            await asyncio.to_thread(self._transport.close)
            raise
        self._cached_state = state
        self._cached_at = time.monotonic()
        self._read_failed_at = 0.0
        self._read_failure_message = ""
        return state

    def _read_live_sync(self, current: ITechIT6000CState) -> ITechIT6000CState:
        query = self._transport.query
        measured_voltage_v = parse_float(query("MEAS:VOLT?"), "measured voltage")
        measured_current_a = parse_float(query("MEAS:CURR?"), "measured current")
        return replace(
            current,
            measured_voltage_v=measured_voltage_v,
            measured_current_a=measured_current_a,
            measured_power_w=measured_voltage_v * measured_current_a,
        )

    def _read_full_sync(self) -> ITechIT6000CState:
        query = self._transport.query
        return ITechIT6000CState(
            priority=parse_priority(query("FUNC?")),
            function_mode=parse_function_mode(query("FUNC:MODE?")),
            output_enabled=parse_bool(query("OUTP?"), "output state"),
            voltage_setpoint_v=parse_float(query("VOLT?"), "voltage setpoint"),
            current_setpoint_a=parse_float(query("CURR?"), "current setpoint"),
            current_limit_positive_a=parse_float(query("CURR:LIM?"), "positive current limit"),
            current_limit_negative_a=parse_float(query("CURR:LIM:NEG?"), "negative current limit"),
            voltage_limit_positive_v=parse_float(query("VOLT:LIM?"), "positive voltage limit"),
            voltage_limit_negative_v=parse_float(query("VOLT:LIM:NEG?"), "negative voltage limit"),
            power_limit_positive_w=parse_float(query("POW:LIM?"), "positive power limit"),
            power_limit_negative_w=parse_float(query("POW:LIM:NEG?"), "negative power limit"),
            measured_voltage_v=parse_float(query("MEAS:VOLT?"), "measured voltage"),
            measured_current_a=parse_float(query("MEAS:CURR?"), "measured current"),
            measured_power_w=parse_float(query("MEAS:POW?"), "measured power"),
            voltage_slew_positive_v_per_ms=parse_float(
                query("VOLT:SLEW:POS?"), "positive voltage slew"
            ),
            voltage_slew_negative_v_per_ms=parse_float(
                query("VOLT:SLEW:NEG?"), "negative voltage slew"
            ),
            current_slew_positive_a_per_ms=parse_float(
                query("CURR:SLEW:POS?"), "positive current slew"
            ),
            current_slew_negative_a_per_ms=parse_float(
                query("CURR:SLEW:NEG?"), "negative current slew"
            ),
            ovp_enabled=parse_bool(query("VOLT:PROT:STAT?"), "OVP state"),
            ovp_level_v=parse_float(query("VOLT:PROT?"), "OVP level"),
            ovp_delay_s=parse_float(query("VOLT:PROT:DEL?"), "OVP delay"),
            ocp_enabled=parse_bool(query("CURR:PROT:STAT?"), "OCP state"),
            ocp_level_a=parse_float(query("CURR:PROT?"), "OCP level"),
            ocp_delay_s=parse_float(query("CURR:PROT:DEL?"), "OCP delay"),
            opp_enabled=parse_bool(query("POW:PROT:STAT?"), "OPP state"),
            opp_level_w=parse_float(query("POW:PROT?"), "OPP level"),
            opp_delay_s=parse_float(query("POW:PROT:DEL?"), "OPP delay"),
            uvp_enabled=parse_bool(query("VOLT:UND:PROT:STAT?"), "UVP state"),
            uvp_level_v=parse_float(query("VOLT:UND:PROT?"), "UVP level"),
            uvp_delay_s=parse_float(query("VOLT:UND:PROT:DEL?"), "UVP delay"),
            uvp_warmup_s=parse_float(query("VOLT:UND:PROT:WARM?"), "UVP warm-up"),
            ucp_enabled=parse_bool(query("CURR:UND:PROT:STAT?"), "UCP state"),
            ucp_level_a=parse_float(query("CURR:UND:PROT?"), "UCP level"),
            ucp_delay_s=parse_float(query("CURR:UND:PROT:DEL?"), "UCP delay"),
            ucp_warmup_s=parse_float(query("CURR:UND:PROT:WARM?"), "UCP warm-up"),
            output_rise_delay_s=parse_float(query("OUTP:DEL:RISE?"), "output rise delay"),
            output_fall_delay_s=parse_float(query("OUTP:DEL:FALL?"), "output fall delay"),
            watchdog_enabled=parse_bool(query("OUTP:PROT:WDOG:STAT?"), "watchdog state"),
            watchdog_delay_s=parse_float(query("OUTP:PROT:WDOG:DEL?"), "watchdog delay"),
            sink_resistance_enabled=parse_bool(query("SINK:RES:STAT?"), "sink resistance state"),
            voltage_rzero_enabled=parse_bool(query("SYST:VOLT:RZERO?"), "RZero state"),
            questionable_status=int(parse_float(query("STAT:QUES:COND?"), "questionable status")),
            operation_status=int(parse_float(query("STAT:OPER:COND?"), "operation status")),
        )

    async def read_meter(self, channel_id: str) -> MeterSample:
        try:
            parameter = self._parameters_by_id[channel_id]
        except KeyError as exc:
            raise ValueError(f"Unknown ITECH channel: {channel_id}") from exc
        state = await self.read_state(full=False)
        values = {
            "measured_voltage": state.measured_voltage_v,
            "measured_current": state.measured_current_a,
            "measured_power": state.measured_power_w,
            "voltage_setpoint": state.voltage_setpoint_v,
            "current_setpoint": state.current_setpoint_a,
            "current_limit_positive": state.current_limit_positive_a,
            "current_limit_negative": state.current_limit_negative_a,
            "voltage_limit_positive": state.voltage_limit_positive_v,
            "voltage_limit_negative": state.voltage_limit_negative_v,
            "power_limit_positive": state.power_limit_positive_w,
            "power_limit_negative": state.power_limit_negative_w,
            "output": float(state.output_enabled),
            "priority": 0.0 if state.priority == "CV" else 1.0,
            "direction": {"SINK": -1.0, "IDLE": 0.0, "SOURCE": 1.0}[state.direction],
        }
        quality = "device_reported"
        if parameter.key == "measured_power":
            quality = "calculated_u_times_i"
        elif parameter.key == "output":
            quality = "ON" if state.output_enabled else "OFF"
        elif parameter.key == "priority":
            quality = state.priority
        elif parameter.key == "direction":
            quality = state.direction
        status = "protection" if state.faults else "ok"
        return MeterSample(values[parameter.key], parameter.unit, quality, status)

    @staticmethod
    def _require_range(value: float, minimum: float, maximum: float, field: str) -> None:
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"ITECH {field} must be between {minimum:g} and {maximum:g}")

    @staticmethod
    def _require_step(value: float, step: float, field: str) -> None:
        if not math.isclose(value, round(value / step) * step, rel_tol=0, abs_tol=step / 100):
            raise ValueError(f"ITECH {field} must use {step:g} increments")

    def _validate_operating_point(
        self, state: ITechIT6000CState, update: ITechOperatingPointUpdate
    ) -> tuple[str, dict[str, float], bool]:
        priority = state.priority if update.priority is None else update.priority.upper()
        if priority not in {"CV", "CC"}:
            raise ValueError("ITECH priority must be CV or CC")
        values = {
            "voltage_setpoint_v": state.voltage_setpoint_v
            if update.voltage_setpoint_v is None
            else update.voltage_setpoint_v,
            "current_setpoint_a": state.current_setpoint_a
            if update.current_setpoint_a is None
            else update.current_setpoint_a,
            "current_limit_positive_a": state.current_limit_positive_a
            if update.current_limit_positive_a is None
            else update.current_limit_positive_a,
            "current_limit_negative_a": state.current_limit_negative_a
            if update.current_limit_negative_a is None
            else update.current_limit_negative_a,
            "voltage_limit_positive_v": state.voltage_limit_positive_v
            if update.voltage_limit_positive_v is None
            else update.voltage_limit_positive_v,
            "voltage_limit_negative_v": state.voltage_limit_negative_v
            if update.voltage_limit_negative_v is None
            else update.voltage_limit_negative_v,
            "power_limit_positive_w": state.power_limit_positive_w
            if update.power_limit_positive_w is None
            else update.power_limit_positive_w,
            "power_limit_negative_w": state.power_limit_negative_w
            if update.power_limit_negative_w is None
            else update.power_limit_negative_w,
        }
        p = self._profile
        self._require_range(values["voltage_setpoint_v"], 0, p.rated_voltage_v, "voltage setpoint")
        self._require_range(
            values["current_setpoint_a"], -p.rated_current_a, p.rated_current_a, "current setpoint"
        )
        self._require_range(
            values["current_limit_positive_a"], 0, p.scpi_current_max_a, "positive current limit"
        )
        self._require_range(
            values["current_limit_negative_a"], -p.scpi_current_max_a, 0, "negative current limit"
        )
        self._require_range(
            values["voltage_limit_positive_v"], 0, p.scpi_voltage_max_v, "positive voltage limit"
        )
        self._require_range(values["voltage_limit_negative_v"], 0, 0.2, "negative voltage limit")
        self._require_range(
            values["power_limit_positive_w"], 0, p.scpi_power_max_w, "positive power limit"
        )
        self._require_range(
            values["power_limit_negative_w"], -p.scpi_power_max_w, -1, "negative power limit"
        )
        step_groups = (
            (
                p.voltage_resolution_v,
                ("voltage_setpoint_v", "voltage_limit_positive_v", "voltage_limit_negative_v"),
            ),
            (
                p.current_resolution_a,
                ("current_setpoint_a", "current_limit_positive_a", "current_limit_negative_a"),
            ),
            (p.power_resolution_w, ("power_limit_positive_w", "power_limit_negative_w")),
        )
        # The IT6000C reports some stored values with more digits than its documented
        # front-panel programming resolution. Validate resolution only for values the
        # caller is changing so an unrelated partial update remains possible.
        for step, fields in step_groups:
            for field in fields:
                if getattr(update, field) is not None:
                    self._require_step(values[field], step, field.replace("_", " "))
        enabled = state.output_enabled if update.output_enabled is None else update.output_enabled
        if enabled and not update.wiring_confirmed:
            raise ValueError("ITECH output enable requires wiring_confirmed=true")
        if enabled and not (state.ovp_enabled and state.ocp_enabled and state.opp_enabled):
            raise ValueError("ITECH output requires OVP, OCP, and OPP protections enabled")
        return priority, values, enabled

    async def update_operating_point(self, update: ITechOperatingPointUpdate) -> ITechIT6000CState:
        fields = set(update.__dataclass_fields__) - {"wiring_confirmed"}
        if not any(getattr(update, field) is not None for field in fields):
            raise ValueError("ITECH operating-point update has no fields")
        async with self._operation_lock:
            requested_fields = {
                field
                for field in fields
                if field != "output_enabled" and getattr(update, field) is not None
            }
            cached = self._cached_state
            live_setpoint_field: str | None = None
            if cached is not None and cached.output_enabled:
                if (
                    update.output_enabled is None
                    and update.priority is None
                    and cached.priority == "CC"
                    and requested_fields == {"current_setpoint_a"}
                ):
                    live_setpoint_field = "current_setpoint_a"
                elif (
                    update.output_enabled is None
                    and update.priority is None
                    and cached.priority == "CV"
                    and requested_fields == {"voltage_setpoint_v"}
                ):
                    live_setpoint_field = "voltage_setpoint_v"

            # A live CC/CV step is intentionally command-only. The experiment
            # reads the three measured values once, after its short settle, while
            # the oscilloscope frame is already being acquired. Re-reading every
            # setting here made one setpoint change trigger multiple full state
            # transactions and added seconds of avoidable serial traffic.
            if live_setpoint_field is not None:
                assert cached is not None
                priority, values, _enabled = self._validate_operating_point(cached, update)
                command = "CURR" if live_setpoint_field == "current_setpoint_a" else "VOLT"
                await asyncio.to_thread(self._transport.write, "SYST:REM")
                try:
                    await asyncio.to_thread(
                        self._transport.write,
                        f"{command} {values[live_setpoint_field]:.6f}",
                    )
                finally:
                    await asyncio.to_thread(self._transport.write, "SYST:LOC")
                if live_setpoint_field == "current_setpoint_a":
                    updated = replace(
                        cached,
                        current_setpoint_a=values[live_setpoint_field],
                    )
                else:
                    updated = replace(
                        cached,
                        voltage_setpoint_v=values[live_setpoint_field],
                    )
                self._cached_state = updated
                self._cached_at = time.monotonic()
                return updated

            current = await self._read_state_locked(force=True, full=True)
            priority, values, enabled = self._validate_operating_point(current, update)
            writes: list[str] = []
            mode_fields = (
                "priority",
                "voltage_setpoint_v",
                "current_setpoint_a",
                "current_limit_positive_a",
                "current_limit_negative_a",
                "voltage_limit_positive_v",
                "voltage_limit_negative_v",
            )
            rebuild_mode_state = live_setpoint_field is None and any(
                getattr(update, field) is not None for field in mode_fields
            )
            if rebuild_mode_state:
                writes.append("FUNC:MODE FIXED")
                if priority == "CV":
                    # VOLT:LIM is writable only in CC priority. Enter the inactive
                    # priority first, rebuild its values, then finish in CV and
                    # rebuild CV values. Priority changes otherwise copy active
                    # setpoints into the other priority's limit registers.
                    writes.extend(
                        (
                            "FUNC CURR",
                            f"CURR {values['current_setpoint_a']:.6f}",
                            f"VOLT:LIM {values['voltage_limit_positive_v']:.6f}",
                            f"VOLT:LIM:NEG {values['voltage_limit_negative_v']:.6f}",
                            "FUNC VOLT",
                            f"VOLT {values['voltage_setpoint_v']:.6f}",
                            f"CURR:LIM {values['current_limit_positive_a']:.6f}",
                            f"CURR:LIM:NEG {values['current_limit_negative_a']:.6f}",
                        )
                    )
                else:
                    writes.extend(
                        (
                            "FUNC VOLT",
                            f"VOLT {values['voltage_setpoint_v']:.6f}",
                            f"CURR:LIM {values['current_limit_positive_a']:.6f}",
                            f"CURR:LIM:NEG {values['current_limit_negative_a']:.6f}",
                            "FUNC CURR",
                            f"CURR {values['current_setpoint_a']:.6f}",
                            f"VOLT:LIM {values['voltage_limit_positive_v']:.6f}",
                            f"VOLT:LIM:NEG {values['voltage_limit_negative_v']:.6f}",
                        )
                    )
            for field, command in (
                ("power_limit_positive_w", "POW:LIM"),
                ("power_limit_negative_w", "POW:LIM:NEG"),
            ):
                if getattr(update, field) is not None:
                    writes.append(f"{command} {values[field]:.6f}")
            must_pause = current.output_enabled and live_setpoint_field is None and (
                bool(writes) or update.output_enabled is False
            )
            await asyncio.to_thread(self._transport.write, "SYST:REM")
            try:
                if must_pause:
                    await asyncio.to_thread(self._transport.write, "OUTP OFF")
                for command in writes:
                    await asyncio.to_thread(self._transport.write, command)
                if enabled and (must_pause or not current.output_enabled):
                    await asyncio.to_thread(self._transport.write, "OUTP ON")
                    await asyncio.sleep(0.15)
                elif update.output_enabled is False and not must_pause:
                    await asyncio.to_thread(self._transport.write, "OUTP OFF")
            except Exception:
                try:
                    await asyncio.to_thread(self._transport.write, "OUTP OFF")
                finally:
                    raise
            finally:
                await asyncio.to_thread(self._transport.write, "SYST:LOC")
            try:
                updated = await self._read_state_locked(force=True, full=True)
                self._verify_operating_point(
                    update,
                    priority,
                    values,
                    enabled,
                    updated,
                    rebuild_mode_state=rebuild_mode_state,
                )
            except Exception:
                if enabled:
                    try:
                        await asyncio.to_thread(self._transport.write, "SYST:REM")
                        await asyncio.to_thread(self._transport.write, "OUTP OFF")
                    finally:
                        await asyncio.to_thread(self._transport.write, "SYST:LOC")
                raise
            return updated

    def _verify_operating_point(
        self,
        update: ITechOperatingPointUpdate,
        priority: str,
        values: dict[str, float],
        enabled: bool,
        actual: ITechIT6000CState,
        *,
        rebuild_mode_state: bool,
    ) -> None:
        if actual.priority != priority or actual.output_enabled != enabled:
            raise RuntimeError("ITECH rejected priority or output state")
        resolutions = {
            "voltage_setpoint_v": self._profile.voltage_resolution_v,
            "current_setpoint_a": self._profile.current_resolution_a,
            "current_limit_positive_a": self._profile.current_resolution_a,
            "current_limit_negative_a": self._profile.current_resolution_a,
            "voltage_limit_positive_v": self._profile.voltage_resolution_v,
            "voltage_limit_negative_v": self._profile.voltage_resolution_v,
            "power_limit_positive_w": self._profile.power_resolution_w,
            "power_limit_negative_w": self._profile.power_resolution_w,
        }
        mode_fields = {
            "voltage_setpoint_v",
            "current_setpoint_a",
            "current_limit_positive_a",
            "current_limit_negative_a",
            "voltage_limit_positive_v",
            "voltage_limit_negative_v",
        }
        # On the physical IT6054C the inactive setpoint shares a register with
        # the active priority's positive limit: CURR? mirrors CURR:LIM? in CV,
        # and VOLT? mirrors VOLT:LIM? in CC. It therefore cannot be preserved
        # independently across a priority change and must not make an otherwise
        # successful update fail verification.
        inactive_setpoint = (
            "current_setpoint_a" if priority == "CV" else "voltage_setpoint_v"
        )
        for field, resolution in resolutions.items():
            if field == inactive_setpoint:
                continue
            if getattr(update, field) is None and not (rebuild_mode_state and field in mode_fields):
                continue
            if not math.isclose(
                values[field], getattr(actual, field), rel_tol=1e-6, abs_tol=resolution / 2
            ):
                raise RuntimeError(f"ITECH rejected {field.replace('_', ' ')}")

    async def update_protections(self, update: ITechProtectionUpdate) -> ITechIT6000CState:
        if not any(getattr(update, field) is not None for field in update.__dataclass_fields__):
            raise ValueError("ITECH protection update has no fields")
        async with self._operation_lock:
            current = await self._read_state_locked(force=True, full=True)
            if current.output_enabled:
                raise ValueError("Turn ITECH output OFF before changing protections")
            limits = {
                "ovp_level_v": (0, self._profile.scpi_voltage_max_v),
                "ocp_level_a": (
                    -self._profile.scpi_current_max_a,
                    self._profile.scpi_current_max_a,
                ),
                "opp_level_w": (-self._profile.scpi_power_max_w, self._profile.scpi_power_max_w),
                "uvp_level_v": (0, self._profile.scpi_voltage_max_v),
                "ucp_level_a": (
                    -self._profile.scpi_current_max_a,
                    self._profile.scpi_current_max_a,
                ),
            }
            for field, (minimum, maximum) in limits.items():
                value = getattr(update, field)
                if value is not None:
                    self._require_range(value, minimum, maximum, field.replace("_", " "))
            for field in (
                "ovp_delay_s",
                "ocp_delay_s",
                "opp_delay_s",
                "uvp_delay_s",
                "uvp_warmup_s",
                "ucp_delay_s",
                "ucp_warmup_s",
            ):
                value = getattr(update, field)
                if value is not None:
                    self._require_range(value, 0, 60, field.replace("_", " "))
            mapping = (
                ("ovp_level_v", "VOLT:PROT"),
                ("ovp_delay_s", "VOLT:PROT:DEL"),
                ("ocp_level_a", "CURR:PROT"),
                ("ocp_delay_s", "CURR:PROT:DEL"),
                ("opp_level_w", "POW:PROT"),
                ("opp_delay_s", "POW:PROT:DEL"),
                ("uvp_level_v", "VOLT:UND:PROT"),
                ("uvp_delay_s", "VOLT:UND:PROT:DEL"),
                ("uvp_warmup_s", "VOLT:UND:PROT:WARM"),
                ("ucp_level_a", "CURR:UND:PROT"),
                ("ucp_delay_s", "CURR:UND:PROT:DEL"),
                ("ucp_warmup_s", "CURR:UND:PROT:WARM"),
            )
            toggles = (
                ("ovp_enabled", "VOLT:PROT:STAT"),
                ("ocp_enabled", "CURR:PROT:STAT"),
                ("opp_enabled", "POW:PROT:STAT"),
                ("uvp_enabled", "VOLT:UND:PROT:STAT"),
                ("ucp_enabled", "CURR:UND:PROT:STAT"),
            )
            await asyncio.to_thread(self._transport.write, "SYST:REM")
            try:
                for field, command in mapping:
                    value = getattr(update, field)
                    if value is not None:
                        await asyncio.to_thread(self._transport.write, f"{command} {value:.6f}")
                for field, command in toggles:
                    value = getattr(update, field)
                    if value is not None:
                        await asyncio.to_thread(
                            self._transport.write, f"{command} {'ON' if value else 'OFF'}"
                        )
            finally:
                await asyncio.to_thread(self._transport.write, "SYST:LOC")
            updated = await self._read_state_locked(force=True, full=True)
            for field in update.__dataclass_fields__:
                expected = getattr(update, field)
                if expected is None:
                    continue
                actual = getattr(updated, field)
                if isinstance(expected, bool):
                    if actual != expected:
                        raise RuntimeError(f"ITECH rejected {field.replace('_', ' ')}")
                elif not math.isclose(expected, actual, rel_tol=1e-6, abs_tol=0.001):
                    raise RuntimeError(f"ITECH rejected {field.replace('_', ' ')}")
            return updated

    async def clear_protection(self) -> ITechIT6000CState:
        """Clear a latched protection while keeping the output safely disabled."""
        async with self._operation_lock:
            current = await self._read_state_locked(force=True, full=True)
            if current.output_enabled:
                raise ValueError("Turn ITECH output OFF before clearing protection")
            await asyncio.to_thread(self._transport.write, "SYST:REM")
            try:
                await asyncio.to_thread(self._transport.write, "OUTP:PROT:CLE")
            finally:
                await asyncio.to_thread(self._transport.write, "SYST:LOC")
            await asyncio.sleep(0.1)
            updated = await self._read_state_locked(force=True, full=True)
            if updated.faults:
                raise RuntimeError(
                    "ITECH protection remains active: " + ", ".join(updated.faults)
                )
            return updated

    async def update_advanced(self, update: ITechAdvancedUpdate) -> ITechIT6000CState:
        if not any(getattr(update, field) is not None for field in update.__dataclass_fields__):
            raise ValueError("ITECH advanced update has no fields")
        async with self._operation_lock:
            current = await self._read_state_locked(force=True, full=True)
            if current.output_enabled:
                raise ValueError("Turn ITECH output OFF before changing advanced settings")
            slew_fields = (
                "voltage_slew_positive_v_per_ms",
                "voltage_slew_negative_v_per_ms",
                "current_slew_positive_a_per_ms",
                "current_slew_negative_a_per_ms",
            )
            for field in slew_fields:
                value = getattr(update, field)
                if value is not None:
                    self._require_range(value, 0.001, 2000, field.replace("_", " "))
            for field in ("output_rise_delay_s", "output_fall_delay_s"):
                value = getattr(update, field)
                if value is not None:
                    self._require_range(value, 0, 60, field.replace("_", " "))
            if update.watchdog_delay_s is not None:
                self._require_range(update.watchdog_delay_s, 1, 6000, "watchdog delay")
            mapping = (
                ("voltage_slew_positive_v_per_ms", "VOLT:SLEW:POS"),
                ("voltage_slew_negative_v_per_ms", "VOLT:SLEW:NEG"),
                ("current_slew_positive_a_per_ms", "CURR:SLEW:POS"),
                ("current_slew_negative_a_per_ms", "CURR:SLEW:NEG"),
                ("output_rise_delay_s", "OUTP:DEL:RISE"),
                ("output_fall_delay_s", "OUTP:DEL:FALL"),
                ("watchdog_delay_s", "OUTP:PROT:WDOG:DEL"),
            )
            await asyncio.to_thread(self._transport.write, "SYST:REM")
            try:
                for field, command in mapping:
                    value = getattr(update, field)
                    if value is not None:
                        await asyncio.to_thread(self._transport.write, f"{command} {value:.6f}")
                if update.watchdog_enabled is not None:
                    await asyncio.to_thread(
                        self._transport.write,
                        f"OUTP:PROT:WDOG:STAT {'ON' if update.watchdog_enabled else 'OFF'}",
                    )
            finally:
                await asyncio.to_thread(self._transport.write, "SYST:LOC")
            updated = await self._read_state_locked(force=True, full=True)
            for field in update.__dataclass_fields__:
                expected = getattr(update, field)
                if expected is None:
                    continue
                actual = getattr(updated, field)
                if isinstance(expected, bool):
                    matched = expected == actual
                else:
                    matched = math.isclose(expected, actual, rel_tol=1e-6, abs_tol=0.001)
                if not matched:
                    raise RuntimeError(f"ITECH rejected {field.replace('_', ' ')}")
            return updated

    async def force_output_off(self) -> None:
        async with self._operation_lock:
            await asyncio.to_thread(self._transport.write, "SYST:REM")
            try:
                await asyncio.to_thread(self._transport.write, "OUTP OFF")
                if parse_bool(await asyncio.to_thread(self._transport.query, "OUTP?"), "output"):
                    raise RuntimeError("ITECH output remained ON")
            finally:
                await asyncio.to_thread(self._transport.write, "SYST:LOC")
            self._cached_state = None

    async def release_transport_for_reconnect(self) -> None:
        """Release a failed link without issuing any instrument write."""
        async with self._operation_lock:
            await asyncio.to_thread(self._transport.close)
            self._cached_state = None
            self._cached_at = 0.0
            self._read_failed_at = 0.0
            self._read_failure_message = ""

    async def close(self) -> None:
        try:
            await self.force_output_off()
        finally:
            await asyncio.to_thread(self._transport.close)
