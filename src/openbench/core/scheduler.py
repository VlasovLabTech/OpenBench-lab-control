from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from openbench.core.capabilities import AsyncClosable, MeterCapability, MeterSample
from openbench.core.events import MeasurementEventBus
from openbench.domain import Channel, Measurement
from openbench.services.measurement_service import MeasurementService

MIN_POLL_INTERVAL_S = 0.01
MAX_POLL_INTERVAL_S = 600.0
DEFAULT_POLL_INTERVAL_S = 0.5
DEFAULT_STALE_GRACE_S = 2.0
DEFAULT_WATCHDOG_INTERVAL_S = 0.25


@dataclass(slots=True)
class PollTarget:
    channel: Channel
    meter: MeterCapability
    connected: bool = True
    last_activity_monotonic_s: float = field(default_factory=time.monotonic)
    stale_notified: bool = False
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    polling_resumed: asyncio.Event = field(default_factory=asyncio.Event)
    polling_suspend_count: int = 0
    polling_not_before_monotonic_s: float = 0.0

    def __post_init__(self) -> None:
        self.polling_resumed.set()


class PollingSuspendedError(RuntimeError):
    """Raised when an explicit sample races with a device reservation."""


class PollingScheduler:
    def __init__(
        self,
        measurement_service: MeasurementService,
        event_bus: MeasurementEventBus,
        *,
        stale_grace_s: float = DEFAULT_STALE_GRACE_S,
        watchdog_interval_s: float = DEFAULT_WATCHDOG_INTERVAL_S,
    ) -> None:
        if stale_grace_s <= 0:
            raise ValueError("Stale grace must be positive")
        if watchdog_interval_s <= 0:
            raise ValueError("Watchdog interval must be positive")
        self._measurement_service = measurement_service
        self._event_bus = event_bus
        self._stale_grace_s = stale_grace_s
        self._watchdog_interval_s = watchdog_interval_s
        self._targets: dict[str, PollTarget] = {}
        self._closables: dict[int, AsyncClosable] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._watchdog_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._running = False

    @staticmethod
    def validate_interval(interval_s: float) -> float:
        if not MIN_POLL_INTERVAL_S <= interval_s <= MAX_POLL_INTERVAL_S:
            raise ValueError(
                f"Polling interval must be between {MIN_POLL_INTERVAL_S} and "
                f"{MAX_POLL_INTERVAL_S} seconds"
            )
        return interval_s

    def add_target(self, channel: Channel, meter: MeterCapability) -> None:
        self.validate_interval(channel.poll_interval_s)
        if channel.id in self._targets:
            raise ValueError(f"Poll target already exists: {channel.id}")
        if channel.device_id != meter.device_id:
            raise ValueError("Poll channel and meter device IDs must match")
        self._targets[channel.id] = PollTarget(channel=channel, meter=meter)
        if isinstance(meter, AsyncClosable):
            self.add_closable(meter)
        if self._running:
            self._tasks[channel.id] = self._create_target_task(self._targets[channel.id])

    def add_closable(self, instrument: AsyncClosable) -> None:
        self._closables[id(instrument)] = instrument

    def remove_closable(self, instrument: AsyncClosable) -> None:
        self._closables.pop(id(instrument), None)

    async def remove_target(self, channel_id: str) -> PollTarget:
        try:
            target = self._targets.pop(channel_id)
        except KeyError as exc:
            raise KeyError(f"Unknown poll target: {channel_id}") from exc
        task = self._tasks.pop(channel_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return target

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        now = time.monotonic()
        for target in self._targets.values():
            target.last_activity_monotonic_s = now
        self._tasks = {
            channel_id: self._create_target_task(target)
            for channel_id, target in self._targets.items()
        }
        self._watchdog_task = asyncio.create_task(
            self._run_health_watchdog(),
            name="openbench-device-health",
        )

    async def stop(self) -> None:
        if self._running:
            self._running = False
            self._stop_event.set()
            tasks = tuple(self._tasks.values())
            self._tasks.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            watchdog_task = self._watchdog_task
            self._watchdog_task = None
            if watchdog_task is not None:
                watchdog_task.cancel()
                await asyncio.gather(watchdog_task, return_exceptions=True)
        closables = tuple(self._closables.values())
        self._closables.clear()
        for instrument in closables:
            await instrument.close()

    def _create_target_task(self, target: PollTarget) -> asyncio.Task[None]:
        return asyncio.create_task(
            self._run_target(target),
            name=f"openbench-poll-{target.channel.id}",
        )

    async def update_interval(self, channel_id: str, interval_s: float) -> Channel:
        self.validate_interval(interval_s)
        try:
            target = self._targets[channel_id]
        except KeyError as exc:
            raise KeyError(f"Unknown poll target: {channel_id}") from exc

        target.channel = replace(target.channel, poll_interval_s=interval_s)
        task = self._tasks.pop(channel_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._running:
            self._tasks[channel_id] = self._create_target_task(target)
        return target.channel

    def interval_for(self, channel_id: str) -> float:
        try:
            return self._targets[channel_id].channel.poll_interval_s
        except KeyError as exc:
            raise KeyError(f"Unknown poll target: {channel_id}") from exc

    def target_channels(self) -> tuple[Channel, ...]:
        return tuple(target.channel for target in self._targets.values())

    def device_suspended(self, device_id: str) -> bool:
        return any(
            target.polling_suspend_count > 0
            for target in self._targets.values()
            if target.channel.device_id == device_id
        )

    async def suspend_device(self, device_id: str) -> int:
        """Pause every poll target for a device after in-flight reads finish."""
        targets = tuple(
            target
            for target in self._targets.values()
            if target.channel.device_id == device_id
        )
        for target in targets:
            target.polling_suspend_count += 1
            target.polling_resumed.clear()
        for target in targets:
            async with target.operation_lock:
                pass
        return len(targets)

    def resume_device(self, device_id: str) -> int:
        """Release one suspension level without an immediate catch-up poll."""
        resumed = 0
        now = time.monotonic()
        for target in self._targets.values():
            if target.channel.device_id != device_id or target.polling_suspend_count == 0:
                continue
            target.polling_suspend_count -= 1
            if target.polling_suspend_count == 0:
                target.last_activity_monotonic_s = now
                target.polling_not_before_monotonic_s = (
                    now + target.channel.poll_interval_s
                )
                target.stale_notified = not target.connected
                target.polling_resumed.set()
                resumed += 1
        return resumed

    def freshness_timeout_for(self, channel_id: str) -> float:
        try:
            target = self._targets[channel_id]
        except KeyError as exc:
            raise KeyError(f"Unknown poll target: {channel_id}") from exc
        return self._freshness_timeout(target)

    def _freshness_timeout(self, target: PollTarget) -> float:
        interval_s = target.channel.poll_interval_s
        return interval_s + max(
            self._stale_grace_s,
            min(5.0, interval_s * 0.25),
        )

    def target_connected(self, channel_id: str) -> bool:
        try:
            target = self._targets[channel_id]
        except KeyError as exc:
            raise KeyError(f"Unknown poll target: {channel_id}") from exc
        return self._target_connected(target)

    def _target_connected(self, target: PollTarget) -> bool:
        if target.polling_suspend_count > 0:
            return target.connected
        age_s = time.monotonic() - target.last_activity_monotonic_s
        return target.connected and age_s <= self._freshness_timeout(target)

    def device_connected(self, device_id: str, *, default: bool = True) -> bool:
        targets = tuple(
            target for target in self._targets.values() if target.channel.device_id == device_id
        )
        if not targets:
            return default
        return any(self.target_connected(target.channel.id) for target in targets)

    async def sample_now(self, channel_id: str) -> Measurement:
        try:
            target = self._targets[channel_id]
        except KeyError as exc:
            raise KeyError(f"Unknown poll target: {channel_id}") from exc
        return await self._sample_target(target)

    async def sample_now_reserved(self, channel_id: str) -> Measurement:
        """Sample explicitly while ordinary polling is reserved by a workflow."""
        try:
            target = self._targets[channel_id]
        except KeyError as exc:
            raise KeyError(f"Unknown poll target: {channel_id}") from exc
        async with target.operation_lock:
            return await self._sample_target_unlocked(target)

    async def sample_all(self) -> tuple[Measurement, ...]:
        targets = tuple(
            target for target in self._targets.values() if target.polling_suspend_count == 0
        )
        if not targets:
            return ()
        results = await asyncio.gather(
            *(self._sample_target_or_none(target) for target in targets)
        )
        return tuple(result for result in results if result is not None)

    async def _sample_target_or_none(self, target: PollTarget) -> Measurement | None:
        try:
            return await self._sample_target(target)
        except PollingSuspendedError:
            return None

    async def _sample_target(self, target: PollTarget) -> Measurement:
        async with target.operation_lock:
            if target.polling_suspend_count > 0:
                raise PollingSuspendedError(
                    f"Dashboard polling is suspended for device {target.channel.device_id}"
                )
            return await self._sample_target_unlocked(target)

    async def _sample_target_unlocked(self, target: PollTarget) -> Measurement:
        loop = asyncio.get_running_loop()
        connected = True
        try:
            result = await target.meter.read_meter(target.channel.id)
            if isinstance(result, MeterSample):
                value = result.value
                unit = result.unit or target.channel.unit
                quality = result.mode or "device_reported"
                status = result.status
            else:
                value = result
                unit = target.channel.unit
                quality = "device_reported"
                status = "ok"
            measurement = Measurement(
                timestamp_utc=datetime.now(UTC),
                monotonic_s=loop.time(),
                device_id=target.channel.device_id,
                channel_id=target.channel.id,
                value=value,
                unit=unit,
                quality=quality,
                status=status,
            )
            self._mark_activity(target, connected=True)
        except asyncio.CancelledError:
            raise
        except ValueError:
            self._mark_activity(target, connected=True)
            measurement = Measurement(
                timestamp_utc=datetime.now(UTC),
                monotonic_s=loop.time(),
                device_id=target.channel.device_id,
                channel_id=target.channel.id,
                value=None,
                unit=target.channel.unit,
                quality="stale",
                status="invalid",
            )
        except Exception:
            connected = False
            self._mark_activity(target, connected=False)
            measurement = self._disconnected_measurement(target, loop.time())
        self._measurement_service.record(measurement)
        # Persistence can be noticeably slower than the polling interval on a
        # loaded Windows host. Refresh activity after the durable write so the
        # watchdog cannot overwrite a just-completed recovery with stale state
        # before subscribers receive the successful sample.
        self._mark_activity(target, connected=connected)
        await self._event_bus.publish(measurement)
        return measurement

    @staticmethod
    def _mark_activity(target: PollTarget, *, connected: bool) -> None:
        target.connected = connected
        target.last_activity_monotonic_s = time.monotonic()
        target.stale_notified = not connected

    @staticmethod
    def _disconnected_measurement(
        target: PollTarget,
        monotonic_s: float,
    ) -> Measurement:
        return Measurement(
            timestamp_utc=datetime.now(UTC),
            monotonic_s=monotonic_s,
            device_id=target.channel.device_id,
            channel_id=target.channel.id,
            value=None,
            unit=target.channel.unit,
            quality="stale",
            status="disconnected",
        )

    async def _run_health_watchdog(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            for target in tuple(self._targets.values()):
                if target.polling_suspend_count > 0:
                    continue
                if target.stale_notified or self._target_connected(target):
                    continue
                target.connected = False
                target.stale_notified = True
                measurement = self._disconnected_measurement(target, loop.time())
                self._measurement_service.record(measurement)
                await self._event_bus.publish(measurement)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._watchdog_interval_s,
                )
            except TimeoutError:
                continue

    async def _run_target(self, target: PollTarget) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            await target.polling_resumed.wait()
            delay_s = target.polling_not_before_monotonic_s - loop.time()
            if delay_s > 0:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay_s)
                except TimeoutError:
                    continue
                continue
            target.polling_not_before_monotonic_s = 0.0
            started = loop.time()
            try:
                await self._sample_target(target)
            except PollingSuspendedError:
                continue

            remaining = max(0.0, target.channel.poll_interval_s - (loop.time() - started))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
            except TimeoutError:
                continue

    @property
    def running(self) -> bool:
        return self._running
