"""Capture one complete MHO1 ASCII/screenshot/measurement session.

The successful-path SCPI contract is fixed:

1. ``:MENU:STOP``
2. ``:WAVeform:SOURce CH1``
3. ``:WAVeform:MODE NORMal``
4. ``:WAVeform:PREamble?``
5. ``:WAVeform:DATA:ASCii?``
6. ``:WAVeform:SOURce CH2``
7. ``:WAVeform:MODE NORMal``
8. ``:WAVeform:DATA:ASCii?``
9. ``:WAVeform:SOURce CH3``
10. ``:WAVeform:MODE NORMal``
11. ``:WAVeform:DATA:ASCii?``
12. ``:WAVeform:SOURce CH4``
13. ``:WAVeform:MODE NORMal``
14. ``:WAVeform:DATA:ASCii?``
15. ``:MEASure:CLEar all``
16-25. Ten ``:MEASure:OPEN`` writes
26. ``:SYS:SCR?`` (one bounded repeat after at least 1 s only if needed)
27-36. Ten matching scalar measurement queries
37. ``:MENU:RUN`` (38 commands total when the screenshot repeat is used)

Every line uses a fresh TCP connection. There is no fixed host-side delay,
setting read-back, or unrelated state query. The only automatic repeat is the
known screenshot warm-up case: at most two ``:SYS:SCR?`` attempts. RUN is sent
from a finally block.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mho1_ascii_minimal_probe import (
    ANALOG_SOURCES,
    ASCII_QUERY,
    BLOCK_TIMEOUT_S,
    CONNECT_TIMEOUT_S,
    MAX_RESPONSE_BYTES,
    NORMAL_MODE_COMMAND,
    RUN_COMMAND,
    SCPI_PORT,
    STOP_COMMAND,
    AsciiBlock,
    _exception_text,
    _parse_values,
    _query_ascii_block,
    _recv_exact,
    _send_line,
    _sha256,
    _write_csv,
    _write_metadata,
)

SOURCES = ANALOG_SOURCES
SOURCE_COMMANDS = {source: f":WAVeform:SOURce {source}" for source in SOURCES}
PREAMBLE_QUERY = ":WAVeform:PREamble?"
SCREENSHOT_QUERY = ":SYS:SCR?"
MEASUREMENT_CLEAR_COMMAND = ":MEASure:CLEar all"
MAX_PREAMBLE_BYTES = 4096
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
SCREENSHOT_MAX_ATTEMPTS = 2
SCREENSHOT_MIN_INTERVAL_S = 1.0
MEASUREMENT_CLEAR_DELAY_S = 0.1
MEASUREMENT_OPEN_DELAY_S = 0.1
MEASUREMENT_READY_DELAY_S = 0.1
MEASUREMENT_PROFILE = (
    ("CH1", "amplitude", "AMP", "V"),
    ("CH1", "peak_to_peak", "PKPK", "V"),
    ("CH1", "rms", "RMS", "V"),
    ("CH1", "frequency", "FREQ", "Hz"),
    ("CH1", "maximum", "MAX", "V"),
    ("CH2", "amplitude", "AMP", "V"),
    ("CH2", "peak_to_peak", "PKPK", "V"),
    ("CH2", "rms", "RMS", "V"),
    ("CH2", "frequency", "FREQ", "Hz"),
    ("CH2", "minimum", "MIN", "V"),
)
MEASUREMENT_OPEN_COMMANDS = tuple(
    f":MEASure:OPEN {scpi_name},{source}" for source, _name, scpi_name, _unit in MEASUREMENT_PROFILE
)
MEASUREMENT_QUERY_COMMANDS = tuple(
    f":MEASure:{scpi_name}? {source}" for source, _name, scpi_name, _unit in MEASUREMENT_PROFILE
)
WAVEFORM_COMMAND_CONTRACT = (
    STOP_COMMAND,
    SOURCE_COMMANDS["CH1"],
    NORMAL_MODE_COMMAND,
    PREAMBLE_QUERY,
    ASCII_QUERY,
    SOURCE_COMMANDS["CH2"],
    NORMAL_MODE_COMMAND,
    ASCII_QUERY,
    SOURCE_COMMANDS["CH3"],
    NORMAL_MODE_COMMAND,
    ASCII_QUERY,
    SOURCE_COMMANDS["CH4"],
    NORMAL_MODE_COMMAND,
    ASCII_QUERY,
)
COMMAND_CONTRACT = (
    *WAVEFORM_COMMAND_CONTRACT,
    MEASUREMENT_CLEAR_COMMAND,
    *MEASUREMENT_OPEN_COMMANDS,
    SCREENSHOT_QUERY,
    *MEASUREMENT_QUERY_COMMANDS,
    RUN_COMMAND,
)
FRAME_COMMAND_CONTRACT = (
    *WAVEFORM_COMMAND_CONTRACT,
    SCREENSHOT_QUERY,
    *MEASUREMENT_QUERY_COMMANDS,
    RUN_COMMAND,
)

_SCPI_NUMBER_RE = re.compile(r"^[ \t]*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)")


@dataclass(frozen=True, slots=True)
class FourChannelOutcome:
    session_directory: Path
    metadata_path: Path
    success: bool
    sample_counts: dict[str, int]
    capture_error: str | None
    preamble_error: str | None
    screenshot_error: str | None
    measurement_error: str | None
    run_error: str | None


@dataclass(frozen=True, slots=True)
class WaveformPreamble:
    format_code: int
    mode_code: int
    count: int
    x_increment_s: float
    x_origin_s: float
    x_reference: float
    y_increment: float
    y_origin: float
    y_reference: float

    def time_at(self, index: int) -> float:
        return (index - self.x_reference) * self.x_increment_s + self.x_origin_s


@dataclass(frozen=True, slots=True)
class DefiniteBlock:
    header: bytes
    payload: bytes

    @property
    def raw(self) -> bytes:
        return self.header + self.payload


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _artifact_metadata(path: Path) -> dict[str, str | int]:
    return {
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _query_text(
    host: str,
    port: int,
    command: str,
    *,
    connect_timeout_s: float,
    response_timeout_s: float,
    max_response_bytes: int,
    on_sent: Callable[[], None],
) -> str:
    deadline = time.monotonic() + response_timeout_s
    response = bytearray()
    with socket.create_connection((host, port), timeout=connect_timeout_s) as connection:
        connection.sendall(command.encode("ascii") + b"\n")
        on_sent()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if response:
                    break
                raise TimeoutError(f"Timed out waiting for {command}")
            connection.settimeout(remaining)
            try:
                chunk = connection.recv(1024)
            except TimeoutError:
                if response:
                    break
                raise
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > max_response_bytes:
                raise ValueError(f"{command} exceeded the {max_response_bytes}-byte limit")
            if b"\n" in response or b"\r" in response:
                break
    try:
        text = bytes(response).decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise ValueError(f"{command} returned non-ASCII data") from error
    if not text:
        raise ValueError(f"{command} returned an empty response")
    return text.splitlines()[0].strip()


def _query_definite_block(
    host: str,
    port: int,
    command: str,
    *,
    connect_timeout_s: float,
    response_timeout_s: float,
    max_payload_bytes: int,
    on_sent: Callable[[], None],
) -> DefiniteBlock:
    deadline = time.monotonic() + response_timeout_s
    with socket.create_connection((host, port), timeout=connect_timeout_s) as connection:
        connection.sendall(command.encode("ascii") + b"\n")
        on_sent()
        marker = _recv_exact(connection, 1, deadline=deadline)
        if marker != b"#":
            raise ValueError(f"{command} did not return a definite block")
        digit_count_raw = _recv_exact(connection, 1, deadline=deadline)
        if not digit_count_raw.isdigit() or digit_count_raw == b"0":
            raise ValueError(f"{command} returned an invalid block descriptor")
        digit_count = int(digit_count_raw)
        declared_raw = _recv_exact(connection, digit_count, deadline=deadline)
        if not declared_raw.isdigit():
            raise ValueError(f"{command} returned an invalid payload length")
        declared_bytes = int(declared_raw)
        if not 0 <= declared_bytes <= max_payload_bytes:
            raise ValueError(
                f"{command} payload length {declared_bytes} is outside the probe limit"
            )
        payload = _recv_exact(connection, declared_bytes, deadline=deadline)
    return DefiniteBlock(
        header=marker + digit_count_raw + declared_raw,
        payload=payload,
    )


def _normalize_screenshot(payload: bytes) -> tuple[bytes, str]:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return payload, "png"
    if payload.startswith(b"\xff\xd8\xff"):
        return payload, "jpeg"
    if len(payload) >= 10 and payload.startswith(b"\xff\xd8") and payload[4:10] == b"\x00\x10JFIF":
        repaired = payload[:2] + b"\xff\xe0" + payload[4:]
        return repaired, "jpeg"
    raise ValueError(f"Unknown screenshot signature: {payload[:16].hex(' ')}")


def _parse_measurement_value(response: str) -> float | None:
    normalized = response.strip()
    if normalized in {"", "--", "---", "*****"} or normalized.startswith("Error:"):
        return None
    match = _SCPI_NUMBER_RE.match(normalized)
    if match is None:
        return None
    value = float(match.group(1))
    return value if math.isfinite(value) else None


def _write_measurements_csv(path: Path, measurements: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(
            (
                "channel",
                "measurement",
                "scpi_name",
                "value",
                "unit",
                "status",
                "raw_response",
                "query_elapsed_s",
            )
        )
        for item in measurements:
            writer.writerow(
                (
                    item["channel"],
                    item["measurement"],
                    item["scpi_name"],
                    "" if item["value"] is None else format(item["value"], ".17g"),
                    item["unit"],
                    item["status"],
                    item["raw_response"],
                    format(item["query_elapsed_s"], ".17g"),
                )
            )


def _parse_preamble(text: str) -> WaveformPreamble:
    fields = [field.strip() for field in text.split(",")]
    if len(fields) != 9:
        raise ValueError(f"Waveform preamble has {len(fields)} fields instead of 9")
    try:
        integer_fields = [int(fields[index]) for index in range(3)]
        real_fields = [float(fields[index]) for index in range(3, 9)]
    except ValueError as error:
        raise ValueError("Waveform preamble contains an invalid number") from error
    if not all(math.isfinite(value) for value in real_fields):
        raise ValueError("Waveform preamble contains a non-finite number")
    if real_fields[0] <= 0:
        raise ValueError("Waveform preamble X increment must be positive")
    return WaveformPreamble(*integer_fields, *real_fields)


def _write_timed_csv(
    path: Path,
    values: tuple[float, ...],
    *,
    source: str,
    preamble: WaveformPreamble,
) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("sample_index", "time_s", f"{source.lower()}_v"))
        for index, value in enumerate(values):
            writer.writerow((index, format(preamble.time_at(index), ".17g"), format(value, ".17g")))


def run_four_channel_probe(
    host: str,
    *,
    port: int = SCPI_PORT,
    output_root: Path = Path(".openbench/data/captures/mho1-ascii-four-channel"),
    connect_timeout_s: float = CONNECT_TIMEOUT_S,
    block_timeout_s: float = BLOCK_TIMEOUT_S,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    measurement_clear_delay_s: float = MEASUREMENT_CLEAR_DELAY_S,
    measurement_open_delay_s: float = MEASUREMENT_OPEN_DELAY_S,
    measurement_ready_delay_s: float = MEASUREMENT_READY_DELAY_S,
    screenshot_min_interval_s: float = SCREENSHOT_MIN_INTERVAL_S,
    configure_measurements: bool = True,
) -> FourChannelOutcome:
    if not host.strip():
        raise ValueError("MHO1 host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("TCP port must be between 1 and 65535")
    if connect_timeout_s <= 0 or block_timeout_s <= 0:
        raise ValueError("Probe timeouts must be positive")
    if max_response_bytes <= 0:
        raise ValueError("Maximum response size must be positive")
    if (
        min(
            measurement_clear_delay_s,
            measurement_open_delay_s,
            measurement_ready_delay_s,
            screenshot_min_interval_s,
        )
        < 0
    ):
        raise ValueError("Measurement and screenshot pacing delays must not be negative")

    transaction_started_monotonic = time.monotonic()
    started_at = _utc_now()
    session_name = started_at.strftime("%Y%m%dT%H%M%S_%fZ")
    session_directory = output_root / session_name
    session_directory.mkdir(parents=True, exist_ok=False)
    metadata_path = session_directory / "transaction.json"

    sent_commands: list[dict[str, str]] = []
    channel_results: dict[str, dict[str, Any]] = {}
    sample_counts: dict[str, int] = {}
    capture_error: str | None = None
    preamble_error: str | None = None
    screenshot_error: str | None = None
    measurement_error: str | None = None
    run_error: str | None = None
    screenshot_block: DefiniteBlock | None = None
    screenshot_format: str | None = None
    screenshot_query_started_monotonic: float | None = None
    screenshot_data_sent_monotonic: float | None = None
    screenshot_response_completed_monotonic: float | None = None
    screenshot_phase_started_monotonic: float | None = None
    screenshot_phase_completed_monotonic: float | None = None
    screenshot_attempts: list[dict[str, Any]] = []
    measurement_configuration_started_monotonic: float | None = None
    measurement_configuration_completed_monotonic: float | None = None
    measurement_read_started_monotonic: float | None = None
    measurement_read_completed_monotonic: float | None = None
    measurements: list[dict[str, Any]] = []
    preamble: WaveformPreamble | None = None
    preamble_text: str | None = None
    preamble_query_started_monotonic: float | None = None
    preamble_data_sent_monotonic: float | None = None
    preamble_response_completed_monotonic: float | None = None
    read_phase_started_monotonic: float | None = None
    read_phase_completed_monotonic: float | None = None

    try:
        _send_line(host, port, STOP_COMMAND, timeout_s=connect_timeout_s)
        sent_commands.append({"command": STOP_COMMAND, "sent_at_utc": _timestamp(_utc_now())})
        read_phase_started_monotonic = time.monotonic()

        for source in SOURCES:
            channel_started_monotonic = time.monotonic()
            source_command = SOURCE_COMMANDS[source]
            result: dict[str, Any] = {
                "capture_error": None,
                "capture": None,
                "artifacts": {},
                "timing": {},
            }
            channel_results[source] = result
            block: AsciiBlock | None = None
            values: tuple[float, ...] = ()
            source_sent_monotonic: float | None = None
            ascii_query_started_monotonic: float | None = None
            ascii_data_sent_monotonic: float | None = None
            ascii_response_completed_monotonic: float | None = None

            try:
                _send_line(host, port, source_command, timeout_s=connect_timeout_s)
                source_sent_monotonic = time.monotonic()
                sent_commands.append(
                    {
                        "command": source_command,
                        "source": source,
                        "sent_at_utc": _timestamp(_utc_now()),
                    }
                )

                _send_line(host, port, NORMAL_MODE_COMMAND, timeout_s=connect_timeout_s)
                sent_commands.append(
                    {
                        "command": NORMAL_MODE_COMMAND,
                        "source": source,
                        "sent_at_utc": _timestamp(_utc_now()),
                    }
                )

                if source == "CH1":

                    def record_preamble_sent(selected_source: str = source) -> None:
                        nonlocal preamble_data_sent_monotonic
                        preamble_data_sent_monotonic = time.monotonic()
                        sent_commands.append(
                            {
                                "command": PREAMBLE_QUERY,
                                "source": selected_source,
                                "sent_at_utc": _timestamp(_utc_now()),
                            }
                        )

                    try:
                        preamble_query_started_monotonic = time.monotonic()
                        preamble_text = _query_text(
                            host,
                            port,
                            PREAMBLE_QUERY,
                            connect_timeout_s=connect_timeout_s,
                            response_timeout_s=min(block_timeout_s, 2.0),
                            max_response_bytes=MAX_PREAMBLE_BYTES,
                            on_sent=record_preamble_sent,
                        )
                        preamble_response_completed_monotonic = time.monotonic()
                        preamble = _parse_preamble(preamble_text)
                        preamble_path = session_directory / "preamble.txt"
                        preamble_path.write_text(preamble_text + "\n", encoding="ascii")
                    except Exception as error:
                        preamble_error = _exception_text(error)

                def record_ascii_sent(selected_source: str = source) -> None:
                    nonlocal ascii_data_sent_monotonic
                    ascii_data_sent_monotonic = time.monotonic()
                    sent_commands.append(
                        {
                            "command": ASCII_QUERY,
                            "source": selected_source,
                            "sent_at_utc": _timestamp(_utc_now()),
                        }
                    )

                ascii_query_started_monotonic = time.monotonic()
                block = _query_ascii_block(
                    host,
                    port,
                    connect_timeout_s=connect_timeout_s,
                    block_timeout_s=block_timeout_s,
                    max_response_bytes=max_response_bytes,
                    on_sent=record_ascii_sent,
                )
                ascii_response_completed_monotonic = time.monotonic()

                raw_path = session_directory / f"{source.lower()}.raw"
                csv_path = session_directory / f"{source.lower()}.csv"
                raw_path.write_bytes(block.raw)
                values = _parse_values(block.payload, expected_points=block.declared_points)
                if preamble is not None:
                    _write_timed_csv(csv_path, values, source=source, preamble=preamble)
                else:
                    _write_csv(csv_path, values, value_column=f"{source.lower()}_v")
                result["artifacts"] = {
                    "raw": _artifact_metadata(raw_path),
                    "csv": _artifact_metadata(csv_path),
                }
                sample_counts[source] = len(values)
            except Exception as error:
                result["capture_error"] = _exception_text(error)
                sample_counts[source] = 0
                if block is not None:
                    raw_path = session_directory / f"{source.lower()}.raw"
                    if not raw_path.exists():
                        raw_path.write_bytes(block.raw)
                    result["artifacts"] = {"raw": _artifact_metadata(raw_path)}
            finally:
                channel_completed_monotonic = time.monotonic()
                timing = result["timing"]
                timing["source_to_channel_complete_s"] = (
                    channel_completed_monotonic - source_sent_monotonic
                    if source_sent_monotonic is not None
                    else None
                )
                timing["ascii_connect_send_receive_s"] = (
                    ascii_response_completed_monotonic - ascii_query_started_monotonic
                    if ascii_query_started_monotonic is not None
                    and ascii_response_completed_monotonic is not None
                    else None
                )
                timing["ascii_data_sent_to_response_complete_s"] = (
                    ascii_response_completed_monotonic - ascii_data_sent_monotonic
                    if ascii_data_sent_monotonic is not None
                    and ascii_response_completed_monotonic is not None
                    else None
                )
                timing["channel_sequence_s"] = (
                    channel_completed_monotonic - channel_started_monotonic
                )
                if block is not None:
                    result["capture"] = {
                        "header_ascii": block.header.decode("ascii"),
                        "declared_points": block.declared_points,
                        "payload_bytes": len(block.payload),
                        "parsed_points": len(values),
                        "minimum_v": min(values) if values else None,
                        "maximum_v": max(values) if values else None,
                    }

        read_phase_completed_monotonic = time.monotonic()

        if configure_measurements:
            try:
                measurement_configuration_started_monotonic = time.monotonic()
                _send_line(host, port, MEASUREMENT_CLEAR_COMMAND, timeout_s=connect_timeout_s)
                sent_commands.append(
                    {
                        "command": MEASUREMENT_CLEAR_COMMAND,
                        "sent_at_utc": _timestamp(_utc_now()),
                    }
                )
                time.sleep(measurement_clear_delay_s)
                for (
                    (source, measurement_name, scpi_name, _unit),
                    open_command,
                ) in zip(MEASUREMENT_PROFILE, MEASUREMENT_OPEN_COMMANDS, strict=True):
                    _send_line(host, port, open_command, timeout_s=connect_timeout_s)
                    sent_commands.append(
                        {
                            "command": open_command,
                            "source": source,
                            "measurement": measurement_name,
                            "scpi_name": scpi_name,
                            "sent_at_utc": _timestamp(_utc_now()),
                        }
                    )
                    time.sleep(measurement_open_delay_s)
                time.sleep(measurement_ready_delay_s)
                measurement_configuration_completed_monotonic = time.monotonic()
            except Exception as error:
                measurement_error = _exception_text(error)

        screenshot_phase_started_monotonic = time.monotonic()
        previous_screenshot_sent_monotonic: float | None = None
        for screenshot_attempt_number in range(1, SCREENSHOT_MAX_ATTEMPTS + 1):
            if previous_screenshot_sent_monotonic is not None:
                elapsed_since_previous_send = time.monotonic() - previous_screenshot_sent_monotonic
                time.sleep(max(0.0, screenshot_min_interval_s - elapsed_since_previous_send))

            attempt_query_started_monotonic = time.monotonic()
            attempt_data_sent_monotonic: float | None = None
            attempt_response_completed_monotonic: float | None = None
            attempt_block: DefiniteBlock | None = None

            def record_screenshot_sent(
                selected_attempt_number: int = screenshot_attempt_number,
            ) -> None:
                nonlocal attempt_data_sent_monotonic
                nonlocal screenshot_data_sent_monotonic
                attempt_data_sent_monotonic = time.monotonic()
                screenshot_data_sent_monotonic = attempt_data_sent_monotonic
                sent_commands.append(
                    {
                        "command": SCREENSHOT_QUERY,
                        "attempt": str(selected_attempt_number),
                        "sent_at_utc": _timestamp(_utc_now()),
                    }
                )

            attempt_error: str | None = None
            try:
                attempt_block = _query_definite_block(
                    host,
                    port,
                    SCREENSHOT_QUERY,
                    connect_timeout_s=connect_timeout_s,
                    response_timeout_s=block_timeout_s,
                    max_payload_bytes=MAX_SCREENSHOT_BYTES,
                    on_sent=record_screenshot_sent,
                )
                attempt_response_completed_monotonic = time.monotonic()
                if not attempt_block.payload:
                    raise ValueError(f"{SCREENSHOT_QUERY} returned an empty payload")
                normalized_screenshot, attempt_format = _normalize_screenshot(attempt_block.payload)
                screenshot_block = attempt_block
                screenshot_format = attempt_format
                screenshot_query_started_monotonic = attempt_query_started_monotonic
                screenshot_response_completed_monotonic = attempt_response_completed_monotonic
                (session_directory / "screenshot.scpi").write_bytes(screenshot_block.raw)
                (session_directory / "screenshot_payload.raw").write_bytes(screenshot_block.payload)
                screenshot_extension = "png" if screenshot_format == "png" else "jpg"
                (session_directory / f"screenshot.{screenshot_extension}").write_bytes(
                    normalized_screenshot
                )
            except Exception as error:
                if attempt_response_completed_monotonic is None:
                    attempt_response_completed_monotonic = time.monotonic()
                attempt_error = _exception_text(error)

            screenshot_attempts.append(
                {
                    "attempt": screenshot_attempt_number,
                    "status": "ok" if attempt_error is None else "failed",
                    "error": attempt_error,
                    "declared_payload_bytes": (
                        len(attempt_block.payload) if attempt_block is not None else None
                    ),
                    "connect_send_receive_s": (
                        attempt_response_completed_monotonic - attempt_query_started_monotonic
                    ),
                    "data_sent_to_response_complete_s": (
                        attempt_response_completed_monotonic - attempt_data_sent_monotonic
                        if attempt_data_sent_monotonic is not None
                        else None
                    ),
                }
            )
            previous_screenshot_sent_monotonic = attempt_data_sent_monotonic
            if attempt_error is None:
                screenshot_error = None
                break
            screenshot_error = attempt_error
        screenshot_phase_completed_monotonic = time.monotonic()

        if measurement_error is None:
            measurement_read_started_monotonic = time.monotonic()
            for (
                source,
                measurement_name,
                scpi_name,
                unit,
            ), query_command in zip(
                MEASUREMENT_PROFILE,
                MEASUREMENT_QUERY_COMMANDS,
                strict=True,
            ):
                query_started_monotonic = time.monotonic()

                def record_measurement_sent(
                    selected_source: str = source,
                    selected_measurement: str = measurement_name,
                    selected_scpi_name: str = scpi_name,
                    selected_command: str = query_command,
                ) -> None:
                    sent_commands.append(
                        {
                            "command": selected_command,
                            "source": selected_source,
                            "measurement": selected_measurement,
                            "scpi_name": selected_scpi_name,
                            "sent_at_utc": _timestamp(_utc_now()),
                        }
                    )

                raw_response = ""
                query_error: str | None = None
                value: float | None = None
                try:
                    raw_response = _query_text(
                        host,
                        port,
                        query_command,
                        connect_timeout_s=connect_timeout_s,
                        response_timeout_s=min(block_timeout_s, 2.0),
                        max_response_bytes=MAX_PREAMBLE_BYTES,
                        on_sent=record_measurement_sent,
                    )
                    value = _parse_measurement_value(raw_response)
                except Exception as error:
                    query_error = _exception_text(error)
                query_elapsed_s = time.monotonic() - query_started_monotonic
                status = "ok" if value is not None and query_error is None else "unavailable"
                measurements.append(
                    {
                        "channel": source,
                        "measurement": measurement_name,
                        "scpi_name": scpi_name,
                        "value": value,
                        "unit": unit,
                        "status": status,
                        "raw_response": raw_response,
                        "query_error": query_error,
                        "query_elapsed_s": query_elapsed_s,
                    }
                )
            measurement_read_completed_monotonic = time.monotonic()
            measurements_path = session_directory / "measurements.csv"
            _write_measurements_csv(measurements_path, measurements)
            unavailable_count = sum(item["status"] != "ok" for item in measurements)
            if unavailable_count:
                measurement_error = (
                    f"{unavailable_count} of {len(MEASUREMENT_PROFILE)} measurements unavailable"
                )
    except Exception as error:
        capture_error = _exception_text(error)
    finally:
        try:
            _send_line(host, port, RUN_COMMAND, timeout_s=connect_timeout_s)
            sent_commands.append({"command": RUN_COMMAND, "sent_at_utc": _timestamp(_utc_now())})
        except Exception as error:
            run_error = _exception_text(error)

    transaction_completed_monotonic = time.monotonic()
    completed_at = _utc_now()
    successful_transfer_times = [
        result["timing"]["ascii_data_sent_to_response_complete_s"]
        for result in channel_results.values()
        if result["timing"]["ascii_data_sent_to_response_complete_s"] is not None
    ]
    preamble_metadata: dict[str, Any] | None = None
    if preamble is not None and preamble_text is not None:
        preamble_path = session_directory / "preamble.txt"
        preamble_metadata = {
            "raw_text": preamble_text,
            "queried_source": "CH1",
            "x_calibration_applies_to_sources": list(SOURCES),
            "y_calibration_source": "CH1",
            "format_code": preamble.format_code,
            "mode_code": preamble.mode_code,
            "count": preamble.count,
            "x_increment_s": preamble.x_increment_s,
            "x_origin_s": preamble.x_origin_s,
            "x_reference": preamble.x_reference,
            "y_increment": preamble.y_increment,
            "y_origin": preamble.y_origin,
            "y_reference": preamble.y_reference,
            "artifact": _artifact_metadata(preamble_path),
        }
    screenshot_metadata: dict[str, Any] = {
        "max_attempts": SCREENSHOT_MAX_ATTEMPTS,
        "minimum_interval_s": screenshot_min_interval_s,
        "attempt_count": len(screenshot_attempts),
        "retry_count": max(0, len(screenshot_attempts) - 1),
        "attempts": screenshot_attempts,
        "format": None,
        "block_header_ascii": None,
        "declared_payload_bytes": None,
        "artifacts": None,
    }
    if screenshot_block is not None and screenshot_format is not None:
        screenshot_extension = "png" if screenshot_format == "png" else "jpg"
        screenshot_metadata.update(
            {
                "format": screenshot_format,
                "block_header_ascii": screenshot_block.header.decode("ascii"),
                "declared_payload_bytes": len(screenshot_block.payload),
                "artifacts": {
                    "scpi_block": _artifact_metadata(session_directory / "screenshot.scpi"),
                    "raw_payload": _artifact_metadata(session_directory / "screenshot_payload.raw"),
                    "image": _artifact_metadata(
                        session_directory / f"screenshot.{screenshot_extension}"
                    ),
                },
            }
        )
    measurements_metadata: dict[str, Any] = {
        "configured_in_this_transaction": configure_measurements,
        "profile": [
            {
                "channel": source,
                "measurement": measurement_name,
                "scpi_name": scpi_name,
                "unit": unit,
            }
            for source, measurement_name, scpi_name, unit in MEASUREMENT_PROFILE
        ],
        "configured_slots": len(MEASUREMENT_OPEN_COMMANDS),
        "returned_values": len(measurements),
        "available_values": sum(item["status"] == "ok" for item in measurements),
        "values": measurements,
        "pacing_s": {
            "after_clear": measurement_clear_delay_s,
            "after_each_open": measurement_open_delay_s,
            "before_capture": measurement_ready_delay_s,
        },
        "artifact": (
            _artifact_metadata(session_directory / "measurements.csv")
            if (session_directory / "measurements.csv").exists()
            else None
        ),
    }
    selected_command_contract = (
        COMMAND_CONTRACT if configure_measurements else FRAME_COMMAND_CONTRACT
    )
    actual_command_contract = list(selected_command_contract)
    screenshot_contract_index = actual_command_contract.index(SCREENSHOT_QUERY)
    for _ in range(max(0, len(screenshot_attempts) - 1)):
        actual_command_contract.insert(screenshot_contract_index + 1, SCREENSHOT_QUERY)
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "probe_kind": "mho1_ascii_four_channel",
        "started_at_utc": _timestamp(started_at),
        "completed_at_utc": _timestamp(completed_at),
        "target": {"host": host, "port": port},
        "command_contract": actual_command_contract,
        "base_command_contract": list(selected_command_contract),
        "conditional_behavior": {
            "screenshot_max_attempts": SCREENSHOT_MAX_ATTEMPTS,
            "screenshot_repeat_only_after_failure": True,
        },
        "sent_commands": sent_commands,
        "sources": list(SOURCES),
        "selected_mode": "NORMAL",
        "post_stop_delay_s": 0.0,
        "automatic_retries": max(0, len(screenshot_attempts) - 1),
        "capture_error": capture_error,
        "preamble_error": preamble_error,
        "screenshot_error": screenshot_error,
        "measurement_error": measurement_error,
        "run_error": run_error,
        "preamble": preamble_metadata,
        "screenshot": screenshot_metadata,
        "measurements": measurements_metadata,
        "timing": {
            "total_transaction_s": (
                transaction_completed_monotonic - transaction_started_monotonic
            ),
            "four_channel_read_phase_s": (
                read_phase_completed_monotonic - read_phase_started_monotonic
                if read_phase_started_monotonic is not None
                and read_phase_completed_monotonic is not None
                else None
            ),
            "sum_ascii_data_transfer_s": sum(successful_transfer_times),
            "preamble_connect_send_receive_s": (
                preamble_response_completed_monotonic - preamble_query_started_monotonic
                if preamble_query_started_monotonic is not None
                and preamble_response_completed_monotonic is not None
                else None
            ),
            "preamble_data_sent_to_response_complete_s": (
                preamble_response_completed_monotonic - preamble_data_sent_monotonic
                if preamble_data_sent_monotonic is not None
                and preamble_response_completed_monotonic is not None
                else None
            ),
            "measurement_configuration_s": (
                measurement_configuration_completed_monotonic
                - measurement_configuration_started_monotonic
                if measurement_configuration_started_monotonic is not None
                and measurement_configuration_completed_monotonic is not None
                else None
            ),
            "screenshot_phase_s": (
                screenshot_phase_completed_monotonic - screenshot_phase_started_monotonic
                if screenshot_phase_started_monotonic is not None
                and screenshot_phase_completed_monotonic is not None
                else None
            ),
            "screenshot_connect_send_receive_s": (
                screenshot_response_completed_monotonic - screenshot_query_started_monotonic
                if screenshot_query_started_monotonic is not None
                and screenshot_response_completed_monotonic is not None
                else None
            ),
            "screenshot_data_sent_to_response_complete_s": (
                screenshot_response_completed_monotonic - screenshot_data_sent_monotonic
                if screenshot_data_sent_monotonic is not None
                and screenshot_response_completed_monotonic is not None
                else None
            ),
            "measurement_read_s": (
                measurement_read_completed_monotonic - measurement_read_started_monotonic
                if measurement_read_started_monotonic is not None
                and measurement_read_completed_monotonic is not None
                else None
            ),
            "sum_measurement_query_s": sum(item["query_elapsed_s"] for item in measurements),
        },
        "channels": channel_results,
    }
    _write_metadata(metadata_path, metadata)

    all_channels_succeeded = (
        len(channel_results) == len(SOURCES)
        and all(result["capture_error"] is None for result in channel_results.values())
        and all(sample_counts.get(source, 0) > 0 for source in SOURCES)
    )
    success = (
        capture_error is None
        and preamble_error is None
        and preamble is not None
        and screenshot_error is None
        and screenshot_metadata is not None
        and measurement_error is None
        and len(measurements) == len(MEASUREMENT_PROFILE)
        and all(item["status"] == "ok" for item in measurements)
        and run_error is None
        and all_channels_succeeded
    )
    return FourChannelOutcome(
        session_directory=session_directory,
        metadata_path=metadata_path,
        success=success,
        sample_counts=sample_counts,
        capture_error=capture_error,
        preamble_error=preamble_error,
        screenshot_error=screenshot_error,
        measurement_error=measurement_error,
        run_error=run_error,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read CH1-CH4 ASCII sequentially inside one zero-delay STOP/RUN transaction."
    )
    parser.add_argument("--host", required=True, help="MHO1 IPv4 address or hostname")
    parser.add_argument("--port", type=int, default=SCPI_PORT, help="SCPI TCP port")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(".openbench/data/captures/mho1-ascii-four-channel"),
        help="Directory for per-channel raw/CSV files and transaction metadata",
    )
    return parser


def main() -> int:
    arguments = _build_parser().parse_args()
    outcome = run_four_channel_probe(
        arguments.host,
        port=arguments.port,
        output_root=arguments.output_root,
    )
    print(
        json.dumps(
            {
                "success": outcome.success,
                "sample_counts": outcome.sample_counts,
                "session_directory": str(outcome.session_directory.resolve()),
                "metadata": str(outcome.metadata_path.resolve()),
                "capture_error": outcome.capture_error,
                "preamble_error": outcome.preamble_error,
                "screenshot_error": outcome.screenshot_error,
                "measurement_error": outcome.measurement_error,
                "run_error": outcome.run_error,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if outcome.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
