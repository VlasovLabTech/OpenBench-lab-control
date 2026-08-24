#!/usr/bin/env python3
"""Small dependency-free CLI for the local OpenBench REST API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1"


class OpenBenchError(RuntimeError):
    """A readable API or connection error."""


def request_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_s: float = 45,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        try:
            details = json.loads(raw.decode("utf-8")).get("detail", raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            details = raw.decode("utf-8", errors="replace")
        raise OpenBenchError(f"OpenBench API returned HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise OpenBenchError(f"OpenBench is unavailable at {base_url}: {exc.reason}") from exc
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def download_file(base_url: str, filename: str, output: Path) -> dict[str, Any]:
    safe_name = quote(filename, safe="")
    request = Request(
        f"{base_url.rstrip('/')}/captures/files/{safe_name}",
        headers={"Accept": "text/csv"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=45) as response:
            body = response.read()
    except HTTPError as exc:
        raise OpenBenchError(f"OpenBench API returned HTTP {exc.code}: capture not found") from exc
    except URLError as exc:
        raise OpenBenchError(f"OpenBench is unavailable at {base_url}: {exc.reason}") from exc
    output.write_bytes(body)
    return {"file": str(output.resolve()), "bytes": len(body)}


def download_logic_file(
    base_url: str,
    device_id: str,
    capture_id: str,
    filename: str,
    output: Path,
) -> dict[str, Any]:
    path = (
        f"/logic-analyzers/{quote(device_id, safe='')}/captures/"
        f"{quote(capture_id, safe='')}/files/{quote(filename, safe='')}"
    )
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/octet-stream"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=120) as response:
            body = response.read()
    except HTTPError as exc:
        raise OpenBenchError(
            f"OpenBench API returned HTTP {exc.code}: logic capture not found"
        ) from exc
    except URLError as exc:
        raise OpenBenchError(f"OpenBench is unavailable at {base_url}: {exc.reason}") from exc
    output.write_bytes(body)
    return {"file": str(output.resolve()), "bytes": len(body)}


def download_scope_waveform_file(
    base_url: str,
    device_id: str,
    filename: str,
    output: Path,
) -> dict[str, Any]:
    path = (
        f"/oscilloscopes/{quote(device_id, safe='')}/storage-waveforms/{quote(filename, safe='')}"
    )
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "text/csv"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=45) as response:
            body = response.read()
    except HTTPError as exc:
        raise OpenBenchError(
            f"OpenBench API returned HTTP {exc.code}: scope waveform not found"
        ) from exc
    except URLError as exc:
        raise OpenBenchError(f"OpenBench is unavailable at {base_url}: {exc.reason}") from exc
    output.write_bytes(body)
    return {"file": str(output.resolve()), "bytes": len(body)}


def download_scope_maximum_file(
    base_url: str,
    device_id: str,
    filename: str,
    output: Path,
) -> dict[str, Any]:
    path = (
        f"/oscilloscopes/{quote(device_id, safe='')}/maximum-capture/files/"
        f"{quote(filename, safe='')}"
    )
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/octet-stream"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=120) as response:
            body = response.read()
    except HTTPError as exc:
        raise OpenBenchError(
            f"OpenBench API returned HTTP {exc.code}: scope maximum capture file not found"
        ) from exc
    except URLError as exc:
        raise OpenBenchError(f"OpenBench is unavailable at {base_url}: {exc.reason}") from exc
    output.write_bytes(body)
    return {"file": str(output.resolve()), "bytes": len(body)}


def add_capture_metadata(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title", default="")
    parser.add_argument("--comment", default="")


def parse_power_step(value: str) -> dict[str, float]:
    try:
        voltage_v, current_a, dwell_s = (float(field.strip()) for field in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "power step must be VOLTAGE,CURRENT,DWELL_SECONDS"
        ) from exc
    return {
        "voltage_v": voltage_v,
        "current_a": current_a,
        "dwell_s": dwell_s,
    }


def parse_logic_channels(value: str) -> list[int]:
    try:
        channels = [int(field.strip().removeprefix("CH")) for field in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "logic channels must be comma-separated indices such as 0,1,7"
        ) from exc
    if not channels or any(channel < 0 or channel > 15 for channel in channels):
        raise argparse.ArgumentTypeError("logic channels must be between 0 and 15")
    return channels


def parse_logic_trigger(value: str) -> dict[str, Any]:
    try:
        channel_text, condition = (field.strip() for field in value.split("=", 1))
        channel = int(channel_text.upper().removeprefix("CH"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "logic trigger must be CH=CONDITION, for example CH0=rising"
        ) from exc
    if channel < 0 or channel > 15:
        raise argparse.ArgumentTypeError("logic trigger channel must be between 0 and 15")
    if condition not in {"low", "high", "rising", "falling"}:
        raise argparse.ArgumentTypeError(
            "logic trigger condition must be low, high, rising, or falling"
        )
    return {"channel": channel, "condition": condition}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENBENCH_URL", DEFAULT_BASE_URL),
        help="API base URL (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("health", help="Check whether the server is ready")
    commands.add_parser("devices", help="List registered instruments")
    discover = commands.add_parser("discover", help="Discover one instrument type")
    discover.add_argument(
        "driver",
        choices=(
            "simulated",
            "ut197",
            "ut61d",
            "ut61e",
            "ut61eplus",
            "micsig",
            "micsig_eto",
            "feeltech",
            "dps150",
            "owon_spm",
            "kingst",
            "itech_it6000c",
            "all",
        ),
    )

    disconnect = commands.add_parser("disconnect", help="Disconnect one instrument")
    disconnect.add_argument("device_id")

    commands.add_parser("channels", help="List channels")
    latest = commands.add_parser("latest", help="Read one or all latest measurements")
    latest.add_argument("channel_id", nargs="?")

    settings_get = commands.add_parser("settings-get", help="Read instrument settings")
    settings_get.add_argument("device_id")

    settings_set = commands.add_parser("settings-set", help="Update instrument settings")
    settings_set.add_argument("device_id")
    settings_set.add_argument("--context")
    settings_set.add_argument("--poll-interval-s", type=float)
    settings_set.add_argument("--screen", choices=("on", "off"))
    settings_set.add_argument("--data", choices=("on", "off"))
    settings_set.add_argument("--wait-for-trigger", choices=("on", "off"))
    settings_set.add_argument(
        "--scope-channel",
        action="append",
        choices=("CH1", "CH2", "CH3", "CH4"),
    )

    generator_get = commands.add_parser(
        "generator-get",
        help="Read complete signal-generator state",
    )
    generator_get.add_argument("device_id")

    generator_set = commands.add_parser(
        "generator-set",
        help="Update one generator channel and verify read-back",
    )
    generator_set.add_argument("device_id")
    generator_set.add_argument("channel", type=int, choices=(1, 2))
    generator_set.add_argument("--waveform-code", type=int)
    generator_set.add_argument("--frequency-hz", type=float)
    generator_set.add_argument("--amplitude-vpp", type=float)
    generator_set.add_argument("--offset-v", type=float)
    generator_set.add_argument("--duty-percent", type=float)
    generator_set.add_argument("--phase-deg", type=float)
    generator_set.add_argument("--pulse-width-ns", type=float)
    generator_set.add_argument("--output", choices=("on", "off"))

    generator_outputs = commands.add_parser(
        "generator-outputs",
        help="Set both generator output states",
    )
    generator_outputs.add_argument("device_id")
    generator_outputs.add_argument("--ch1", required=True, choices=("on", "off"))
    generator_outputs.add_argument("--ch2", required=True, choices=("on", "off"))

    generator_sync = commands.add_parser(
        "generator-sync",
        help="Enable or disable CH2-follow-CH1 synchronization",
    )
    generator_sync.add_argument("device_id")
    generator_sync.add_argument(
        "parameter",
        choices=("waveform", "frequency", "amplitude", "offset", "duty"),
    )
    generator_sync.add_argument("state", choices=("on", "off"))

    generator_burst = commands.add_parser(
        "generator-burst",
        help="Configure verified CH1 burst source and cycle count",
    )
    generator_burst.add_argument("device_id")
    generator_burst.add_argument("source", choices=("off", "ch2", "external"))
    generator_burst.add_argument("cycles", type=int)

    generator_trigger = commands.add_parser(
        "generator-trigger",
        help="Trigger one active CH1 burst",
    )
    generator_trigger.add_argument("device_id")
    generator_trigger.add_argument("--cycles", type=int)

    generator_keying = commands.add_parser(
        "generator-keying",
        help="Configure ASK, FSK, or PSK",
    )
    generator_keying.add_argument("device_id")
    generator_keying.add_argument("kind", choices=("ask", "fsk", "psk"))
    generator_keying.add_argument("source", choices=("off", "external", "manual"))
    generator_keying.add_argument("--secondary-frequency-hz", type=float)

    generator_counter = commands.add_parser(
        "generator-counter",
        help="Configure and read the external counter",
    )
    generator_counter.add_argument("device_id")
    generator_counter.add_argument(
        "--mode",
        default="frequency",
        choices=("frequency", "count", "both"),
    )
    generator_counter.add_argument("--gate-time-s", required=True, type=int, choices=(1, 10, 100))
    generator_counter.add_argument("--coupling", required=True, choices=("dc", "ac"))

    generator_counter_pause = commands.add_parser(
        "generator-counter-pause",
        help="Pause external counter polling",
    )
    generator_counter_pause.add_argument("device_id")

    generator_counter_reset = commands.add_parser(
        "generator-counter-reset",
        help="Reset the external counter",
    )
    generator_counter_reset.add_argument("device_id")

    generator_sweep = commands.add_parser(
        "generator-sweep",
        help="Configure CH1 sweep (write-only on FY6200 firmware)",
    )
    generator_sweep.add_argument("device_id")
    generator_sweep.add_argument(
        "--target",
        required=True,
        choices=("frequency", "amplitude", "offset", "duty"),
    )
    generator_sweep.add_argument("--start", required=True, type=float)
    generator_sweep.add_argument("--end", required=True, type=float)
    generator_sweep.add_argument("--duration-s", required=True, type=float)
    generator_sweep.add_argument(
        "--mode",
        required=True,
        choices=("linear", "logarithmic"),
    )
    generator_sweep.add_argument("--source", required=True, choices=("time", "vco"))
    generator_sweep.add_argument("--enabled", required=True, choices=("on", "off"))

    for action in ("save", "load"):
        generator_preset = commands.add_parser(
            f"generator-preset-{action}",
            help=f"{action.capitalize()} one generator preset",
        )
        generator_preset.add_argument("device_id")
        generator_preset.add_argument("slot", type=int, choices=range(1, 21))

    power_get = commands.add_parser(
        "power-get",
        help="Read complete DPS-150 state",
    )
    power_get.add_argument("device_id")

    power_set = commands.add_parser(
        "power-set",
        help="Set DPS-150 voltage/current/output and verify read-back",
    )
    power_set.add_argument("device_id")
    power_set.add_argument("--voltage-v", type=float)
    power_set.add_argument("--current-a", type=float)
    power_set.add_argument("--output", choices=("on", "off"))

    power_protections = commands.add_parser(
        "power-protections",
        help="Set DPS-150 protection thresholds and verify read-back",
    )
    power_protections.add_argument("device_id")
    power_protections.add_argument("--ovp-v", type=float)
    power_protections.add_argument("--ocp-a", type=float)
    power_protections.add_argument("--opp-w", type=float)
    power_protections.add_argument("--otp-c", type=float)
    power_protections.add_argument("--lvp-v", type=float)

    power_display = commands.add_parser(
        "power-display",
        help="Set DPS-150 brightness or volume",
    )
    power_display.add_argument("device_id")
    power_display.add_argument("--brightness", type=int)
    power_display.add_argument("--volume", type=int)

    power_metering = commands.add_parser(
        "power-metering",
        help="Start or stop DPS-150 Ah/Wh metering",
    )
    power_metering.add_argument("device_id")
    power_metering.add_argument("state", choices=("on", "off"))

    power_preset_save = commands.add_parser(
        "power-preset-save",
        help="Overwrite one DPS-150 instrument preset",
    )
    power_preset_save.add_argument("device_id")
    power_preset_save.add_argument("slot", type=int, choices=range(1, 7))
    power_preset_save.add_argument("voltage_v", type=float)
    power_preset_save.add_argument("current_a", type=float)

    power_preset_apply = commands.add_parser(
        "power-preset-apply",
        help="Apply one DPS-150 preset, preserving output unless specified",
    )
    power_preset_apply.add_argument("device_id")
    power_preset_apply.add_argument("slot", type=int, choices=range(1, 7))
    power_preset_apply.add_argument("--output", choices=("on", "off"))

    power_sequence = commands.add_parser(
        "power-sequence",
        help="Start a DPS-150 voltage/current sequence",
    )
    power_sequence.add_argument("device_id")
    power_sequence.add_argument(
        "--step",
        action="append",
        required=True,
        type=parse_power_step,
        metavar="V,A,SECONDS",
    )
    power_sequence.add_argument("--loops", type=int, default=1)

    power_sweep = commands.add_parser(
        "power-sweep",
        help="Start a DPS-150 voltage or current sweep",
    )
    power_sweep.add_argument("device_id")
    power_sweep.add_argument("parameter", choices=("voltage", "current"))
    power_sweep.add_argument("--start", required=True, type=float)
    power_sweep.add_argument("--end", required=True, type=float)
    power_sweep.add_argument("--step", required=True, type=float)
    power_sweep.add_argument("--fixed-value", required=True, type=float)
    power_sweep.add_argument("--dwell-s", required=True, type=float)
    power_sweep.add_argument("--loops", type=int, default=1)

    for action in ("status", "pause", "resume", "stop"):
        power_program = commands.add_parser(
            f"power-program-{action}",
            help=f"{action.capitalize()} a DPS-150 output program",
        )
        power_program.add_argument("device_id")
        if action == "stop":
            power_program.add_argument(
                "--keep-output",
                action="store_true",
                help="Do not force output off after stopping",
            )

    commands.add_parser(
        "itech-list",
        help="List ITECH IT6000C bidirectional supplies and complete state",
    )

    itech_get = commands.add_parser(
        "itech-get",
        help="Read one ITECH IT6000C identity, state, limits, and warnings",
    )
    itech_get.add_argument("device_id")

    itech_measurements = commands.add_parser(
        "itech-measurements",
        help="Read only measured ITECH voltage, current, and power",
    )
    itech_measurements.add_argument("device_id")

    for action, help_text in (
        ("reserve", "Reserve ITECH for an external experiment and suspend polling"),
        ("release", "Release an ITECH external-experiment reservation"),
    ):
        itech_reservation = commands.add_parser(f"itech-{action}", help=help_text)
        itech_reservation.add_argument("device_id")

    itech_set = commands.add_parser(
        "itech-set",
        help="Set a bounded ITECH fixed CV/CC operating point",
    )
    itech_set.add_argument("device_id")
    itech_set.add_argument("--priority", choices=("CV", "CC"))
    itech_set.add_argument("--voltage-v", type=float)
    itech_set.add_argument("--current-a", type=float)
    itech_set.add_argument("--current-limit-positive-a", type=float)
    itech_set.add_argument("--current-limit-negative-a", type=float)
    itech_set.add_argument("--voltage-limit-positive-v", type=float)
    itech_set.add_argument("--voltage-limit-negative-v", type=float)
    itech_set.add_argument("--power-limit-positive-w", type=float)
    itech_set.add_argument("--power-limit-negative-w", type=float)
    itech_set.add_argument("--output", choices=("on", "off"))
    itech_set.add_argument(
        "--wiring-confirmed",
        action="store_true",
        help="Confirm known wiring/load; required with --output on",
    )

    itech_protections = commands.add_parser(
        "itech-protections",
        help="Update ITECH protections while Output is OFF",
    )
    itech_protections.add_argument("device_id")
    for code in ("ovp", "ocp", "opp", "uvp", "ucp"):
        itech_protections.add_argument(f"--{code}", choices=("on", "off"))
        itech_protections.add_argument(f"--{code}-level", type=float)
        itech_protections.add_argument(f"--{code}-delay-s", type=float)
    itech_protections.add_argument("--uvp-warmup-s", type=float)
    itech_protections.add_argument("--ucp-warmup-s", type=float)

    itech_clear = commands.add_parser(
        "itech-clear-protection",
        help="Clear a latched ITECH protection while Output is OFF",
    )
    itech_clear.add_argument("device_id")

    itech_advanced = commands.add_parser(
        "itech-advanced",
        help="Update ITECH slew, delay, and watchdog settings while Output is OFF",
    )
    itech_advanced.add_argument("device_id")
    itech_advanced.add_argument("--voltage-slew-positive", type=float)
    itech_advanced.add_argument("--voltage-slew-negative", type=float)
    itech_advanced.add_argument("--current-slew-positive", type=float)
    itech_advanced.add_argument("--current-slew-negative", type=float)
    itech_advanced.add_argument("--output-rise-delay-s", type=float)
    itech_advanced.add_argument("--output-fall-delay-s", type=float)
    itech_advanced.add_argument("--watchdog", choices=("on", "off"))
    itech_advanced.add_argument("--watchdog-delay-s", type=float)

    commands.add_parser(
        "smu-list",
        help="List OWON SPM source-measure units and complete state",
    )

    smu_get = commands.add_parser(
        "smu-get",
        help="Read one OWON SPM source and multimeter state",
    )
    smu_get.add_argument("device_id")

    smu_set = commands.add_parser(
        "smu-set",
        help="Set OWON SPM source voltage/current/output and verify read-back",
    )
    smu_set.add_argument("device_id")
    smu_set.add_argument("--voltage-v", type=float)
    smu_set.add_argument("--current-a", type=float)
    smu_set.add_argument("--output", choices=("on", "off"))

    smu_protections = commands.add_parser(
        "smu-protections",
        help="Set OWON SPM OVP/OCP thresholds and verify read-back",
    )
    smu_protections.add_argument("device_id")
    smu_protections.add_argument("--ovp-v", type=float)
    smu_protections.add_argument("--ocp-a", type=float)

    smu_dmm = commands.add_parser(
        "smu-dmm",
        help="Configure the OWON SPM multimeter and verify read-back",
    )
    smu_dmm.add_argument("device_id")
    smu_dmm.add_argument(
        "function",
        nargs="?",
        choices=(
            "dc_voltage",
            "ac_voltage",
            "dc_current",
            "ac_current",
            "resistance",
            "capacitance",
            "diode",
            "continuity",
        ),
    )
    smu_dmm.add_argument("--range-mode", choices=("auto", "manual"))
    smu_dmm.add_argument("--range-value", type=float)
    smu_dmm.add_argument("--relative", choices=("on", "off"))
    smu_dmm.add_argument("--hold", choices=("on", "off"))

    scope_get = commands.add_parser("scope-get", help="Read complete MHO1 scope state")
    scope_get.add_argument("device_id")

    scope_settings = commands.add_parser(
        "scope-settings",
        help="Update bounded MHO1 acquisition, timebase, or edge-trigger settings",
    )
    scope_settings.add_argument("device_id")
    scope_settings.add_argument(
        "--acquisition-type",
        choices=("NORMAL", "MEAN", "ENVELOP", "PEAK"),
    )
    scope_settings.add_argument(
        "--averaging-count",
        type=int,
        choices=(2, 4, 8, 16, 32, 64, 128, 256),
    )
    scope_settings.add_argument("--memory-depth-setting")
    scope_settings.add_argument("--timebase-s-per-div", type=float)
    scope_settings.add_argument("--timebase-position-s", type=float)
    scope_settings.add_argument("--timebase-mode", choices=("YT", "XY"))
    scope_settings.add_argument("--trigger-mode", choices=("AUTO", "NORMAL"))
    scope_settings.add_argument(
        "--trigger-source",
        choices=("CH1", "CH2", "CH3", "CH4"),
    )
    scope_settings.add_argument("--trigger-slope", choices=("RISE", "FALL", "DUAL"))
    scope_settings.add_argument("--trigger-level-v", type=float)
    scope_settings.add_argument(
        "--trigger-coupling",
        choices=("DC", "AC", "HFREJ", "LFREJ", "NOISEREJ"),
    )

    for action in ("run", "stop"):
        scope_action = commands.add_parser(
            f"scope-{action}",
            help=f"{action.capitalize()} MHO1 acquisition",
        )
        scope_action.add_argument("device_id")
    scope_single = commands.add_parser("scope-single", help="Run one MHO1 acquisition")
    scope_single.add_argument("device_id")
    scope_single.add_argument("--timeout-s", type=float, default=2.0)
    for action in ("get", "set", "read"):
        scope_measurements = commands.add_parser(
            f"scope-measurements-{action}",
            help=(
                "Read the configured MHO1 measurement profile"
                if action == "get"
                else (
                    "Replace the complete MHO1 measurement profile; omit --measurement to clear"
                    if action == "set"
                    else "Read MHO1 measurements without changing the configured profile"
                )
            ),
        )
        scope_measurements.add_argument("device_id")
        if action != "get":
            scope_measurements.add_argument(
                "--measurement",
                action="append",
                default=[],
                metavar="CHn:ITEM[:CHn[:EDGE:EDGE]]",
            )
    scope_numeric_csv = commands.add_parser(
        "scope-numeric-csv",
        help="Capture real MHO1 waveform samples and write a local CSV",
    )
    scope_numeric_csv.add_argument("device_id")
    scope_numeric_csv.add_argument(
        "--channel",
        action="append",
        choices=("CH1", "CH2", "CH3", "CH4"),
    )
    scope_numeric_csv.add_argument("--mode", default="NORMAL", choices=("NORMAL",))
    scope_numeric_csv.add_argument("--filename-prefix", default="openbench_numeric")
    scope_numeric_csv.add_argument("--output", type=Path)

    scope_maximum_start = commands.add_parser(
        "scope-maximum-start",
        help="Start one STOP-only Micsig MAXIMUM ASCII capture",
    )
    scope_maximum_start.add_argument("device_id")
    scope_maximum_start.add_argument(
        "--channel",
        action="append",
        choices=("CH1", "CH2", "CH3", "CH4"),
    )
    scope_maximum_status = commands.add_parser(
        "scope-maximum-status",
        help="Read Micsig MAXIMUM ASCII capture progress",
    )
    scope_maximum_status.add_argument("device_id")
    scope_maximum_download = commands.add_parser(
        "scope-maximum-download",
        help="Download one Micsig MAXIMUM ASCII artifact",
    )
    scope_maximum_download.add_argument("device_id")
    scope_maximum_download.add_argument("filename")
    scope_maximum_download.add_argument("--output", type=Path)

    all_outputs_off = commands.add_parser(
        "all-outputs-off",
        help="Latch safety, open the matrix, and disable generator and supply outputs",
    )
    all_outputs_off.add_argument("--reason", default="Codex operator request")

    snapshot = commands.add_parser("snapshot", help="Capture one synchronized snapshot")
    add_capture_metadata(snapshot)

    record_start = commands.add_parser("record-start", help="Start CSV recording")
    add_capture_metadata(record_start)
    record_start.add_argument("--duration-s", type=float)
    record_start.add_argument(
        "--scope-capture-mode",
        choices=("periodic", "manual"),
        default="periodic",
    )
    record_scope_frame = commands.add_parser(
        "record-scope-frame",
        help="Capture one exact scope frame into a manual-mode CSV recording",
    )
    record_scope_frame.add_argument("device_id")
    record_scope_frame.add_argument("--label", default="")
    commands.add_parser("record-status", help="Read capture status")
    commands.add_parser("record-stop", help="Stop CSV recording")

    commands.add_parser("logic-list", help="List Kingst logic analyzers and state")

    logic_settings_get = commands.add_parser(
        "logic-settings-get",
        help="Read LA2016 acquisition and scheduling settings",
    )
    logic_settings_get.add_argument("device_id")

    logic_settings_set = commands.add_parser(
        "logic-settings-set",
        help="Update any subset of LA2016 acquisition and scheduling settings",
    )
    logic_settings_set.add_argument("device_id")
    logic_settings_set.add_argument("--channels", type=parse_logic_channels)
    logic_settings_set.add_argument("--sample-rate-hz", type=int)
    logic_settings_set.add_argument("--sample-count", type=int)
    logic_settings_set.add_argument("--threshold-v", type=float)
    logic_settings_set.add_argument("--capture-ratio-percent", type=int)
    logic_settings_set.add_argument(
        "--trigger",
        action="append",
        type=parse_logic_trigger,
        help="Repeat for each trigger, for example --trigger CH0=rising",
    )
    logic_settings_set.add_argument(
        "--clear-triggers",
        action="store_true",
        help="Remove all configured hardware triggers",
    )
    logic_settings_set.add_argument("--auto-start", choices=("on", "off"))
    logic_settings_set.add_argument("--auto-start-delay-s", type=float)

    logic_status = commands.add_parser(
        "logic-status",
        help="Read current LA2016 capture state and countdown",
    )
    logic_status.add_argument("device_id")

    logic_capture_get = commands.add_parser(
        "logic-capture-get",
        help="Read one known LA2016 capture by ID",
    )
    logic_capture_get.add_argument("device_id")
    logic_capture_get.add_argument("capture_id")

    for action, description in (
        ("start", "Start an immediate triggerless LA2016 capture"),
        ("arm", "Arm an LA2016 capture with configured hardware triggers"),
    ):
        logic_capture = commands.add_parser(f"logic-{action}", help=description)
        logic_capture.add_argument("device_id")
        add_capture_metadata(logic_capture)

    logic_stop = commands.add_parser("logic-stop", help="Stop an active LA2016 capture")
    logic_stop.add_argument("device_id")

    logic_download = commands.add_parser(
        "logic-download",
        help="Download capture.sr or metadata.json from an LA2016 capture",
    )
    logic_download.add_argument("device_id")
    logic_download.add_argument("capture_id")
    logic_download.add_argument("filename", choices=("capture.sr", "metadata.json"))
    logic_download.add_argument("--output", type=Path)

    capture_download = commands.add_parser(
        "capture-download",
        help="Download a capture file",
    )
    capture_download.add_argument("filename")
    capture_download.add_argument("--output", type=Path)
    return parser


def all_latest(base_url: str) -> list[dict[str, Any]]:
    channels = request_json(base_url, "GET", "/channels")
    results: list[dict[str, Any]] = []
    for channel in channels:
        channel_id = channel["id"]
        try:
            measurement = request_json(
                base_url,
                "GET",
                f"/channels/{quote(channel_id, safe='')}/latest",
            )
        except OpenBenchError as exc:
            results.append({"channel_id": channel_id, "error": str(exc)})
        else:
            results.append(measurement)
    return results


def scope_measurement_payload(values: list[str]) -> dict[str, list[dict[str, str]]]:
    measurements: list[dict[str, str]] = []
    for value in values:
        fields = [field.strip() for field in value.split(":")]
        if len(fields) < 2:
            raise OpenBenchError(f"Invalid measurement {value!r}; expected CHn:ITEM")
        normalized_channel = fields[0].upper()
        normalized_item = fields[1].lower()
        if normalized_channel not in {"CH1", "CH2", "CH3", "CH4"} or not normalized_item:
            raise OpenBenchError(
                f"Invalid measurement {value!r}; expected CH1..CH4 and a non-empty item"
            )
        measurement = {"channel": normalized_channel, "item": normalized_item}
        if normalized_item == "phase":
            if len(fields) != 3:
                raise OpenBenchError(f"Invalid PHASE {value!r}; expected CHn:phase:CHn")
            measurement["secondary_channel"] = fields[2].upper()
        elif normalized_item == "delay":
            if len(fields) not in {3, 5}:
                raise OpenBenchError(f"Invalid DELAY {value!r}; expected CHn:delay:CHn[:EDGE:EDGE]")
            measurement["secondary_channel"] = fields[2].upper()
            measurement["source_edge"] = fields[3] if len(fields) == 5 else "FRISe"
            measurement["target_edge"] = fields[4] if len(fields) == 5 else "FRISe"
        elif len(fields) != 2:
            raise OpenBenchError(
                f"Invalid measurement {value!r}; only PHASE and DELAY accept two channels"
            )
        if measurement.get("secondary_channel") not in {None, "CH1", "CH2", "CH3", "CH4"}:
            raise OpenBenchError(f"Invalid secondary channel in measurement {value!r}")
        measurements.append(measurement)
    return {"measurements": measurements}


def execute(args: argparse.Namespace) -> Any:
    base_url = args.base_url
    if args.command == "health":
        return request_json(base_url, "GET", "/health")
    if args.command == "devices":
        return request_json(base_url, "GET", "/devices")
    if args.command == "discover":
        return request_json(base_url, "POST", f"/devices/discover/{args.driver}")
    if args.command == "disconnect":
        device_id = quote(args.device_id, safe="")
        return request_json(base_url, "DELETE", f"/devices/{device_id}")
    if args.command == "channels":
        return request_json(base_url, "GET", "/channels")
    if args.command == "latest":
        if args.channel_id is None:
            return all_latest(base_url)
        channel_id = quote(args.channel_id, safe="")
        return request_json(base_url, "GET", f"/channels/{channel_id}/latest")
    if args.command == "settings-get":
        device_id = quote(args.device_id, safe="")
        return request_json(base_url, "GET", f"/devices/{device_id}/settings")
    if args.command == "settings-set":
        payload: dict[str, Any] = {}
        if args.context is not None:
            payload["context"] = args.context
        if args.poll_interval_s is not None:
            payload["poll_interval_s"] = args.poll_interval_s
        if args.screen is not None:
            payload["scope_screen"] = args.screen == "on"
        if args.data is not None:
            payload["scope_data"] = args.data == "on"
        if args.wait_for_trigger is not None:
            payload["scope_wait_for_trigger"] = args.wait_for_trigger == "on"
        if args.scope_channel is not None:
            payload["scope_channels"] = args.scope_channel
        if not payload:
            raise OpenBenchError("settings-set requires at least one setting")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/devices/{device_id}/settings",
            payload,
        )
    if args.command == "generator-get":
        device_id = quote(args.device_id, safe="")
        return request_json(base_url, "GET", f"/generators/{device_id}")
    if args.command == "generator-set":
        field_names = (
            "waveform_code",
            "frequency_hz",
            "amplitude_vpp",
            "offset_v",
            "duty_percent",
            "phase_deg",
            "pulse_width_ns",
        )
        payload = {
            field_name: getattr(args, field_name)
            for field_name in field_names
            if getattr(args, field_name) is not None
        }
        if args.output is not None:
            payload["output_enabled"] = args.output == "on"
        if not payload:
            raise OpenBenchError("generator-set requires at least one setting")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/generators/{device_id}/channels/{args.channel}",
            payload,
        )
    if args.command == "generator-outputs":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PUT",
            f"/generators/{device_id}/outputs",
            {"channel_1": args.ch1 == "on", "channel_2": args.ch2 == "on"},
        )
    if args.command == "generator-sync":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/generators/{device_id}/synchronization",
            {"parameter": args.parameter, "enabled": args.state == "on"},
        )
    if args.command == "generator-burst":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/generators/{device_id}/burst",
            {"source": args.source, "cycles": args.cycles},
        )
    if args.command == "generator-trigger":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "POST",
            f"/generators/{device_id}/burst/trigger",
            {"cycles": args.cycles},
        )
    if args.command == "generator-keying":
        device_id = quote(args.device_id, safe="")
        payload = {"kind": args.kind, "source": args.source}
        if args.secondary_frequency_hz is not None:
            payload["secondary_frequency_hz"] = args.secondary_frequency_hz
        return request_json(
            base_url,
            "PATCH",
            f"/generators/{device_id}/keying",
            payload,
        )
    if args.command == "generator-counter":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/generators/{device_id}/counter",
            {
                "mode": args.mode,
                "gate_time_s": args.gate_time_s,
                "coupling": args.coupling,
            },
        )
    if args.command == "generator-counter-pause":
        device_id = quote(args.device_id, safe="")
        return request_json(base_url, "POST", f"/generators/{device_id}/counter/pause")
    if args.command == "generator-counter-reset":
        device_id = quote(args.device_id, safe="")
        return request_json(base_url, "POST", f"/generators/{device_id}/counter/reset")
    if args.command == "generator-sweep":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/generators/{device_id}/sweep",
            {
                "target": args.target,
                "start": args.start,
                "end": args.end,
                "duration_s": args.duration_s,
                "mode": args.mode,
                "source": args.source,
                "enabled": args.enabled == "on",
            },
        )
    if args.command in {"generator-preset-save", "generator-preset-load"}:
        device_id = quote(args.device_id, safe="")
        action = args.command.removeprefix("generator-preset-")
        return request_json(
            base_url,
            "POST",
            f"/generators/{device_id}/presets/{args.slot}/{action}",
        )
    if args.command == "power-get":
        device_id = quote(args.device_id, safe="")
        return request_json(base_url, "GET", f"/power-supplies/{device_id}")
    if args.command == "power-set":
        payload = {}
        if args.voltage_v is not None:
            payload["voltage_v"] = args.voltage_v
        if args.current_a is not None:
            payload["current_a"] = args.current_a
        if args.output is not None:
            payload["enabled"] = args.output == "on"
        if not payload:
            raise OpenBenchError("power-set requires at least one setting")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/power-supplies/{device_id}/output",
            payload,
        )
    if args.command == "power-protections":
        names = {
            "ovp_v": "over_voltage_v",
            "ocp_a": "over_current_a",
            "opp_w": "over_power_w",
            "otp_c": "over_temperature_c",
            "lvp_v": "low_input_voltage_v",
        }
        payload = {
            api_name: getattr(args, argument_name)
            for argument_name, api_name in names.items()
            if getattr(args, argument_name) is not None
        }
        if not payload:
            raise OpenBenchError("power-protections requires at least one threshold")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/power-supplies/{device_id}/protections",
            payload,
        )
    if args.command == "power-display":
        payload = {
            field: getattr(args, field)
            for field in ("brightness", "volume")
            if getattr(args, field) is not None
        }
        if not payload:
            raise OpenBenchError("power-display requires at least one setting")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/power-supplies/{device_id}/display",
            payload,
        )
    if args.command == "power-metering":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/power-supplies/{device_id}/metering",
            {"enabled": args.state == "on"},
        )
    if args.command == "power-preset-save":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PUT",
            f"/power-supplies/{device_id}/presets/{args.slot}",
            {"voltage_v": args.voltage_v, "current_a": args.current_a},
        )
    if args.command == "power-preset-apply":
        device_id = quote(args.device_id, safe="")
        payload = None if args.output is None else {"enabled": args.output == "on"}
        return request_json(
            base_url,
            "POST",
            f"/power-supplies/{device_id}/presets/{args.slot}/apply",
            payload,
        )
    if args.command == "power-sequence":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "POST",
            f"/power-supplies/{device_id}/programs/sequence",
            {"steps": args.step, "loops": args.loops},
        )
    if args.command == "power-sweep":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "POST",
            f"/power-supplies/{device_id}/programs/sweep",
            {
                "parameter": args.parameter,
                "start": args.start,
                "end": args.end,
                "step": args.step,
                "fixed_value": args.fixed_value,
                "dwell_s": args.dwell_s,
                "loops": args.loops,
            },
        )
    if args.command.startswith("power-program-"):
        device_id = quote(args.device_id, safe="")
        action = args.command.removeprefix("power-program-")
        if action == "status":
            return request_json(
                base_url,
                "GET",
                f"/power-supplies/{device_id}/programs/status",
            )
        payload = {"output_off": not args.keep_output} if action == "stop" else None
        return request_json(
            base_url,
            "POST",
            f"/power-supplies/{device_id}/programs/{action}",
            payload,
        )
    if args.command == "itech-list":
        return request_json(base_url, "GET", "/bidirectional-power-supplies")
    if args.command == "itech-get":
        device_id = quote(args.device_id, safe="")
        return request_json(base_url, "GET", f"/bidirectional-power-supplies/{device_id}")
    if args.command == "itech-measurements":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "GET",
            f"/bidirectional-power-supplies/{device_id}/measurements",
        )
    if args.command in {"itech-reserve", "itech-release"}:
        device_id = quote(args.device_id, safe="")
        method = "POST" if args.command == "itech-reserve" else "DELETE"
        return request_json(
            base_url,
            method,
            f"/bidirectional-power-supplies/{device_id}/experiment-reservation",
        )
    if args.command == "itech-set":
        names = {
            "priority": "priority",
            "voltage_v": "voltage_setpoint_v",
            "current_a": "current_setpoint_a",
            "current_limit_positive_a": "current_limit_positive_a",
            "current_limit_negative_a": "current_limit_negative_a",
            "voltage_limit_positive_v": "voltage_limit_positive_v",
            "voltage_limit_negative_v": "voltage_limit_negative_v",
            "power_limit_positive_w": "power_limit_positive_w",
            "power_limit_negative_w": "power_limit_negative_w",
        }
        payload = {
            api_name: getattr(args, argument_name)
            for argument_name, api_name in names.items()
            if getattr(args, argument_name) is not None
        }
        if args.output is not None:
            payload["output_enabled"] = args.output == "on"
        if args.wiring_confirmed:
            payload["wiring_confirmed"] = True
        if not payload or payload == {"wiring_confirmed": True}:
            raise OpenBenchError("itech-set requires at least one operating-point setting")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/bidirectional-power-supplies/{device_id}/operating-point",
            payload,
        )
    if args.command == "itech-protections":
        payload = {}
        unit_names = {"ovp": "v", "ocp": "a", "opp": "w", "uvp": "v", "ucp": "a"}
        for code, unit in unit_names.items():
            enabled = getattr(args, code)
            if enabled is not None:
                payload[f"{code}_enabled"] = enabled == "on"
            level = getattr(args, f"{code}_level")
            if level is not None:
                payload[f"{code}_level_{unit}"] = level
            delay = getattr(args, f"{code}_delay_s")
            if delay is not None:
                payload[f"{code}_delay_s"] = delay
        for field in ("uvp_warmup_s", "ucp_warmup_s"):
            value = getattr(args, field)
            if value is not None:
                payload[field] = value
        if not payload:
            raise OpenBenchError("itech-protections requires at least one setting")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/bidirectional-power-supplies/{device_id}/protections",
            payload,
        )
    if args.command == "itech-clear-protection":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "POST",
            f"/bidirectional-power-supplies/{device_id}/protections/clear",
        )
    if args.command == "itech-advanced":
        names = {
            "voltage_slew_positive": "voltage_slew_positive_v_per_ms",
            "voltage_slew_negative": "voltage_slew_negative_v_per_ms",
            "current_slew_positive": "current_slew_positive_a_per_ms",
            "current_slew_negative": "current_slew_negative_a_per_ms",
            "output_rise_delay_s": "output_rise_delay_s",
            "output_fall_delay_s": "output_fall_delay_s",
            "watchdog_delay_s": "watchdog_delay_s",
        }
        payload = {
            api_name: getattr(args, argument_name)
            for argument_name, api_name in names.items()
            if getattr(args, argument_name) is not None
        }
        if args.watchdog is not None:
            payload["watchdog_enabled"] = args.watchdog == "on"
        if not payload:
            raise OpenBenchError("itech-advanced requires at least one setting")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/bidirectional-power-supplies/{device_id}/advanced",
            payload,
        )
    if args.command == "smu-list":
        return request_json(base_url, "GET", "/source-measure-units")
    if args.command == "smu-get":
        device_id = quote(args.device_id, safe="")
        return request_json(base_url, "GET", f"/source-measure-units/{device_id}")
    if args.command == "smu-set":
        payload = {}
        if args.voltage_v is not None:
            payload["voltage_v"] = args.voltage_v
        if args.current_a is not None:
            payload["current_a"] = args.current_a
        if args.output is not None:
            payload["enabled"] = args.output == "on"
        if not payload:
            raise OpenBenchError("smu-set requires at least one setting")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/source-measure-units/{device_id}/output",
            payload,
        )
    if args.command == "smu-protections":
        payload = {}
        if args.ovp_v is not None:
            payload["over_voltage_v"] = args.ovp_v
        if args.ocp_a is not None:
            payload["over_current_a"] = args.ocp_a
        if not payload:
            raise OpenBenchError("smu-protections requires at least one threshold")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/source-measure-units/{device_id}/protections",
            payload,
        )
    if args.command == "smu-dmm":
        payload = {}
        if args.function is not None:
            payload["function"] = args.function
        if args.range_mode is not None:
            payload["range_mode"] = args.range_mode
        if args.range_value is not None:
            payload["range_value"] = args.range_value
        if args.relative is not None:
            payload["relative_enabled"] = args.relative == "on"
        if args.hold is not None:
            payload["hold_enabled"] = args.hold == "on"
        if not payload:
            raise OpenBenchError("smu-dmm requires a function or DMM setting")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/source-measure-units/{device_id}/multimeter",
            payload,
        )
    if args.command == "scope-get":
        device_id = quote(args.device_id, safe="")
        return request_json(base_url, "GET", f"/oscilloscopes/{device_id}")
    if args.command == "scope-settings":
        field_names = (
            "acquisition_type",
            "averaging_count",
            "memory_depth_setting",
            "timebase_s_per_div",
            "timebase_position_s",
            "timebase_mode",
        )
        payload = {
            field_name: getattr(args, field_name)
            for field_name in field_names
            if getattr(args, field_name) is not None
        }
        trigger_fields = {
            "mode": args.trigger_mode,
            "source": args.trigger_source,
            "slope": args.trigger_slope,
            "level_v": args.trigger_level_v,
            "coupling": args.trigger_coupling,
        }
        trigger = {key: value for key, value in trigger_fields.items() if value is not None}
        if trigger:
            payload["trigger"] = trigger
        if not payload:
            raise OpenBenchError("scope-settings requires at least one setting")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/oscilloscopes/{device_id}/settings",
            payload,
        )
    if args.command in {"scope-run", "scope-stop"}:
        device_id = quote(args.device_id, safe="")
        action = args.command.removeprefix("scope-")
        return request_json(base_url, "POST", f"/oscilloscopes/{device_id}/{action}")
    if args.command == "scope-single":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "POST",
            f"/oscilloscopes/{device_id}/single",
            {"timeout_s": args.timeout_s},
        )
    if args.command == "scope-measurements-get":
        device_id = quote(args.device_id, safe="")
        return request_json(base_url, "GET", f"/oscilloscopes/{device_id}/measurements")
    if args.command in {"scope-measurements-set", "scope-measurements-read"}:
        device_id = quote(args.device_id, safe="")
        payload = scope_measurement_payload(args.measurement)
        if args.command == "scope-measurements-read" and not payload["measurements"]:
            current = request_json(
                base_url,
                "GET",
                f"/oscilloscopes/{device_id}/measurements",
            )
            payload = {
                "measurements": [
                    {
                        key: item[key]
                        for key in (
                            "channel",
                            "item",
                            "secondary_channel",
                            "source_edge",
                            "target_edge",
                        )
                        if item.get(key) is not None
                    }
                    for item in current["measurements"]
                ]
            }
        method = "PUT" if args.command == "scope-measurements-set" else "POST"
        suffix = "measurements" if method == "PUT" else "measurements/read"
        return request_json(
            base_url,
            method,
            f"/oscilloscopes/{device_id}/{suffix}",
            payload,
        )
    if args.command == "scope-numeric-csv":
        device_id = quote(args.device_id, safe="")
        result = request_json(
            base_url,
            "POST",
            f"/oscilloscopes/{device_id}/numeric-waveforms/csv",
            {
                "channels": args.channel or ["CH1", "CH2", "CH3", "CH4"],
                "mode": args.mode,
                "filename_prefix": args.filename_prefix,
            },
            timeout_s=300,
        )
        if args.output is not None:
            result["download"] = download_scope_waveform_file(
                base_url,
                args.device_id,
                result["filename"],
                args.output,
            )
        return result
    if args.command == "scope-maximum-start":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "POST",
            f"/oscilloscopes/{device_id}/maximum-capture",
            {"channels": args.channel or ["CH1", "CH2", "CH3", "CH4"]},
        )
    if args.command == "scope-maximum-status":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "GET",
            f"/oscilloscopes/{device_id}/maximum-capture",
        )
    if args.command == "scope-maximum-download":
        output = args.output or Path(args.filename).name
        return download_scope_maximum_file(
            base_url,
            args.device_id,
            args.filename,
            Path(output),
        )
    if args.command == "all-outputs-off":
        return request_json(
            base_url,
            "POST",
            "/emergency-stop",
            {"reason": args.reason},
        )
    if args.command == "snapshot":
        return request_json(
            base_url,
            "POST",
            "/captures/snapshot",
            {"title": args.title, "comment": args.comment},
        )
    if args.command == "record-start":
        payload = {
            "title": args.title,
            "comment": args.comment,
            "scope_capture_mode": args.scope_capture_mode,
        }
        if args.duration_s is not None:
            payload["duration_s"] = args.duration_s
        return request_json(base_url, "POST", "/captures/recording/start", payload)
    if args.command == "record-scope-frame":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "POST",
            f"/captures/recording/scopes/{device_id}/frame",
            {"label": args.label},
        )
    if args.command == "record-status":
        return request_json(base_url, "GET", "/captures/status")
    if args.command == "record-stop":
        return request_json(base_url, "POST", "/captures/recording/stop")
    if args.command == "logic-list":
        return request_json(base_url, "GET", "/logic-analyzers")
    if args.command == "logic-settings-get":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "GET",
            f"/logic-analyzers/{device_id}/settings",
        )
    if args.command == "logic-settings-set":
        field_names = (
            "channels",
            "sample_rate_hz",
            "sample_count",
            "threshold_v",
            "capture_ratio_percent",
            "auto_start_delay_s",
        )
        payload = {
            field_name: getattr(args, field_name)
            for field_name in field_names
            if getattr(args, field_name) is not None
        }
        if args.clear_triggers:
            payload["triggers"] = []
        elif args.trigger is not None:
            payload["triggers"] = args.trigger
        if args.auto_start is not None:
            payload["auto_start_enabled"] = args.auto_start == "on"
        if not payload:
            raise OpenBenchError("logic-settings-set requires at least one setting")
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "PATCH",
            f"/logic-analyzers/{device_id}/settings",
            payload,
        )
    if args.command == "logic-status":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "GET",
            f"/logic-analyzers/{device_id}/captures/status",
        )
    if args.command == "logic-capture-get":
        device_id = quote(args.device_id, safe="")
        capture_id = quote(args.capture_id, safe="")
        return request_json(
            base_url,
            "GET",
            f"/logic-analyzers/{device_id}/captures/{capture_id}",
        )
    if args.command in {"logic-start", "logic-arm"}:
        device_id = quote(args.device_id, safe="")
        action = args.command.removeprefix("logic-")
        return request_json(
            base_url,
            "POST",
            f"/logic-analyzers/{device_id}/captures/{action}",
            {"title": args.title, "comment": args.comment},
        )
    if args.command == "logic-stop":
        device_id = quote(args.device_id, safe="")
        return request_json(
            base_url,
            "POST",
            f"/logic-analyzers/{device_id}/captures/stop",
        )
    if args.command == "logic-download":
        output = args.output or Path(args.filename)
        return download_logic_file(
            base_url,
            args.device_id,
            args.capture_id,
            args.filename,
            output,
        )
    if args.command == "capture-download":
        output = args.output or Path(args.filename).name
        return download_file(base_url, args.filename, Path(output))
    raise OpenBenchError(f"Unsupported command: {args.command}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = execute(args)
    except OpenBenchError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
