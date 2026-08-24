from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openbench.domain import Channel, Device
from openbench.drivers.owon_spm import (
    OwonSPMDescriptor,
    OwonSPMDMMUpdate,
    OwonSPMIdentity,
    OwonSPMInstrument,
    OwonSPMOutputUpdate,
    OwonSPMProtectionUpdate,
    OwonSPMProtocolError,
    parse_dmm_state,
    parse_identity,
    parse_measurement_info,
)


class StatefulSPMTransport:
    def __init__(self) -> None:
        self.voltage = 5.0
        self.current = 9.6
        self.enabled = False
        self.ovp = 62.0
        self.ocp = 10.0
        self.dmm_function = "VOLT:DC"
        self.dmm_range = 2.0
        self.dmm_auto = True
        self.dmm_relative = False
        self.dmm_hold = False
        self.writes: list[str] = []
        self.closed = False

    def query(self, command: str) -> str:
        if command.endswith(":RANG?"):
            return f"{self.dmm_range:g}"
        if command.endswith(":RANG:AUTO?"):
            return "ON" if self.dmm_auto else "OFF"
        if command.endswith(":NULL?"):
            return "ON" if self.dmm_relative else "OFF"
        responses = {
            "*IDN?": "OWON,SPM6103,SPM-DEMO-0001,FV:V2.1.0",
            "VOLT?": f"{self.voltage:.3f}",
            "CURR?": f"{self.current:.3f}",
            "OUTP?": "ON" if self.enabled else "OFF",
            "VOLT:LIM?": f"{self.ovp:.3f}",
            "CURR:LIM?": f"{self.ocp:.3f}",
            "MEAS:ALL:INFO?": (
                "1.000,0.010,0.010,OFF,OFF,OFF,1"
                if self.enabled
                else "0.000,0.000,0.000,OFF,OFF,OFF,0"
            ),
            "CONF:ALL?": (
                f"{self.dmm_function},+0.0012V,{'AUTO' if self.dmm_auto else 'MANUAL'},2V"
            ),
            "MULT:HOLD?": "ON" if self.dmm_hold else "OFF",
        }
        return responses[command]

    def write(self, command: str) -> None:
        self.writes.append(command)
        if command == "OUTP ON":
            self.enabled = True
        elif command == "OUTP OFF":
            self.enabled = False
        elif command.startswith("VOLT:LIM "):
            self.ovp = float(command.split()[1])
        elif command.startswith("CURR:LIM "):
            self.ocp = float(command.split()[1])
        elif command.startswith("VOLT "):
            self.voltage = float(command.split()[1])
        elif command.startswith("CURR "):
            self.current = float(command.split()[1])
        elif command.startswith("SENS:FUNC:"):
            self.dmm_function = command.removeprefix("SENS:FUNC:")
        elif ":RANG:AUTO " in command:
            self.dmm_auto = command.endswith(" ON")
        elif ":RANG " in command:
            self.dmm_range = float(command.split()[1])
            self.dmm_auto = False
        elif ":NULL " in command:
            self.dmm_relative = command.endswith(" ON")
        elif command.startswith("MULT:HOLD "):
            self.dmm_hold = command.endswith(" ON")

    def close(self) -> None:
        self.closed = True


def descriptor(serial_number: str = "SPM-DEMO-0001") -> OwonSPMDescriptor:
    return OwonSPMDescriptor(
        port="COM35",
        vid=0x1A86,
        pid=0x7523,
        serial_number="",
        location="1-9.3",
        description="USB-SERIAL CH340",
        manufacturer="wch.cn",
        identity=OwonSPMIdentity("OWON", "SPM6103", serial_number, "FV:V2.1.0"),
    )


def test_parses_official_responses() -> None:
    identity = parse_identity("OWON,SPM6103,SPM-DEMO-0001,FV:V2.1.0")
    assert identity.serial_number == "SPM-DEMO-0001"
    assert parse_measurement_info("1.000,0.010,0.010,OFF,OFF,OFF,1") == (
        1.0,
        0.01,
        0.01,
        False,
        False,
        False,
        "CV",
    )
    dmm = parse_dmm_state("VOLT:DC,+0.0012V,AUTO,2V")
    assert (dmm.function, dmm.value, dmm.unit, dmm.range_label) == (
        "dc_voltage",
        0.0012,
        "V",
        "2V",
    )
    resistance = parse_dmm_state("RES,+1.2kOHM,MANUAL,2kOhm")
    assert (resistance.value, resistance.unit) == (1200.0, "Ohm")
    overload = parse_dmm_state("RES,+9.9E+37OHM,AUTO,200Ohm")
    assert overload.value is None
    assert overload.status == "overload"


@pytest.mark.parametrize(
    "response",
    ["", "OWON,SPM6103", "FEELTECH,FY6200,1,2", "OWON,SPM6053,1,FV:V1.0"],
)
def test_rejects_non_spm_identification(response: str) -> None:
    with pytest.raises(OwonSPMProtocolError):
        parse_identity(response)


@pytest.mark.asyncio
async def test_driver_reads_and_safely_updates_output() -> None:
    transport = StatefulSPMTransport()
    instrument = OwonSPMInstrument(descriptor(), transport=transport)
    assert instrument.device_id == "owon_spm6103_spm_demo_0001"
    assert "SN SPM-DEMO-0001" in await instrument.identify()

    state = await instrument.read_state(force=True)
    assert not state.source.output_enabled
    assert state.multimeter.function == "dc_voltage"

    await instrument.update_output(OwonSPMOutputUpdate(voltage_v=1.0, current_a=0.1, enabled=True))
    assert transport.enabled
    assert transport.writes[:5] == [
        "SYST:REM",
        "CURR 0.100",
        "VOLT 1.00",
        "OUTP ON",
        "SYST:LOC",
    ]

    transport.writes.clear()
    await instrument.update_output(OwonSPMOutputUpdate(voltage_v=0.5))
    assert transport.writes == [
        "SYST:REM",
        "OUTP OFF",
        "VOLT 0.50",
        "OUTP ON",
        "SYST:LOC",
    ]

    await instrument.update_protections(
        OwonSPMProtectionUpdate(over_voltage_v=12.0, over_current_a=1.0)
    )
    assert (transport.ovp, transport.ocp) == (12.0, 1.0)
    await instrument.update_multimeter(OwonSPMDMMUpdate(function="ac_voltage"))
    assert transport.dmm_function == "VOLT:AC"
    configured = await instrument.update_multimeter(
        OwonSPMDMMUpdate(
            range_mode="manual",
            range_value=20.0,
            relative_enabled=True,
            hold_enabled=True,
        )
    )
    assert configured.multimeter.range_value == 20.0
    assert configured.multimeter.relative_enabled
    assert configured.multimeter.hold_enabled

    await instrument.close()
    assert not transport.enabled
    assert transport.closed


@pytest.mark.asyncio
async def test_driver_rejects_unsafe_or_unrepresentable_setpoints() -> None:
    instrument = OwonSPMInstrument(descriptor(), transport=StatefulSPMTransport())
    with pytest.raises(ValueError, match="increments"):
        await instrument.update_output(OwonSPMOutputUpdate(voltage_v=1.234))
    with pytest.raises(ValueError, match="300 W"):
        await instrument.update_output(OwonSPMOutputUpdate(voltage_v=60.0, current_a=10.0))


def _register_fake_spm(client: TestClient) -> tuple[OwonSPMInstrument, StatefulSPMTransport]:
    context = client.app.state.context
    transport = StatefulSPMTransport()
    instrument = OwonSPMInstrument(descriptor(), transport=transport)
    device = Device(
        id=instrument.device_id,
        name="OWON SPM6103",
        kind="owon_spm",
        connected=True,
        capabilities=("source_measure_unit", "dc_power_supply", "multimeter"),
    )
    channels = tuple(
        Channel(
            id=channel_id,
            device_id=instrument.device_id,
            name=parameter.name,
            capability=parameter.capability,
            unit=parameter.unit,
            poll_interval_s=0.5,
        )
        for channel_id, parameter in instrument.parameters
    )
    context.registry.register(device, instrument, channels)
    context.device_service.register(device, channels)
    for channel in channels:
        client.portal.call(context.scheduler.add_target, channel, instrument)
    return instrument, transport


def test_api_and_dashboard_expose_combined_instrument(client: TestClient) -> None:
    instrument, transport = _register_fake_spm(client)
    response = client.get(f"/api/v1/source-measure-units/{instrument.device_id}")
    assert response.status_code == 200
    assert response.json()["identity"]["serial_number"] == "SPM-DEMO-0001"

    updated = client.patch(
        f"/api/v1/source-measure-units/{instrument.device_id}/output",
        json={"voltage_v": 1.0, "current_a": 0.1, "enabled": True},
    )
    assert updated.status_code == 200
    assert updated.json()["source"]["output_enabled"] is True

    protected = client.patch(
        f"/api/v1/source-measure-units/{instrument.device_id}/protections",
        json={"over_voltage_v": 12.0, "over_current_a": 1.0},
    )
    assert protected.status_code == 200
    assert protected.json()["source"]["over_voltage_v"] == 12.0

    dmm = client.patch(
        f"/api/v1/source-measure-units/{instrument.device_id}/multimeter",
        json={
            "function": "dc_voltage",
            "range_mode": "manual",
            "range_value": 20.0,
            "relative_enabled": True,
            "hold_enabled": True,
        },
    )
    assert dmm.status_code == 200
    assert dmm.json()["multimeter"]["range_value"] == 20.0
    assert dmm.json()["multimeter"]["relative_enabled"] is True
    assert dmm.json()["multimeter"]["hold_enabled"] is True

    page = client.get("/")
    assert page.status_code == 200
    assert "DC power supply + multimeter" in page.text
    assert "Multimeter" in page.text

    stopped = client.post("/api/v1/emergency-stop", json={"reason": "test"})
    assert stopped.status_code == 200
    assert stopped.json()["source_measure_unit_errors"] == []
    assert transport.enabled is False

    blocked = client.patch(
        f"/api/v1/source-measure-units/{instrument.device_id}/output",
        json={"enabled": True},
    )
    assert blocked.status_code == 400
    assert transport.enabled is False
