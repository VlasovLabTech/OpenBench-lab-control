"""Run one minimal MHO1 fast-ASCII transaction.

The oscilloscope is assumed to be in RUN and to have the intended waveform mode
selected already.  The default SCPI commands emitted are, in order:

1. ``:MENU:STOP``
2. ``:WAVeform:DATA:ASCii?``
3. ``:MENU:RUN``

Each command uses a fresh TCP connection.  This intentionally does not import
the OpenBench application or its MHO1 driver.  An explicitly requested CH1-CH4
source adds one fixed ``:WAVeform:SOURce`` write between STOP and the query,
without read-back.  An explicitly requested NORMAL waveform mode adds one fixed
``:WAVeform:MODE NORMal`` write after SOURCE and before the query.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCPI_PORT = 5025
CONNECT_TIMEOUT_S = 1.0
BLOCK_TIMEOUT_S = 60.0
POST_STOP_DELAY_S = 0.1
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_DECLARED_POINTS = 1_000_000

STOP_COMMAND = ":MENU:STOP"
ASCII_QUERY = ":WAVeform:DATA:ASCii?"
RUN_COMMAND = ":MENU:RUN"
COMMAND_CONTRACT = (STOP_COMMAND, ASCII_QUERY, RUN_COMMAND)
ANALOG_SOURCES = ("CH1", "CH2", "CH3", "CH4")
WAVEFORM_MODES = ("NORMAL",)
NORMAL_MODE_COMMAND = ":WAVeform:MODE NORMal"

_NUMBER_BYTES = frozenset(b"0123456789+-.eE")
_DELIMITER_BYTES = frozenset(b", \t\r\n")


class ProbeError(RuntimeError):
    """A bounded MHO1 probe operation failed."""


@dataclass(frozen=True, slots=True)
class AsciiBlock:
    header: bytes
    payload: bytes
    declared_points: int

    @property
    def raw(self) -> bytes:
        return self.header + self.payload


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    session_directory: Path
    metadata_path: Path
    success: bool
    sample_count: int
    capture_error: str | None
    run_error: str | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _exception_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _send_line(host: str, port: int, command: str, *, timeout_s: float) -> None:
    if "\n" in command or "\r" in command:
        raise ValueError("SCPI command must be a single line")
    with socket.create_connection((host, port), timeout=timeout_s) as connection:
        connection.settimeout(timeout_s)
        connection.sendall(command.encode("ascii") + b"\n")


def _recv_exact(connection: socket.socket, size: int, *, deadline: float) -> bytes:
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Timed out after receiving {len(result)} of {size} bytes")
        connection.settimeout(remaining)
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise ProbeError(f"MHO1 closed the connection after {len(result)} of {size} bytes")
        result.extend(chunk)
    return bytes(result)


def _read_point_count_ascii_block(
    connection: socket.socket,
    *,
    timeout_s: float,
    max_response_bytes: int,
) -> AsciiBlock:
    """Read the point-count block emitted by MHO14-200 firmware 2.154.75.

    The firmware places its point count in the SCPI definite-block length field
    and may append inconsistent padding.  Stop at the delimiter completing the
    declared final numeric token; the caller then closes this connection.
    """

    deadline = time.monotonic() + timeout_s
    marker = _recv_exact(connection, 1, deadline=deadline)
    if marker != b"#":
        raise ProbeError(f"Expected '#' block marker, received {marker!r}")

    digit_count_raw = _recv_exact(connection, 1, deadline=deadline)
    if not digit_count_raw.isdigit() or digit_count_raw == b"0":
        raise ProbeError(f"Invalid block length descriptor: {digit_count_raw!r}")
    digit_count = int(digit_count_raw)

    declared_raw = _recv_exact(connection, digit_count, deadline=deadline)
    if not declared_raw.isdigit():
        raise ProbeError(f"Invalid declared point count: {declared_raw!r}")
    declared_points = int(declared_raw)
    if not 0 <= declared_points <= MAX_DECLARED_POINTS:
        raise ProbeError(f"Declared point count is outside the probe limit: {declared_points}")

    header = marker + digit_count_raw + declared_raw
    if declared_points == 0:
        return AsciiBlock(header=header, payload=b"", declared_points=0)

    payload = bytearray()
    completed_points = 0
    token_open = False

    while completed_points < declared_points:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Timed out after {completed_points} of {declared_points} ASCII points"
            )
        connection.settimeout(remaining)
        try:
            chunk = connection.recv(64 * 1024)
        except TimeoutError:
            if token_open and completed_points + 1 == declared_points:
                completed_points += 1
                break
            raise
        if not chunk:
            if token_open and completed_points + 1 == declared_points:
                completed_points += 1
                break
            raise ProbeError(
                "MHO1 closed the ASCII connection after "
                f"{completed_points} of {declared_points} points"
            )

        for value in chunk:
            payload.append(value)
            if len(payload) > max_response_bytes:
                raise ProbeError(
                    f"ASCII waveform exceeded the {max_response_bytes}-byte safety limit"
                )
            if value in _NUMBER_BYTES:
                token_open = True
            elif value in _DELIMITER_BYTES:
                if token_open:
                    completed_points += 1
                    token_open = False
                    if completed_points == declared_points:
                        break
            else:
                raise ProbeError(f"Invalid byte in ASCII waveform: 0x{value:02x}")

    if completed_points != declared_points:
        raise ProbeError(f"Parsed {completed_points} points, expected exactly {declared_points}")
    return AsciiBlock(
        header=header,
        payload=bytes(payload),
        declared_points=declared_points,
    )


def _query_ascii_block(
    host: str,
    port: int,
    *,
    connect_timeout_s: float,
    block_timeout_s: float,
    max_response_bytes: int,
    on_sent: Callable[[], None],
) -> AsciiBlock:
    with socket.create_connection((host, port), timeout=connect_timeout_s) as connection:
        connection.settimeout(block_timeout_s)
        connection.sendall(ASCII_QUERY.encode("ascii") + b"\n")
        on_sent()
        return _read_point_count_ascii_block(
            connection,
            timeout_s=block_timeout_s,
            max_response_bytes=max_response_bytes,
        )


def _parse_values(payload: bytes, *, expected_points: int) -> tuple[float, ...]:
    if expected_points == 0:
        raise ProbeError("MHO1 ASCII waveform block declared zero points")
    try:
        text = payload.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise ProbeError("ASCII waveform contains non-ASCII data") from error
    fields = tuple(field for field in text.replace(",", " ").split() if field)
    if len(fields) != expected_points:
        raise ProbeError(f"ASCII payload contains {len(fields)} values, expected {expected_points}")
    try:
        values = tuple(float(field) for field in fields)
    except ValueError as error:
        raise ProbeError("ASCII waveform contains an invalid number") from error
    if not all(math.isfinite(value) for value in values):
        raise ProbeError("ASCII waveform contains a non-finite number")
    return values


def _write_csv(path: Path, values: tuple[float, ...], *, value_column: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("sample_index", value_column))
        for index, value in enumerate(values):
            writer.writerow((index, format(value, ".17g")))


def _write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_probe(
    host: str,
    *,
    port: int = SCPI_PORT,
    output_root: Path = Path(".openbench/data/captures/mho1-ascii-minimal"),
    connect_timeout_s: float = CONNECT_TIMEOUT_S,
    block_timeout_s: float = BLOCK_TIMEOUT_S,
    post_stop_delay_s: float = POST_STOP_DELAY_S,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    source: str | None = None,
    mode: str | None = None,
) -> ProbeOutcome:
    if not host.strip():
        raise ValueError("MHO1 host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("TCP port must be between 1 and 65535")
    if connect_timeout_s <= 0 or block_timeout_s <= 0:
        raise ValueError("Probe timeouts must be positive")
    if post_stop_delay_s < 0:
        raise ValueError("Post-STOP delay must not be negative")
    if max_response_bytes <= 0:
        raise ValueError("Maximum response size must be positive")
    normalized_source = source.strip().upper() if source is not None else None
    if normalized_source is not None and normalized_source not in ANALOG_SOURCES:
        raise ValueError(f"MHO1 source must be one of: {', '.join(ANALOG_SOURCES)}")
    normalized_mode = mode.strip().upper() if mode is not None else None
    if normalized_mode is not None and normalized_mode not in WAVEFORM_MODES:
        raise ValueError(f"MHO1 waveform mode must be one of: {', '.join(WAVEFORM_MODES)}")
    source_command = (
        f":WAVeform:SOURce {normalized_source}" if normalized_source is not None else None
    )
    mode_command = NORMAL_MODE_COMMAND if normalized_mode == "NORMAL" else None
    command_contract_parts = [STOP_COMMAND]
    if source_command is not None:
        command_contract_parts.append(source_command)
    if mode_command is not None:
        command_contract_parts.append(mode_command)
    command_contract_parts.extend((ASCII_QUERY, RUN_COMMAND))
    command_contract = tuple(command_contract_parts)

    transaction_started_monotonic = time.monotonic()
    started_at = _utc_now()
    session_name = started_at.strftime("%Y%m%dT%H%M%S_%fZ")
    session_directory = output_root / session_name
    session_directory.mkdir(parents=True, exist_ok=False)
    metadata_path = session_directory / "transaction.json"
    raw_path = session_directory / "ascii.raw"
    csv_path = session_directory / "ascii.csv"

    sent_commands: list[dict[str, str]] = []
    block: AsciiBlock | None = None
    values: tuple[float, ...] = ()
    capture_error: str | None = None
    run_error: str | None = None
    stop_sent_monotonic: float | None = None
    ascii_query_started_monotonic: float | None = None
    ascii_data_sent_monotonic: float | None = None
    ascii_response_completed_monotonic: float | None = None

    def record_ascii_sent() -> None:
        nonlocal ascii_data_sent_monotonic
        ascii_data_sent_monotonic = time.monotonic()
        sent_commands.append({"command": ASCII_QUERY, "sent_at_utc": _timestamp(_utc_now())})

    try:
        _send_line(host, port, STOP_COMMAND, timeout_s=connect_timeout_s)
        stop_sent_monotonic = time.monotonic()
        sent_commands.append({"command": STOP_COMMAND, "sent_at_utc": _timestamp(_utc_now())})
        time.sleep(post_stop_delay_s)

        if source_command is not None:
            _send_line(host, port, source_command, timeout_s=connect_timeout_s)
            sent_commands.append({"command": source_command, "sent_at_utc": _timestamp(_utc_now())})

        if mode_command is not None:
            _send_line(host, port, mode_command, timeout_s=connect_timeout_s)
            sent_commands.append({"command": mode_command, "sent_at_utc": _timestamp(_utc_now())})

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
        raw_path.write_bytes(block.raw)
        values = _parse_values(block.payload, expected_points=block.declared_points)
        value_column = f"{normalized_source.lower()}_v" if normalized_source else "current_source_v"
        _write_csv(csv_path, values, value_column=value_column)
    except Exception as error:  # The RUN command below must survive every capture failure.
        capture_error = _exception_text(error)
    finally:
        try:
            _send_line(host, port, RUN_COMMAND, timeout_s=connect_timeout_s)
            sent_commands.append({"command": RUN_COMMAND, "sent_at_utc": _timestamp(_utc_now())})
        except Exception as error:
            run_error = _exception_text(error)

    transaction_completed_monotonic = time.monotonic()
    completed_at = _utc_now()
    total_transaction_s = transaction_completed_monotonic - transaction_started_monotonic
    timing: dict[str, float | None] = {
        "total_transaction_s": total_transaction_s,
        "stop_to_ascii_data_sent_s": (
            ascii_data_sent_monotonic - stop_sent_monotonic
            if stop_sent_monotonic is not None and ascii_data_sent_monotonic is not None
            else None
        ),
        "ascii_connect_send_receive_s": (
            ascii_response_completed_monotonic - ascii_query_started_monotonic
            if ascii_query_started_monotonic is not None
            and ascii_response_completed_monotonic is not None
            else None
        ),
        "ascii_data_sent_to_response_complete_s": (
            ascii_response_completed_monotonic - ascii_data_sent_monotonic
            if ascii_data_sent_monotonic is not None
            and ascii_response_completed_monotonic is not None
            else None
        ),
    }
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "started_at_utc": _timestamp(started_at),
        "completed_at_utc": _timestamp(completed_at),
        "elapsed_s": total_transaction_s,
        "timing": timing,
        "target": {"host": host, "port": port},
        "command_contract": list(command_contract),
        "sent_commands": sent_commands,
        "selected_source": normalized_source,
        "selected_mode": normalized_mode,
        "post_stop_delay_s": post_stop_delay_s,
        "automatic_retries": 0,
        "header_interpretation": "mho14-200_firmware_2.154.75_point_count",
        "capture_error": capture_error,
        "run_error": run_error,
        "capture": None,
        "artifacts": {},
    }
    if block is not None:
        metadata["capture"] = {
            "header_ascii": block.header.decode("ascii"),
            "header_hex": block.header.hex(" "),
            "declared_points": block.declared_points,
            "payload_bytes": len(block.payload),
            "parsed_points": len(values),
            "minimum_v": min(values) if values else None,
            "maximum_v": max(values) if values else None,
        }
    artifacts = metadata["artifacts"]
    if isinstance(artifacts, dict):
        if raw_path.exists():
            artifacts["raw"] = {
                "filename": raw_path.name,
                "bytes": raw_path.stat().st_size,
                "sha256": _sha256(raw_path),
            }
        if csv_path.exists():
            artifacts["csv"] = {
                "filename": csv_path.name,
                "bytes": csv_path.stat().st_size,
                "sha256": _sha256(csv_path),
            }
    _write_metadata(metadata_path, metadata)

    success = capture_error is None and run_error is None and bool(values)
    return ProbeOutcome(
        session_directory=session_directory,
        metadata_path=metadata_path,
        success=success,
        sample_count=len(values),
        capture_error=capture_error,
        run_error=run_error,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run bounded STOP -> optional SOURCE -> optional MODE -> DATA:ASCii? -> RUN on an MHO1."
        )
    )
    parser.add_argument("--host", required=True, help="MHO1 IPv4 address or hostname")
    parser.add_argument("--port", type=int, default=SCPI_PORT, help="SCPI TCP port")
    parser.add_argument(
        "--source",
        choices=ANALOG_SOURCES,
        help="Write one analog waveform source before the ASCII query; no read-back is sent",
    )
    parser.add_argument(
        "--mode",
        choices=WAVEFORM_MODES,
        help="Write NORMAL waveform mode before the ASCII query; no read-back is sent",
    )
    parser.add_argument(
        "--post-stop-delay-s",
        type=float,
        default=POST_STOP_DELAY_S,
        help="Fixed host-side delay between STOP and the ASCII query",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(".openbench/data/captures/mho1-ascii-minimal"),
        help="Directory for raw, CSV, and transaction metadata artifacts",
    )
    return parser


def main() -> int:
    arguments = _build_parser().parse_args()
    outcome = run_probe(
        arguments.host,
        port=arguments.port,
        output_root=arguments.output_root,
        post_stop_delay_s=arguments.post_stop_delay_s,
        source=arguments.source,
        mode=arguments.mode,
    )
    print(
        json.dumps(
            {
                "success": outcome.success,
                "sample_count": outcome.sample_count,
                "session_directory": str(outcome.session_directory.resolve()),
                "metadata": str(outcome.metadata_path.resolve()),
                "capture_error": outcome.capture_error,
                "run_error": outcome.run_error,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if outcome.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
