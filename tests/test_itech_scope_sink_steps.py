from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from threading import Event
from types import ModuleType
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_itech_scope_sink_steps.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_itech_scope_sink_steps", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


class FakeApiClient:
    instance: FakeApiClient | None = None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        type(self).instance = self
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.recording_active = False
        self.frame_count = 0
        self.state = {
            "priority": "CV",
            "function_mode": "FIXED",
            "output_enabled": False,
            "direction": "IDLE",
            "regulation": "CV",
            "faults": [],
            "voltage_setpoint_v": 13.5,
            "current_setpoint_a": 2.31,
            "current_limit_positive_a": 2.31,
            "current_limit_negative_a": -2.27,
            "voltage_limit_positive_v": 0.2,
            "voltage_limit_negative_v": 0.0,
            "power_limit_positive_w": 55080.0,
            "power_limit_negative_w": -55080.0,
            "measured_voltage_v": 12.0,
            "measured_current_a": 0.0,
            "measured_power_w": 0.0,
            "ovp_enabled": True,
            "ovp_level_v": 12.8,
            "ovp_delay_s": 0.1,
            "ocp_enabled": True,
            "ocp_level_a": 227.25,
            "ocp_delay_s": 0.1,
            "opp_enabled": True,
            "opp_level_w": 3200.0,
            "opp_delay_s": 0.1,
            "uvp_enabled": True,
            "uvp_level_v": 10.0,
            "uvp_delay_s": 1.0,
            "uvp_warmup_s": 0.0,
            "ucp_enabled": False,
            "ucp_level_a": 0.1,
            "ucp_delay_s": 1.0,
            "ucp_warmup_s": 0.0,
        }
        self.scope_settings = {
            "poll_interval_s": 2.0,
            "scope_screen": False,
            "scope_data": False,
            "scope_channels": ["CH1"],
            "scope_wait_for_trigger": False,
        }
        self.scope_profile = {
            "measurements": [{"channel": "CH1", "item": "amplitude"}]
        }
        self.scope_state = {
            "sample_rate_sps": 1_000_000.0,
            "memory_depth_setting": "5500",
            "memory_depth_points": 5500,
            "timebase_mode": "YT",
            "channels": [
                {"channel": 1, "displayed": True},
                {"channel": 2, "displayed": True},
                {"channel": 3, "displayed": True},
                {"channel": 4, "displayed": True},
            ],
        }

    def get(self, path: str, *, timeout_s: float | None = None) -> Any:
        del timeout_s
        self.calls.append(("GET", path, None))
        if path == "/devices":
            return [
                {
                    "id": "itech_test",
                    "kind": "itech_it6000c",
                    "connected": True,
                    "capabilities": [],
                },
                {
                    "id": "scope_test",
                    "kind": "micsig_eto",
                    "connected": True,
                    "capabilities": ["oscilloscope", "screenshot_capture"],
                },
                {
                    "id": "ut61eplus_test",
                    "kind": "ut61eplus",
                    "connected": True,
                    "capabilities": ["multimeter", "voltage"],
                },
            ]
        if path == "/health":
            return {"status": "ok", "safety_state": "safe"}
        if path == "/captures/status":
            return {"active": self.recording_active, "current_file": None}
        if path == "/bidirectional-power-supplies/itech_test":
            return {
                "profile": {
                    "rated_voltage_v": 800.0,
                    "rated_current_a": 225.0,
                    "rated_power_w": 54000.0,
                },
                "state": deepcopy(self.state),
            }
        if path == "/bidirectional-power-supplies/itech_test/measurements":
            return {
                "device_id": "itech_test",
                "timestamp_utc": "2026-08-13T00:00:00+00:00",
                "measured_voltage_v": self.state["measured_voltage_v"],
                "measured_current_a": self.state["measured_current_a"],
                "measured_power_w": self.state["measured_power_w"],
            }
        if path == "/devices/scope_test/settings":
            return deepcopy(self.scope_settings)
        if path == "/oscilloscopes/scope_test/measurements":
            return deepcopy(self.scope_profile)
        if path == "/oscilloscopes/scope_test":
            return deepcopy(self.scope_state)
        raise AssertionError(path)

    def patch(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> Any:
        del timeout_s
        self.calls.append(("PATCH", path, deepcopy(payload)))
        if path == "/bidirectional-power-supplies/itech_test/protections":
            self.state.update(payload)
            return {"state": deepcopy(self.state)}
        if path == "/bidirectional-power-supplies/itech_test/operating-point":
            self.state.update(
                {key: value for key, value in payload.items() if key != "wiring_confirmed"}
            )
            current = self.state["current_setpoint_a"] if self.state["output_enabled"] else 0.0
            self.state["measured_current_a"] = current
            self.state["measured_power_w"] = self.state["measured_voltage_v"] * current
            return {"state": deepcopy(self.state)}
        if path == "/devices/scope_test/settings":
            self.scope_settings.update(payload)
            return deepcopy(self.scope_settings)
        raise AssertionError(path)

    def delete(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        del timeout_s
        self.calls.append(("DELETE", path, deepcopy(payload)))
        assert path == "/bidirectional-power-supplies/itech_test/experiment-reservation"
        return {
            "device_id": "itech_test",
            "active": False,
            "polling_targets_suspended": 14,
        }

    def put(self, path: str, payload: dict[str, Any]) -> Any:
        self.calls.append(("PUT", path, deepcopy(payload)))
        assert path == "/oscilloscopes/scope_test/measurements"
        self.scope_profile = deepcopy(payload)
        return deepcopy(payload)

    def post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        del timeout_s
        body = payload or {}
        self.calls.append(("POST", path, deepcopy(body)))
        if path == "/devices/discover/itech_it6000c":
            return {"devices": ["itech_test"]}
        if path == "/bidirectional-power-supplies/itech_test/experiment-reservation":
            return {
                "device_id": "itech_test",
                "active": True,
                "polling_targets_suspended": 14,
            }
        if path == "/captures/recording/start":
            self.recording_active = True
            return {"current_file": "test_rec.csv"}
        if path == "/captures/recording/scopes/scope_test/frame":
            self.frame_count += 1
            return {
                "capture_id": str(self.frame_count),
                "status": "ok",
                "screen_file": f"test_rec/mho1_frame_{self.frame_count:06d}/mho1_screen.png",
                "data_file": f"test_rec/mho1_frame_{self.frame_count:06d}/mho1_capture.json",
                "error": "",
            }
        if path == "/captures/recording/stop":
            self.recording_active = False
            return {"last_recording_file": "test_rec.csv"}
        raise AssertionError(path)


def test_sink_step_script_requests_exact_full_frames_and_restores_safe_state(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(script, "ApiClient", FakeApiClient)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    args = script.build_parser().parse_args(["--execute", "--wiring-confirmed"])
    assert args.settle_s == 1.0
    assert args.title == "последний тестовый прогон"
    assert args.comment == (
        "Канал 1 - напряжение на выходе, канал 2 - ток, "
        "канал 3 и канал 4 - тестовые каналы с меандром."  # noqa: RUF001
    )

    assert script.run(args) == 0
    client = FakeApiClient.instance
    assert client is not None
    frame_calls = [call for call in client.calls if "/recording/scopes/" in call[1]]
    assert len(frame_calls) == 6
    labels = [call[2]["label"] for call in frame_calls]
    assert [label.split("_", 3)[2] for label in labels] == ["0A", "2A", "4A", "7A", "9A", "11A"]
    measurement_profile = next(
        call[2]
        for call in client.calls
        if call[0] == "PUT"
        and call[1] == "/oscilloscopes/scope_test/measurements"
        and len(call[2]["measurements"]) == 8
    )
    assert measurement_profile["measurements"] == list(script.TEST_MEASUREMENT_PROFILE)
    scope_capture = next(
        call[2]
        for call in client.calls
        if call[0] == "PATCH"
        and call[1] == "/devices/scope_test/settings"
        and call[2].get("scope_data") is True
    )
    assert scope_capture["scope_screen"] is True
    assert scope_capture["scope_data"] is True
    assert scope_capture["scope_channels"] == ["CH1", "CH2", "CH3", "CH4"]
    reservation_call = (
        "POST",
        "/bidirectional-power-supplies/itech_test/experiment-reservation",
        {},
    )
    release_call = (
        "DELETE",
        "/bidirectional-power-supplies/itech_test/experiment-reservation",
        None,
    )
    assert client.calls.count(reservation_call) == 1
    assert client.calls.count(release_call) == 1
    reservation_index = client.calls.index(reservation_call)
    release_index = client.calls.index(release_call)
    first_itech_full_state = client.calls.index(
        ("GET", "/bidirectional-power-supplies/itech_test", None)
    )
    first_itech_write = next(
        index
        for index, call in enumerate(client.calls)
        if call[0] == "PATCH" and call[1].startswith("/bidirectional-power-supplies/")
    )
    assert first_itech_full_state < reservation_index < first_itech_write < release_index
    point_reads = [
        call
        for call in client.calls
        if call == ("GET", "/bidirectional-power-supplies/itech_test/measurements", None)
    ]
    assert len(point_reads) == 6
    full_state_reads = [
        index
        for index, call in enumerate(client.calls)
        if call == ("GET", "/bidirectional-power-supplies/itech_test", None)
    ]
    last_point_read_index = max(
        index
        for index, call in enumerate(client.calls)
        if call == ("GET", "/bidirectional-power-supplies/itech_test/measurements", None)
    )
    first_point_read_index = min(
        index
        for index, call in enumerate(client.calls)
        if call == ("GET", "/bidirectional-power-supplies/itech_test/measurements", None)
    )
    assert len(full_state_reads) == 2
    assert full_state_reads[0] < first_point_read_index
    assert full_state_reads[1] > last_point_read_index
    protection_setup = next(
        call[2]
        for call in client.calls
        if call[0] == "PATCH"
        and call[1].endswith("/protections")
        and call[2].get("ovp_level_v") == 13.0
    )
    assert protection_setup == {
        "ovp_enabled": True,
        "ovp_level_v": 13.0,
        "ocp_enabled": True,
        "ocp_level_a": 12.0,
        "opp_enabled": True,
        "opp_level_w": 150.0,
        "uvp_enabled": False,
    }
    current_updates = [
        call[2]["current_setpoint_a"]
        for call in client.calls
        if call[0] == "PATCH"
        and call[1].endswith("/operating-point")
        and call[2] is not None
        and "current_setpoint_a" in call[2]
    ]
    assert [0.0, -2.0, -4.0, -7.0, -9.0, -11.0] == current_updates[2:-1]
    assert client.state["output_enabled"] is False
    assert client.state["uvp_enabled"] is True
    assert client.scope_settings["scope_data"] is False
    assert client.scope_profile["measurements"] == [
        {"channel": "CH1", "item": "amplitude"}
    ]


def test_itech_discovery_retries_before_preflight(monkeypatch: Any) -> None:
    class RetryingClient:
        def __init__(self) -> None:
            self.attempts = 0

        def get(self, path: str) -> list[dict[str, Any]]:
            assert path == "/devices"
            return [
                {
                    "id": "scope_test",
                    "kind": "micsig_mho1",
                    "connected": True,
                }
            ]

        def post(
            self,
            path: str,
            payload: dict[str, Any] | None = None,
            *,
            timeout_s: float | None = None,
        ) -> dict[str, Any]:
            del payload, timeout_s
            assert path == "/devices/discover/itech_it6000c"
            self.attempts += 1
            if self.attempts < 3:
                raise script.OpenBenchApiError("COM10 is temporarily busy")
            return {"devices": ["itech_test"]}

    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    client = RetryingClient()

    script.discover_missing_bench_devices(client, None, "scope_test")

    assert client.attempts == 3


def test_sink_step_script_never_changes_physical_scope_settings(monkeypatch: Any) -> None:
    monkeypatch.setattr(script, "ApiClient", FakeApiClient)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    args = script.build_parser().parse_args(
        ["--currents", "0", "--execute", "--wiring-confirmed"]
    )

    assert script.run(args) == 0
    client = FakeApiClient.instance
    assert client is not None
    scope_capture = next(
        call[2]
        for call in client.calls
        if call[0] == "PATCH"
        and call[1] == "/devices/scope_test/settings"
        and call[2].get("scope_screen") is True
    )
    assert scope_capture["scope_data"] is True
    assert scope_capture["scope_channels"] == ["CH1", "CH2", "CH3", "CH4"]
    assert not any(
        call[1] == "/oscilloscopes/scope_test/settings" for call in client.calls
    )


def test_full_range_limits_and_output_off_precede_recording_stop(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(script, "ApiClient", FakeApiClient)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    args = script.build_parser().parse_args(
        [
            "--currents",
            "0,75,150,225",
            "--voltage-limit-v",
            "13",
            "--current-limit-a",
            "225",
            "--power-limit-w",
            "3000",
            "--ovp-v",
            "13",
            "--ocp-a",
            "225",
            "--opp-w",
            "3000",
            "--title",
            "Full-range load test",
            "--comment",
            "Documented high-current wiring and cooling",
            "--measurement-profile",
            "test",
            "--require-ut61eplus",
            "--require-comment",
            "--execute",
            "--wiring-confirmed",
        ]
    )

    assert script.run(args) == 0
    client = FakeApiClient.instance
    assert client is not None
    protection_setup = next(
        call[2]
        for call in client.calls
        if call[0] == "PATCH"
        and call[1].endswith("/protections")
        and call[2].get("ocp_level_a") == 225.0
    )
    assert protection_setup["ovp_level_v"] == 13.0
    assert protection_setup["opp_level_w"] == 3000.0
    operating_setup = next(
        call[2]
        for call in client.calls
        if call[0] == "PATCH"
        and call[1].endswith("/operating-point")
        and call[2].get("current_limit_positive_a") == 225.0
    )
    assert operating_setup["current_limit_negative_a"] == -225.0
    assert operating_setup["power_limit_positive_w"] == 3000.0
    assert operating_setup["power_limit_negative_w"] == -3000.0
    current_updates = [
        call[2]["current_setpoint_a"]
        for call in client.calls
        if call[0] == "PATCH"
        and call[1].endswith("/operating-point")
        and call[2] is not None
        and "current_setpoint_a" in call[2]
    ]
    assert [
        0.0,
        -75.0,
        -150.0,
        -225.0,
    ] == current_updates[2:-1]

    last_frame_index = max(
        index
        for index, call in enumerate(client.calls)
        if call[0] == "POST" and "/recording/scopes/" in call[1]
    )
    normal_output_off_index = next(
        index
        for index, call in enumerate(
            client.calls[last_frame_index + 1 :], last_frame_index + 1
        )
        if call
        == (
            "PATCH",
            "/bidirectional-power-supplies/itech_test/operating-point",
            {"output_enabled": False},
        )
    )
    output_off_readback_index = next(
        index
        for index, call in enumerate(
            client.calls[normal_output_off_index + 1 :], normal_output_off_index + 1
        )
        if call == ("GET", "/bidirectional-power-supplies/itech_test", None)
    )
    recording_stop_index = next(
        index
        for index, call in enumerate(client.calls)
        if call == ("POST", "/captures/recording/stop", {})
    )
    assert (
        last_frame_index
        < normal_output_off_index
        < output_off_readback_index
        < recording_stop_index
    )


def test_missing_itech_measurement_forces_off_before_scope_finishes(
    monkeypatch: Any,
) -> None:
    class MissingMeasurementClient(FakeApiClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.frame_started = Event()
            self.allow_frame_finish = Event()

        def get(self, path: str, *, timeout_s: float | None = None) -> Any:
            if path == "/bidirectional-power-supplies/itech_test/measurements":
                del timeout_s
                self.calls.append(("GET", path, None))
                assert self.frame_started.wait(timeout=1.0)
                return {
                    "measured_voltage_v": 12.0,
                    "measured_current_a": None,
                    "measured_power_w": None,
                }
            return super().get(path, timeout_s=timeout_s)

        def patch(
            self,
            path: str,
            payload: dict[str, Any],
            *,
            timeout_s: float | None = None,
        ) -> Any:
            response = super().patch(path, payload, timeout_s=timeout_s)
            if path.endswith("/operating-point") and payload == {"output_enabled": False}:
                self.allow_frame_finish.set()
            return response

        def post(
            self,
            path: str,
            payload: dict[str, Any] | None = None,
            *,
            timeout_s: float | None = None,
        ) -> Any:
            if path == "/captures/recording/scopes/scope_test/frame":
                del timeout_s
                body = payload or {}
                self.calls.append(("POST", path, deepcopy(body)))
                self.frame_started.set()
                assert self.allow_frame_finish.wait(timeout=1.0)
                self.calls.append(("EVENT", "scope_frame_completed", None))
                self.frame_count += 1
                return {
                    "capture_id": str(self.frame_count),
                    "status": "ok",
                    "screen_file": "test_rec/mho1_frame_000001/mho1_screen.png",
                    "data_file": "test_rec/mho1_frame_000001/mho1_capture.json",
                    "error": "",
                }
            return super().post(path, payload, timeout_s=timeout_s)

    monkeypatch.setattr(script, "ApiClient", MissingMeasurementClient)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    args = script.build_parser().parse_args(
        ["--currents", "0", "--execute", "--wiring-confirmed"]
    )

    with pytest.raises(
        script.OpenBenchApiError,
        match=r"missing fields at 0 A.*Output OFF was verified",
    ):
        script.run(args)

    client = MissingMeasurementClient.instance
    assert client is not None
    missing_read_index = next(
        index
        for index, call in enumerate(client.calls)
        if call == ("GET", "/bidirectional-power-supplies/itech_test/measurements", None)
    )
    emergency_off_index = next(
        index
        for index, call in enumerate(
            client.calls[missing_read_index + 1 :], missing_read_index + 1
        )
        if call
        == (
            "PATCH",
            "/bidirectional-power-supplies/itech_test/operating-point",
            {"output_enabled": False},
        )
    )
    frame_completed_index = client.calls.index(("EVENT", "scope_frame_completed", None))
    recording_stop_index = client.calls.index(("POST", "/captures/recording/stop", {}))
    assert missing_read_index < emergency_off_index < frame_completed_index
    assert emergency_off_index < recording_stop_index
    assert client.state["output_enabled"] is False
    assert not any(
        call[1] == "/devices/discover/itech_it6000c" for call in client.calls
    )


def test_emergency_off_rediscovers_same_itech_after_connection_failure(
    monkeypatch: Any,
) -> None:
    class RecoveringClient(FakeApiClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.off_attempts = 0
            self.state["output_enabled"] = True

        def patch(
            self,
            path: str,
            payload: dict[str, Any],
            *,
            timeout_s: float | None = None,
        ) -> Any:
            if path.endswith("/operating-point") and payload == {"output_enabled": False}:
                self.off_attempts += 1
                if self.off_attempts == 1:
                    del timeout_s
                    self.calls.append(("PATCH", path, deepcopy(payload)))
                    raise script.OpenBenchApiError("COM10 disconnected")
            return super().patch(path, payload, timeout_s=timeout_s)

    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    client = RecoveringClient()

    script.force_itech_output_off(client, "itech_test", reason="unit test")

    assert client.off_attempts == 2
    assert client.state["output_enabled"] is False
    assert any(call == ("POST", "/devices/discover/itech_it6000c", {}) for call in client.calls)
    assert any(call == ("GET", "/devices", None) for call in client.calls)
    assert client.calls[-1] == (
        "GET",
        "/bidirectional-power-supplies/itech_test",
        None,
    )


def test_missing_power_is_calculated_when_voltage_and_current_arrive(
    monkeypatch: Any,
) -> None:
    class MissingPowerClient(FakeApiClient):
        def get(self, path: str, *, timeout_s: float | None = None) -> Any:
            if path == "/bidirectional-power-supplies/itech_test/measurements":
                del timeout_s
                self.calls.append(("GET", path, None))
                return {
                    "measured_voltage_v": 12.0,
                    "measured_current_a": -2.0,
                    "measured_power_w": None,
                }
            return super().get(path, timeout_s=timeout_s)

    monkeypatch.setattr(script, "ApiClient", MissingPowerClient)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    args = script.build_parser().parse_args(
        ["--currents", "0", "--execute", "--wiring-confirmed"]
    )

    assert script.run(args) == 0

    client = MissingPowerClient.instance
    assert client is not None
    assert client.state["output_enabled"] is False
    assert not any(
        call[1] == "/devices/discover/itech_it6000c" for call in client.calls
    )


def test_emergency_off_retries_and_raises_critical_if_output_stays_on(
    monkeypatch: Any,
) -> None:
    class StuckOutputClient(FakeApiClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.off_attempts = 0
            self.state["output_enabled"] = True

        def patch(
            self,
            path: str,
            payload: dict[str, Any],
            *,
            timeout_s: float | None = None,
        ) -> Any:
            if path.endswith("/operating-point") and payload == {
                "output_enabled": False
            }:
                del timeout_s
                self.off_attempts += 1
                self.calls.append(("PATCH", path, deepcopy(payload)))
                return {"state": deepcopy(self.state)}
            return super().patch(path, payload, timeout_s=timeout_s)

    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    client = StuckOutputClient()

    with pytest.raises(
        script.OpenBenchApiError,
        match=r"CRITICAL: ITECH Output OFF could not be verified.*FRONT PANEL",
    ):
        script.force_itech_output_off(client, "itech_test", reason="unit test")

    assert client.off_attempts == script.EMERGENCY_OFF_ATTEMPTS
    assert client.state["output_enabled"] is True
    assert not any(
        call[1] == "/devices/discover/itech_it6000c" for call in client.calls
    )


def test_sink_step_script_rejects_non_yt_without_changing_it(monkeypatch: Any) -> None:
    class NonYTFakeApiClient(FakeApiClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.scope_state["timebase_mode"] = "XY"

    monkeypatch.setattr(script, "ApiClient", NonYTFakeApiClient)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    args = script.build_parser().parse_args(
        ["--currents", "0", "--execute", "--wiring-confirmed"]
    )

    with pytest.raises(script.OpenBenchApiError, match="normal YT mode"):
        script.run(args)
    client = NonYTFakeApiClient.instance
    assert client is not None
    assert not any(
        call[1] == "/oscilloscopes/scope_test/settings" for call in client.calls
    )


def test_sink_step_script_accepts_operator_confirmed_yt_without_changing_it(
    monkeypatch: Any,
) -> None:
    class NonYTFakeApiClient(FakeApiClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.scope_state["timebase_mode"] = "XY"

    monkeypatch.setattr(script, "ApiClient", NonYTFakeApiClient)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "  run  ")
    args = script.build_parser().parse_args(
        [
            "--currents",
            "0",
            "--execute",
            "--wiring-confirmed",
            "--scope-yt-confirmed",
            "--operator-confirmation-phrase",
            "RUN",
        ]
    )

    assert script.run(args) == 0
    client = NonYTFakeApiClient.instance
    assert client is not None
    assert not any(
        call[1] == "/oscilloscopes/scope_test/settings" for call in client.calls
    )


def test_operator_confirmation_cancels_before_any_instrument_write(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(script, "ApiClient", FakeApiClient)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "not the phrase")
    args = script.build_parser().parse_args(
        [
            "--currents",
            "0",
            "--execute",
            "--wiring-confirmed",
            "--operator-confirmation-phrase",
            "RUN",
        ]
    )

    assert script.run(args) == script.EXIT_OPERATOR_CANCELLED
    client = FakeApiClient.instance
    assert client is not None
    assert not any(call[0] in {"PATCH", "PUT", "DELETE"} for call in client.calls)
    assert not any("experiment-reservation" in call[1] for call in client.calls)
    assert not any(call[1] == "/captures/recording/start" for call in client.calls)
