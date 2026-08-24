from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
PROBE_PATH = SCRIPTS_ROOT / "mho1_ascii_four_channel_probe.py"
sys.path.insert(0, str(SCRIPTS_ROOT))


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mho1_ascii_four_channel_probe", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def _point_count_response(values: list[float]) -> bytes:
    declared = str(len(values)).encode("ascii")
    header = b"#" + str(len(declared)).encode("ascii") + declared
    payload = b"".join(f"{value:+.9E},".encode("ascii") for value in values)
    return header + payload


class FourChannelScope:
    def __init__(self, *, configure_measurements: bool = True) -> None:
        self.responses = {
            "CH1": _point_count_response([1.0, 1.5]),
            "CH2": _point_count_response([2.0, 2.5, 3.0]),
            "CH3": _point_count_response([-3.0]),
            "CH4": _point_count_response([4.0, 4.5, 5.0, 5.5]),
        }
        self.current_source = "CH1"
        self.preamble_response = b"2,0,1,1.0e-9,-5.0e-7,0,0.01,0,0\n"
        self.screenshot_payload = b"\x89PNG\r\n\x1a\nfake-screenshot"
        self.screenshot_queries = 0
        self.command_contract = (
            probe.COMMAND_CONTRACT if configure_measurements else probe.FRAME_COMMAND_CONTRACT
        )
        self.commands: list[str] = []
        self.error: BaseException | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(5.0)
        self.host, self.port = self._listener.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FourChannelScope:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._thread.join(timeout=5.0)
        self._listener.close()
        assert not self._thread.is_alive(), "fake MHO1 server did not finish"
        if self.error is not None:
            raise self.error

    @staticmethod
    def _read_line(connection: socket.socket) -> bytes:
        received = bytearray()
        while not received.endswith(b"\n"):
            chunk = connection.recv(1024)
            if not chunk:
                break
            received.extend(chunk)
        return bytes(received)

    def _serve(self) -> None:
        try:
            for _ in range(len(self.command_contract) + 1):
                connection, _address = self._listener.accept()
                with connection:
                    command = self._read_line(connection).decode("ascii").strip()
                    self.commands.append(command)
                    if command.startswith(":WAVeform:SOURce "):
                        self.current_source = command.rsplit(" ", 1)[1]
                    elif command == probe.PREAMBLE_QUERY:
                        connection.sendall(self.preamble_response)
                    elif command == probe.ASCII_QUERY:
                        connection.sendall(self.responses[self.current_source])
                    elif command == probe.SCREENSHOT_QUERY:
                        self.screenshot_queries += 1
                        payload = b"" if self.screenshot_queries == 1 else self.screenshot_payload
                        declared = str(len(payload)).encode("ascii")
                        block = b"#" + str(len(declared)).encode("ascii") + declared + payload
                        connection.sendall(block)
                    elif command in probe.MEASUREMENT_QUERY_COMMANDS:
                        index = probe.MEASUREMENT_QUERY_COMMANDS.index(command)
                        connection.sendall(f"{index + 0.5}\n".encode("ascii"))
        except BaseException as error:
            self.error = error


def test_four_channel_probe_uses_exact_zero_delay_contract(tmp_path: Path) -> None:
    with FourChannelScope() as scope:
        outcome = probe.run_four_channel_probe(
            scope.host,
            port=scope.port,
            output_root=tmp_path,
            connect_timeout_s=1.0,
            block_timeout_s=2.0,
            measurement_clear_delay_s=0.0,
            measurement_open_delay_s=0.0,
            measurement_ready_delay_s=0.0,
            screenshot_min_interval_s=0.0,
        )

    assert outcome.success is True
    expected_commands = list(probe.COMMAND_CONTRACT)
    screenshot_index = expected_commands.index(probe.SCREENSHOT_QUERY)
    expected_commands.insert(screenshot_index + 1, probe.SCREENSHOT_QUERY)
    assert scope.commands == expected_commands
    assert scope.commands.count(probe.NORMAL_MODE_COMMAND) == 4
    assert scope.commands.count(probe.PREAMBLE_QUERY) == 1
    assert scope.commands.count(probe.MEASUREMENT_CLEAR_COMMAND) == 1
    assert [command for command in scope.commands if command.startswith(":MEASure:OPEN ")] == list(
        probe.MEASUREMENT_OPEN_COMMANDS
    )
    assert [
        command
        for command in scope.commands
        if command.endswith("? CH1") or command.endswith("? CH2")
    ] == list(probe.MEASUREMENT_QUERY_COMMANDS)
    assert scope.commands.count(probe.SCREENSHOT_QUERY) == 2
    assert outcome.sample_counts == {"CH1": 2, "CH2": 3, "CH3": 1, "CH4": 4}

    metadata = json.loads(outcome.metadata_path.read_text(encoding="utf-8"))
    assert metadata["command_contract"] == expected_commands
    assert metadata["base_command_contract"] == list(probe.COMMAND_CONTRACT)
    assert [item["command"] for item in metadata["sent_commands"]] == expected_commands
    assert metadata["post_stop_delay_s"] == 0.0
    assert metadata["automatic_retries"] == 1
    assert metadata["preamble_error"] is None
    assert metadata["preamble"]["queried_source"] == "CH1"
    assert metadata["preamble"]["x_calibration_applies_to_sources"] == list(probe.SOURCES)
    assert metadata["preamble"]["y_calibration_source"] == "CH1"
    assert metadata["preamble"]["x_increment_s"] == pytest.approx(1.0e-9)
    assert metadata["preamble"]["x_origin_s"] == pytest.approx(-5.0e-7)
    # In-process fake replies can complete within one Windows clock tick.
    assert metadata["timing"]["preamble_data_sent_to_response_complete_s"] >= 0
    assert metadata["screenshot_error"] is None
    assert metadata["screenshot"]["format"] == "png"
    assert metadata["screenshot"]["attempt_count"] == 2
    assert metadata["screenshot"]["retry_count"] == 1
    assert metadata["screenshot"]["attempts"][0]["declared_payload_bytes"] == 0
    assert metadata["screenshot"]["attempts"][0]["status"] == "failed"
    assert metadata["screenshot"]["attempts"][1]["status"] == "ok"
    assert metadata["screenshot"]["artifacts"]["image"]["sha256"]
    assert (outcome.session_directory / "screenshot.png").read_bytes() == scope.screenshot_payload
    assert metadata["measurement_error"] is None
    assert metadata["measurements"]["configured_slots"] == 10
    assert metadata["measurements"]["returned_values"] == 10
    assert metadata["measurements"]["available_values"] == 10
    assert metadata["timing"]["screenshot_data_sent_to_response_complete_s"] >= 0
    assert metadata["timing"]["screenshot_phase_s"] >= 0
    assert metadata["timing"]["measurement_read_s"] >= 0
    measurement_lines = (
        (outcome.session_directory / "measurements.csv").read_text(encoding="utf-8").splitlines()
    )
    assert len(measurement_lines) == 11
    assert metadata["timing"]["total_transaction_s"] > 0
    assert metadata["timing"]["four_channel_read_phase_s"] >= 0
    assert metadata["timing"]["sum_ascii_data_transfer_s"] >= 0

    for source, point_count in outcome.sample_counts.items():
        result = metadata["channels"][source]
        assert result["capture_error"] is None
        assert result["capture"]["declared_points"] == point_count
        assert result["capture"]["parsed_points"] == point_count
        assert result["timing"]["ascii_data_sent_to_response_complete_s"] >= 0
        assert (outcome.session_directory / f"{source.lower()}.raw").exists()
        csv_lines = (
            (outcome.session_directory / f"{source.lower()}.csv")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert csv_lines[0] == f"sample_index,time_s,{source.lower()}_v"
        assert len(csv_lines) == point_count + 1
        assert float(csv_lines[1].split(",")[1]) == pytest.approx(-5.0e-7)


def test_four_channel_probe_has_no_post_stop_delay_or_openbench_import() -> None:
    source = PROBE_PATH.read_text(encoding="utf-8")
    assert '"post_stop_delay_s": 0.0' in source
    assert "from openbench" not in source
    assert "import openbench" not in source


def test_four_channel_probe_can_reuse_preconfigured_measurements(tmp_path: Path) -> None:
    with FourChannelScope(configure_measurements=False) as scope:
        outcome = probe.run_four_channel_probe(
            scope.host,
            port=scope.port,
            output_root=tmp_path,
            connect_timeout_s=1.0,
            block_timeout_s=2.0,
            screenshot_min_interval_s=0.0,
            configure_measurements=False,
        )

    expected_commands = list(probe.FRAME_COMMAND_CONTRACT)
    screenshot_index = expected_commands.index(probe.SCREENSHOT_QUERY)
    expected_commands.insert(screenshot_index + 1, probe.SCREENSHOT_QUERY)
    assert outcome.success is True
    assert scope.commands == expected_commands
    assert probe.MEASUREMENT_CLEAR_COMMAND not in scope.commands
    assert not any(command.startswith(":MEASure:OPEN ") for command in scope.commands)
    metadata = json.loads(outcome.metadata_path.read_text(encoding="utf-8"))
    assert metadata["measurements"]["configured_in_this_transaction"] is False
    assert metadata["timing"]["measurement_configuration_s"] is None
