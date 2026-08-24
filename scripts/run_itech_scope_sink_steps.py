#!/usr/bin/env python3
"""Run a bounded ITECH sink-step experiment with exact Micsig screenshots.

The script uses only the supported OpenBench REST API. It is read-only unless
both --execute and --wiring-confirmed are present. Every exit path attempts to
turn the ITECH output OFF before restoring the saved, de-energized settings.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1"
DEFAULT_CURRENT_STEPS_A = (0.0, 2.0, 4.0, 7.0, 9.0, 11.0)
ITECH_DISCOVERY_ATTEMPTS = 4
OTHER_DISCOVERY_ATTEMPTS = 2
DISCOVERY_RETRY_DELAY_S = 0.75
EMERGENCY_OFF_ATTEMPTS = 4
EMERGENCY_OFF_REQUEST_TIMEOUT_S = 3.0
EMERGENCY_DISCOVERY_TIMEOUT_S = 5.0
EMERGENCY_RETRY_DELAY_S = 0.25
MAX_CURRENT_MAGNITUDE_A = 225.0
EXIT_OPERATOR_CANCELLED = 3
TEST_MEASUREMENT_PROFILE = (
    {"channel": "CH1", "item": "maximum"},
    {"channel": "CH1", "item": "high"},
    {"channel": "CH2", "item": "maximum"},
    {"channel": "CH2", "item": "high"},
    {"channel": "CH3", "item": "frequency"},
    {"channel": "CH3", "item": "amplitude"},
    {"channel": "CH4", "item": "frequency"},
    {"channel": "CH4", "item": "amplitude"},
)
MEASUREMENT_PROFILES = {
    "test": TEST_MEASUREMENT_PROFILE,
}
PROTECTION_FIELDS = (
    "ovp_enabled",
    "ovp_level_v",
    "ocp_enabled",
    "ocp_level_a",
    "opp_enabled",
    "opp_level_w",
    "uvp_enabled",
)
OPERATING_FIELDS = (
    "priority",
    "voltage_setpoint_v",
    "current_setpoint_a",
    "current_limit_positive_a",
    "current_limit_negative_a",
    "voltage_limit_positive_v",
    "voltage_limit_negative_v",
    "power_limit_positive_w",
    "power_limit_negative_w",
)
OPERATING_RESOLUTIONS = {
    "voltage_setpoint_v": 0.01,
    "current_setpoint_a": 0.01,
    "current_limit_positive_a": 0.01,
    "current_limit_negative_a": 0.01,
    "voltage_limit_positive_v": 0.01,
    "voltage_limit_negative_v": 0.01,
    "power_limit_positive_w": 1.0,
    "power_limit_negative_w": 1.0,
}


class OpenBenchApiError(RuntimeError):
    pass


class ApiClient:
    def __init__(self, base_url: str, timeout_s: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout_s or self.timeout_s) as response:
                body = response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("detail", body)
            except json.JSONDecodeError:
                detail = body
            raise OpenBenchApiError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, URLError) as exc:
            raise OpenBenchApiError(f"{method} {path}: {exc}") from exc
        if not body:
            return None
        return json.loads(body)

    def get(self, path: str, *, timeout_s: float | None = None) -> Any:
        return self.request("GET", path, timeout_s=timeout_s)

    def post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        return self.request("POST", path, payload or {}, timeout_s=timeout_s)

    def patch(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> Any:
        return self.request("PATCH", path, payload, timeout_s=timeout_s)

    def delete(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        return self.request("DELETE", path, payload, timeout_s=timeout_s)

    def put(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("PUT", path, payload)


@dataclass(frozen=True)
class Bench:
    itech_id: str
    scope_id: str
    ut61eplus_id: str | None = None


def _device_path(device_id: str) -> str:
    return quote(device_id, safe="")


def _single(items: list[dict[str, Any]], description: str) -> dict[str, Any]:
    if len(items) != 1:
        raise OpenBenchApiError(f"Expected exactly one connected {description}, found {len(items)}")
    return items[0]


def find_bench(
    client: ApiClient,
    itech_id: str | None,
    scope_id: str | None,
    *,
    require_ut61eplus: bool = False,
) -> Bench:
    devices = client.get("/devices")
    connected = [item for item in devices if item.get("connected")]
    itech = _single(
        [
            item
            for item in connected
            if item.get("kind") == "itech_it6000c"
            and (itech_id is None or item.get("id") == itech_id)
        ],
        "ITECH IT6000C",
    )
    scopes = [
        item
        for item in connected
        if item.get("kind") in {"micsig_mho1", "micsig_eto"}
        and "screenshot_capture" in item.get("capabilities", ())
        and (scope_id is None or item.get("id") == scope_id)
    ]
    scope = _single(scopes, "supported Micsig oscilloscope with screenshot support")
    ut61eplus_id: str | None = None
    if require_ut61eplus:
        meter = _single(
            [item for item in connected if item.get("kind") == "ut61eplus"],
            "UT61E+ output-voltage meter",
        )
        ut61eplus_id = meter["id"]
    return Bench(
        itech_id=itech["id"],
        scope_id=scope["id"],
        ut61eplus_id=ut61eplus_id,
    )


def discover_missing_bench_devices(
    client: ApiClient,
    itech_id: str | None,
    scope_id: str | None,
    *,
    require_ut61eplus: bool = False,
) -> None:
    def discover(driver_id: str, attempts: int) -> None:
        last_error: OpenBenchApiError | None = None
        for attempt in range(1, attempts + 1):
            try:
                client.post(f"/devices/discover/{driver_id}", timeout_s=20.0)
                return
            except OpenBenchApiError as exc:
                last_error = exc
                if attempt == attempts:
                    break
                print(
                    f"Discovery {driver_id} attempt {attempt}/{attempts} failed; "
                    "releasing/rebinding and retrying..."
                )
                time.sleep(DISCOVERY_RETRY_DELAY_S)
        assert last_error is not None
        raise last_error

    devices = client.get("/devices")
    connected = [item for item in devices if item.get("connected")]
    has_itech = any(
        item.get("kind") == "itech_it6000c"
        and (itech_id is None or item.get("id") == itech_id)
        for item in connected
    )
    has_scope = any(
        item.get("kind") in {"micsig_mho1", "micsig_eto"}
        and (scope_id is None or item.get("id") == scope_id)
        for item in connected
    )
    if not has_itech:
        discover("itech_it6000c", ITECH_DISCOVERY_ATTEMPTS)
    if not has_scope:
        discover("micsig", OTHER_DISCOVERY_ATTEMPTS)
    if require_ut61eplus and not any(
        item.get("kind") == "ut61eplus" for item in connected
    ):
        discover("ut61eplus", OTHER_DISCOVERY_ATTEMPTS)


def force_itech_output_off(
    client: ApiClient,
    itech_id: str,
    *,
    reason: str,
) -> None:
    """Force Output OFF and prove it by a fresh full-state read-back.

    A failed command or read-back triggers bounded rediscovery of only the
    ITECH driver. The stable serial-number device ID must reconnect before the
    next OFF attempt. This never enables Output or changes another setting.
    """

    itech_path = f"/bidirectional-power-supplies/{_device_path(itech_id)}"
    failures: list[str] = []
    for attempt in range(1, EMERGENCY_OFF_ATTEMPTS + 1):
        connection_failed = False
        try:
            client.patch(
                f"{itech_path}/operating-point",
                {"output_enabled": False},
                timeout_s=EMERGENCY_OFF_REQUEST_TIMEOUT_S,
            )
            readback = client.get(
                itech_path,
                timeout_s=EMERGENCY_OFF_REQUEST_TIMEOUT_S,
            )
            if readback.get("state", {}).get("output_enabled") is False:
                print(
                    f"ITECH Output OFF verified ({reason}); "
                    f"attempt {attempt}/{EMERGENCY_OFF_ATTEMPTS}."
                )
                return
            failures.append(f"OFF attempt {attempt}: read-back still reports Output ON")
        except Exception as exc:
            connection_failed = True
            failures.append(f"OFF attempt {attempt}: {exc}")

        if attempt == EMERGENCY_OFF_ATTEMPTS:
            break
        if connection_failed:
            try:
                client.post(
                    "/devices/discover/itech_it6000c",
                    timeout_s=EMERGENCY_DISCOVERY_TIMEOUT_S,
                )
                devices = client.get(
                    "/devices",
                    timeout_s=EMERGENCY_OFF_REQUEST_TIMEOUT_S,
                )
                if not any(
                    item.get("id") == itech_id
                    and item.get("kind") == "itech_it6000c"
                    and item.get("connected")
                    for item in devices
                ):
                    raise OpenBenchApiError(f"stable ITECH device {itech_id} did not reconnect")
                print(f"ITECH reconnected for emergency OFF ({reason}).")
            except Exception as exc:
                failures.append(f"rediscovery after attempt {attempt}: {exc}")
        time.sleep(EMERGENCY_RETRY_DELAY_S)

    detail = "; ".join(failures)
    message = (
        f"CRITICAL: ITECH Output OFF could not be verified after "
        f"{EMERGENCY_OFF_ATTEMPTS} attempts ({reason}). "
        f"USE THE FRONT PANEL OR MAINS DISCONNECT IMMEDIATELY. {detail}"
    )
    print(f"*** {message} ***", file=sys.stderr)
    raise OpenBenchApiError(message)


def parse_currents(value: str) -> tuple[float, ...]:
    try:
        currents = tuple(float(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Currents must be comma-separated numbers") from exc
    if not currents or currents[0] != 0:
        raise argparse.ArgumentTypeError("The first current point must be 0 A")
    if any(value < 0 or value > MAX_CURRENT_MAGNITUDE_A for value in currents):
        raise argparse.ArgumentTypeError(
            f"Current magnitudes must be between 0 and {MAX_CURRENT_MAGNITUDE_A:g} A"
        )
    return currents


def preflight(
    client: ApiClient,
    bench: Bench,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    health = client.get("/health")
    if health.get("status") != "ok" or health.get("safety_state") != "safe":
        raise OpenBenchApiError(f"OpenBench is not ready and safe: {health}")
    capture = client.get("/captures/status")
    if capture.get("active"):
        raise OpenBenchApiError(
            f"A common CSV recording is already active: {capture['current_file']}"
        )
    itech = client.get(f"/bidirectional-power-supplies/{_device_path(bench.itech_id)}")
    state = itech["state"]
    if state.get("output_enabled"):
        raise OpenBenchApiError("ITECH output is already ON; turn it OFF before this experiment")
    if state.get("faults"):
        raise OpenBenchApiError(f"ITECH reports active faults: {state['faults']}")
    scope_settings = client.get(f"/devices/{_device_path(bench.scope_id)}/settings")
    scope_profile = client.get(f"/oscilloscopes/{_device_path(bench.scope_id)}/measurements")
    scope_state = client.get(f"/oscilloscopes/{_device_path(bench.scope_id)}")
    return itech, scope_settings, scope_profile, scope_state


def test_plan(
    args: argparse.Namespace,
    bench: Bench,
    itech: dict[str, Any],
    scope_state: dict[str, Any],
) -> dict[str, Any]:
    state = itech["state"]
    measurement_profile = MEASUREMENT_PROFILES[args.measurement_profile]
    return {
        "itech_id": bench.itech_id,
        "scope_id": bench.scope_id,
        "ut61eplus_output_voltage_id": bench.ut61eplus_id,
        "source_voltage_now_v": state["measured_voltage_v"],
        "current_setpoints_a": [-value for value in args.currents],
        "control_mode": (
            f"CC sink with matched {args.voltage_limit_v:g} V "
            "setpoint/positive voltage limit"
        ),
        "settle_s": args.settle_s,
        "point_sequence": "set current, settle, parallel ITECH U/I and scope frame, repeat",
        "itech_readback": "one V/I sample and calculated P per point, parallel with scope capture",
        "itech_readback_timeout_s": args.itech_measurement_timeout_s,
        "itech_experiment_reservation": (
            "Dashboard polling suspended; only explicit per-point U/I samples run"
        ),
        "shutdown_policy": (
            "missing ITECH U/I or any run error -> Output OFF + full-state read-back; "
            "on connection loss rediscover the same stable ID and retry; repeat after final point"
        ),
        "scope_frames": len(args.currents),
        "scope_screen": True,
        "scope_data": True,
        "scope_channels": ["CH1", "CH2", "CH3", "CH4"],
        "scope_timebase_mode": scope_state.get("timebase_mode"),
        "scope_sample_rate_sps": scope_state.get("sample_rate_sps"),
        "scope_memory_depth_setting": scope_state.get("memory_depth_setting"),
        "scope_memory_depth_points": scope_state.get("memory_depth_points"),
        "scope_acquisition_settings_changed": False,
        "measurements": list(measurement_profile),
        "operating_limits": {
            "voltage_v": args.voltage_limit_v,
            "current_positive_a": args.current_limit_a,
            "current_negative_a": -args.current_limit_a,
            "power_positive_w": args.power_limit_w,
            "power_negative_w": -args.power_limit_w,
        },
        "protections": {
            "OVP": f"enabled, {args.ovp_v:g} V",
            "OCP": f"enabled, {args.ocp_a:g} A",
            "OPP": f"enabled, {args.opp_w:g} W",
            "UVP": "disabled for expected collapse toward 0 V",
        },
        "title": args.title,
        "comment": args.comment,
    }


def validate_experiment_limits(args: argparse.Namespace, itech: dict[str, Any]) -> None:
    profile = itech["profile"]
    maximum_current_a = max(args.currents)
    if maximum_current_a > args.current_limit_a:
        raise OpenBenchApiError(
            f"Maximum point {maximum_current_a:g} A exceeds the configured "
            f"current limit {args.current_limit_a:g} A"
        )
    if args.current_limit_a > args.ocp_a:
        raise OpenBenchApiError("ITECH OCP must not be below the configured current limit")
    required_power_w = args.voltage_limit_v * maximum_current_a
    if required_power_w > args.power_limit_w:
        raise OpenBenchApiError(
            f"Configured power limit {args.power_limit_w:g} W is below the "
            f"V*I envelope {required_power_w:g} W"
        )
    if args.power_limit_w > args.opp_w:
        raise OpenBenchApiError("ITECH OPP must not be below the configured power limit")
    if args.current_limit_a > profile["rated_current_a"]:
        raise OpenBenchApiError("Configured current limit exceeds the ITECH model rating")
    if args.voltage_limit_v > profile["rated_voltage_v"]:
        raise OpenBenchApiError("Configured voltage limit exceeds the ITECH model rating")
    if args.power_limit_w > profile["rated_power_w"]:
        raise OpenBenchApiError("Configured power limit exceeds the ITECH model rating")
    measured_voltage_v = abs(float(itech["state"]["measured_voltage_v"]))
    if measured_voltage_v > args.ovp_v:
        raise OpenBenchApiError(
            f"Present source voltage {measured_voltage_v:g} V exceeds OVP {args.ovp_v:g} V"
        )


def _restore_payload(state: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: state[field] for field in fields}


def _restore_operating_payload(state: dict[str, Any]) -> dict[str, Any]:
    payload = _restore_payload(state, OPERATING_FIELDS)
    for field, resolution in OPERATING_RESOLUTIONS.items():
        value = payload[field]
        rounded = round(value / resolution) * resolution
        payload[field] = 0.0 if rounded == 0 else rounded
    return payload


def _print_step(index: int, total: int, target_a: float, state: dict[str, Any]) -> None:
    print(
        f"[{index}/{total}] set {target_a:+.3f} A; "
        f"measured {state['measured_current_a']:+.4f} A, "
        f"{state['measured_voltage_v']:.4f} V, {state['measured_power_w']:+.3f} W"
    )


def run(args: argparse.Namespace) -> int:
    client = ApiClient(args.base_url, timeout_s=args.api_timeout_s)
    discover_missing_bench_devices(
        client,
        args.itech_id,
        args.scope_id,
        require_ut61eplus=args.require_ut61eplus,
    )
    bench = find_bench(
        client,
        args.itech_id,
        args.scope_id,
        require_ut61eplus=args.require_ut61eplus,
    )
    (
        itech,
        original_scope_settings,
        original_scope_profile,
        scope_state,
    ) = preflight(client, bench)
    validate_experiment_limits(args, itech)
    measurement_profile = MEASUREMENT_PROFILES[args.measurement_profile]
    scope_mode = scope_state.get("timebase_mode")
    if scope_mode != "YT":
        warning = (
            f"Oscilloscope mode readback is {scope_mode!r}, not 'YT'. "
            "The experiment will not change the mode."
        )
        if args.execute and not args.scope_yt_confirmed:
            raise OpenBenchApiError(
                warning + " Visually confirm normal YT mode and pass --scope-yt-confirmed."
            )
        print(
            "WARNING: " + warning + " Operator-confirmed YT is required for execution.",
            file=sys.stderr,
        )
    displayed_channels = {
        int(channel["channel"])
        for channel in scope_state.get("channels", ())
        if channel.get("displayed")
    }
    if displayed_channels != {1, 2, 3, 4}:
        raise OpenBenchApiError(
            "CH1-CH4 must already be displayed; the experiment will not change channels"
        )
    plan = test_plan(args, bench, itech, scope_state)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.execute:
        print("Dry run only. Add --execute --wiring-confirmed after checking the wiring.")
        return 0
    if args.operator_confirmation_phrase:
        try:
            entered_confirmation = input(
                "Type "
                f"{args.operator_confirmation_phrase} "
                "to confirm the wiring and start: "
            )
        except EOFError as exc:
            raise OpenBenchApiError(
                "Operator confirmation input is unavailable; no output was enabled"
            ) from exc
        normalized_entered = " ".join(entered_confirmation.split())
        normalized_expected = " ".join(args.operator_confirmation_phrase.split())
        if normalized_entered.casefold() != normalized_expected.casefold():
            print("Cancelled. No output was enabled.")
            return EXIT_OPERATOR_CANCELLED
    if not args.wiring_confirmed:
        raise OpenBenchApiError("--execute also requires --wiring-confirmed")

    itech_path = f"/bidirectional-power-supplies/{_device_path(bench.itech_id)}"
    reservation_path = f"{itech_path}/experiment-reservation"
    scope_path = f"/devices/{_device_path(bench.scope_id)}/settings"
    scope_measurements_path = f"/oscilloscopes/{_device_path(bench.scope_id)}/measurements"
    original_state = itech["state"]
    recording_active = False
    output_may_be_on = False
    frames: list[dict[str, Any]] = []
    point_timings: list[dict[str, Any]] = []
    run_error: BaseException | None = None
    cleanup_errors: list[str] = []
    reservation_active = False

    try:
        reservation = client.post(reservation_path)
        if not reservation.get("active"):
            raise OpenBenchApiError("ITECH experiment reservation was not activated")
        reservation_active = True
        print(
            "ITECH reserved for this experiment; "
            f"Dashboard polling targets suspended: "
            f"{reservation['polling_targets_suspended']}"
        )
        preparation_started = time.monotonic()
        client.patch(
            f"{itech_path}/protections",
            {
                "ovp_enabled": True,
                "ovp_level_v": args.ovp_v,
                "ocp_enabled": True,
                "ocp_level_a": args.ocp_a,
                "opp_enabled": True,
                "opp_level_w": args.opp_w,
                "uvp_enabled": False,
            },
        )
        client.patch(
            f"{itech_path}/operating-point",
            {
                "priority": "CC",
                "voltage_setpoint_v": args.voltage_limit_v,
                "current_setpoint_a": 0.0,
                "current_limit_positive_a": args.current_limit_a,
                "current_limit_negative_a": -args.current_limit_a,
                "voltage_limit_positive_v": args.voltage_limit_v,
                "voltage_limit_negative_v": 0.0,
                "power_limit_positive_w": args.power_limit_w,
                "power_limit_negative_w": -args.power_limit_w,
                "output_enabled": False,
            },
        )
        client.patch(
            scope_path,
            {
                "scope_screen": True,
                "scope_data": True,
                "scope_channels": ["CH1", "CH2", "CH3", "CH4"],
                "scope_wait_for_trigger": False,
            },
        )
        client.put(scope_measurements_path, {"measurements": list(measurement_profile)})
        preparation_elapsed_s = time.monotonic() - preparation_started
        print(f"Preparation elapsed: {preparation_elapsed_s:.3f} s")

        output_enable_started = time.monotonic()
        # The command can reach the instrument even if its HTTP response is
        # lost. Mark Output as potentially live before sending it so every
        # failure path runs the verified emergency shutdown sequence.
        output_may_be_on = True
        client.patch(
            f"{itech_path}/operating-point",
            {
                "current_setpoint_a": 0.0,
                "output_enabled": True,
                "wiring_confirmed": True,
            },
        )
        output_enable_elapsed_s = time.monotonic() - output_enable_started
        print(f"ITECH Output ON response: {output_enable_elapsed_s:.3f} s")
        time.sleep(args.initial_settle_s)

        recording_start_started = time.monotonic()
        started = client.post(
            "/captures/recording/start",
            {
                "title": args.title,
                "comment": args.comment,
                "duration_s": args.maximum_duration_s,
                "scope_capture_mode": "manual",
            },
        )
        recording_active = True
        print(f"Recording: {started['current_file']}")
        print(
            f"Recording start response: "
            f"{time.monotonic() - recording_start_started:.3f} s"
        )

        previous_point_started: float | None = None
        previous_point_completed: float | None = None
        series_started = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as executor:
            for index, magnitude_a in enumerate(args.currents, 1):
                point_started = time.monotonic()
                actual_interval_s = (
                    None
                    if previous_point_started is None
                    else point_started - previous_point_started
                )
                previous_point_started = point_started
                after_previous_s = (
                    None
                    if previous_point_completed is None
                    else point_started - previous_point_completed
                )
                target_a = -magnitude_a
                command_started = time.monotonic()
                client.patch(
                    f"{itech_path}/operating-point",
                    {
                        "current_setpoint_a": target_a,
                        "wiring_confirmed": True,
                    },
                    timeout_s=args.itech_measurement_timeout_s,
                )
                command_done = time.monotonic()
                command_elapsed_s = command_done - command_started
                time.sleep(args.settle_s)
                capture_started = time.monotonic()
                settle_elapsed_s = capture_started - command_done
                point_label = f"sink_set_{magnitude_a:g}A"

                def read_settled_measurements() -> tuple[dict[str, Any], float]:
                    request_started = time.monotonic()
                    result = client.get(
                        f"{itech_path}/measurements",
                        timeout_s=args.itech_measurement_timeout_s,
                    )
                    return result, time.monotonic() - request_started

                def capture_settled_scope_frame(label: str) -> tuple[dict[str, Any], float]:
                    request_started = time.monotonic()
                    result = client.post(
                        f"/captures/recording/scopes/{_device_path(bench.scope_id)}/frame",
                        {"label": label},
                        timeout_s=args.scope_timeout_s,
                    )
                    return result, time.monotonic() - request_started

                measurement_future = executor.submit(read_settled_measurements)
                frame_future = executor.submit(capture_settled_scope_frame, point_label)
                try:
                    state, itech_measurement_elapsed_s = measurement_future.result()
                except BaseException as measurement_error:
                    frame_future.cancel()
                    try:
                        force_itech_output_off(
                            client,
                            bench.itech_id,
                            reason=f"ITECH read-back error at {magnitude_a:g} A",
                        )
                        output_may_be_on = False
                    except Exception as shutdown_error:
                        raise OpenBenchApiError(
                            f"ITECH read-back failed at {magnitude_a:g} A and "
                            f"emergency Output OFF was not verified: {shutdown_error}"
                        ) from measurement_error
                    raise OpenBenchApiError(
                        f"ITECH read-back failed at {magnitude_a:g} A; Output OFF was verified"
                    ) from measurement_error
                missing_measurements = [
                    field
                    for field in (
                        "measured_voltage_v",
                        "measured_current_a",
                    )
                    if state.get(field) is None
                ]
                if missing_measurements:
                    frame_future.cancel()
                    try:
                        force_itech_output_off(
                            client,
                            bench.itech_id,
                            reason=f"missing ITECH U/I at {magnitude_a:g} A",
                        )
                        output_may_be_on = False
                    except Exception as shutdown_error:
                        raise OpenBenchApiError(
                            f"ITECH returned missing fields at {magnitude_a:g} A "
                            f"({', '.join(missing_measurements)}) and emergency "
                            f"Output OFF was not verified: {shutdown_error}"
                        ) from shutdown_error
                    raise OpenBenchApiError(
                        f"ITECH returned missing fields at {magnitude_a:g} A "
                        f"({', '.join(missing_measurements)}); Output OFF was verified"
                    )
                if state.get("measured_power_w") is None:
                    state["measured_power_w"] = (
                        state["measured_voltage_v"] * state["measured_current_a"]
                    )
                frame, scope_frame_elapsed_s = frame_future.result()
                _print_step(index, len(args.currents), target_a, state)
                if frame["status"] != "ok" or not frame["screen_file"]:
                    raise OpenBenchApiError(f"Scope frame {index} failed: {frame}")
                frames.append(frame)
                previous_point_completed = time.monotonic()
                point_elapsed_s = previous_point_completed - point_started
                timing = {
                    "point": index,
                    "target_current_a": target_a,
                    "start_offset_s": point_started - series_started,
                    "actual_interval_s": actual_interval_s,
                    "after_previous_completion_s": after_previous_s,
                    "itech_command_s": command_elapsed_s,
                    "settle_elapsed_s": settle_elapsed_s,
                    "itech_measurements_s": itech_measurement_elapsed_s,
                    "scope_frame_s": scope_frame_elapsed_s,
                    "point_elapsed_s": point_elapsed_s,
                }
                point_timings.append(timing)
                print(
                    f"  screenshot: {frame['screen_file']}; "
                    f"command {command_elapsed_s:.3f} s; "
                    f"settle {settle_elapsed_s:.3f} s; "
                    f"ITECH read {itech_measurement_elapsed_s:.3f} s; "
                    f"scope frame {scope_frame_elapsed_s:.3f} s; "
                    f"point {point_elapsed_s:.3f} s; "
                    f"next-step gap "
                    f"{'first' if after_previous_s is None else f'{after_previous_s:.3f} s'}"
                )

        force_itech_output_off(
            client,
            bench.itech_id,
            reason=f"completed final point {args.currents[-1]:g} A",
        )
        output_may_be_on = False
        stopped = client.post("/captures/recording/stop")
        recording_active = False
        print(f"Completed: {stopped['last_recording_file']}; exact scope frames: {len(frames)}")
        result_folder = PurePosixPath(frames[0]["screen_file"]).parts[0]
        print(f"RESULT_FOLDER={result_folder}")
        print("TIMING_JSON=" + json.dumps(point_timings, ensure_ascii=False))
        if args.open_result_folder:
            captures_root = (
                Path(__file__).resolve().parents[1]
                / ".openbench"
                / "data"
                / "captures"
                / "sessions"
            )
            resolved_root = captures_root.resolve()
            resolved_result = (resolved_root / result_folder).resolve()
            if resolved_result.parent != resolved_root or not resolved_result.is_dir():
                raise OpenBenchApiError(f"Result folder is unavailable: {resolved_result}")
            subprocess.Popen(["explorer.exe", str(resolved_result)])
    except BaseException as exc:
        run_error = exc
    finally:
        if output_may_be_on:
            try:
                force_itech_output_off(
                    client,
                    bench.itech_id,
                    reason="experiment error cleanup",
                )
                output_may_be_on = False
            except Exception as exc:
                cleanup_errors.append(f"ITECH emergency Output OFF failed: {exc}")
        if recording_active:
            try:
                client.post("/captures/recording/stop")
                recording_active = False
            except Exception as exc:
                cleanup_errors.append(f"CSV recording stop failed: {exc}")
        if output_may_be_on:
            cleanup_errors.append(
                "ITECH settings restore skipped because Output OFF is not verified"
            )
        else:
            try:
                client.patch(
                    f"{itech_path}/protections",
                    _restore_payload(original_state, PROTECTION_FIELDS),
                )
            except Exception as exc:
                cleanup_errors.append(f"ITECH protection restore failed: {exc}")
            try:
                operating = _restore_operating_payload(original_state)
                operating["output_enabled"] = False
                client.patch(f"{itech_path}/operating-point", operating)
            except Exception as exc:
                cleanup_errors.append(f"ITECH operating-point restore failed: {exc}")
        try:
            client.put(
                scope_measurements_path,
                {"measurements": original_scope_profile.get("measurements", [])},
            )
            client.patch(
                scope_path,
                {
                    "poll_interval_s": original_scope_settings["poll_interval_s"],
                    "scope_screen": original_scope_settings["scope_screen"],
                    "scope_data": original_scope_settings["scope_data"],
                    "scope_channels": original_scope_settings["scope_channels"],
                    "scope_wait_for_trigger": original_scope_settings["scope_wait_for_trigger"],
                },
            )
        except Exception as exc:
            cleanup_errors.append(f"Scope settings restore failed: {exc}")
        if reservation_active:
            try:
                released = client.delete(reservation_path)
                reservation_active = False
                if released.get("active"):
                    cleanup_errors.append(
                        "ITECH experiment reservation release still reports active"
                    )
            except Exception as exc:
                cleanup_errors.append(f"ITECH experiment reservation release failed: {exc}")
    if cleanup_errors:
        print("Cleanup warnings:", file=sys.stderr)
        for item in cleanup_errors:
            print(f"- {item}", file=sys.stderr)
    if run_error is not None:
        raise run_error
    if cleanup_errors:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--itech-id")
    parser.add_argument("--scope-id")
    parser.add_argument("--currents", type=parse_currents, default=DEFAULT_CURRENT_STEPS_A)
    parser.add_argument("--settle-s", type=float, default=1.0)
    parser.add_argument("--initial-settle-s", type=float, default=0.5)
    parser.add_argument("--voltage-limit-v", type=float, default=13.0)
    parser.add_argument("--current-limit-a", type=float, default=12.0)
    parser.add_argument("--power-limit-w", type=float, default=150.0)
    parser.add_argument("--ovp-v", type=float, default=13.0)
    parser.add_argument("--ocp-a", type=float, default=12.0)
    parser.add_argument("--opp-w", type=float, default=150.0)
    parser.add_argument("--maximum-duration-s", type=float, default=120.0)
    parser.add_argument("--api-timeout-s", type=float, default=90.0)
    parser.add_argument("--itech-measurement-timeout-s", type=float, default=3.0)
    parser.add_argument("--scope-timeout-s", type=float, default=30.0)
    parser.add_argument(
        "--measurement-profile",
        choices=tuple(MEASUREMENT_PROFILES),
        default="test",
    )
    parser.add_argument("--title", default="последний тестовый прогон")
    parser.add_argument(
        "--comment",
        default=(
            "Канал 1 - напряжение на выходе, канал 2 - ток, "
            "канал 3 и канал 4 - тестовые каналы с меандром."  # noqa: RUF001
        ),
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--wiring-confirmed", action="store_true")
    parser.add_argument("--scope-yt-confirmed", action="store_true")
    parser.add_argument("--operator-confirmation-phrase")
    parser.add_argument("--require-comment", action="store_true")
    parser.add_argument("--require-ut61eplus", action="store_true")
    parser.add_argument("--open-result-folder", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.settle_s < 0:
        raise SystemExit("--settle-s cannot be negative")
    if args.initial_settle_s < 0:
        raise SystemExit("--initial-settle-s cannot be negative")
    if args.itech_measurement_timeout_s <= 0:
        raise SystemExit("--itech-measurement-timeout-s must be positive")
    for field in (
        "voltage_limit_v",
        "current_limit_a",
        "power_limit_w",
        "ovp_v",
        "ocp_a",
        "opp_w",
    ):
        if getattr(args, field) <= 0:
            raise SystemExit(f"--{field.replace('_', '-')} must be positive")
    if args.require_comment and not args.comment.strip():
        raise SystemExit("--comment is required for this experiment profile")
    try:
        return run(args)
    except (KeyboardInterrupt, OpenBenchApiError) as exc:
        print(f"Experiment aborted: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
