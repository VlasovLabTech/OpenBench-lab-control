from __future__ import annotations

import asyncio

import pytest

from openbench.core.capabilities import MeterSample
from openbench.core.events import MeasurementEventBus
from openbench.core.scheduler import PollingScheduler
from openbench.domain import Channel
from openbench.services.measurement_service import MeasurementService
from openbench.storage import Database


class StableMeter:
    device_id = "stable-meter"

    async def identify(self) -> str:
        return "stable"

    async def read_meter(self, channel_id: str) -> float:
        assert channel_id == "stable-channel"
        return 12.01


class FlakyMeter:
    device_id = "flaky-meter"

    def __init__(self) -> None:
        self.calls = 0

    async def identify(self) -> str:
        return "flaky"

    async def read_meter(self, channel_id: str) -> float:
        assert channel_id == "flaky-channel"
        self.calls += 1
        if self.calls == 1:
            raise OSError("simulated transport failure")
        return 11.99


class InvalidFrameMeter:
    device_id = "invalid-frame-meter"

    async def identify(self) -> str:
        return "invalid frame"

    async def read_meter(self, channel_id: str) -> float:
        assert channel_id == "invalid-frame-channel"
        raise ValueError("simulated malformed frame")


class HangingMeter:
    device_id = "hanging-meter"

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def identify(self) -> str:
        return "hanging"

    async def read_meter(self, channel_id: str) -> float:
        assert channel_id == "hanging-channel"
        await self.release.wait()
        return 7.5


class CountingMeter:
    device_id = "counting-meter"

    def __init__(self) -> None:
        self.calls = 0

    async def identify(self) -> str:
        return "counting"

    async def read_meter(self, channel_id: str) -> float:
        assert channel_id == "counting-channel"
        self.calls += 1
        return float(self.calls)


class DynamicModeMeter:
    device_id = "dynamic-meter"

    async def identify(self) -> str:
        return "dynamic"

    async def read_meter(self, channel_id: str) -> MeterSample:
        assert channel_id == "dynamic-channel"
        return MeterSample(value=12345.0, unit="Ω", mode="OHM")


def _scheduler(
    tmp_path: pytest.TempPathFactory,
) -> tuple[
    Database,
    MeasurementEventBus,
    PollingScheduler,
]:
    database = Database(f"sqlite:///{tmp_path / 'scheduler.db'}")
    database.create_schema()
    bus = MeasurementEventBus(queue_size=4)
    service = MeasurementService(database)
    return database, bus, PollingScheduler(service, bus)


@pytest.mark.asyncio
async def test_scheduler_produces_measurements(tmp_path: pytest.TempPathFactory) -> None:
    database, bus, scheduler = _scheduler(tmp_path)
    channel = Channel(
        id="stable-channel",
        device_id="stable-meter",
        name="Stable",
        capability="dc_voltage_meter",
        unit="V",
        poll_interval_s=0.01,
    )
    scheduler.add_target(channel, StableMeter())
    try:
        async with bus.subscribe() as queue:
            await scheduler.start()
            sample = await asyncio.wait_for(queue.get(), timeout=0.3)
            assert sample.value == 12.01
            assert sample.status == "ok"
    finally:
        await scheduler.stop()
        database.dispose()


@pytest.mark.asyncio
async def test_scheduler_survives_instrument_exception(
    tmp_path: pytest.TempPathFactory,
) -> None:
    database, bus, scheduler = _scheduler(tmp_path)
    meter = FlakyMeter()
    channel = Channel(
        id="flaky-channel",
        device_id="flaky-meter",
        name="Flaky",
        capability="dc_voltage_meter",
        unit="V",
        poll_interval_s=0.01,
    )
    scheduler.add_target(channel, meter)
    try:
        async with bus.subscribe() as queue:
            await scheduler.start()
            failed = await asyncio.wait_for(queue.get(), timeout=0.3)
            recovered = await asyncio.wait_for(queue.get(), timeout=0.3)
            assert failed.status == "disconnected"
            assert failed.quality == "stale"
            assert recovered.status == "ok"
            assert meter.calls >= 2
    finally:
        await scheduler.stop()
        database.dispose()


@pytest.mark.asyncio
async def test_scheduler_keeps_invalid_for_malformed_data(
    tmp_path: pytest.TempPathFactory,
) -> None:
    database, _bus, scheduler = _scheduler(tmp_path)
    channel = Channel(
        id="invalid-frame-channel",
        device_id="invalid-frame-meter",
        name="Invalid frame",
        capability="multimeter_reading",
        unit="V",
        poll_interval_s=0.1,
    )
    scheduler.add_target(channel, InvalidFrameMeter())
    try:
        sample = await scheduler.sample_now(channel.id)
        assert sample.status == "invalid"
        assert scheduler.target_connected(channel.id) is True
    finally:
        await scheduler.stop()
        database.dispose()


@pytest.mark.asyncio
async def test_watchdog_disconnects_stalled_meter_and_recovers(
    tmp_path: pytest.TempPathFactory,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'watchdog.db'}")
    database.create_schema()
    bus = MeasurementEventBus(queue_size=4)
    scheduler = PollingScheduler(
        MeasurementService(database),
        bus,
        stale_grace_s=0.02,
        watchdog_interval_s=0.01,
    )
    meter = HangingMeter()
    channel = Channel(
        id="hanging-channel",
        device_id="hanging-meter",
        name="Hanging",
        capability="multimeter_reading",
        unit="V",
        poll_interval_s=0.01,
    )
    scheduler.add_target(channel, meter)
    try:
        async with bus.subscribe() as queue:
            await scheduler.start()
            disconnected = await asyncio.wait_for(queue.get(), timeout=0.3)
            assert disconnected.status == "disconnected"
            assert scheduler.device_connected(meter.device_id) is False

            meter.release.set()
            recovered = await asyncio.wait_for(queue.get(), timeout=0.3)
            assert recovered.status == "ok"
            assert recovered.value == 7.5
            assert scheduler.device_connected(meter.device_id) is True
    finally:
        await scheduler.stop()
        database.dispose()


@pytest.mark.asyncio
async def test_scheduler_preserves_dynamic_meter_mode_and_unit(
    tmp_path: pytest.TempPathFactory,
) -> None:
    database, _bus, scheduler = _scheduler(tmp_path)
    channel = Channel(
        id="dynamic-channel",
        device_id="dynamic-meter",
        name="Dynamic",
        capability="multimeter_reading",
        unit="V",
        poll_interval_s=0.1,
    )
    scheduler.add_target(channel, DynamicModeMeter())
    try:
        sample = await scheduler.sample_now(channel.id)
        assert sample.value == 12345.0
        assert sample.unit == "Ω"
        assert sample.quality == "OHM"
        assert sample.status == "ok"
    finally:
        await scheduler.stop()
        database.dispose()


@pytest.mark.parametrize("interval", [0.009, 600.01])
def test_polling_interval_rejects_out_of_range(interval: float) -> None:
    with pytest.raises(ValueError, match="between"):
        PollingScheduler.validate_interval(interval)


@pytest.mark.parametrize("interval", [0.01, 600.0])
def test_polling_interval_accepts_boundaries(interval: float) -> None:
    assert PollingScheduler.validate_interval(interval) == interval


@pytest.mark.asyncio
async def test_event_bus_removes_subscriber_and_bounds_queue() -> None:
    bus = MeasurementEventBus(queue_size=1)
    assert bus.subscriber_count == 0
    async with bus.subscribe() as queue:
        assert bus.subscriber_count == 1
        assert queue.maxsize == 1
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_target_added_after_start_is_polled(
    tmp_path: pytest.TempPathFactory,
) -> None:
    database, bus, scheduler = _scheduler(tmp_path)
    channel = Channel(
        id="stable-channel",
        device_id="stable-meter",
        name="Stable",
        capability="dc_voltage_meter",
        unit="V",
        poll_interval_s=0.1,
    )
    try:
        await scheduler.start()
        async with bus.subscribe() as queue:
            scheduler.add_target(channel, StableMeter())
            sample = await asyncio.wait_for(queue.get(), timeout=0.3)
            assert sample.channel_id == channel.id
            assert sample.value == 12.01
    finally:
        await scheduler.stop()
        database.dispose()


@pytest.mark.asyncio
async def test_polling_interval_can_be_changed_while_running(
    tmp_path: pytest.TempPathFactory,
) -> None:
    database, _bus, scheduler = _scheduler(tmp_path)
    channel = Channel(
        id="stable-channel",
        device_id="stable-meter",
        name="Stable",
        capability="dc_voltage_meter",
        unit="V",
        poll_interval_s=1.0,
    )
    scheduler.add_target(channel, StableMeter())
    try:
        await scheduler.start()
        updated = await scheduler.update_interval(channel.id, 600.0)
        assert updated.poll_interval_s == 600.0
        assert scheduler.interval_for(channel.id) == 600.0
    finally:
        await scheduler.stop()
        database.dispose()


@pytest.mark.asyncio
async def test_device_polling_can_be_suspended_and_resumed(
    tmp_path: pytest.TempPathFactory,
) -> None:
    database, bus, scheduler = _scheduler(tmp_path)
    meter = CountingMeter()
    channel = Channel(
        id="counting-channel",
        device_id=meter.device_id,
        name="Counting",
        capability="multimeter_reading",
        unit="V",
        poll_interval_s=0.08,
    )
    scheduler.add_target(channel, meter)
    try:
        async with bus.subscribe() as queue:
            await scheduler.start()
            await asyncio.wait_for(queue.get(), timeout=0.3)
            assert await scheduler.suspend_device(meter.device_id) == 1
            calls_at_suspend = meter.calls
            assert scheduler.device_suspended(meter.device_id) is True
            assert await scheduler.sample_all() == ()
            await asyncio.sleep(0.03)
            assert meter.calls == calls_at_suspend

            assert scheduler.resume_device(meter.device_id) == 1
            await asyncio.sleep(0.03)
            assert meter.calls == calls_at_suspend
            await asyncio.wait_for(queue.get(), timeout=0.3)
            assert meter.calls > calls_at_suspend
            assert scheduler.device_suspended(meter.device_id) is False
    finally:
        await scheduler.stop()
        database.dispose()
