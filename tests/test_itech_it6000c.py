from __future__ import annotations

import csv
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from openbench import bootstrap
from openbench.bootstrap import register_itech_it6000c_devices
from openbench.domain import Channel, Device
from openbench.drivers.itech_it6000c import (
    ITechAdvancedUpdate,
    ITechIT6000C,
    ITechIT6000CDescriptor,
    ITechIT6000CSerialTransport,
    ITechOperatingPointUpdate,
    ITechProtectionUpdate,
    safety_warnings,
)
from openbench.drivers.itech_it6000c.protocol import parse_identity

IDN = "ITECH Electronics,IT6054C-800-225,ITECH-DEMO-0001,000.006.101,640.R,132.R"


class StatefulITechTransport:
    def __init__(self) -> None:
        self.values = {
            "OUTP": "0",
            "FUNC": "VOLTage",
            "FUNC:MODE": "FIXed",
            "VOLT": "4",
            "CURR": "2.31",
            "CURR:LIM": "2.31",
            "CURR:LIM:NEG": "-2.27",
            "VOLT:LIM": "0.2",
            "VOLT:LIM:NEG": "0",
            "POW:LIM": "55080",
            "POW:LIM:NEG": "-55080",
            "VOLT:SLEW:POS": "0.1",
            "VOLT:SLEW:NEG": "0.1",
            "CURR:SLEW:POS": "0.1",
            "CURR:SLEW:NEG": "0.1",
            "VOLT:PROT:STAT": "1",
            "VOLT:PROT": "12.8",
            "VOLT:PROT:DEL": "0.005",
            "CURR:PROT:STAT": "1",
            "CURR:PROT": "227.25",
            "CURR:PROT:DEL": "0.01",
            "POW:PROT:STAT": "1",
            "POW:PROT": "3200",
            "POW:PROT:DEL": "10",
            "VOLT:UND:PROT:STAT": "1",
            "VOLT:UND:PROT": "10",
            "VOLT:UND:PROT:DEL": "1",
            "VOLT:UND:PROT:WARM": "20",
            "CURR:UND:PROT:STAT": "0",
            "CURR:UND:PROT": "-227.25",
            "CURR:UND:PROT:DEL": "60",
            "CURR:UND:PROT:WARM": "0",
            "OUTP:DEL:RISE": "0",
            "OUTP:DEL:FALL": "0",
            "OUTP:PROT:WDOG:STAT": "0",
            "OUTP:PROT:WDOG:DEL": "30",
            "SINK:RES:STAT": "0",
            "SYST:VOLT:RZERO": "1",
            "STAT:QUES:COND": "0",
            "STAT:OPER:COND": "0",
            "MEAS:VOLT": "0.03",
            "MEAS:CURR": "-0.002",
            "MEAS:POW": "-0.0001",
        }
        self.writes: list[str] = []
        self.queries: list[str] = []
        self.closed = False

    def query(self, command: str) -> str:
        self.queries.append(command)
        if command == "*IDN?":
            return IDN
        key = command.removesuffix("?")
        if key == "CURR" and self.values["FUNC"].startswith("VOLT"):
            key = "CURR:LIM"
        elif key == "VOLT" and self.values["FUNC"].startswith("CURR"):
            key = "VOLT:LIM"
        return self.values[key]

    def write(self, command: str) -> None:
        command = " ".join(command.upper().split())
        self.writes.append(command)
        if command in {"SYST:REM", "SYST:LOC"}:
            return
        if command == "OUTP ON":
            self.values["OUTP"] = "1"
            self.values["STAT:OPER:COND"] = "256"
            return
        if command == "OUTP OFF":
            self.values["OUTP"] = "0"
            self.values["STAT:OPER:COND"] = "0"
            return
        if command == "OUTP:PROT:CLE":
            self.values["STAT:QUES:COND"] = "0"
            return
        if command == "FUNC VOLT":
            current = float(self.values["CURR"])
            self.values["CURR:LIM" if current >= 0 else "CURR:LIM:NEG"] = str(current)
            self.values["FUNC"] = "VOLTage"
            return
        if command == "FUNC CURR":
            self.values["VOLT:LIM"] = self.values["VOLT"]
            self.values["FUNC"] = "CURRent"
            return
        if command == "FUNC:MODE FIXED":
            self.values["FUNC:MODE"] = "FIXed"
            return
        key, value = command.split(" ", 1)
        self.values[key] = "1" if value == "ON" else "0" if value == "OFF" else value

    def close(self) -> None:
        self.closed = True


def descriptor(baud_rate: int = 115200, *, port: str = "COM11") -> ITechIT6000CDescriptor:
    return ITechIT6000CDescriptor(
        port=port,
        vid=0x2EC7,
        pid=0xA4A7,
        usb_serial_number="",
        location="1-4",
        description="USB Serial Device",
        identity=parse_identity(IDN),
        baud_rate=baud_rate,
    )


def test_parses_exact_live_identity_and_rejects_other_models() -> None:
    identity = parse_identity(IDN)
    assert identity.model == "IT6054C-800-225"
    assert identity.serial_number == "ITECH-DEMO-0001"
    with pytest.raises(ValueError, match="Unsupported"):
        parse_identity("ITECH Electronics,IT6012C-800-50,1,1")


def test_discovery_falls_back_to_9600_and_keeps_identity() -> None:
    class FakePort:
        def __init__(self, *, baudrate: int, **_: object) -> None:
            self.baudrate = baudrate
            self.is_open = True

        def reset_input_buffer(self) -> None:
            pass

        def write(self, _: bytes) -> None:
            pass

        def flush(self) -> None:
            pass

        def read_until(self, *_: object) -> bytes:
            return (IDN + "\n").encode() if self.baudrate == 9600 else b""

        def close(self) -> None:
            self.is_open = False

    serial_module = SimpleNamespace(Serial=FakePort)
    list_ports = SimpleNamespace(
        comports=lambda: [
            SimpleNamespace(
                device="COM11",
                vid=0x2EC7,
                pid=0xA4A7,
                serial_number=None,
                location="1-4",
                description="USB Serial Device",
            )
        ]
    )
    found = ITechIT6000CSerialTransport.discover(
        serial_module=serial_module,
        list_ports_module=list_ports,
    )
    assert len(found) == 1
    assert found[0].baud_rate == 9600
    assert found[0].identity.serial_number == "ITECH-DEMO-0001"


def test_discovery_releases_serialexception_claim_and_retries_same_baud() -> None:
    class FakePort:
        def __init__(self, **_: object) -> None:
            self.is_open = True

        def reset_input_buffer(self) -> None:
            pass

        def write(self, _: bytes) -> None:
            pass

        def flush(self) -> None:
            pass

        def read_until(self, *_: object) -> bytes:
            return (IDN + "\n").encode()

        def close(self) -> None:
            self.is_open = False

    opens = 0

    def open_port(**kwargs: object) -> FakePort:
        nonlocal opens
        opens += 1
        if opens == 1:
            raise OSError("could not open port 'COM10': Access is denied")
        return FakePort(**kwargs)

    releases = 0

    def release_port() -> None:
        nonlocal releases
        releases += 1

    found = ITechIT6000CSerialTransport.discover(
        serial_module=SimpleNamespace(Serial=open_port),
        list_ports_module=SimpleNamespace(
            comports=lambda: [
                SimpleNamespace(
                    device="COM10",
                    vid=0x2EC7,
                    pid=0xA4A7,
                    serial_number=None,
                    location="1-4",
                    description="USB Serial Device",
                )
            ]
        ),
        release_port=release_port,
    )

    assert releases == 1
    assert opens == 2
    assert len(found) == 1
    assert found[0].port == "COM10"
    assert found[0].baud_rate == 115200


def test_itech_discovery_restores_saved_poll_interval(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = client.app.state.context
    instrument = ITechIT6000C(descriptor(), transport=StatefulITechTransport())
    context.instrument_preferences.update(instrument.device_id, poll_interval_s=5.0)
    monkeypatch.setattr(
        ITechIT6000CSerialTransport,
        "discover",
        staticmethod(lambda: (descriptor(),)),
    )
    monkeypatch.setattr(bootstrap, "ITechIT6000C", lambda _descriptor: instrument)

    discovered = client.portal.call(register_itech_it6000c_devices, context)

    assert [device.id for device in discovered] == [instrument.device_id]
    intervals = {
        context.scheduler.interval_for(channel.id)
        for channel in context.registry.channels()
        if channel.device_id == instrument.device_id
    }
    assert intervals == {5.0}


def test_itech_discovery_replaces_disconnected_transport(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = client.app.state.context
    old_transport = StatefulITechTransport()
    new_transport = StatefulITechTransport()
    old_instrument = ITechIT6000C(
        descriptor(port="COM11"),
        transport=old_transport,
    )
    new_instrument = ITechIT6000C(
        descriptor(port="COM10"),
        transport=new_transport,
    )
    context.instrument_preferences.update(old_instrument.device_id, poll_interval_s=5.0)
    instruments = iter((old_instrument, new_instrument))
    descriptors = iter(((descriptor(port="COM11"),), (descriptor(port="COM10"),)))
    monkeypatch.setattr(
        ITechIT6000CSerialTransport,
        "discover",
        staticmethod(lambda: next(descriptors)),
    )
    monkeypatch.setattr(bootstrap, "ITechIT6000C", lambda _descriptor: next(instruments))

    client.portal.call(register_itech_it6000c_devices, context)
    monkeypatch.setattr(context.scheduler, "device_connected", lambda *_args, **_kwargs: False)
    discovered = client.portal.call(register_itech_it6000c_devices, context)

    assert [device.id for device in discovered] == [new_instrument.device_id]
    assert context.registry.instrument(new_instrument.device_id) is new_instrument
    assert old_transport.closed
    assert "OUTP OFF" not in old_transport.writes
    assert {
        context.scheduler.interval_for(channel.id)
        for channel in context.registry.channels()
        if channel.device_id == new_instrument.device_id
    } == {5.0}


@pytest.mark.asyncio
async def test_failed_read_cools_down_sibling_channel_retries() -> None:
    class FailingLiveTransport(StatefulITechTransport):
        def __init__(self) -> None:
            super().__init__()
            self.fail_live = False
            self.failed_voltage_queries = 0

        def query(self, command: str) -> str:
            if self.fail_live and command == "MEAS:VOLT?":
                self.failed_voltage_queries += 1
                raise TimeoutError("link lost")
            return super().query(command)

    transport = FailingLiveTransport()
    instrument = ITechIT6000C(descriptor(), transport=transport)
    await instrument.read_state(force=True, full=True)
    transport.fail_live = True
    instrument._cached_at = 0.0

    with pytest.raises(TimeoutError, match="link lost"):
        await instrument.read_state(full=False)
    with pytest.raises(ConnectionError, match="link lost"):
        await instrument.read_state(full=False)

    assert transport.failed_voltage_queries == 1
    assert transport.closed


@pytest.mark.asyncio
async def test_driver_reads_updates_and_forces_output_off() -> None:
    transport = StatefulITechTransport()
    instrument = ITechIT6000C(descriptor(), transport=transport)
    state = await instrument.read_state(force=True, full=True)
    assert state.priority == "CV"
    assert state.direction == "IDLE"
    assert "power_limit_positive_w" in {item["field"] for item in safety_warnings(state)}

    with pytest.raises(ValueError, match="wiring_confirmed"):
        await instrument.update_operating_point(ITechOperatingPointUpdate(output_enabled=True))

    updated = await instrument.update_operating_point(
        ITechOperatingPointUpdate(
            priority="CC",
            current_setpoint_a=-0.1,
            voltage_limit_positive_v=1.0,
            power_limit_positive_w=10,
            power_limit_negative_w=-10,
            output_enabled=True,
            wiring_confirmed=True,
        )
    )
    assert updated.priority == "CC"
    assert updated.output_enabled
    assert updated.current_setpoint_a == -0.1
    assert updated.current_limit_positive_a == 2.31
    assert updated.current_limit_negative_a == -2.27

    transport.writes.clear()
    transport.queries.clear()
    stepped = await instrument.update_operating_point(
        ITechOperatingPointUpdate(current_setpoint_a=-0.2, wiring_confirmed=True)
    )
    assert stepped.output_enabled
    assert stepped.current_setpoint_a == -0.2
    assert transport.writes == ["SYST:REM", "CURR -0.200000", "SYST:LOC"]
    assert transport.queries == []

    await instrument.update_operating_point(ITechOperatingPointUpdate(output_enabled=False))
    protected = await instrument.update_protections(
        ITechProtectionUpdate(uvp_enabled=False, ocp_level_a=1.0)
    )
    assert not protected.uvp_enabled
    assert protected.ocp_level_a == 1.0
    advanced = await instrument.update_advanced(
        ITechAdvancedUpdate(watchdog_enabled=True, watchdog_delay_s=60)
    )
    assert advanced.watchdog_enabled

    await instrument.close()
    assert transport.values["OUTP"] == "0"
    assert transport.closed


@pytest.mark.asyncio
async def test_priority_change_accepts_hardware_aliased_inactive_setpoint() -> None:
    transport = StatefulITechTransport()
    instrument = ITechIT6000C(descriptor(), transport=transport)

    updated = await instrument.update_operating_point(
        ITechOperatingPointUpdate(
            priority="CV",
            voltage_setpoint_v=1.0,
            current_setpoint_a=0.0,
            current_limit_positive_a=0.1,
            current_limit_negative_a=-0.1,
            voltage_limit_positive_v=1.0,
            voltage_limit_negative_v=0.0,
            output_enabled=False,
        )
    )

    assert updated.priority == "CV"
    assert updated.voltage_setpoint_v == 1.0
    assert updated.current_setpoint_a == 0.1
    assert updated.current_limit_positive_a == 0.1


@pytest.mark.asyncio
async def test_clear_protection_requires_output_off_and_clears_latch() -> None:
    transport = StatefulITechTransport()
    instrument = ITechIT6000C(descriptor(), transport=transport)
    transport.values["STAT:QUES:COND"] = "1"

    cleared = await instrument.clear_protection()

    assert cleared.faults == ()
    assert "OUTP:PROT:CLE" in transport.writes
    assert transport.values["OUTP"] == "0"

    transport.values["OUTP"] = "1"
    with pytest.raises(ValueError, match="output OFF"):
        await instrument.clear_protection()


def _register_fake_itech(client: TestClient) -> tuple[ITechIT6000C, StatefulITechTransport]:
    context = client.app.state.context
    transport = StatefulITechTransport()
    instrument = ITechIT6000C(descriptor(), transport=transport)

    async def read_initial_state() -> None:
        await instrument.read_state(force=True, full=True)

    client.portal.call(read_initial_state)
    device = Device(
        id=instrument.device_id,
        name="ITECH IT6054C-800-225",
        kind="itech_it6000c",
        connected=True,
        capabilities=("bidirectional_power_supply", "source", "sink"),
    )
    channels = tuple(
        Channel(
            id=channel_id,
            device_id=instrument.device_id,
            name=parameter.name,
            capability=parameter.capability,
            unit=parameter.unit,
            poll_interval_s=2.0,
        )
        for channel_id, parameter in instrument.parameters
    )
    context.registry.register(device, instrument, channels)
    context.device_service.register(device, channels)
    for channel in channels:
        client.portal.call(context.scheduler.add_target, channel, instrument)
    return instrument, transport


def test_api_and_dashboard_expose_safe_bidirectional_control(client: TestClient) -> None:
    instrument, transport = _register_fake_itech(client)
    response = client.get(f"/api/v1/bidirectional-power-supplies/{instrument.device_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["identity"]["model"] == "IT6054C-800-225"
    assert payload["baud_rate"] == 115200
    assert payload["state"]["priority"] == "CV"
    assert payload["warnings"]

    blocked = client.patch(
        f"/api/v1/bidirectional-power-supplies/{instrument.device_id}/operating-point",
        json={"output_enabled": True},
    )
    assert blocked.status_code == 400
    assert transport.values["OUTP"] == "0"

    enabled = client.patch(
        f"/api/v1/bidirectional-power-supplies/{instrument.device_id}/operating-point",
        json={
            "priority": "CV",
            "voltage_setpoint_v": 1.0,
            "current_limit_positive_a": 0.1,
            "current_limit_negative_a": -0.1,
            "power_limit_positive_w": 10,
            "power_limit_negative_w": -10,
            "output_enabled": True,
            "wiring_confirmed": True,
        },
    )
    assert enabled.status_code == 200
    assert enabled.json()["state"]["output_enabled"] is True

    disabled = client.patch(
        f"/api/v1/bidirectional-power-supplies/{instrument.device_id}/operating-point",
        json={"output_enabled": False},
    )
    assert disabled.status_code == 200
    transport.values["STAT:QUES:COND"] = "1"
    cleared = client.post(
        f"/api/v1/bidirectional-power-supplies/{instrument.device_id}/protections/clear"
    )
    assert cleared.status_code == 200
    assert cleared.json()["state"]["faults"] == []

    base_path = f"/api/v1/bidirectional-power-supplies/{instrument.device_id}"
    reserved = client.post(f"{base_path}/experiment-reservation")
    assert reserved.status_code == 200
    transport.queries.clear()
    instrument._cached_at = 0
    measured = client.get(f"{base_path}/measurements")
    assert measured.status_code == 200
    assert measured.json()["measured_voltage_v"] == 0.03
    assert measured.json()["measured_current_a"] == -0.002
    assert measured.json()["measured_power_w"] == pytest.approx(-0.00006)
    assert transport.queries == ["MEAS:VOLT?", "MEAS:CURR?"]
    released = client.delete(f"{base_path}/experiment-reservation")
    assert released.status_code == 200

    page = client.get("/")
    assert page.status_code == 200
    assert "IT6054C-800-225" in page.text
    assert "Positive current limit" in page.text
    assert "Safety review" in page.text

    stopped = client.post("/api/v1/emergency-stop", json={"reason": "test"})
    assert stopped.status_code == 200
    assert stopped.json()["bidirectional_power_supply_errors"] == []
    assert transport.values["OUTP"] == "0"


def test_experiment_reservation_blocks_background_polling_and_keeps_compact_reads(
    client: TestClient,
) -> None:
    instrument, transport = _register_fake_itech(client)
    context = client.app.state.context
    base_path = f"/api/v1/bidirectional-power-supplies/{instrument.device_id}"

    reserved = client.post(f"{base_path}/experiment-reservation")
    assert reserved.status_code == 200
    assert reserved.json() == {
        "device_id": instrument.device_id,
        "active": True,
        "polling_targets_suspended": len(instrument.parameters),
    }
    assert context.scheduler.device_suspended(instrument.device_id) is True

    duplicate = client.post(f"{base_path}/experiment-reservation")
    assert duplicate.status_code == 400

    transport.queries.clear()
    instrument._cached_at = 0.0
    measured = client.get(f"{base_path}/measurements")
    assert measured.status_code == 200
    assert transport.queries
    assert len(transport.queries) % 2 == 0
    assert all(
        transport.queries[index : index + 2] == ["MEAS:VOLT?", "MEAS:CURR?"]
        for index in range(0, len(transport.queries), 2)
    )

    queries_after_point_read = list(transport.queries)
    started = client.post(
        "/api/v1/captures/recording/start",
        json={"title": "Reserved ITECH", "comment": "No extra full state read"},
    )
    assert started.status_code == 201
    assert transport.queries == queries_after_point_read
    assert client.post("/api/v1/captures/recording/stop").status_code == 200

    instrument._cached_state = None
    transport.queries.clear()
    started_without_cache = client.post(
        "/api/v1/captures/recording/start",
        json={"title": "Reserved no-cache", "comment": "Do not add a full read"},
    )
    assert started_without_cache.status_code == 201
    assert transport.queries == []
    assert client.post("/api/v1/captures/recording/stop").status_code == 200

    released = client.delete(f"{base_path}/experiment-reservation")
    assert released.status_code == 200
    assert released.json() == {
        "device_id": instrument.device_id,
        "active": False,
        "polling_targets_suspended": len(instrument.parameters),
    }
    assert context.scheduler.device_suspended(instrument.device_id) is False
    assert client.delete(f"{base_path}/experiment-reservation").json()[
        "polling_targets_suspended"
    ] == 0


def test_recording_streams_only_itech_measurements_and_writes_settings_once(
    client: TestClient,
) -> None:
    instrument, _transport = _register_fake_itech(client)

    started = client.post(
        "/api/v1/captures/recording/start",
        json={"title": "ITECH compact", "comment": "Settings belong in the header"},
    )
    assert started.status_code == 201
    stopped = client.post("/api/v1/captures/recording/stop")
    assert stopped.status_code == 200
    path = client.app.state.context.capture_service.status().last_recording_file
    assert path is not None
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.reader(stream))

    group_header = rows[2]
    field_header = rows[3]
    itech_header = next(cell for cell in group_header if f"ID: {instrument.device_id}" in cell)
    assert "initial_settings:" in itech_header
    assert "current_limit_positive=2.31 A" in itech_header
    assert "power_limit_negative=-55080 W" in itech_header
    assert "output=OFF" in itech_header
    assert "priority=CV" in itech_header
    assert "direction=IDLE" in itech_header

    itech_fields = [field for field in field_header if instrument.device_id in field]
    for suffix in ("voltage", "current", "power", "set_voltage", "set_current"):
        assert any(f".{suffix}_value" in field for field in itech_fields)
    for static_suffix in (
        "current_limit_positive",
        "current_limit_negative",
        "voltage_limit_positive",
        "voltage_limit_negative",
        "power_limit_positive",
        "power_limit_negative",
        "output",
        "priority",
        "direction",
    ):
        assert not any(f".{static_suffix}_" in field for field in itech_fields)
