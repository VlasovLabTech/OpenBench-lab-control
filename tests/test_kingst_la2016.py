from __future__ import annotations

import asyncio
import subprocess
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openbench.domain import Device
from openbench.drivers.kingst_la2016 import (
    KingstCaptureConfig,
    KingstDescriptor,
    KingstTrigger,
    SigrokCLITransport,
)


class FakeLogicAnalyzer:
    device_id = "kingst_la2016_test"

    def __init__(self) -> None:
        self.last_config: KingstCaptureConfig | None = None
        self.trigger = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.fail_error: str | None = None
        self.capture_delay_s = 0.04
        self.download_delay_s = 0.0

    async def identify(self) -> str:
        return "Kingst LA2016 test double"

    async def capture(
        self,
        config: KingstCaptureConfig,
        output_file: Path,
        *,
        on_state: object,
    ) -> None:
        self.last_config = config
        if self.fail_error is not None:
            await asyncio.sleep(0.01)
            raise RuntimeError(self.fail_error)
        callback = on_state
        if config.triggers:
            await callback("pretrigger")  # type: ignore[operator]
            await asyncio.sleep(0.01)
            await callback("armed")  # type: ignore[operator]
            trigger_task = asyncio.create_task(self.trigger.wait())
            stop_task = asyncio.create_task(self.stop_requested.wait())
            completed, pending = await asyncio.wait(
                (trigger_task, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if stop_task in completed:
                raise RuntimeError("stopped")
            await callback("posttrigger")  # type: ignore[operator]
        await asyncio.sleep(self.capture_delay_s)
        if self.stop_requested.is_set():
            raise RuntimeError("stopped")
        await callback("downloading")  # type: ignore[operator]
        await asyncio.sleep(self.download_delay_s)
        await asyncio.to_thread(
            output_file.write_bytes,
            b"PK\x03\x04fake-sigrok-session",
        )

    async def stop(self) -> None:
        self.stop_requested.set()


def descriptor() -> KingstDescriptor:
    return KingstDescriptor(
        connection="2.49",
        model="LA2016",
        logic_channels=tuple(f"CH{index}" for index in range(16)),
        max_sample_rate_hz=200_000_000,
    )


def test_descriptor_prefers_stable_usb_port_path() -> None:
    first = descriptor()
    moved_address = KingstDescriptor(
        connection="2.55",
        model=first.model,
        logic_channels=first.logic_channels,
        max_sample_rate_hz=first.max_sample_rate_hz,
        port_path="usb/2-9",
    )
    another_address = KingstDescriptor(
        connection="2.99",
        model=first.model,
        logic_channels=first.logic_channels,
        max_sample_rate_hz=first.max_sample_rate_hz,
        port_path="usb/2-9",
    )

    assert moved_address.device_id == "kingst_la2016_usb-2-9"
    assert another_address.device_id == moved_address.device_id


@pytest.mark.asyncio
async def test_sigrok_discovery_parses_kingst_and_ignores_pwm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "sigrok-cli.exe"
    executable.write_bytes(b"test")
    output = (
        "The following devices were found:\n"
        "demo - Demo device with 13 channels: D0 D1\n"
        "kingst-la2016:conn=2.49 - Kingst LA2016 with 18 channels: "
        "CH0 CH1 CH2 CH3 CH4 CH5 CH6 CH7 CH8 CH9 CH10 CH11 CH12 CH13 CH14 CH15 "
        "PWM1 PWM2\n"
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=output, stderr="harmless warning")

    monkeypatch.setattr(subprocess, "run", fake_run)

    found = await SigrokCLITransport.discover(executable=executable)

    assert len(found) == 1
    assert found[0].model == "LA2016"
    assert found[0].logic_channels == tuple(f"CH{index}" for index in range(16))
    assert "PWM1" not in found[0].logic_channels


@pytest.mark.asyncio
async def test_sigrok_discovery_retries_empty_scan_and_uses_usb_port_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "sigrok-cli.exe"
    executable.write_bytes(b"test")
    calls: list[tuple[object, ...]] = []
    output = (
        "sr: kingst-la2016: USB enum found 77a1:01a2 at path usb/2-9, 2.55.\n"
        "kingst-la2016:conn=2.55 - Kingst LA2016 with 18 channels: "
        "CH0 CH1 CH2 CH3 CH4 CH5 CH6 CH7 CH8 CH9 CH10 CH11 CH12 CH13 CH14 "
        "CH15 PWM1 PWM2\n"
    )

    def fake_run(*args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if len(calls) == 1:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="not ready")
        return subprocess.CompletedProcess([], 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    found = await SigrokCLITransport.discover(
        executable=executable,
        attempts=3,
        retry_delay_s=0,
    )

    assert len(calls) == 2
    assert found[0].connection == "2.55"
    assert found[0].port_path == "usb/2-9"
    assert found[0].device_id == "kingst_la2016_usb-2-9"


def test_capture_config_validates_hardware_limits_and_builds_atomic_command(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "sigrok-cli.exe"
    executable.write_bytes(b"test")
    transport = SigrokCLITransport(descriptor(), executable=executable)
    config = KingstCaptureConfig(
        channels=(0, 1, 7),
        sample_rate_hz=20_000_000,
        sample_count=2_000_000,
        threshold_v=1.4,
        capture_ratio_percent=25,
        triggers=(
            KingstTrigger(0, "high"),
            KingstTrigger(1, "rising"),
        ),
    )

    command = transport.command(config, tmp_path / "capture.sr")

    assert "CH0,CH1,CH7" in command
    assert "samplerate=20000000" in command
    assert "voltage_threshold=1.4-1.4" in command
    assert "captureratio=25" in command
    assert "CH0=1,CH1=r" in command
    assert config.duration_s == pytest.approx(0.1)
    assert config.post_trigger_duration_s == pytest.approx(0.075)

    with pytest.raises(ValueError, match="at most one edge"):
        KingstCaptureConfig(
            triggers=(KingstTrigger(0, "rising"), KingstTrigger(1, "falling"))
        )
    with pytest.raises(ValueError, match="must be enabled"):
        KingstCaptureConfig(channels=(0,), triggers=(KingstTrigger(1, "high"),))


def test_sigrok_session_validation_requires_complete_logic_archive(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.sr"
    with zipfile.ZipFile(valid, "w") as session:
        session.writestr("version", "2")
        session.writestr("metadata", "[device 1]\n")
        session.writestr("logic-1-1", b"\x00\x00")

    empty_logic = tmp_path / "empty.sr"
    with zipfile.ZipFile(empty_logic, "w") as session:
        session.writestr("version", "2")
        session.writestr("metadata", "[device 1]\n")
        session.writestr("logic-1-1", b"")

    assert SigrokCLITransport._is_valid_session_file(valid)
    assert not SigrokCLITransport._is_valid_session_file(empty_logic)
    assert not SigrokCLITransport._is_valid_session_file(tmp_path / "missing.sr")


def register_fake_analyzer(client: TestClient) -> FakeLogicAnalyzer:
    analyzer = FakeLogicAnalyzer()
    device = Device(
        id=analyzer.device_id,
        name="Kingst LA2016",
        kind="kingst_la2016",
        connected=True,
        capabilities=("logic_analyzer", "hardware_trigger"),
    )
    context = client.app.state.context
    context.registry.register(device, analyzer)
    context.device_service.register(device, ())
    context.logic_analyzer_service.add_device(analyzer)
    return analyzer


def wait_for_logic_state(
    client: TestClient,
    device_id: str,
    expected: set[str],
    *,
    timeout_s: float = 1.5,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/logic-analyzers/{device_id}/captures/status")
        response.raise_for_status()
        latest = response.json()
        if latest["state"] in expected:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"Logic state did not reach {expected}: {latest}")


def test_logic_analyzer_api_dashboard_artifact_and_common_csv(
    client: TestClient,
) -> None:
    analyzer = register_fake_analyzer(client)
    base = f"/api/v1/logic-analyzers/{analyzer.device_id}"

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "<h2>LA2016</h2>" in dashboard.text
    assert "logic-analyzer-panel" in dashboard.text
    assert "hardware trigger" in dashboard.text
    assert "Start with global RUN" in dashboard.text
    assert 'name="sample_count"' in dashboard.text
    assert "20 KSa" in dashboard.text
    assert 'name="trigger_condition"' in dashboard.text
    assert 'name="trigger_0"' not in dashboard.text
    assert "Apply the acquisition settings below" not in dashboard.text
    assert "Simple delay only" not in dashboard.text
    assert "Optional. The complete text" not in dashboard.text
    assert 'class="logic-settings-footer"' in dashboard.text
    assert '<span>Apply</span><span>all</span>' in dashboard.text

    settings = client.patch(
        f"{base}/settings",
        json={
            "channels": [0, 1, 2, 3],
            "sample_rate_hz": 2_000_000,
            "sample_count": 100_000,
            "threshold_v": 1.2,
            "capture_ratio_percent": 40,
            "triggers": [{"channel": 1, "condition": "rising"}],
            "auto_start_enabled": False,
            "auto_start_delay_s": 0,
        },
    )
    assert settings.status_code == 200
    assert settings.json()["duration_s"] == pytest.approx(0.05)
    assert settings.json()["trigger_label"] == "CH1 rising"

    recording = client.post(
        "/api/v1/captures/recording/start",
        json={"title": "Logic API", "comment": "Common timeline test"},
    )
    assert recording.status_code == 201
    analyzer.capture_delay_s = 0.2
    analyzer.download_delay_s = 0.3
    started = client.post(
        f"{base}/captures/start",
        json={"title": "Digital edge", "comment": "Immediate capture"},
    )
    assert started.status_code == 201
    assert started.json()["estimated_duration_s"] > 0.05
    assert started.json()["remaining_s"] is not None
    capture_id = started.json()["capture_id"]
    downloading = wait_for_logic_state(
        client,
        analyzer.device_id,
        {"downloading"},
    )
    assert downloading["remaining_s"] is not None
    active_panel = client.get(
        f"/ui/devices/{analyzer.device_id}/logic-analyzer/status"
    )
    assert active_panel.status_code == 200
    assert 'class="logic-progress-time"' in active_panel.text
    assert "<progress" in active_panel.text
    assert "logic-progress-indeterminate" not in active_panel.text
    completed = wait_for_logic_state(client, analyzer.device_id, {"completed"})
    assert completed["artifact_file"] == "capture.sr"
    assert completed["recording_file"] == recording.json()["current_file"]
    assert completed["triggered_at"] is None
    assert completed["trigger_timestamp_quality"] == "unavailable"

    artifact = client.get(completed["artifact_download_url"])
    metadata = client.get(completed["metadata_download_url"])
    assert artifact.status_code == 200
    assert artifact.content.startswith(b"PK")
    assert metadata.status_code == 200
    assert metadata.json()["configuration"]["sample_rate_hz"] == 2_000_000

    history = client.get(f"{base}/captures/{capture_id}")
    assert history.status_code == 200
    assert history.json()["state"] == "completed"

    stopped = client.post("/api/v1/captures/recording/stop")
    assert stopped.status_code == 200
    recording_path = client.app.state.context.capture_service.status().last_recording_file
    assert recording_path is not None
    common_csv = recording_path.read_text(encoding="utf-8-sig")
    assert "LA2016 | ID: kingst_la2016_test" in common_csv
    assert "event,capture_id,state,trigger,artifact_file" in common_csv
    assert "logic_capture_started" in common_csv
    assert "logic_capture_completed" in common_csv
    assert f"{capture_id}/capture.sr" in common_csv
    assert analyzer.last_config is not None
    assert analyzer.last_config.triggers == ()


def test_hardware_arm_and_delayed_start_are_available_through_api(
    client: TestClient,
) -> None:
    analyzer = register_fake_analyzer(client)
    base = f"/api/v1/logic-analyzers/{analyzer.device_id}"
    configured = client.patch(
        f"{base}/settings",
        json={
            "sample_rate_hz": 1_000_000,
            "sample_count": 100_000,
            "triggers": [{"channel": 0, "condition": "rising"}],
        },
    )
    assert configured.status_code == 200

    armed = client.post(
        f"{base}/captures/arm",
        json={"title": "Triggered", "comment": "Wait for CH0"},
    )
    assert armed.status_code == 201
    waiting = wait_for_logic_state(client, analyzer.device_id, {"armed"})
    assert waiting["remaining_s"] is None
    assert waiting["trigger"] == "CH0 rising"

    analyzer.capture_delay_s = 0.2
    analyzer.download_delay_s = 0.2
    assert client.portal is not None
    client.portal.call(analyzer.trigger.set)
    after_trigger = wait_for_logic_state(
        client,
        analyzer.device_id,
        {"capturing", "downloading"},
    )
    assert after_trigger["estimated_duration_s"] > 0
    assert after_trigger["remaining_s"] is not None
    completed = wait_for_logic_state(client, analyzer.device_id, {"completed"})
    assert completed["triggered_at"] is not None
    assert completed["trigger_timestamp_quality"] == "driver_log"

    analyzer.trigger = asyncio.Event()
    analyzer.stop_requested = asyncio.Event()
    delayed = client.patch(
        f"{base}/settings",
        json={
            "triggers": [],
            "auto_start_enabled": True,
            "auto_start_delay_s": 0.05,
        },
    )
    assert delayed.status_code == 200
    recording = client.post(
        "/api/v1/captures/recording/start",
        json={"title": "Delayed LA", "comment": "Automatic schedule"},
    )
    assert recording.status_code == 201
    scheduled = wait_for_logic_state(client, analyzer.device_id, {"scheduled"})
    assert scheduled["scheduled_start_at"] is not None
    assert scheduled["estimated_duration_s"] == pytest.approx(0.05)
    assert scheduled["remaining_s"] is not None
    completed = wait_for_logic_state(client, analyzer.device_id, {"completed"})
    assert completed["source"] == "global_recording"
    client.post("/api/v1/captures/recording/stop").raise_for_status()


def test_logic_api_rejects_invalid_trigger_combinations(client: TestClient) -> None:
    analyzer = register_fake_analyzer(client)
    response = client.patch(
        f"/api/v1/logic-analyzers/{analyzer.device_id}/settings",
        json={
            "triggers": [
                {"channel": 0, "condition": "rising"},
                {"channel": 1, "condition": "falling"},
            ]
        },
    )
    assert response.status_code == 400
    assert "at most one edge trigger" in response.json()["detail"]


def test_compact_logic_settings_apply_one_common_trigger(client: TestClient) -> None:
    analyzer = register_fake_analyzer(client)

    response = client.post(
        f"/ui/devices/{analyzer.device_id}/logic-analyzer/settings",
        data={
            "channels": ["0", "2"],
            "sample_rate_hz": "5000000",
            "sample_count": "200000",
            "threshold_v": "0.9",
            "capture_ratio_percent": "25",
            "trigger_condition": "high",
            "trigger_channels": ["1", "2"],
            "auto_start_delay_s": "0",
        },
    )

    assert response.status_code == 200
    settings = client.app.state.context.logic_analyzer_service.settings(
        analyzer.device_id
    )
    assert settings.channels == (0, 1, 2)
    assert settings.sample_rate_hz == 5_000_000
    assert settings.sample_count == 200_000
    assert settings.threshold_v == 0.9
    assert settings.capture_ratio_percent == 25
    assert settings.common_trigger_condition == "high"
    assert settings.trigger_channels == (1, 2)
    assert "LA2016 settings applied" in response.text


def test_logic_settings_are_restored_when_device_returns(client: TestClient) -> None:
    analyzer = register_fake_analyzer(client)
    service = client.app.state.context.logic_analyzer_service
    expected = service.update_settings(
        analyzer.device_id,
        channels=(0, 3, 7),
        sample_rate_hz=20_000_000,
        sample_count=2_000_000,
        threshold_v=1.4,
        capture_ratio_percent=25,
        triggers=(KingstTrigger(3, "rising"),),
        auto_start_enabled=True,
        auto_start_delay_s=1.5,
    )

    client.portal.call(service.remove_device, analyzer.device_id)
    service.add_device(analyzer)

    assert service.settings(analyzer.device_id) == expected


def test_logic_failure_details_are_hidden_behind_compact_button(
    client: TestClient,
) -> None:
    analyzer = register_fake_analyzer(client)
    analyzer.fail_error = "Cannot open device: LIBUSB_ERROR_NOT_SUPPORTED"

    started = client.post(
        f"/api/v1/logic-analyzers/{analyzer.device_id}/captures/start",
        json={"title": "Failure UI", "comment": "Details popover"},
    )
    assert started.status_code == 201
    wait_for_logic_state(client, analyzer.device_id, {"error"})

    panel = client.get(
        f"/ui/devices/{analyzer.device_id}/logic-analyzer/status"
    )
    assert panel.status_code == 200
    assert "logic-error-button" in panel.text
    assert ">Error details</button>" in panel.text
    assert 'class="logic-error-popover"' in panel.text
    assert "LIBUSB_ERROR_NOT_SUPPORTED" in panel.text
    assert 'class="logic-last-capture"' not in panel.text
