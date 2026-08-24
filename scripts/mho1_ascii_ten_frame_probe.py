"""Configure MHO1 measurements once, then capture ten consecutive frames."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mho1_ascii_four_channel_probe import (
    BLOCK_TIMEOUT_S,
    CONNECT_TIMEOUT_S,
    MEASUREMENT_CLEAR_COMMAND,
    MEASUREMENT_CLEAR_DELAY_S,
    MEASUREMENT_OPEN_COMMANDS,
    MEASUREMENT_OPEN_DELAY_S,
    MEASUREMENT_PROFILE,
    MEASUREMENT_READY_DELAY_S,
    SCPI_PORT,
    _exception_text,
    _send_line,
    run_four_channel_probe,
)

FRAME_COUNT = 10
FRAME_LIMIT_S = 2.0


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _configure_measurements_once(
    host: str,
    *,
    port: int,
    connect_timeout_s: float,
) -> dict[str, Any]:
    started_at_utc = _timestamp()
    started_monotonic = time.monotonic()
    sent_commands: list[dict[str, str]] = []

    _send_line(host, port, MEASUREMENT_CLEAR_COMMAND, timeout_s=connect_timeout_s)
    sent_commands.append({"command": MEASUREMENT_CLEAR_COMMAND, "completed_at_utc": _timestamp()})
    time.sleep(MEASUREMENT_CLEAR_DELAY_S)
    for (source, measurement_name, scpi_name, _unit), command in zip(
        MEASUREMENT_PROFILE,
        MEASUREMENT_OPEN_COMMANDS,
        strict=True,
    ):
        _send_line(host, port, command, timeout_s=connect_timeout_s)
        sent_commands.append(
            {
                "command": command,
                "source": source,
                "measurement": measurement_name,
                "scpi_name": scpi_name,
                "completed_at_utc": _timestamp(),
            }
        )
        time.sleep(MEASUREMENT_OPEN_DELAY_S)
    time.sleep(MEASUREMENT_READY_DELAY_S)

    return {
        "started_at_utc": started_at_utc,
        "completed_at_utc": _timestamp(),
        "elapsed_s": time.monotonic() - started_monotonic,
        "fixed_delay_s": (
            MEASUREMENT_CLEAR_DELAY_S
            + len(MEASUREMENT_OPEN_COMMANDS) * MEASUREMENT_OPEN_DELAY_S
            + MEASUREMENT_READY_DELAY_S
        ),
        "pacing_s": {
            "after_clear": MEASUREMENT_CLEAR_DELAY_S,
            "after_each_open": MEASUREMENT_OPEN_DELAY_S,
            "after_final_open": MEASUREMENT_READY_DELAY_S,
        },
        "sent_commands": sent_commands,
    }


def run_ten_frame_probe(
    host: str,
    *,
    port: int = SCPI_PORT,
    output_root: Path = Path(".openbench/data/captures/mho1-ascii-ten-frame"),
    frame_count: int = FRAME_COUNT,
    frame_limit_s: float = FRAME_LIMIT_S,
    inter_frame_run_s: float = 0.0,
    connect_timeout_s: float = CONNECT_TIMEOUT_S,
    block_timeout_s: float = BLOCK_TIMEOUT_S,
) -> tuple[Path, dict[str, Any]]:
    if frame_count <= 0:
        raise ValueError("Frame count must be positive")
    if frame_limit_s <= 0:
        raise ValueError("Frame limit must be positive")
    if inter_frame_run_s < 0:
        raise ValueError("Inter-frame RUN interval must not be negative")

    batch_started_at = datetime.now(UTC)
    session_directory = output_root / batch_started_at.strftime("%Y%m%dT%H%M%S_%fZ")
    frames_root = session_directory / "frames"
    frames_root.mkdir(parents=True, exist_ok=False)
    summary_path = session_directory / "summary.json"

    setup_error: str | None = None
    setup: dict[str, Any] | None = None
    try:
        setup = _configure_measurements_once(
            host,
            port=port,
            connect_timeout_s=connect_timeout_s,
        )
    except Exception as error:
        setup_error = _exception_text(error)

    frames: list[dict[str, Any]] = []
    if setup_error is None:
        for frame_number in range(1, frame_count + 1):
            if frame_number > 1:
                time.sleep(inter_frame_run_s)
            try:
                outcome = run_four_channel_probe(
                    host,
                    port=port,
                    output_root=frames_root,
                    connect_timeout_s=connect_timeout_s,
                    block_timeout_s=block_timeout_s,
                    configure_measurements=False,
                )
                frame_metadata = json.loads(outcome.metadata_path.read_text(encoding="utf-8"))
                elapsed_s = float(frame_metadata["timing"]["total_transaction_s"])
                frame_result = {
                    "frame": frame_number,
                    "success": outcome.success,
                    "within_limit": elapsed_s <= frame_limit_s,
                    "elapsed_s": elapsed_s,
                    "sample_counts": outcome.sample_counts,
                    "available_measurements": frame_metadata["measurements"]["available_values"],
                    "screenshot_attempts": frame_metadata["screenshot"]["attempt_count"],
                    "errors": {
                        "capture": outcome.capture_error,
                        "preamble": outcome.preamble_error,
                        "screenshot": outcome.screenshot_error,
                        "measurement": outcome.measurement_error,
                        "run": outcome.run_error,
                    },
                    "channel_errors": {
                        source: frame_metadata["channels"][source]["capture_error"]
                        for source in frame_metadata["sources"]
                    },
                    "session_directory": str(outcome.session_directory.resolve()),
                    "metadata_path": str(outcome.metadata_path.resolve()),
                }
            except Exception as error:
                frame_result = {
                    "frame": frame_number,
                    "success": False,
                    "within_limit": False,
                    "elapsed_s": None,
                    "error": _exception_text(error),
                }
            frames.append(frame_result)
            print(json.dumps(frame_result, ensure_ascii=False), flush=True)

    elapsed_values = [
        float(frame["elapsed_s"]) for frame in frames if frame.get("elapsed_s") is not None
    ]
    passed = (
        setup_error is None
        and len(frames) == frame_count
        and all(frame["success"] and frame["within_limit"] for frame in frames)
    )
    summary: dict[str, Any] = {
        "schema_version": 1,
        "probe_kind": "mho1_ascii_ten_consecutive_frames",
        "started_at_utc": batch_started_at.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "completed_at_utc": _timestamp(),
        "target": {"host": host, "port": port},
        "frame_count_requested": frame_count,
        "frame_limit_s": frame_limit_s,
        "inter_frame_run_s": inter_frame_run_s,
        "criterion": "Every frame succeeds and STOP-through-RUN time is <= frame_limit_s",
        "passed": passed,
        "setup_error": setup_error,
        "one_time_measurement_setup": setup,
        "statistics_s": {
            "minimum": min(elapsed_values) if elapsed_values else None,
            "maximum": max(elapsed_values) if elapsed_values else None,
            "mean": statistics.fmean(elapsed_values) if elapsed_values else None,
            "median": statistics.median(elapsed_values) if elapsed_values else None,
        },
        "frames": frames,
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return summary_path, summary


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Configure ten measurements once, then capture consecutive MHO1 frames."
    )
    parser.add_argument("--host", required=True, help="MHO1 IPv4 address or hostname")
    parser.add_argument("--port", type=int, default=SCPI_PORT, help="SCPI TCP port")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(".openbench/data/captures/mho1-ascii-ten-frame"),
        help="Directory for the batch summary and per-frame artifacts",
    )
    parser.add_argument("--frames", type=int, default=FRAME_COUNT, help="Number of frames")
    parser.add_argument(
        "--limit-s",
        type=float,
        default=FRAME_LIMIT_S,
        help="Maximum allowed STOP-through-RUN duration per frame",
    )
    parser.add_argument(
        "--inter-frame-run-s",
        type=float,
        default=0.0,
        help="Acquisition time left in RUN before the next frame; excluded from frame timing",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    summary_path, summary = run_ten_frame_probe(
        arguments.host,
        port=arguments.port,
        output_root=arguments.output_root,
        frame_count=arguments.frames,
        frame_limit_s=arguments.limit_s,
        inter_frame_run_s=arguments.inter_frame_run_s,
    )
    print(
        json.dumps(
            {
                "passed": summary["passed"],
                "statistics_s": summary["statistics_s"],
                "summary": str(summary_path.resolve()),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
