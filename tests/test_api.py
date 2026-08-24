from __future__ import annotations

import asyncio
import csv
import json
import re
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from openbench.api import devices as api_devices
from openbench.domain import Device
from openbench.drivers.micsig_mho1 import (
    DEFAULT_SCALAR_MEASUREMENTS,
    MicsigScalarMeasurement,
    MicsigScalarMeasurementSpec,
    MicsigScreenshot,
    MicsigSnapshot,
    MicsigWaveformCapture,
    MicsigWaveformPreamble,
)
from openbench.services.matrix_service import INITIAL_PROFILE_ID
from openbench.storage.models import MeasurementModel
from openbench.web import routes as web_routes


class FakeDashboardScope:
    device_id = "micsig_mho1_test"

    def __init__(self) -> None:
        self.replaced_profiles: list[tuple[MicsigScalarMeasurementSpec, ...]] = []
        self.capture_requests: list[tuple[tuple[str, ...], bool]] = []
        self.capture_modes: list[tuple[bool, bool]] = []
        self.control_actions: list[str] = []

    @staticmethod
    def _measurements(
        items: tuple[str, ...],
        channels: tuple[str, ...] = ("CH1", "CH2", "CH3", "CH4"),
    ) -> tuple[MicsigScalarMeasurement, ...]:
        return tuple(
            MicsigScalarMeasurement(
                item=item,
                channel=channel,
                value=float(index),
                unit="V",
                status="ok",
            )
            for channel in channels
            for index, item in enumerate(items, start=1)
        )

    async def capture_screenshot(self) -> MicsigScreenshot:
        return MicsigScreenshot(data=b"fake-png", image_format="png")

    async def read_scalar_measurements(
        self,
        channel: int | str,
        items: tuple[str, ...] = DEFAULT_SCALAR_MEASUREMENTS,
    ) -> tuple[MicsigScalarMeasurement, ...]:
        normalized = f"CH{channel}" if isinstance(channel, int) else channel
        return self._measurements(items, (normalized,))

    async def replace_scalar_measurements(
        self,
        measurements: tuple[MicsigScalarMeasurementSpec, ...],
    ) -> tuple[MicsigScalarMeasurement, ...]:
        self.replaced_profiles.append(measurements)
        return tuple(
            MicsigScalarMeasurement(
                item=spec.item,
                channel=spec.channel,
                secondary_channel=spec.secondary_channel,
                source_edge=spec.source_edge,
                target_edge=spec.target_edge,
                value=float(index),
                unit="V",
                status="ok",
            )
            for index, spec in enumerate(measurements, start=1)
        )

    async def read_scalar_measurement_profile(
        self,
        measurements: tuple[MicsigScalarMeasurementSpec, ...],
    ) -> tuple[MicsigScalarMeasurement, ...]:
        return await self.replace_scalar_measurements(measurements)

    async def capture_frame(
        self,
        measurements: tuple[MicsigScalarMeasurementSpec, ...],
        *,
        channels: tuple[int | str, ...] = (1, 2, 3, 4),
        include_screenshot: bool = True,
        stop_before_capture: bool = True,
        resume_after: bool = True,
    ) -> MicsigSnapshot:
        selected_channels = tuple(
            f"CH{channel}" if isinstance(channel, int) else channel for channel in channels
        )
        self.capture_requests.append((selected_channels, include_screenshot))
        self.capture_modes.append((stop_before_capture, resume_after))
        scalars = tuple(
            MicsigScalarMeasurement(
                item=spec.item,
                channel=spec.channel,
                secondary_channel=spec.secondary_channel,
                source_edge=spec.source_edge,
                target_edge=spec.target_edge,
                value=float(index),
                unit="V",
                status="ok",
            )
            for index, spec in enumerate(measurements, start=1)
        )
        preamble = MicsigWaveformPreamble(
            format_code=2,
            mode_code=0,
            averaging_count=1,
            x_increment_s=1e-9,
            x_origin_s=-1e-9,
            x_reference=0,
            y_increment=1,
            y_origin=0,
            y_reference=0,
        )
        reported_preamble = MicsigWaveformPreamble(
            format_code=0,
            mode_code=0,
            averaging_count=1,
            x_increment_s=1e-9,
            x_origin_s=-1e-9,
            x_reference=0,
            y_increment=1,
            y_origin=0,
            y_reference=0,
        )
        waveforms = tuple(
            MicsigWaveformCapture(
                source=channel,
                mode="NORMAL",
                samples=(float(index),),
                preamble=preamble,
                ascii_data=f"{index}".encode(),
                preamble_text="0,0,1,1e-9,-1e-9,0,1,0,0",
                reported_preamble=reported_preamble,
            )
            for index, channel in enumerate(selected_channels, 1)
        )
        return MicsigSnapshot(
            region="screen",
            measurements=scalars,
            measurements_csv=b"",
            screenshot=(
                MicsigScreenshot(data=b"fake-png", image_format="png")
                if include_screenshot
                else None
            ),
            screenshot_error=None,
            waveforms=waveforms,
            waveform_csv=(
                (
                    "sample_index,time_s,"
                    + ",".join(f"{channel.lower()}_v" for channel in selected_channels)
                    + "\n0,0,"
                    + ",".join(str(index) for index, _channel in enumerate(selected_channels, 1))
                    + "\n"
                ).encode()
                if selected_channels
                else b""
            ),
            elapsed_s=0.75,
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
        await asyncio.sleep(0.01)
        self.control_actions.append("STOP")
        return "STOP"

    async def start(self) -> None:
        self.control_actions.append("RUN")

    async def capture_snapshot(
        self,
        *,
        region: str = "screen",
        measurement_items: tuple[str, ...] = DEFAULT_SCALAR_MEASUREMENTS,
        include_screenshot: bool = True,
        resume: bool = False,
        stop_timeout_s: float = 2.0,
    ) -> MicsigSnapshot:
        del resume, stop_timeout_s
        measurements = self._measurements(measurement_items)
        return MicsigSnapshot(
            region=region,
            measurements=measurements,
            measurements_csv=b"",
            screenshot=(
                MicsigScreenshot(data=b"fake-png", image_format="png")
                if include_screenshot
                else None
            ),
            screenshot_error=None,
        )


def test_api_health(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "safety_state": "safe",
        "scheduler_running": True,
        "devices": 2,
    }


def test_api_devices(client: TestClient) -> None:
    response = client.get("/api/v1/devices")
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()}
    assert ids == {"sim_meter_output_voltage", "sim_matrix_main"}


def test_device_discovery_and_settings_api(client: TestClient) -> None:
    discovery = client.post("/api/v1/devices/discover/simulated")
    assert discovery.status_code == 200
    assert discovery.json()[0]["id"] == "sim_meter_output_voltage"

    initial = client.get("/api/v1/devices/sim_meter_output_voltage/settings")
    assert initial.status_code == 200
    assert initial.json()["context"] == ""
    assert initial.json()["minimum_poll_interval_s"] == 0.1

    updated = client.patch(
        "/api/v1/devices/sim_meter_output_voltage/settings",
        json={
            "context": "Выходной каскад усилителя",
            "poll_interval_s": 0.5,
        },
    )
    assert updated.status_code == 200
    assert updated.json() == {
        "device_id": "sim_meter_output_voltage",
        "context": "Выходной каскад усилителя",
        "poll_interval_s": 0.5,
        "minimum_poll_interval_s": 0.1,
        "maximum_poll_interval_s": 600.0,
        "scope_screen": None,
        "scope_data": None,
        "scope_channels": None,
        "scope_wait_for_trigger": None,
    }
    channels = client.get("/api/v1/channels").json()
    simulated = next(item for item in channels if item["device_id"] == "sim_meter_output_voltage")
    assert simulated["poll_interval_s"] == 0.5

    invalid = client.patch(
        "/api/v1/devices/sim_meter_output_voltage/settings",
        json={"poll_interval_s": 0.01},
    )
    assert invalid.status_code == 400


def test_capture_api_supports_snapshot_and_recording(client: TestClient) -> None:
    snapshot = client.post(
        "/api/v1/captures/snapshot",
        json={
            "title": "API snapshot",
            "comment": "Снимок, созданный через JSON API",
        },
    )
    assert snapshot.status_code == 201
    snapshot_payload = snapshot.json()
    assert snapshot_payload["file_name"].endswith("_snap_api_snapsh.csv")
    assert snapshot_payload["measurement_count"] == 1
    downloaded = client.get(snapshot_payload["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.content.startswith(b"\xef\xbb\xbf")
    decoded = downloaded.content.decode("utf-8-sig")
    assert "API snapshot" in decoded
    assert "Снимок, созданный через JSON API" in decoded

    started = client.post(
        "/api/v1/captures/recording/start",
        json={
            "title": "API recording",
            "comment": "Started by Codex",
        },
    )
    assert started.status_code == 201
    assert started.json()["active"] is True
    assert started.json()["title"] == "API recording"
    status = client.get("/api/v1/captures/status")
    assert status.status_code == 200
    assert status.json()["active"] is True
    stopped = client.post("/api/v1/captures/recording/stop")
    assert stopped.status_code == 200
    assert stopped.json()["active"] is False
    assert stopped.json()["last_recording_file"].endswith("_rec_api_record.csv")


def test_api_profile_create_validate_apply(client: TestClient) -> None:
    create_response = client.post(
        "/api/v1/matrix/profiles",
        json={
            "name": "Scope output",
            "connections": [{"from": "dut.vout", "to": "scope.ch1"}],
        },
    )
    assert create_response.status_code == 201
    profile = create_response.json()
    profile_id = profile["id"]

    validate_response = client.post(f"/api/v1/matrix/profiles/{profile_id}/validate")
    assert validate_response.status_code == 200
    assert validate_response.json() == {"valid": True, "errors": []}

    apply_response = client.post(f"/api/v1/matrix/profiles/{profile_id}/apply")
    assert apply_response.status_code == 200
    assert apply_response.json()["profile_id"] == profile_id

    active_response = client.get("/api/v1/matrix/active")
    assert active_response.json()["profile_id"] == profile_id


def test_emergency_stop_opens_matrix_and_latches_safety(client: TestClient) -> None:
    apply_response = client.post(f"/api/v1/matrix/profiles/{INITIAL_PROFILE_ID}/apply")
    assert apply_response.status_code == 200

    stop_response = client.post(
        "/api/v1/emergency-stop",
        json={"reason": "test operator stop"},
    )
    assert stop_response.status_code == 200
    payload = stop_response.json()
    assert payload["safety"]["state"] == "emergency_stop"
    assert payload["matrix"]["active_connections"] == []
    interlocked_dashboard = client.get("/")
    assert "INTERLOCKED" in interlocked_dashboard.text
    assert 'class="status-danger"' in interlocked_dashboard.text

    blocked = client.post(f"/api/v1/matrix/profiles/{INITIAL_PROFILE_ID}/apply")
    assert blocked.status_code == 409

    reset = client.post("/api/v1/simulation/reset-safety")
    assert reset.status_code == 200
    assert reset.json()["state"] == "safe"


def test_dashboard_and_matrix_return_html(client: TestClient) -> None:
    dashboard = client.get("/")
    matrix = client.get("/matrix")
    assert dashboard.status_code == 200
    assert "Bench dashboard" not in dashboard.text
    assert 'data-channel-id="sim_meter_output_voltage.primary"' in dashboard.text
    assert 'id="devices-popover"' in dashboard.text
    assert 'popovertarget="devices-popover"' in dashboard.text
    assert "MANAGE" not in dashboard.text
    assert 'title="Connected / added instruments"' in dashboard.text
    assert "OPERATIONAL" in dashboard.text
    assert 'title="OpenBench safety state.' in dashboard.text
    assert 'title="Live link between this browser' in dashboard.text
    assert "Find all" in dashboard.text
    assert "data-discovery-progress" in dashboard.text
    assert "s elapsed" in dashboard.text
    assert ">RUN</button>" in dashboard.text
    assert '<option value="once">Once</option>' in dashboard.text
    assert "Start CSV recording" not in dashboard.text
    assert "Data capture" not in dashboard.text
    assert "Snapshot or CSV recording" not in dashboard.text
    assert "Download CSV" not in dashboard.text
    assert 'aria-label="Open saved folder"' in dashboard.text
    assert 'class="vlasovlab-logo"' in dashboard.text
    assert "ALL OPEN · SIMULATED" not in dashboard.text
    assert "Electrical Matrix <span>simulated</span>" not in dashboard.text
    assert matrix.status_code == 200
    assert "Commutation Matrix" in matrix.text


def test_api_documentation_has_back_button(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    assert 'class="openbench-docs-back"' in response.text
    assert 'href="/"' in response.text
    assert 'aria-label="Switch API color theme"' in response.text
    assert "/static/favicon.svg?v=20260730-2" in response.text


def test_interface_language_switch_is_persistent_and_stays_local(client: TestClient) -> None:
    switched = client.get("/ui/language/ru?next=/matrix", follow_redirects=False)
    assert switched.status_code == 303
    assert switched.headers["location"] == "/matrix"
    assert "openbench_language=ru" in switched.headers["set-cookie"]

    dashboard = client.get("/")
    assert '<html lang="ru" data-language="ru">' in dashboard.text
    assert 'href="/ui/language/en?next=/"' in dashboard.text
    assert "/static/i18n.js?v=20260824-5" in dashboard.text

    docs = client.get("/docs")
    assert '<html lang="ru" data-language="ru">' in docs.text
    assert "/static/i18n.js?v=20260824-5" in docs.text

    safe_redirect = client.get(
        "/ui/language/en?next=//example.invalid",
        follow_redirects=False,
    )
    assert safe_redirect.headers["location"] == "/"


def test_russian_translation_catalog_covers_primary_safety_controls(client: TestClient) -> None:
    response = client.get("/static/i18n.js")
    assert response.status_code == 200
    script = response.text
    assert '"ALL OUTPUTS OFF": "ВЫКЛЮЧИТЬ ВСЕ ВЫХОДЫ"' in script  # noqa: RUF001
    assert '"Wiring and load checked — required to enable Output"' in script
    assert '"Commutation Matrix": "Коммутационная матрица"' in script


def test_dashboard_has_openbench_identity(client: TestClient) -> None:
    dashboard = client.get("/")
    assert "<title>OpenBench</title>" in dashboard.text
    assert 'rel="icon"' in dashboard.text
    assert "/static/favicon.svg?v=20260730-2" in dashboard.text
    assert "/static/htmx-2.0.4.min.js" in dashboard.text
    assert "unpkg.com" not in dashboard.text
    favicon = client.get("/static/favicon.svg")
    assert favicon.status_code == 200
    assert b"<svg" in favicon.content
    htmx = client.get("/static/htmx-2.0.4.min.js")
    assert htmx.status_code == 200
    assert b"htmx" in htmx.content[:200]


def test_dashboard_theme_toggle_and_separate_simulator(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="theme-toggle"' in response.text
    assert "<h2>Simulated Meter</h2>" in response.text
    assert '<p class="device-type">Multimeter</p>' in response.text
    assert "dc voltage meter" not in response.text
    assert "Output voltage" not in response.text
    assert "Dictate title" not in response.text
    assert "Dictate comment" not in response.text
    assert "SpeechRecognition" not in response.text
    assert 'placeholder="Title"' in response.text
    assert 'placeholder="Comment"' in response.text
    assert 'maxlength="10000"' in response.text
    assert "data-auto-grow" in response.text
    assert "Disconnect" in response.text
    assert "Settings" in response.text
    assert "Measurement context" in response.text
    assert "Changes are applied immediately" in response.text
    assert "data-device-polling-input" in response.text
    assert 'class="polling-form"' not in response.text
    assert 'data-role="device-status"' in response.text
    assert "DISCONNECTED" in response.text
    assert 'sample.status === "invalid"' in response.text


def test_snapshot_and_recording_controls_create_csv(client: TestClient) -> None:
    snapshot = client.post(
        "/ui/captures/run",
        data={
            "title": "Bench idle",
            "comment": "Rev A, no load",
            "capture_mode": "once",
        },
    )
    assert snapshot.status_code == 200
    assert "Snapshot saved:" in snapshot.text
    assert "_snap_bench_idle.csv" in snapshot.text
    match = re.search(r"(\d{8}_\d{4}_snap_bench_idle\.csv)", snapshot.text)
    assert match is not None
    snapshot_link = match.group(1)
    downloaded = client.get(f"/captures/{snapshot_link}")
    assert downloaded.status_code == 200
    assert downloaded.content.startswith(b"\xef\xbb\xbf")
    assert "capture_type,snapshot,title,Bench idle" in downloaded.text
    assert "Bench idle" in downloaded.text
    assert "Rev A, no load" in downloaded.text
    assert "Simulated Meter | ID: sim_meter_output_voltage | poll_interval_s:" in (downloaded.text)
    assert "timestamp_utc,value,unit,status,quality" in downloaded.text

    started = client.post(
        "/ui/captures/run",
        data={
            "title": "Thermal drift",
            "comment": "Ten minute plan",
            "capture_mode": "30",
        },
    )
    assert started.status_code == 200
    assert "CSV recording started:" in started.text
    assert "Thermal drift" in started.text
    assert 'data-duration="30.0"' in started.text
    time.sleep(0.05)
    stopped = client.post("/ui/captures/recording/stop")
    assert stopped.status_code == 200
    assert "CSV recording stopped:" in stopped.text
    recording_match = re.search(r"(\d{8}_\d{4}_rec_thermal_dr\.csv)", stopped.text)
    assert recording_match is not None
    recording = client.get(f"/captures/{recording_match.group(1)}")
    assert recording.status_code == 200
    assert "Thermal drift" in recording.text
    assert "Ten minute plan" in recording.text


def test_instrument_context_is_previewed_and_written_once_per_csv(
    client: TestClient,
) -> None:
    instrument_context = "Выходной каскад силового модуля усилителя"
    saved = client.post(
        "/ui/devices/sim_meter_output_voltage/context",
        data={"instrument_context": instrument_context},
    )
    assert saved.status_code == 204

    dashboard = client.get("/")
    assert f"{instrument_context[:20]}…" in dashboard.text
    assert f'title="{instrument_context}"' in dashboard.text

    snapshot = client.post(
        "/ui/captures/run",
        data={
            "title": "Context header",
            "comment": "Instrument context test",
            "capture_mode": "once",
        },
    )
    match = re.search(r"(\d{8}_\d{4}_snap_context_he\.csv)", snapshot.text)
    assert match is not None
    snapshot_csv = client.get(f"/captures/{match.group(1)}").content.decode("utf-8-sig")
    assert f"context: {instrument_context}" in snapshot_csv
    assert snapshot_csv.count(instrument_context) == 1

    started = client.post(
        "/ui/captures/run",
        data={
            "title": "Context record",
            "comment": "Instrument context recording test",
            "capture_mode": "0",
        },
    )
    assert started.status_code == 200
    time.sleep(0.05)
    stopped = client.post("/ui/captures/recording/stop")
    assert stopped.status_code == 200
    recording_path = client.app.state.context.capture_service.status().last_recording_file
    assert recording_path is not None
    recording_csv = recording_path.read_text(encoding="utf-8-sig")
    assert f"context: {instrument_context}" in recording_csv
    assert recording_csv.count(instrument_context) == 1


def test_capture_filename_collision_and_cyrillic_metadata(
    client: TestClient,
) -> None:
    cyrillic_comment = "Комментарий \u0441 кириллицей"
    first = client.post(
        "/ui/captures/snapshot",
        data={
            "title": "Русский тест длиннее",
            "comment": cyrillic_comment,
        },
    )
    second = client.post(
        "/ui/captures/snapshot",
        data={
            "title": "Русский тест длиннее",
            "comment": "Второй комментарий",
        },
    )
    first_match = re.search(
        r"(\d{8}_\d{4}_snap_русский_те\.csv)",
        first.text,
    )
    second_match = re.search(
        r"(\d{8}_\d{4}_snap_русский_те_02\.csv)",
        second.text,
    )
    assert first_match is not None
    assert second_match is not None
    downloaded = client.get(f"/captures/{first_match.group(1)}")
    assert downloaded.content.startswith(b"\xef\xbb\xbf")
    decoded = downloaded.content.decode("utf-8-sig")
    assert "Русский тест длиннее" in decoded
    assert cyrillic_comment in decoded


def test_eto5004_renders_with_oscilloscope_controls(client: TestClient) -> None:
    context = client.app.state.context
    scope = FakeDashboardScope()
    scope.device_id = "micsig_eto5004_test"
    scope.screenshot_supported = False
    device = Device(
        id=scope.device_id,
        name="Micsig ETO5004",
        kind="micsig_eto",
        connected=True,
        capabilities=("oscilloscope",),
    )
    context.registry.register(device, scope)
    context.device_service.register(device, ())
    client.portal.call(context.scope_measurement_service.add_scope, scope)

    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "<h2>ETO5004</h2>" in dashboard.text
    assert 'name="screen"' not in dashboard.text
    assert 'name="data"' in dashboard.text
    assert dashboard.text.count('name="scope_channel"') == 4
    assert "Full memory ASCII" in dashboard.text
    assert "Capture MAX once" in dashboard.text
    assert 'name="maximum_channel"' in dashboard.text

    maximum_status = client.get(f"/api/v1/oscilloscopes/{scope.device_id}/maximum-capture")
    assert maximum_status.status_code == 200
    assert maximum_status.json()["state"] == "ready"
    assert maximum_status.json()["active"] is False


def test_active_eto_maximum_capture_blocks_new_device_mutations(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = client.app.state.context
    scope = FakeDashboardScope()
    scope.device_id = "micsig_eto5004_busy"
    scope.screenshot_supported = False
    device = Device(
        id=scope.device_id,
        name="Micsig ETO5004",
        kind="micsig_eto",
        connected=True,
        capabilities=("oscilloscope", "waveform_capture"),
    )
    context.registry.register(device, scope)
    context.device_service.register(device, ())
    client.portal.call(context.scope_measurement_service.add_scope, scope)
    monkeypatch.setattr(
        context.scope_maximum_capture_service,
        "owns_device",
        lambda device_id: device_id == scope.device_id,
    )

    api_update = client.patch(
        f"/api/v1/oscilloscopes/{scope.device_id}/settings",
        json={"timebase_s_per_div": 0.001},
    )
    ui_update = client.post(
        f"/ui/devices/{scope.device_id}/polling",
        data={"interval_s": 2},
    )

    assert api_update.status_code == 409
    assert ui_update.status_code == 409
    assert "wait for it to finish" in api_update.text


def test_mho1_snapshot_saves_selected_artifacts_and_measurements(
    client: TestClient,
) -> None:
    context = client.app.state.context
    scope = FakeDashboardScope()
    device = Device(
        id=scope.device_id,
        name="Micsig MHO14-200",
        kind="micsig_mho1",
        connected=True,
        capabilities=("oscilloscope", "screenshot_capture"),
    )
    context.registry.register(device, scope)
    context.device_service.register(device, ())
    client.portal.call(context.scope_measurement_service.add_scope, scope)

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "<h2>MHO1</h2>" in dashboard.text
    assert 'name="screen"' in dashboard.text
    assert 'name="data"' in dashboard.text
    assert dashboard.text.count('name="scope_channel"') == 4
    assert 'name="wait_for_trigger"' in dashboard.text
    assert "screen-trace CSV" not in dashboard.text
    assert 'class="scope-measurement-settings"' in dashboard.text
    assert "scope-settings-popover" in dashboard.text
    assert "Apply settings" in dashboard.text
    assert "Measurements and frame data" not in dashboard.text
    assert "Apply clears the measurement slots" not in dashboard.text
    assert "Numeric CSV" not in dashboard.text
    assert "Open Settings" not in dashboard.text
    assert "Full memory ASCII" in dashboard.text
    assert "Capture MAX once" in dashboard.text
    assert dashboard.text.count('name="maximum_channel"') == 4
    maximum_status = client.get(f"/api/v1/oscilloscopes/{scope.device_id}/maximum-capture")
    assert maximum_status.status_code == 200
    assert maximum_status.json()["state"] == "ready"
    scope_panel = re.search(
        r'<section class="scope-capture-panel">(.*?)</section>',
        dashboard.text,
        re.DOTALL,
    )
    assert scope_panel is None
    enabled_ascii_data = client.patch(
        f"/api/v1/devices/{scope.device_id}/settings",
        json={"scope_data": True},
    )
    assert enabled_ascii_data.status_code == 200
    scope_settings = client.get(f"/api/v1/devices/{scope.device_id}/settings")
    assert scope_settings.json()["scope_screen"] is True
    assert scope_settings.json()["scope_data"] is True
    assert scope_settings.json()["scope_channels"] == ["CH1", "CH2", "CH3", "CH4"]
    assert scope_settings.json()["scope_wait_for_trigger"] is False
    assert scope_settings.json()["minimum_poll_interval_s"] == 2.0
    disabled_artifacts = client.patch(
        f"/api/v1/devices/{scope.device_id}/settings",
        json={
            "scope_screen": False,
            "scope_data": False,
            "scope_channels": ["CH2", "CH4"],
            "scope_wait_for_trigger": True,
        },
    )
    assert disabled_artifacts.status_code == 200
    assert disabled_artifacts.json()["scope_screen"] is False
    assert disabled_artifacts.json()["scope_data"] is False
    assert disabled_artifacts.json()["scope_channels"] == ["CH2", "CH4"]
    assert disabled_artifacts.json()["scope_wait_for_trigger"] is True
    reenabled_data = client.patch(
        f"/api/v1/devices/{scope.device_id}/settings",
        json={"scope_data": True},
    )
    assert reenabled_data.status_code == 200
    assert reenabled_data.json()["scope_channels"] == ["CH2", "CH4"]
    no_enabled_channel = client.patch(
        f"/api/v1/devices/{scope.device_id}/settings",
        json={"scope_data": True, "scope_channels": []},
    )
    assert no_enabled_channel.status_code == 400
    too_fast = client.patch(
        f"/api/v1/devices/{scope.device_id}/settings",
        json={"poll_interval_s": 1.99},
    )
    assert too_fast.status_code == 400
    selected = (
        ("CH1", "amplitude"),
        ("CH2", "frequency"),
        ("CH3", "positive_duty"),
    )
    selection_response = client.post(
        f"/ui/devices/{scope.device_id}/scope-measurements/apply",
        data={
            "measurement_channel": [channel for channel, _item in selected],
            "measurement_item": [item for _channel, item in selected],
            "scope_capture_present": "true",
            "screen": "true",
            "data": "true",
            "scope_channel": ["CH1", "CH3"],
            "wait_for_trigger": "true",
        },
    )
    assert [
        tuple((spec.channel, spec.item) for spec in profile) for profile in scope.replaced_profiles
    ] == [selected]
    assert selection_response.status_code == 200
    assert "CH1 AMP" in selection_response.text
    assert "CH2 FREQ" in selection_response.text
    assert "CH3 DUTY+" in selection_response.text
    scope_panel = re.search(
        r'<section class="scope-capture-panel">(.*?)</section>',
        selection_response.text,
        re.DOTALL,
    )
    assert scope_panel is not None
    assert 'name="screen"' not in scope_panel.group(1)
    assert 'name="data"' not in scope_panel.group(1)
    assert 'name="scope_channel"' not in scope_panel.group(1)
    assert scope_panel.group(1).count("data-scope-measurement") == len(selected)
    assert 'data-scope-channel="CH1"' in scope_panel.group(1)
    assert 'data-scope-channel="CH2"' in scope_panel.group(1)
    assert 'data-scope-channel="CH3"' in scope_panel.group(1)
    formatted_dashboard = client.get("/")
    assert formatted_dashboard.status_code == 200
    assert "const formatScopeMeasurement" in formatted_dashboard.text
    assert 'displayUnit = "mV"' in formatted_dashboard.text
    assert 'displayUnit = "kHz"' in formatted_dashboard.text
    assert 'displayUnit = "MHz"' in formatted_dashboard.text
    assert "scaled.toFixed(3)" in formatted_dashboard.text
    profile_response = client.get(f"/api/v1/oscilloscopes/{scope.device_id}/measurements")
    assert profile_response.status_code == 200
    assert [
        (item["channel"], item["item"]) for item in profile_response.json()["measurements"]
    ] == list(selected)

    # Live card values are transient event-bus data. They are not measurement
    # history and become durable only through the common RUN capture workflow.
    with context.database.session() as session:
        stored_scope_values = session.scalar(
            select(func.count(MeasurementModel.id)).where(
                MeasurementModel.device_id == scope.device_id
            )
        )
    assert stored_scope_values == 0
    selected_settings = client.get(f"/api/v1/devices/{scope.device_id}/settings").json()
    assert selected_settings["scope_screen"] is True
    assert selected_settings["scope_data"] is True
    assert selected_settings["scope_channels"] == ["CH1", "CH3"]
    assert selected_settings["scope_wait_for_trigger"] is True

    response = client.post(
        "/ui/captures/snapshot",
        data={"title": "Scope test", "comment": "All MHO1 outputs"},
    )
    assert response.status_code == 200
    match = re.search(r"(\d{8}_\d{4}_snap_scope_test\.csv)", response.text)
    assert match is not None
    table_path = context.capture_service.resolve_artifact(match.group(1))
    with table_path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.reader(stream))

    group_header = rows[2]
    field_header = rows[3]
    data_row = rows[4]
    assert any("Simulated Meter | ID:" in cell for cell in group_header)
    assert any("MHO1 | ID: micsig_mho1_test" in cell for cell in group_header)
    screen_index = field_header.index("screen_file")
    data_index = field_header.index("data_file")
    screen_file = data_row[screen_index]
    data_file = data_row[data_index]
    assert screen_file.startswith(f"{table_path.stem}/")
    assert data_file.startswith(f"{table_path.stem}/")
    assert (context.capture_service.output_directory / screen_file).read_bytes() == b"fake-png"
    assert scope.control_actions[:3] == ["SINGLE", "STOP", "RUN"]
    assert scope.capture_modes[-1] == (False, False)
    manifest_path = context.capture_service.output_directory / data_file
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["captured_at_utc"]
    assert manifest["capture"]["selected_channels"] == ["CH1", "CH3"]
    assert manifest["waveform"]["common_preamble_raw"] == ("0,0,1,1e-9,-1e-9,0,1,0,0")
    assert manifest["waveform"]["common_preamble"]["format_code"] == 0
    assert manifest["waveform"]["common_preamble"]["x_increment_s"] == 1e-9
    assert manifest["waveform"]["horizontal_timing"] == {
        "captured_span_s": 0.0,
        "first_sample_time_s": -1e-9,
        "last_sample_time_s": -1e-9,
        "reference_sample": 0,
        "sample_interval_s": 1e-9,
        "source": "waveform_preamble",
        "time_origin_s": -1e-9,
    }
    assert [item["source"] for item in manifest["waveform"]["channels"]] == [
        "CH1",
        "CH3",
    ]
    assert (
        (manifest_path.parent / manifest["files"]["measurements"])
        .read_text(encoding="utf-8")
        .startswith("channel,measurement,value,unit,status")
    )
    assert (
        (manifest_path.parent / manifest["files"]["waveforms_csv"])
        .read_text(encoding="utf-8")
        .startswith("sample_index,time_s,ch1_v,ch3_v")
    )
    for index, item in enumerate(manifest["waveform"]["channels"], 1):
        assert (manifest_path.parent / item["ascii_file"]).read_bytes() == str(index).encode()
    assert (("CH1", "CH3"), True) in scope.capture_requests
    measurement_value_columns = [
        header for header in field_header if header.startswith("CH") and header.endswith("_value")
    ]
    assert len(measurement_value_columns) == len(selected)

    only_screen = client.post(
        f"/ui/devices/{scope.device_id}/scope-capture",
        data={"screen": "true"},
    )
    assert only_screen.status_code == 200
    screen_response = client.post(
        "/ui/captures/snapshot",
        data={"title": "Scope screen only"},
    )
    assert screen_response.status_code == 200
    screen_table = context.capture_service.status().last_snapshot_file
    assert screen_table is not None
    with screen_table.open(newline="", encoding="utf-8-sig") as stream:
        screen_rows = list(csv.reader(stream))
    screen_headers = screen_rows[3]
    screen_values = screen_rows[4]
    assert screen_values[screen_headers.index("screen_file")].endswith("/mho1_screen.png")
    screen_manifest_file = screen_values[screen_headers.index("data_file")]
    assert screen_manifest_file.endswith("/mho1_capture.json")
    screen_manifest = json.loads(
        (context.capture_service.output_directory / screen_manifest_file).read_text(
            encoding="utf-8"
        )
    )
    assert screen_manifest["capture"]["ascii_enabled"] is False
    assert screen_manifest["waveform"]["channels"] == []
    assert ((), True) in scope.capture_requests

    client.post(
        f"/ui/devices/{scope.device_id}/scope-capture",
        data={"screen": "true"},
    )
    started = client.post(
        "/ui/captures/recording/start",
        data={"title": "Scope live", "comment": "Selected MHO1 measurements"},
    )
    assert started.status_code == 200
    time.sleep(2.2)
    stopped = client.post("/ui/captures/recording/stop")
    assert stopped.status_code == 200
    recording_path = context.capture_service.status().last_recording_file
    assert recording_path is not None
    with recording_path.open(newline="", encoding="utf-8-sig") as stream:
        recording_rows = list(csv.reader(stream))
    recording_headers = recording_rows[3]
    assert "CH1_amplitude_value" in recording_headers
    assert "CH2_frequency_value" in recording_headers
    assert "CH3_positive_duty_value" in recording_headers
    assert any(row[recording_headers.index("CH1_amplitude_value")] for row in recording_rows[4:])

    trigger_setting = client.patch(
        f"/api/v1/devices/{scope.device_id}/settings",
        json={"scope_wait_for_trigger": True},
    )
    assert trigger_setting.status_code == 200
    triggered_started = client.post(
        "/ui/captures/recording/start",
        data={"title": "Scope trigger", "comment": "SINGLE acquisition"},
    )
    assert triggered_started.status_code == 200
    time.sleep(0.08)
    triggered_stopped = client.post("/ui/captures/recording/stop")
    assert triggered_stopped.status_code == 200
    triggered_path = context.capture_service.status().last_recording_file
    assert triggered_path is not None
    with triggered_path.open(newline="", encoding="utf-8-sig") as stream:
        triggered_rows = list(csv.reader(stream))
    triggered_headers = triggered_rows[3]
    trigger_event_index = triggered_headers.index("trigger_event")
    assert any(row[trigger_event_index] == "trigger" for row in triggered_rows[4:])
    assert scope.control_actions.count("SINGLE") >= 2

    removed = client.post(
        f"/ui/devices/{scope.device_id}/scope-measurements/remove",
        data={"channel": "CH2", "item": "frequency"},
    )
    assert removed.status_code == 200
    assert "CH2 FREQ" not in removed.text
    assert removed.text.count('class="scope-live-pill"') == 2


def test_mho1_settings_support_phase_and_delay_between_two_channels(
    client: TestClient,
) -> None:
    context = client.app.state.context
    scope = FakeDashboardScope()
    device = Device(
        id=scope.device_id,
        name="Micsig MHO14-200",
        kind="micsig_mho1",
        connected=True,
        capabilities=("oscilloscope",),
    )
    context.registry.register(device, scope)
    context.device_service.register(device, ())
    client.portal.call(context.scope_measurement_service.add_scope, scope)

    response = client.post(
        f"/ui/devices/{scope.device_id}/scope-measurements/apply",
        data={
            "measurement_channel": ["CH1", "CH1"],
            "measurement_item": ["phase", "delay"],
            "measurement_secondary_channel": ["CH2", "CH2"],
            "measurement_source_edge": ["FRISe", "FRISe"],
            "measurement_target_edge": ["FRISe", "FFALL"],
        },
    )

    assert response.status_code == 200
    assert "CH1→CH2 PHASE" in response.text
    assert "CH1→CH2 DELAY" in response.text
    assert 'value="phase"' in response.text
    assert 'value="delay"' in response.text
    profile = client.get(f"/api/v1/oscilloscopes/{scope.device_id}/measurements")
    assert profile.status_code == 200
    assert [
        (
            item["channel"],
            item["secondary_channel"],
            item["item"],
            item["source_edge"],
            item["target_edge"],
        )
        for item in profile.json()["measurements"]
    ] == [
        ("CH1", "CH2", "phase", None, None),
        ("CH1", "CH2", "delay", "FRISe", "FFALL"),
    ]


def test_polling_rate_can_be_changed_from_dashboard(client: TestClient) -> None:
    response = client.post(
        "/ui/channels/sim_meter_output_voltage.primary/polling",
        data={"interval_s": "10"},
    )
    assert response.status_code == 200
    assert "polling set to every 10 s" in response.text
    channels = client.get("/api/v1/channels").json()
    simulated = next(item for item in channels if item["id"] == "sim_meter_output_voltage.primary")
    assert simulated["poll_interval_s"] == 10.0


def test_timed_recording_stops_automatically(client: TestClient) -> None:
    started = client.post(
        "/ui/captures/recording/start",
        data={
            "title": "Automatic stop",
            "comment": "Timer integration test",
            "duration_s": "1",
        },
    )
    assert started.status_code == 200
    assert 'data-duration="1.0"' in started.text

    time.sleep(1.15)

    dashboard = client.get("/ui/dashboard/content")
    assert dashboard.status_code == 200
    assert "STOPPED" in dashboard.text
    last_file = client.app.state.context.capture_service.status().last_recording_file
    assert last_file is not None
    assert "_rec_automatic.csv" in last_file.name


def test_manual_scope_frames_are_exact_and_linked_in_common_recording(
    client: TestClient,
) -> None:
    context = client.app.state.context
    scope = FakeDashboardScope()
    device = Device(
        id=scope.device_id,
        name="Micsig MHO14-200",
        kind="micsig_mho1",
        connected=True,
        capabilities=("oscilloscope", "screenshot_capture"),
    )
    context.registry.register(device, scope)
    context.device_service.register(device, ())
    client.portal.call(context.scope_measurement_service.add_scope, scope)
    settings = client.patch(
        f"/api/v1/devices/{scope.device_id}/settings",
        json={
            "scope_screen": True,
            "scope_data": True,
            "scope_channels": ["CH1", "CH2", "CH3", "CH4"],
            "scope_wait_for_trigger": False,
        },
    )
    assert settings.status_code == 200

    started = client.post(
        "/api/v1/captures/recording/start",
        json={
            "title": "Manual frames",
            "comment": "Exact externally orchestrated points",
            "scope_capture_mode": "manual",
        },
    )
    assert started.status_code == 201
    assert started.json()["scope_capture_mode"] == "manual"
    assert scope.capture_requests == []

    first = client.post(
        f"/api/v1/captures/recording/scopes/{scope.device_id}/frame",
        json={"label": "sink_0A"},
    )
    second = client.post(
        f"/api/v1/captures/recording/scopes/{scope.device_id}/frame",
        json={"label": "sink_2A"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["capture_id"] == "1"
    assert second.json()["capture_id"] == "2"
    assert first.json()["screen_file"].endswith("/mho1_screen.png")
    assert len(scope.capture_requests) == 2
    assert scope.capture_requests == [
        (("CH1", "CH2", "CH3", "CH4"), True),
        (("CH1", "CH2", "CH3", "CH4"), True),
    ]

    stopped = client.post("/api/v1/captures/recording/stop")
    assert stopped.status_code == 200
    recording_path = context.capture_service.status().last_recording_file
    assert recording_path is not None
    with recording_path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.reader(stream))
    headers = rows[3]
    event_index = headers.index("trigger_event")
    sequence_index = headers.index("trigger_sequence")
    artifact_index = headers.index("trigger_artifact_file")
    message_index = headers.index("trigger_message")
    frame_rows = [row for row in rows[4:] if row[event_index] == "frame"]
    assert [row[sequence_index] for row in frame_rows] == ["1", "2"]
    assert [row[message_index] for row in frame_rows] == ["sink_0A", "sink_2A"]
    assert all(row[artifact_index].endswith("/mho1_capture.json") for row in frame_rows)
    assert scope.control_actions[-1] == "RUN"


def test_selective_discovery_calls_only_requested_driver(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_ut197_discovery(_context: object) -> tuple[object, ...]:
        calls.append("ut197")
        return ()

    async def unexpected_discovery(_context: object) -> tuple[object, ...]:
        calls.append("unexpected")
        return ()

    monkeypatch.setattr(web_routes, "register_ut197_devices", fake_ut197_discovery)
    monkeypatch.setattr(web_routes, "register_ut61d_devices", unexpected_discovery)
    monkeypatch.setattr(web_routes, "register_ut61e_devices", unexpected_discovery)
    monkeypatch.setattr(web_routes, "register_ut61eplus_devices", unexpected_discovery)
    monkeypatch.setattr(web_routes, "register_feeltech_devices", unexpected_discovery)
    monkeypatch.setattr(web_routes, "register_micsig_devices", unexpected_discovery)
    monkeypatch.setattr(web_routes, "register_micsig_eto_devices", unexpected_discovery)

    response = client.post("/ui/devices/discover/ut197")
    assert response.status_code == 200
    assert calls == ["ut197"]
    assert "UNI-T UT197 was not found" in response.text


def test_simulator_can_be_disconnected_and_found_again(client: TestClient) -> None:
    disconnected = client.post("/ui/devices/sim_meter_output_voltage/disconnect")
    assert disconnected.status_code == 200
    assert "Disconnected: Simulated Output Voltage Meter" in disconnected.text
    ids = {item["id"] for item in client.get("/api/v1/devices").json()}
    assert "sim_meter_output_voltage" not in ids

    connected = client.post("/ui/devices/discover/simulated")
    assert connected.status_code == 200
    assert "Connected: Simulated Output Voltage Meter" in connected.text
    ids = {item["id"] for item in client.get("/api/v1/devices").json()}
    assert "sim_meter_output_voltage" in ids


def test_find_all_scans_every_physical_driver(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def discovery(name: str) -> object:
        async def run(_context: object) -> tuple[object, ...]:
            calls.append(name)
            return ()

        return run

    monkeypatch.setattr(web_routes, "register_ut197_devices", discovery("ut197"))
    monkeypatch.setattr(web_routes, "register_ut61d_devices", discovery("ut61d"))
    monkeypatch.setattr(web_routes, "register_ut61e_devices", discovery("ut61e"))
    monkeypatch.setattr(web_routes, "register_ut61eplus_devices", discovery("ut61eplus"))
    monkeypatch.setattr(web_routes, "register_feeltech_devices", discovery("feeltech"))
    monkeypatch.setattr(web_routes, "register_micsig_devices", discovery("micsig"))
    monkeypatch.setattr(web_routes, "register_micsig_eto_devices", discovery("micsig_eto"))

    disconnected = client.post("/ui/devices/sim_meter_output_voltage/disconnect")
    assert disconnected.status_code == 200

    response = client.post("/ui/devices/discover/all")
    assert response.status_code == 200
    assert set(calls) == {
        "feeltech",
        "ut197",
        "ut61d",
        "ut61eplus",
        "micsig",
        "micsig_eto",
    }
    assert "Search complete." in response.text
    ids = {item["id"] for item in client.get("/api/v1/devices").json()}
    assert "sim_meter_output_voltage" not in ids


def test_api_find_all_does_not_connect_simulator(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_physical_devices(_context: object) -> tuple[object, ...]:
        return ()

    monkeypatch.setattr(api_devices, "register_ut197_devices", no_physical_devices)
    monkeypatch.setattr(api_devices, "register_ut61d_devices", no_physical_devices)
    monkeypatch.setattr(api_devices, "register_ut61eplus_devices", no_physical_devices)
    monkeypatch.setattr(api_devices, "register_feeltech_devices", no_physical_devices)
    monkeypatch.setattr(api_devices, "register_micsig_devices", no_physical_devices)
    monkeypatch.setattr(api_devices, "register_micsig_eto_devices", no_physical_devices)
    monkeypatch.setattr(api_devices, "register_kingst_devices", no_physical_devices)
    monkeypatch.setattr(api_devices, "register_owon_spm_devices", no_physical_devices)
    monkeypatch.setattr(api_devices, "register_itech_it6000c_devices", no_physical_devices)

    disconnected = client.delete("/api/v1/devices/sim_meter_output_voltage")
    assert disconnected.status_code == 200

    response = client.post("/api/v1/devices/discover/all")
    assert response.status_code == 404
    ids = {item["id"] for item in client.get("/api/v1/devices").json()}
    assert "sim_meter_output_voltage" not in ids
