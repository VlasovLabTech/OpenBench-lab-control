from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

import pytest

from openbench.core.events import MeasurementEventBus
from openbench.drivers.micsig_mho1 import (
    SCALAR_MEASUREMENT_COMMANDS,
    MicsigScalarMeasurement,
    MicsigScalarMeasurementSpec,
    MicsigSnapshot,
)
from openbench.services.scope_measurement_service import (
    MAX_SCOPE_MEASUREMENTS,
    ScopeMeasurementSelection,
    ScopeMeasurementService,
    scope_event_channel_id,
)
from openbench.storage import Database, InstrumentPreferenceStore


class FakeLiveScope:
    device_id = "micsig_scope_live_test"

    def __init__(self) -> None:
        self.query_times: list[float] = []
        self.capture_times: list[float] = []
        self.capture_requests: list[tuple[tuple[str, ...], bool]] = []
        self.capture_modes: list[tuple[bool, bool]] = []
        self.control_actions: list[str] = []

    async def read_scalar_measurements(
        self,
        channel: int | str,
        items: Sequence[str],
    ) -> tuple[MicsigScalarMeasurement, ...]:
        self.query_times.append(time.monotonic())
        normalized = f"CH{channel}" if isinstance(channel, int) else channel
        return tuple(
            MicsigScalarMeasurement(
                item=item,
                channel=normalized,
                value=float(index),
                unit=SCALAR_MEASUREMENT_COMMANDS[item][1],
                status="ok",
            )
            for index, item in enumerate(items, start=1)
        )

    async def replace_scalar_measurements(
        self,
        measurements: Sequence[MicsigScalarMeasurementSpec],
    ) -> tuple[MicsigScalarMeasurement, ...]:
        return await self.read_scalar_measurement_profile(measurements)

    async def read_scalar_measurement_profile(
        self,
        measurements: Sequence[MicsigScalarMeasurementSpec],
    ) -> tuple[MicsigScalarMeasurement, ...]:
        self.query_times.append(time.monotonic())
        return tuple(
            MicsigScalarMeasurement(
                item=spec.item,
                channel=spec.channel,
                secondary_channel=spec.secondary_channel,
                source_edge=spec.source_edge,
                target_edge=spec.target_edge,
                value=float(index),
                unit=SCALAR_MEASUREMENT_COMMANDS[spec.item][1],
                status="ok",
            )
            for index, spec in enumerate(measurements, start=1)
        )

    async def capture_frame(
        self,
        measurements: Sequence[MicsigScalarMeasurementSpec],
        *,
        channels: Sequence[int | str] = (1, 2, 3, 4),
        include_screenshot: bool = True,
        stop_before_capture: bool = True,
        resume_after: bool = True,
    ) -> MicsigSnapshot:
        self.capture_times.append(time.monotonic())
        self.capture_requests.append(
            (
                tuple(
                    f"CH{channel}" if isinstance(channel, int) else channel for channel in channels
                ),
                include_screenshot,
            )
        )
        self.capture_modes.append((stop_before_capture, resume_after))
        scalars = await self.read_scalar_measurement_profile(measurements)
        return MicsigSnapshot(
            region="screen",
            measurements=scalars,
            measurements_csv=b"",
            screenshot=None,
            screenshot_error=None,
            elapsed_s=0.1,
        )

    async def single(self, *, wait_timeout_s: float | None = None) -> None:
        del wait_timeout_s
        self.control_actions.append("SINGLE")

    async def wait_for_trigger(
        self,
        *,
        timeout_s: float | None = None,
        poll_interval_s: float = 0.05,
    ) -> str:
        del timeout_s, poll_interval_s
        self.control_actions.append("STOP")
        return "STOP"

    async def start(self) -> None:
        self.control_actions.append("RUN")


@pytest.mark.asyncio
async def test_scope_profile_is_limited_to_ten_and_supports_removal() -> None:
    service = ScopeMeasurementService(MeasurementEventBus())
    scope = FakeLiveScope()
    await service.add_scope(scope)
    candidates = tuple(
        (channel, item)
        for channel in ("CH1", "CH2", "CH3", "CH4")
        for item in SCALAR_MEASUREMENT_COMMANDS
        if item not in {"phase", "delay"}
    )

    for channel, item in candidates[:MAX_SCOPE_MEASUREMENTS]:
        await service.add_selection(scope.device_id, channel=channel, item=item)
    await service.add_selection(
        scope.device_id,
        channel=candidates[0][0],
        item=candidates[0][1],
    )
    with pytest.raises(ValueError, match="requires a valid secondary channel"):
        await service.add_selection(scope.device_id, channel="CH1", item="phase")

    assert len(service.selections(scope.device_id)) == MAX_SCOPE_MEASUREMENTS
    with pytest.raises(ValueError, match="No more than 10"):
        await service.add_selection(
            scope.device_id,
            channel=candidates[MAX_SCOPE_MEASUREMENTS][0],
            item=candidates[MAX_SCOPE_MEASUREMENTS][1],
        )

    removed = service.selections(scope.device_id)[3]
    await service.remove_selection(
        scope.device_id,
        channel=removed.channel,
        item=removed.item,
    )
    assert removed not in service.selections(scope.device_id)


@pytest.mark.asyncio
async def test_scope_live_polling_never_starts_faster_than_two_seconds() -> None:
    event_bus = MeasurementEventBus()
    service = ScopeMeasurementService(event_bus)
    scope = FakeLiveScope()
    await service.add_scope(scope)
    await service.add_selection(scope.device_id, channel="CH1", item="amplitude")
    await service.add_selection(scope.device_id, channel="CH1", item="frequency")
    # Even with Screenshot and all ASCII channels selected, idle Dashboard
    # polling is scalar-only and must never enter the STOP/frame path.
    service.update_capture_options(
        scope.device_id,
        screen=True,
        data=True,
        channels=("CH1", "CH2", "CH3", "CH4"),
    )
    scope.query_times.clear()
    scope.capture_requests.clear()

    async with event_bus.subscribe() as queue:
        await service.start()
        try:
            first = await asyncio.wait_for(queue.get(), timeout=0.2)
            second = await asyncio.wait_for(queue.get(), timeout=0.2)
            await asyncio.sleep(2.05)
        finally:
            await service.stop()

    assert first.channel_id.endswith(".scope.ch1.amplitude")
    assert second.channel_id.endswith(".scope.ch1.frequency")
    assert len(scope.query_times) >= 2
    assert scope.query_times[1] - scope.query_times[0] >= 1.99
    assert scope.capture_requests == []


def test_scope_polling_rejects_a_faster_interval() -> None:
    with pytest.raises(ValueError, match=r"at least 2 seconds"):
        ScopeMeasurementService(MeasurementEventBus(), poll_interval_s=1.99)


@pytest.mark.asyncio
async def test_scope_phase_and_delay_keep_their_second_channel_and_edges() -> None:
    service = ScopeMeasurementService(MeasurementEventBus())
    scope = FakeLiveScope()
    await service.add_scope(scope)
    selections = (
        ScopeMeasurementSelection("CH1", "phase", "CH2"),
        ScopeMeasurementSelection("CH1", "delay", "CH2", "FRISe", "FFALL"),
    )

    readings = await service.replace_selections(scope.device_id, selections)

    assert service.selections(scope.device_id) == selections
    assert [reading.scalar.value for reading in readings] == [1.0, 2.0]
    assert readings[0].event_channel_id.endswith(".scope.ch1.ch2.phase")
    assert readings[1].event_channel_id.endswith(".scope.ch1.ch2.delay.frise.ffall")
    assert scope_event_channel_id(
        scope.device_id,
        "CH1",
        "delay",
        secondary_channel="CH2",
        source_edge="FRISe",
        target_edge="FFALL",
    ).endswith(".scope.ch1.ch2.delay.frise.ffall")


@pytest.mark.asyncio
async def test_scope_preferences_survive_service_restart(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'scope-preferences.db'}")
    database.create_schema()
    preferences = InstrumentPreferenceStore(database)
    selections = (
        ScopeMeasurementSelection("CH1", "amplitude"),
        ScopeMeasurementSelection("CH3", "frequency"),
        ScopeMeasurementSelection("CH1", "phase", "CH2"),
        ScopeMeasurementSelection("CH1", "delay", "CH2", "FRISe", "FFALL"),
    )

    first_service = ScopeMeasurementService(
        MeasurementEventBus(),
        preferences=preferences,
    )
    first_scope = FakeLiveScope()
    await first_service.add_scope(first_scope)
    first_service.update_interval(first_scope.device_id, 3.5)
    first_service.update_capture_options(
        first_scope.device_id,
        screen=False,
        data=True,
        channels=("CH1", "CH3"),
        wait_for_trigger=True,
    )
    await first_service.replace_selections(first_scope.device_id, selections)

    restarted_service = ScopeMeasurementService(
        MeasurementEventBus(),
        preferences=preferences,
    )
    reconnected_scope = FakeLiveScope()
    await restarted_service.add_scope(reconnected_scope)

    assert restarted_service.interval_for(reconnected_scope.device_id) == 3.5
    assert restarted_service.capture_options(reconnected_scope.device_id).screen is False
    assert restarted_service.capture_options(reconnected_scope.device_id).channels == (
        "CH1",
        "CH3",
    )
    assert restarted_service.capture_options(reconnected_scope.device_id).wait_for_trigger is True
    assert restarted_service.selections(reconnected_scope.device_id) == selections
    assert reconnected_scope.query_times == []
    database.dispose()


@pytest.mark.asyncio
async def test_explicit_frames_leave_acquisition_running_for_250_ms() -> None:
    service = ScopeMeasurementService(MeasurementEventBus())
    scope = FakeLiveScope()
    await service.add_scope(scope)

    await service.sample_now(scope.device_id)
    await service.sample_now(scope.device_id)

    assert scope.capture_times[1] - scope.capture_times[0] >= 0.249


@pytest.mark.asyncio
async def test_scope_capture_options_control_optional_poll_artifacts() -> None:
    service = ScopeMeasurementService(MeasurementEventBus())
    scope = FakeLiveScope()
    await service.add_scope(scope)

    disabled = service.update_capture_options(
        scope.device_id,
        screen=False,
        data=False,
        channels=("CH2", "CH4"),
    )
    await service.sample_now(scope.device_id)

    assert disabled.active_channels == ()
    assert scope.capture_requests[-1] == ((), False)

    enabled = service.update_capture_options(scope.device_id, data=True)
    await service.sample_now(scope.device_id)

    assert enabled.active_channels == ("CH2", "CH4")
    assert scope.capture_requests[-1] == (("CH2", "CH4"), False)

    await service.sample_now(
        scope.device_id,
        waveform_channels=("CH3",),
        include_screenshot=True,
    )

    assert scope.capture_requests[-1] == (("CH3",), True)


@pytest.mark.asyncio
async def test_triggered_frame_uses_single_and_reads_without_stop_run_cycle() -> None:
    service = ScopeMeasurementService(MeasurementEventBus())
    scope = FakeLiveScope()
    await service.add_scope(scope)
    await service.add_selection(scope.device_id, channel="CH1", item="amplitude")
    service.update_capture_options(
        scope.device_id,
        screen=True,
        data=True,
        channels=("CH1", "CH2"),
        wait_for_trigger=True,
    )

    result = await service.capture_triggered_frame(scope.device_id)

    assert result.frame.measurements[0].item == "amplitude"
    assert scope.control_actions == ["SINGLE", "STOP", "RUN"]
    assert scope.capture_requests[-1] == (("CH1", "CH2"), True)
    assert scope.capture_modes[-1] == (False, False)


@pytest.mark.asyncio
async def test_scope_polling_can_be_configured_per_device() -> None:
    service = ScopeMeasurementService(MeasurementEventBus())
    first = FakeLiveScope()

    class SecondFakeLiveScope(FakeLiveScope):
        device_id = "micsig_scope_second_test"

    second = SecondFakeLiveScope()
    await service.add_scope(first)
    await service.add_scope(second)

    assert service.interval_for(first.device_id) == 2.0
    assert service.interval_for(second.device_id) == 2.0
    assert service.update_interval(first.device_id, 5.0) == 5.0
    assert service.interval_for(first.device_id) == 5.0
    assert service.interval_for(second.device_id) == 2.0
    assert service.freshness_timeout_for(first.device_id) == 8.5
    with pytest.raises(ValueError, match=r"between 2 and 600"):
        service.update_interval(first.device_id, 1.99)
