from __future__ import annotations

from fastapi.testclient import TestClient

from openbench.domain import Device
from openbench.drivers.feeltech_fy import (
    FeelTechDescriptor,
    FeelTechFYGenerator,
)


class ApiGeneratorTransport:
    def __init__(self) -> None:
        self.responses = {
            "UMO": "FY6200-20M",
            "RMW": "0",
            "RMF": "10000.000000",
            "RMA": "50000",
            "RMO": "0",
            "RMD": "50000",
            "RMP": "0",
            "RMN": "255",
            "RFW": "0",
            "RFF": "10000.000000",
            "RFA": "50000",
            "RFO": "0",
            "RFD": "50000",
            "RFP": "0",
            "RFN": "0",
            "RPM": "0",
            "RPN": "10",
            "RTA": "0",
            "RTF": "0",
            "RFK": "1000",
            "RTP": "0",
            "RSS": "50000",
            "RCG": "0",
            "RCF": "0",
            "RCC": "0",
            "RCT": "0",
            "RC+": "0",
            "RC-": "0",
            "RCD": "0",
        }
        self.sync = {index: False for index in range(5)}
        self.writes: list[str] = []

    def query(self, command: str) -> str:
        if command.startswith("RSA"):
            return "255" if self.sync[int(command[-1])] else "0"
        return self.responses[command]

    def write(self, command: str) -> None:
        self.writes.append(command)
        prefix = command[:3]
        payload = command[3:]
        read_command = f"R{prefix[1:]}"
        if prefix in {"WMW", "WFW"}:
            self.responses[read_command] = str(int(payload))
        elif prefix in {"WMF", "WFF"}:
            self.responses[read_command] = str(float(payload))
        elif prefix in {"WMA", "WFA"}:
            self.responses[read_command] = str(round(float(payload) * 10_000))
        elif prefix in {"WMO", "WFO"}:
            encoded = round(float(payload) * 1_000)
            self.responses[read_command] = str(encoded % 0x1_0000_0000)
        elif prefix in {"WMD", "WFD"}:
            self.responses[read_command] = str(round(float(payload) * 1_000))
        elif prefix in {"WMP", "WFP"}:
            self.responses[read_command] = str(round(float(payload) * 1_000))
        elif prefix in {"WMN", "WFN"}:
            self.responses[read_command] = "255" if payload == "1" else "0"
        elif prefix in {"USA", "USD"}:
            self.sync[int(payload)] = prefix == "USA"
        elif prefix == "WPM":
            self.responses["RPM"] = payload
        elif prefix == "WPN":
            self.responses["RPN"] = payload
        elif prefix == "WTA":
            self.responses["RTA"] = payload
        elif prefix == "WTF":
            self.responses["RTF"] = payload
        elif prefix == "WFK":
            self.responses["RFK"] = str(int(payload) / 1_000_000)
        elif prefix == "WTP":
            self.responses["RTP"] = payload
        elif prefix == "WMS":
            self.responses["RSS"] = str(int(payload) * 10)
        elif prefix == "WCG":
            self.responses["RCG"] = payload
        elif prefix == "WCZ":
            self.responses["RCC"] = "0"

    def close(self) -> None:
        pass


def register_generator(client: TestClient) -> tuple[FeelTechFYGenerator, ApiGeneratorTransport]:
    transport = ApiGeneratorTransport()
    generator = FeelTechFYGenerator(
        FeelTechDescriptor(
            port="COM33",
            vid=0x1A86,
            pid=0x7523,
            serial_number="API-TEST",
            location="test",
            description="test",
            manufacturer="test",
        ),
        transport=transport,
    )
    generator.model = "FY6200-20M"
    device = Device(
        id=generator.device_id,
        name=generator.model,
        kind="feeltech_fy",
        connected=True,
        capabilities=("signal_generator", "dual_channel"),
    )
    context = client.app.state.context
    context.registry.register(device, generator)
    context.device_service.register(device, ())
    return generator, transport


def test_generator_api_reads_and_updates_both_channels(client: TestClient) -> None:
    generator, transport = register_generator(client)

    initial = client.get(f"/api/v1/generators/{generator.device_id}")
    assert initial.status_code == 200
    assert initial.json()["model"] == "FY6200-20M"
    assert len(initial.json()["waveforms"]) == 95
    assert initial.json()["channels"][0]["output_enabled"] is True
    assert initial.json()["channels"][1]["output_enabled"] is False

    updated = client.patch(
        f"/api/v1/generators/{generator.device_id}/channels/2",
        json={
            "waveform_code": 1,
            "frequency_hz": 1234.5,
            "amplitude_vpp": 2.5,
            "offset_v": -0.25,
            "duty_percent": 37.5,
            "phase_deg": 12.3,
            "output_enabled": False,
        },
    )

    assert updated.status_code == 200
    channel = updated.json()["channels"][1]
    assert channel["waveform_name"] == "SQUARE"
    assert channel["frequency_hz"] == 1234.5
    assert channel["amplitude_vpp"] == 2.5
    assert channel["offset_v"] == -0.25
    assert channel["duty_percent"] == 37.5
    assert channel["phase_deg"] == 12.3
    assert transport.writes[:2] == ["WFW01", "WFF00001234.500000"]


def test_generator_api_rejects_invalid_combination_before_writing(
    client: TestClient,
) -> None:
    generator, transport = register_generator(client)

    response = client.patch(
        f"/api/v1/generators/{generator.device_id}/channels/1",
        json={"waveform_code": 1, "frequency_hz": 16_000_000},
    )

    assert response.status_code == 400
    assert "frequency must be" in response.json()["detail"]
    assert transport.writes == []


def test_generator_advanced_api_controls(client: TestClient) -> None:
    generator, transport = register_generator(client)

    pulse = client.patch(
        f"/api/v1/generators/{generator.device_id}/channels/1",
        json={"pulse_width_ns": 25_000},
    )
    assert pulse.status_code == 200
    assert pulse.json()["advanced"]["main_pulse_width_ns"] == 25_000

    burst = client.patch(
        f"/api/v1/generators/{generator.device_id}/burst",
        json={"source": "ch2", "cycles": 68},
    )
    assert burst.status_code == 200
    assert burst.json()["advanced"]["burst"]["trigger_source"] == "ch2"
    assert burst.json()["advanced"]["burst"]["cycles"] == 68

    keying = client.patch(
        f"/api/v1/generators/{generator.device_id}/keying",
        json={"kind": "fsk", "source": "manual", "secondary_frequency_hz": 234.5},
    )
    assert keying.status_code == 200
    assert keying.json()["advanced"]["modulation"]["fsk_source"] == "manual"
    assert keying.json()["advanced"]["modulation"]["fsk_secondary_frequency_hz"] == 234.5

    counter = client.patch(
        f"/api/v1/generators/{generator.device_id}/counter",
        json={"gate_time_s": 10, "coupling": "ac", "mode": "both"},
    )
    assert counter.status_code == 200
    assert counter.json()["advanced"]["counter"]["gate_time_s"] == 10
    assert counter.json()["advanced"]["counter"]["coupling"] == "ac"
    assert counter.json()["advanced"]["counter"]["mode"] == "both"
    assert counter.json()["advanced"]["counter"]["paused"] is False

    paused = client.post(f"/api/v1/generators/{generator.device_id}/counter/pause")
    assert paused.status_code == 200
    assert paused.json()["advanced"]["counter"]["paused"] is True
    assert "WCP0" in transport.writes

    sweep = client.patch(
        f"/api/v1/generators/{generator.device_id}/sweep",
        json={
            "target": "frequency",
            "start": 100,
            "end": 10_000,
            "duration_s": 2.5,
            "mode": "logarithmic",
            "source": "time",
            "enabled": False,
        },
    )
    assert sweep.status_code == 200
    assert sweep.json()["advanced"]["sweep"]["enabled"] is False
    assert sweep.json()["advanced"]["sweep"]["verified"] is False
    assert "SBE0" in transport.writes


def test_generator_sync_presets_and_emergency_stop(client: TestClient) -> None:
    generator, transport = register_generator(client)

    synchronized = client.patch(
        f"/api/v1/generators/{generator.device_id}/synchronization",
        json={"parameter": "frequency", "enabled": True},
    )
    assert synchronized.status_code == 200
    assert synchronized.json()["synchronization"]["frequency"] is True

    saved = client.post(f"/api/v1/generators/{generator.device_id}/presets/7/save")
    assert saved.status_code == 200
    assert saved.json()["action"] == "saved"

    stopped = client.post("/api/v1/emergency-stop", json={"reason": "generator test"})
    assert stopped.status_code == 200
    assert stopped.json()["generator_errors"] == []
    assert transport.responses["RMN"] == "0"
    assert transport.responses["RFN"] == "0"

    blocked = client.put(
        f"/api/v1/generators/{generator.device_id}/outputs",
        json={"channel_1": True, "channel_2": False},
    )
    assert blocked.status_code == 400
    assert "blocked" in blocked.json()["detail"]
