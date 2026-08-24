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
PROBE_PATH = PROJECT_ROOT / "scripts" / "mho1_ascii_minimal_probe.py"


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mho1_ascii_minimal_probe", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


class ScriptedScope:
    def __init__(self, ascii_response: bytes, *, expected_connections: int = 3) -> None:
        self._ascii_response = ascii_response
        self._expected_connections = expected_connections
        self.commands: list[str] = []
        self.error: BaseException | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(5.0)
        self.host, self.port = self._listener.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> ScriptedScope:
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
            for _ in range(self._expected_connections):
                connection, _address = self._listener.accept()
                with connection:
                    command = self._read_line(connection).decode("ascii").strip()
                    self.commands.append(command)
                    if command == probe.ASCII_QUERY:
                        connection.sendall(self._ascii_response)
        except BaseException as error:  # Propagate server failures to the test thread.
            self.error = error


def _point_count_response(values: list[float], *, padding: bytes = b"") -> bytes:
    declared = str(len(values)).encode("ascii")
    header = b"#" + str(len(declared)).encode("ascii") + declared
    payload = b"".join(f"{value:+.9E},".encode("ascii") for value in values)
    return header + payload + padding


@pytest.mark.parametrize("point_count", [1100, 5500])
def test_probe_sends_only_three_commands_and_reads_point_count_block(
    tmp_path: Path,
    point_count: int,
) -> None:
    values = [((index % 101) - 50) / 20 for index in range(point_count)]
    response = _point_count_response(values, padding=b"INVALID FIRMWARE PADDING")

    with ScriptedScope(response) as scope:
        outcome = probe.run_probe(
            scope.host,
            port=scope.port,
            output_root=tmp_path,
            connect_timeout_s=1.0,
            block_timeout_s=2.0,
            post_stop_delay_s=0.0,
        )

    assert outcome.success is True
    assert outcome.sample_count == point_count
    assert scope.commands == list(probe.COMMAND_CONTRACT)

    metadata = json.loads(outcome.metadata_path.read_text(encoding="utf-8"))
    assert [item["command"] for item in metadata["sent_commands"]] == list(probe.COMMAND_CONTRACT)
    assert metadata["automatic_retries"] == 0
    assert metadata["timing"]["total_transaction_s"] > 0
    assert metadata["timing"]["stop_to_ascii_data_sent_s"] >= 0
    assert metadata["timing"]["ascii_connect_send_receive_s"] >= 0
    # An in-process fake may reply within one Windows monotonic-clock tick.
    assert metadata["timing"]["ascii_data_sent_to_response_complete_s"] >= 0
    assert metadata["capture"]["declared_points"] == point_count
    assert metadata["capture"]["parsed_points"] == point_count
    assert metadata["artifacts"]["raw"]["sha256"]
    assert metadata["artifacts"]["csv"]["sha256"]
    raw = (outcome.session_directory / "ascii.raw").read_bytes()
    assert raw.endswith(b",")
    assert b"INVALID FIRMWARE PADDING" not in raw

    csv_lines = (outcome.session_directory / "ascii.csv").read_text(encoding="utf-8").splitlines()
    assert csv_lines[0] == "sample_index,current_source_v"
    assert len(csv_lines) == point_count + 1


def test_probe_sends_run_after_malformed_ascii_response(tmp_path: Path) -> None:
    with ScriptedScope(b"!not-a-block\n") as scope:
        outcome = probe.run_probe(
            scope.host,
            port=scope.port,
            output_root=tmp_path,
            connect_timeout_s=1.0,
            block_timeout_s=2.0,
            post_stop_delay_s=0.0,
        )

    assert outcome.success is False
    assert outcome.sample_count == 0
    assert outcome.capture_error is not None
    assert "Expected '#' block marker" in outcome.capture_error
    assert outcome.run_error is None
    assert scope.commands == list(probe.COMMAND_CONTRACT)

    metadata = json.loads(outcome.metadata_path.read_text(encoding="utf-8"))
    assert metadata["capture_error"] == outcome.capture_error
    assert metadata["run_error"] is None
    assert [item["command"] for item in metadata["sent_commands"]] == list(probe.COMMAND_CONTRACT)


def test_probe_preserves_zero_point_header_and_sends_run(tmp_path: Path) -> None:
    with ScriptedScope(b"#10") as scope:
        outcome = probe.run_probe(
            scope.host,
            port=scope.port,
            output_root=tmp_path,
            connect_timeout_s=1.0,
            block_timeout_s=2.0,
            post_stop_delay_s=0.0,
        )

    assert outcome.success is False
    assert outcome.capture_error == "ProbeError: MHO1 ASCII waveform block declared zero points"
    assert outcome.run_error is None
    assert scope.commands == list(probe.COMMAND_CONTRACT)
    assert (outcome.session_directory / "ascii.raw").read_bytes() == b"#10"

    metadata = json.loads(outcome.metadata_path.read_text(encoding="utf-8"))
    assert metadata["capture"]["header_ascii"] == "#10"
    assert metadata["capture"]["declared_points"] == 0
    assert metadata["capture"]["parsed_points"] == 0


def test_probe_with_source_sends_only_four_commands(tmp_path: Path) -> None:
    response = _point_count_response([1.0, -0.25, 0.0], padding=b"IGNORED")

    with ScriptedScope(response, expected_connections=4) as scope:
        outcome = probe.run_probe(
            scope.host,
            port=scope.port,
            output_root=tmp_path,
            connect_timeout_s=1.0,
            block_timeout_s=2.0,
            post_stop_delay_s=0.0,
            source="ch1",
        )

    source_command = ":WAVeform:SOURce CH1"
    expected_commands = [probe.STOP_COMMAND, source_command, probe.ASCII_QUERY, probe.RUN_COMMAND]
    assert outcome.success is True
    assert scope.commands == expected_commands

    metadata = json.loads(outcome.metadata_path.read_text(encoding="utf-8"))
    assert metadata["command_contract"] == expected_commands
    assert metadata["selected_source"] == "CH1"
    assert [item["command"] for item in metadata["sent_commands"]] == expected_commands
    csv_header = (
        (outcome.session_directory / "ascii.csv").read_text(encoding="utf-8").splitlines()[0]
    )
    assert csv_header == "sample_index,ch1_v"


def test_probe_with_source_and_normal_mode_sends_only_five_commands(tmp_path: Path) -> None:
    response = _point_count_response([0.5, -0.5], padding=b"IGNORED")

    with ScriptedScope(response, expected_connections=5) as scope:
        outcome = probe.run_probe(
            scope.host,
            port=scope.port,
            output_root=tmp_path,
            connect_timeout_s=1.0,
            block_timeout_s=2.0,
            post_stop_delay_s=0.0,
            source="CH1",
            mode="normal",
        )

    expected_commands = [
        probe.STOP_COMMAND,
        ":WAVeform:SOURce CH1",
        probe.NORMAL_MODE_COMMAND,
        probe.ASCII_QUERY,
        probe.RUN_COMMAND,
    ]
    assert outcome.success is True
    assert scope.commands == expected_commands

    metadata = json.loads(outcome.metadata_path.read_text(encoding="utf-8"))
    assert metadata["command_contract"] == expected_commands
    assert metadata["selected_source"] == "CH1"
    assert metadata["selected_mode"] == "NORMAL"
    assert metadata["automatic_retries"] == 0
    assert [item["command"] for item in metadata["sent_commands"]] == expected_commands


def test_probe_with_normal_mode_and_no_source_sends_only_four_commands(tmp_path: Path) -> None:
    response = _point_count_response([0.25, -0.25])

    with ScriptedScope(response, expected_connections=4) as scope:
        outcome = probe.run_probe(
            scope.host,
            port=scope.port,
            output_root=tmp_path,
            connect_timeout_s=1.0,
            block_timeout_s=2.0,
            post_stop_delay_s=0.0,
            mode="NORMAL",
        )

    expected_commands = [
        probe.STOP_COMMAND,
        probe.NORMAL_MODE_COMMAND,
        probe.ASCII_QUERY,
        probe.RUN_COMMAND,
    ]
    assert outcome.success is True
    assert scope.commands == expected_commands

    metadata = json.loads(outcome.metadata_path.read_text(encoding="utf-8"))
    assert metadata["command_contract"] == expected_commands
    assert metadata["selected_source"] is None
    assert metadata["selected_mode"] == "NORMAL"
    assert [item["command"] for item in metadata["sent_commands"]] == expected_commands


def test_probe_source_has_no_openbench_import() -> None:
    source = PROBE_PATH.read_text(encoding="utf-8")
    assert "from openbench" not in source
    assert "import openbench" not in source
