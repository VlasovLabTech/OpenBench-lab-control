from __future__ import annotations

import asyncio
import struct
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from openbench.domain import Channel, Device
from openbench.drivers.fnirsi_dps150 import (
    DPS150_PARAMETERS,
    FNIRSIDPS150,
    DPS150Descriptor,
    DPS150OutputUpdate,
    DPS150ProtectionUpdate,
    DPS150SerialTransport,
    build_get_packet,
    build_packet,
    extract_packets,
    parse_all_state,
)
from openbench.drivers.fnirsi_dps150.protocol import (
    COMMAND_GET,
    HEADER_INPUT,
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
    TYPE_PRESET_1_VOLTAGE,
    TYPE_SET_CURRENT,
    TYPE_SET_VOLTAGE,
    TYPE_VOLUME,
)


class StatefulDPS150Transport:
    def __init__(self) -> None:
        self.values = {
            "input_voltage_v": 5.22,
            "set_voltage_v": 1.4,
            "set_current_a": 0.05,
            "output_voltage_v": 0.0,
            "output_current_a": 0.0,
            "output_power_w": 0.0,
            "temperature_c": 24.5,
            "over_voltage_v": 30.0,
            "over_current_a": 5.1,
            "over_power_w": 150.0,
            "over_temperature_c": 80.0,
            "low_input_voltage_v": 5.0,
            "capacity_ah": 0.125,
            "energy_wh": 0.25,
            "upper_voltage_v": 5.02,
            "upper_current_a": 5.1,
        }
        self.presets = [(float(slot), slot / 10) for slot in range(1, 7)]
        self.brightness = 6
        self.volume = 4
        self.metering_enabled = True
        self.output_enabled = False
        self.protection_code = 0
        self.mode = 1
        self.queries: list[int] = []
        self.writes: list[tuple[str, int, float | int]] = []
        self.closed = False

    def _all_payload(self) -> bytes:
        payload = bytearray(139)
        for offset, key in (
            (0, "input_voltage_v"),
            (4, "set_voltage_v"),
            (8, "set_current_a"),
            (12, "output_voltage_v"),
            (16, "output_current_a"),
            (20, "output_power_w"),
            (24, "temperature_c"),
        ):
            struct.pack_into("<f", payload, offset, self.values[key])
        for slot, (voltage_v, current_a) in enumerate(self.presets):
            struct.pack_into("<f", payload, 28 + slot * 8, voltage_v)
            struct.pack_into("<f", payload, 32 + slot * 8, current_a)
        for offset, key in (
            (76, "over_voltage_v"),
            (80, "over_current_a"),
            (84, "over_power_w"),
            (88, "over_temperature_c"),
            (92, "low_input_voltage_v"),
        ):
            struct.pack_into("<f", payload, offset, self.values[key])
        payload[96] = self.brightness
        payload[97] = self.volume
        payload[98] = int(self.metering_enabled)
        struct.pack_into("<f", payload, 99, self.values["capacity_ah"])
        struct.pack_into("<f", payload, 103, self.values["energy_wh"])
        payload[107] = int(self.output_enabled)
        payload[108] = self.protection_code
        payload[109] = self.mode
        struct.pack_into("<f", payload, 111, self.values["upper_voltage_v"])
        struct.pack_into("<f", payload, 115, self.values["upper_current_a"])
        return bytes(payload)

    def query(self, data_type: int) -> bytes:
        self.queries.append(data_type)
        if data_type == TYPE_MODEL:
            return b"DPS-150"
        if data_type == TYPE_HARDWARE_VERSION:
            return b"V1.0"
        if data_type == TYPE_FIRMWARE_VERSION:
            return b"V1.2"
        if data_type == TYPE_ALL:
            return self._all_payload()
        raise KeyError(data_type)

    def write_float(self, data_type: int, value: float) -> None:
        self.writes.append(("float", data_type, value))
        fields = {
            TYPE_SET_VOLTAGE: "set_voltage_v",
            TYPE_SET_CURRENT: "set_current_a",
            TYPE_OVP: "over_voltage_v",
            TYPE_OCP: "over_current_a",
            TYPE_OPP: "over_power_w",
            TYPE_OTP: "over_temperature_c",
            TYPE_LVP: "low_input_voltage_v",
        }
        if data_type in fields:
            self.values[fields[data_type]] = value
            return
        preset_offset = data_type - TYPE_PRESET_1_VOLTAGE
        if 0 <= preset_offset < 12:
            slot = preset_offset // 2
            old_voltage, old_current = self.presets[slot]
            self.presets[slot] = (
                value if preset_offset % 2 == 0 else old_voltage,
                value if preset_offset % 2 else old_current,
            )
            return
        raise KeyError(data_type)

    def write_byte(self, data_type: int, value: int) -> None:
        self.writes.append(("byte", data_type, value))
        if data_type == TYPE_OUTPUT_ENABLE:
            self.output_enabled = bool(value)
        elif data_type == TYPE_METERING_ENABLE:
            self.metering_enabled = bool(value)
        elif data_type == TYPE_BRIGHTNESS:
            self.brightness = value
        elif data_type == TYPE_VOLUME:
            self.volume = value
        else:
            raise KeyError(data_type)

    def close(self) -> None:
        self.closed = True


class FakeListPorts:
    @staticmethod
    def comports() -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                device="COM34",
                vid=0x2E3C,
                pid=0x5740,
                serial_number="DPS-API-TEST",
                location="Port_#0002.Hub_#0001",
                description="AT32 Virtual Com Port",
                manufacturer="Artery",
            ),
            SimpleNamespace(
                device="COM33",
                vid=0x1A86,
                pid=0x7523,
                serial_number=None,
                location="Port_#0001.Hub_#0001",
                description="USB-SERIAL CH340",
                manufacturer="wch.cn",
            ),
        ]


def descriptor() -> DPS150Descriptor:
    return DPS150Descriptor(
        port="COM34",
        vid=0x2E3C,
        pid=0x5740,
        serial_number="DPS-API-TEST",
        location="test",
        description="AT32 Virtual Com Port",
        manufacturer="Artery",
    )


def test_packet_parser_handles_fragmentation_noise_and_bad_checksum() -> None:
    valid = build_packet(COMMAND_GET, TYPE_MODEL, b"DPS-150", header=HEADER_INPUT)
    damaged = bytearray(valid)
    damaged[-1] ^= 0xFF
    buffer = bytearray(b"noise" + damaged + valid[:4])

    assert extract_packets(buffer) == ()
    buffer.extend(valid[4:])
    packets = extract_packets(buffer)

    assert len(packets) == 1
    assert packets[0].data_type == TYPE_MODEL
    assert packets[0].payload == b"DPS-150"
    assert buffer == bytearray()
    assert build_get_packet(TYPE_ALL) == bytes.fromhex("F1 A1 FF 01 00 00")


def test_discovers_only_the_dps150_usb_identity() -> None:
    found = DPS150SerialTransport.discover(list_ports_module=FakeListPorts)

    assert len(found) == 1
    assert found[0].port == "COM34"
    assert found[0].serial_number == "DPS-API-TEST"


def test_parses_complete_state_dump() -> None:
    transport = StatefulDPS150Transport()
    state = parse_all_state(transport.query(TYPE_ALL))

    assert state.set_voltage_v == pytest.approx(1.4)
    assert state.set_current_a == pytest.approx(0.05)
    assert state.protections.over_current_a == pytest.approx(5.1)
    assert state.presets[5].voltage_v == pytest.approx(6)
    assert state.metering_enabled is True
    assert state.output_enabled is False
    assert state.mode == "CV"
    assert state.protection == "OK"


@pytest.mark.asyncio
async def test_driver_reads_updates_and_verifies_safely() -> None:
    transport = StatefulDPS150Transport()
    supply = FNIRSIDPS150(descriptor(), transport=transport)

    assert await supply.identify() == "FNIRSI DPS-150 HW V1.0 FW V1.2 on COM34"
    sample = await supply.read_meter(f"{supply.device_id}.output_voltage")
    assert sample.value == 0
    assert sample.unit == "V"

    transport.output_enabled = True
    updated = await supply.update_output(
        DPS150OutputUpdate(voltage_v=1.2, current_a=0.04, enabled=False)
    )
    assert updated.set_voltage_v == pytest.approx(1.2)
    assert updated.set_current_a == pytest.approx(0.04)
    assert updated.output_enabled is False
    assert transport.writes[0] == ("byte", TYPE_OUTPUT_ENABLE, 0)

    protected = await supply.update_protections(DPS150ProtectionUpdate(over_current_a=5.1))
    assert protected.protections.over_current_a == pytest.approx(5.1)

    saved = await supply.save_preset(2, voltage_v=1.5, current_a=0.025)
    assert saved.presets[1].voltage_v == pytest.approx(1.5)
    assert saved.presets[1].current_a == pytest.approx(0.025)
    applied = await supply.apply_preset(2)
    assert applied.set_voltage_v == pytest.approx(1.5)
    assert applied.output_enabled is False

    with pytest.raises(ValueError, match="between 1 and 6"):
        await supply.apply_preset(0)
    with pytest.raises(ValueError, match="increments"):
        await supply.update_output(DPS150OutputUpdate(voltage_v=1.234))
    with pytest.raises(ValueError, match=r"between 0 and 5\.02"):
        await supply.update_output(DPS150OutputUpdate(voltage_v=6))


async def _register_api_supply(
    client: TestClient,
) -> tuple[FNIRSIDPS150, StatefulDPS150Transport]:
    transport = StatefulDPS150Transport()
    supply = FNIRSIDPS150(descriptor(), transport=transport)
    await supply.identify()
    await supply.read_state(force=True)
    device = Device(
        id=supply.device_id,
        name="DPS-150",
        kind="fnirsi_dps150",
        connected=True,
        capabilities=("dc_power_supply", "program_sequence", "voltage_sweep"),
    )
    channels = tuple(
        Channel(
            id=channel_id,
            device_id=supply.device_id,
            name=parameter.name,
            capability=parameter.capability,
            unit=parameter.unit,
            poll_interval_s=0.5,
        )
        for channel_id, parameter in supply.parameters
    )
    context = client.app.state.context
    context.registry.register(device, supply, channels)
    context.device_service.register(device, channels)
    for channel in channels:
        context.scheduler.add_target(channel, supply)
    return supply, transport


def register_api_supply(
    client: TestClient,
) -> tuple[FNIRSIDPS150, StatefulDPS150Transport]:
    portal = client.portal
    assert portal is not None
    return portal.call(_register_api_supply, client)


def test_power_supply_api_dashboard_capture_and_emergency_stop(
    client: TestClient,
) -> None:
    supply, transport = register_api_supply(client)
    base = f"/api/v1/power-supplies/{supply.device_id}"

    initial = client.get(base)
    assert initial.status_code == 200
    assert initial.json()["identity"] == {
        "model": "DPS-150",
        "hardware_version": "V1.0",
        "firmware_version": "V1.2",
    }
    assert initial.json()["set_voltage_v"] == pytest.approx(1.4)
    assert initial.json()["protections"]["over_current_a"] == pytest.approx(5.1)

    output = client.patch(
        f"{base}/output",
        json={"voltage_v": 1.25, "current_a": 0.03, "enabled": True},
    )
    assert output.status_code == 200
    assert output.json()["output_enabled"] is True

    protections = client.patch(
        f"{base}/protections",
        json={
            "over_voltage_v": 2.0,
            "over_current_a": 0.1,
            "over_power_w": 1.0,
            "over_temperature_c": 70,
            "low_input_voltage_v": 4.8,
        },
    )
    assert protections.status_code == 200
    assert protections.json()["protections"]["over_voltage_v"] == pytest.approx(2)

    display = client.patch(f"{base}/display", json={"brightness": 3, "volume": 2})
    assert display.status_code == 200
    assert display.json()["brightness"] == 3
    assert display.json()["volume"] == 2

    metering = client.patch(f"{base}/metering", json={"enabled": False})
    assert metering.status_code == 200
    assert metering.json()["metering_enabled"] is False

    preset = client.put(
        f"{base}/presets/3",
        json={"voltage_v": 1.1, "current_a": 0.02},
    )
    assert preset.status_code == 200
    assert preset.json()["presets"][2]["voltage_v"] == pytest.approx(1.1)
    applied = client.post(f"{base}/presets/3/apply", json={"enabled": False})
    assert applied.status_code == 200
    assert applied.json()["set_voltage_v"] == pytest.approx(1.1)

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "power-supply-panel" in dashboard.text
    assert "Programmable output" in dashboard.text

    time.sleep(0.55)
    snapshot = client.post(
        "/api/v1/captures/snapshot",
        json={"title": "DPS API", "comment": "Driver integration test"},
    )
    assert snapshot.status_code == 201
    downloaded = client.get(snapshot.json()["download_url"])
    assert downloaded.status_code == 200
    decoded = downloaded.content.decode("utf-8-sig")
    assert "DPS-150" in decoded
    assert "output_voltage_value" in decoded

    enabled = client.patch(f"{base}/output", json={"enabled": True})
    assert enabled.status_code == 200
    stopped = client.post("/api/v1/emergency-stop", json={"reason": "DPS test"})
    assert stopped.status_code == 200
    assert stopped.json()["power_supply_errors"] == []
    assert transport.output_enabled is False
    blocked = client.patch(f"{base}/output", json={"enabled": True})
    assert blocked.status_code == 400
    assert "blocked" in blocked.json()["detail"]


def test_power_supply_program_lifecycle(client: TestClient) -> None:
    supply, transport = register_api_supply(client)
    base = f"/api/v1/power-supplies/{supply.device_id}"

    started = client.post(
        f"{base}/programs/sequence",
        json={
            "steps": [
                {"voltage_v": 1.0, "current_a": 0.02, "dwell_s": 0.5},
                {"voltage_v": 1.2, "current_a": 0.02, "dwell_s": 0.5},
            ],
            "loops": 2,
        },
    )
    assert started.status_code == 200
    assert started.json()["active"] is True

    conflict = client.post(
        f"{base}/programs/sequence",
        json={"steps": [{"voltage_v": 1, "current_a": 0.01, "dwell_s": 0.1}]},
    )
    assert conflict.status_code == 409

    time.sleep(0.15)
    paused = client.post(f"{base}/programs/pause")
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    paused_step = paused.json()["current_step"]
    time.sleep(0.2)
    status = client.get(f"{base}/programs/status")
    assert status.json()["current_step"] == paused_step

    resumed = client.post(f"{base}/programs/resume")
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False
    stopped = client.post(f"{base}/programs/stop")
    assert stopped.status_code == 200
    assert stopped.json()["active"] is False
    assert transport.output_enabled is False

    sweep = client.post(
        f"{base}/programs/sweep",
        json={
            "parameter": "voltage",
            "start": 0.5,
            "end": 0.7,
            "step": 0.1,
            "fixed_value": 0.01,
            "dwell_s": 1,
            "loops": 1,
        },
    )
    assert sweep.status_code == 200
    assert sweep.json()["kind"] == "voltage_sweep"
    client.post(f"{base}/programs/stop").raise_for_status()

    natural = client.post(
        f"{base}/programs/sequence",
        json={"steps": [{"voltage_v": 0.5, "current_a": 0.01, "dwell_s": 0.1}]},
    )
    assert natural.status_code == 200
    time.sleep(0.25)
    completed = client.get(f"{base}/programs/status")
    assert completed.status_code == 200
    assert completed.json()["active"] is False
    assert completed.json()["progress_percent"] == pytest.approx(100)
    assert transport.output_enabled is False


def test_power_supply_disconnect_forces_output_off(client: TestClient) -> None:
    supply, transport = register_api_supply(client)
    base = f"/api/v1/power-supplies/{supply.device_id}"
    enabled = client.patch(
        f"{base}/output",
        json={"voltage_v": 0.5, "current_a": 0.01, "enabled": True},
    )
    assert enabled.status_code == 200
    assert transport.output_enabled is True

    disconnected = client.delete(f"/api/v1/devices/{supply.device_id}")

    assert disconnected.status_code == 200
    assert disconnected.json()["connected"] is False
    assert transport.output_enabled is False
    assert transport.closed is True
    assert supply.device_id not in {device["id"] for device in client.get("/api/v1/devices").json()}


@pytest.mark.asyncio
async def test_cached_state_coalesces_parallel_channel_reads() -> None:
    transport = StatefulDPS150Transport()
    supply = FNIRSIDPS150(descriptor(), transport=transport)
    channel_ids = [f"{supply.device_id}.{parameter.key}" for parameter in DPS150_PARAMETERS]

    await asyncio.gather(*(supply.read_meter(channel_id) for channel_id in channel_ids))

    assert transport.queries.count(TYPE_ALL) == 1
