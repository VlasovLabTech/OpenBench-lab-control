from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from openbench.core.registry import DeviceRegistry
from openbench.drivers.fnirsi_dps150 import (
    FNIRSIDPS150,
    DPS150DisplayUpdate,
    DPS150OutputUpdate,
    DPS150ProtectionUpdate,
    DPS150State,
)
from openbench.services.matrix_service import MatrixService


@dataclass(frozen=True, slots=True)
class PowerSequenceStep:
    voltage_v: float
    current_a: float
    dwell_s: float


@dataclass(frozen=True, slots=True)
class PowerProgramStatus:
    device_id: str
    kind: str
    active: bool
    paused: bool
    started_at: datetime | None
    current_step: int
    total_steps: int
    current_loop: int
    loops: int
    progress_percent: float
    last_error: str


@dataclass(slots=True)
class _PowerProgram:
    device_id: str
    kind: str
    steps: tuple[PowerSequenceStep, ...]
    loops: int
    started_at: datetime
    pause_event: asyncio.Event
    current_step: int = 0
    current_loop: int = 0
    paused: bool = False
    last_error: str = ""
    task: asyncio.Task[None] | None = None


class DCPowerSupplyService:
    def __init__(
        self,
        registry: DeviceRegistry,
        matrix_service: MatrixService,
    ) -> None:
        self._registry = registry
        self._matrix_service = matrix_service
        self._programs: dict[str, _PowerProgram] = {}
        self._completed: dict[str, PowerProgramStatus] = {}
        self._lock = asyncio.Lock()

    def _supply(self, device_id: str) -> FNIRSIDPS150:
        device = self._registry.device(device_id)
        instrument = self._registry.instrument(device_id)
        if device.kind != "fnirsi_dps150" or not isinstance(instrument, FNIRSIDPS150):
            raise ValueError(f"Device is not a supported DC power supply: {device_id}")
        return instrument

    def _require_safe(self) -> None:
        safety = self._matrix_service.safety_state()
        if safety.state != "safe":
            raise ValueError(f"Power-supply output is blocked while safety state is {safety.state}")

    async def state(self, device_id: str) -> DPS150State:
        return await self._supply(device_id).read_state(force=True)

    async def update_output(
        self,
        device_id: str,
        update: DPS150OutputUpdate,
    ) -> DPS150State:
        supply = self._supply(device_id)
        current = await supply.read_state(force=True)
        target_enabled = current.output_enabled if update.enabled is None else update.enabled
        if target_enabled:
            self._require_safe()
        return await supply.update_output(update)

    async def update_protections(
        self,
        device_id: str,
        update: DPS150ProtectionUpdate,
    ) -> DPS150State:
        return await self._supply(device_id).update_protections(update)

    async def update_display(
        self,
        device_id: str,
        update: DPS150DisplayUpdate,
    ) -> DPS150State:
        return await self._supply(device_id).update_display(update)

    async def set_metering(self, device_id: str, enabled: bool) -> DPS150State:
        return await self._supply(device_id).set_metering(enabled)

    async def save_preset(
        self,
        device_id: str,
        slot: int,
        *,
        voltage_v: float,
        current_a: float,
    ) -> DPS150State:
        return await self._supply(device_id).save_preset(
            slot,
            voltage_v=voltage_v,
            current_a=current_a,
        )

    async def apply_preset(
        self,
        device_id: str,
        slot: int,
        *,
        enabled: bool | None = None,
    ) -> DPS150State:
        if enabled:
            self._require_safe()
        return await self._supply(device_id).apply_preset(slot, enabled=enabled)

    @staticmethod
    def _validate_steps(
        steps: tuple[PowerSequenceStep, ...],
        *,
        minimum_dwell_s: float,
    ) -> None:
        if not steps:
            raise ValueError("Power-supply program requires at least one step")
        if len(steps) > 10_000:
            raise ValueError("Power-supply program is limited to 10000 steps")
        for index, step in enumerate(steps, start=1):
            if not (
                math.isfinite(step.voltage_v)
                and math.isfinite(step.current_a)
                and math.isfinite(step.dwell_s)
            ):
                raise ValueError(f"Power-supply program step {index} must be finite")
            if not 0 <= step.voltage_v <= 30:
                raise ValueError(f"Power-supply program step {index} voltage is out of range")
            if not 0 <= step.current_a <= 5:
                raise ValueError(f"Power-supply program step {index} current is out of range")
            if not minimum_dwell_s <= step.dwell_s <= 86400:
                raise ValueError(
                    f"Power-supply program step {index} dwell must be between "
                    f"{minimum_dwell_s:g} and 86400 seconds"
                )

    async def start_sequence(
        self,
        device_id: str,
        *,
        steps: tuple[PowerSequenceStep, ...],
        loops: int = 1,
    ) -> PowerProgramStatus:
        self._supply(device_id)
        self._require_safe()
        self._validate_steps(steps, minimum_dwell_s=0.1)
        if not 1 <= loops <= 1000:
            raise ValueError("Power-supply sequence loops must be between 1 and 1000")
        return await self._start_program(
            device_id,
            kind="sequence",
            steps=steps,
            loops=loops,
        )

    @staticmethod
    def _sweep_values(start: float, end: float, step: float) -> tuple[float, ...]:
        if not all(math.isfinite(value) for value in (start, end, step)):
            raise ValueError("Power-supply sweep values must be finite")
        if step <= 0:
            raise ValueError("Power-supply sweep step must be positive")
        if math.isclose(start, end, rel_tol=0, abs_tol=1e-12):
            raise ValueError("Power-supply sweep start and end must be different")
        direction = 1 if end > start else -1
        values: list[float] = []
        value = start
        epsilon = step / 1000
        while (value <= end + epsilon) if direction > 0 else (value >= end - epsilon):
            values.append(value)
            if len(values) > 10_000:
                raise ValueError("Power-supply sweep is limited to 10000 steps")
            value += direction * step
        if not math.isclose(values[-1], end, rel_tol=0, abs_tol=epsilon):
            values.append(end)
        return tuple(values)

    async def start_sweep(
        self,
        device_id: str,
        *,
        parameter: str,
        start: float,
        end: float,
        step: float,
        fixed_value: float,
        dwell_s: float,
        loops: int = 1,
    ) -> PowerProgramStatus:
        self._supply(device_id)
        self._require_safe()
        if parameter not in {"voltage", "current"}:
            raise ValueError("Power-supply sweep parameter must be voltage or current")
        if not 1 <= loops <= 1000:
            raise ValueError("Power-supply sweep loops must be between 1 and 1000")
        values = self._sweep_values(start, end, step)
        steps = tuple(
            PowerSequenceStep(
                voltage_v=value if parameter == "voltage" else fixed_value,
                current_a=fixed_value if parameter == "voltage" else value,
                dwell_s=dwell_s,
            )
            for value in values
        )
        self._validate_steps(steps, minimum_dwell_s=1.0)
        return await self._start_program(
            device_id,
            kind=f"{parameter}_sweep",
            steps=steps,
            loops=loops,
        )

    async def _start_program(
        self,
        device_id: str,
        *,
        kind: str,
        steps: tuple[PowerSequenceStep, ...],
        loops: int,
    ) -> PowerProgramStatus:
        async with self._lock:
            existing = self._programs.get(device_id)
            if existing is not None and existing.task is not None and not existing.task.done():
                raise RuntimeError("A power-supply program is already active")
            pause_event = asyncio.Event()
            pause_event.set()
            program = _PowerProgram(
                device_id=device_id,
                kind=kind,
                steps=steps,
                loops=loops,
                started_at=datetime.now(UTC),
                pause_event=pause_event,
            )
            self._programs[device_id] = program
            program.task = asyncio.create_task(
                self._run_program(program),
                name=f"openbench-dps150-{device_id}-{kind}",
            )
            return self._program_status(program)

    async def _run_program(self, program: _PowerProgram) -> None:
        completed_normally = False
        try:
            for loop_index in range(program.loops):
                program.current_loop = loop_index + 1
                for step_index, step in enumerate(program.steps, start=1):
                    await program.pause_event.wait()
                    self._require_safe()
                    program.current_step = step_index
                    await self._supply(program.device_id).update_output(
                        DPS150OutputUpdate(
                            voltage_v=step.voltage_v,
                            current_a=step.current_a,
                            enabled=True,
                        )
                    )
                    await self._program_delay(program, step.dwell_s)
            completed_normally = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            program.last_error = str(exc)
            try:
                await self._supply(program.device_id).update_output(
                    DPS150OutputUpdate(enabled=False)
                )
            except Exception:
                pass
        finally:
            if completed_normally:
                try:
                    await self._supply(program.device_id).update_output(
                        DPS150OutputUpdate(enabled=False)
                    )
                except Exception as exc:
                    program.last_error = f"Program completed, but output-off failed: {exc}"
            status = self._program_status(program, active=False)
            self._completed[program.device_id] = status
            current = self._programs.get(program.device_id)
            if current is program:
                self._programs.pop(program.device_id, None)

    @staticmethod
    async def _program_delay(program: _PowerProgram, duration_s: float) -> None:
        loop = asyncio.get_running_loop()
        remaining = duration_s
        while remaining > 0:
            await program.pause_event.wait()
            started = loop.time()
            await asyncio.sleep(min(0.1, remaining))
            if not program.paused:
                remaining -= loop.time() - started

    @staticmethod
    def _program_status(
        program: _PowerProgram,
        *,
        active: bool = True,
    ) -> PowerProgramStatus:
        total_steps = len(program.steps)
        completed_before_loop = max(0, program.current_loop - 1) * total_steps
        completed = completed_before_loop + program.current_step
        total = total_steps * program.loops
        progress = min(100.0, 100.0 * completed / total) if total else 0.0
        return PowerProgramStatus(
            device_id=program.device_id,
            kind=program.kind,
            active=active,
            paused=program.paused,
            started_at=program.started_at,
            current_step=program.current_step,
            total_steps=total_steps,
            current_loop=program.current_loop,
            loops=program.loops,
            progress_percent=progress,
            last_error=program.last_error,
        )

    def program_status(self, device_id: str) -> PowerProgramStatus:
        self._supply(device_id)
        program = self._programs.get(device_id)
        if program is not None:
            return self._program_status(program)
        return self._completed.get(
            device_id,
            PowerProgramStatus(
                device_id=device_id,
                kind="none",
                active=False,
                paused=False,
                started_at=None,
                current_step=0,
                total_steps=0,
                current_loop=0,
                loops=0,
                progress_percent=0.0,
                last_error="",
            ),
        )

    async def pause_program(self, device_id: str) -> PowerProgramStatus:
        program = self._active_program(device_id)
        program.paused = True
        program.pause_event.clear()
        return self._program_status(program)

    async def resume_program(self, device_id: str) -> PowerProgramStatus:
        self._require_safe()
        program = self._active_program(device_id)
        program.paused = False
        program.pause_event.set()
        return self._program_status(program)

    def _active_program(self, device_id: str) -> _PowerProgram:
        self._supply(device_id)
        program = self._programs.get(device_id)
        if program is None or program.task is None or program.task.done():
            raise RuntimeError("No power-supply program is active")
        return program

    async def stop_program(
        self,
        device_id: str,
        *,
        output_off: bool = True,
    ) -> PowerProgramStatus:
        program = self._active_program(device_id)
        task = program.task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if output_off:
            await self._supply(device_id).update_output(DPS150OutputUpdate(enabled=False))
        return replace(
            self._completed.get(device_id, self._program_status(program, active=False)),
            active=False,
            paused=False,
        )

    async def remove_device(self, device_id: str) -> None:
        program = self._programs.get(device_id)
        if program is not None and program.task is not None and not program.task.done():
            program.task.cancel()
            await asyncio.gather(program.task, return_exceptions=True)
        await self._supply(device_id).update_output(DPS150OutputUpdate(enabled=False))

    async def all_outputs_off(self) -> tuple[str, ...]:
        supplies = tuple(
            device.id for device in self._registry.devices() if device.kind == "fnirsi_dps150"
        )
        errors: list[str] = []
        for device_id in supplies:
            program = self._programs.get(device_id)
            if program is not None and program.task is not None and not program.task.done():
                program.task.cancel()
                await asyncio.gather(program.task, return_exceptions=True)
            try:
                await self._supply(device_id).update_output(DPS150OutputUpdate(enabled=False))
            except Exception as exc:
                errors.append(f"{device_id}: {exc}")
        return tuple(errors)

    async def close(self) -> None:
        tasks = tuple(
            program.task
            for program in self._programs.values()
            if program.task is not None and not program.task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._programs.clear()
        await self.all_outputs_off()
