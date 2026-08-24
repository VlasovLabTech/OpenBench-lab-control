from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from openbench.core.registry import DeviceRegistry
from openbench.domain import Device
from openbench.drivers.micsig_eto import (
    ETO_MAXIMUM_ASCII_CHUNK_POINTS,
    MicsigETOMaximumAsciiChunk,
    MicsigETOScope,
    MicsigETOTransport,
    parse_identification,
    parse_word_hex_waveform,
)
from openbench.drivers.micsig_mho1 import (
    MicsigDescriptor,
    MicsigMHO1Scope,
    MicsigProtocolError,
    parse_waveform_preamble,
)
from openbench.drivers.micsig_mho1 import (
    parse_identification as parse_mho1_identification,
)
from openbench.services.scope_maximum_capture_service import ScopeMaximumCaptureService


class FakeETOTransport:
    def __init__(self) -> None:
        self.status = "RUN"
        self.source = "CH4"
        self.mode = "RAW"
        self.waveform_format = "WORD"
        self.waveform_start = 7
        self.waveform_stop = 777
        self.memory_depth_points = ETO_MAXIMUM_ASCII_CHUNK_POINTS + 2
        self.ascii_gate: asyncio.Event | None = None
        self.close_count = 0
        self.empty_word_failures = 0
        self.commands: list[str] = []

    async def query_text(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
        attempts: int = 2,
    ) -> str:
        del timeout_s, attempts
        self.commands.append(command)
        if command == "*IDN?":
            return "Micsig,ETO5004,ETO-SERIAL-1,1.2.3"
        if command == ":TRIGger:STATus?":
            return self.status
        if command == ":WAVeform:SOURce?":
            return self.source
        if command == ":WAVeform:MODE?":
            return self.mode
        if command == ":WAVeform:FORMat?":
            return self.waveform_format
        if command == ":WAVeform:STARt?":
            return str(self.waveform_start)
        if command == ":WAVeform:STOP?":
            return str(self.waveform_stop)
        if command == ":ACQuire:DEPTh?":
            return str(self.memory_depth_points)
        if command == ":WAVeform:DATA?":
            channel = int(self.source[-1])
            return ",".join(str(channel + index / 10) for index in range(4))
        if command == ":WAVeform:PREamble?":
            format_code = 2 if self.waveform_format == "ASCII" else 0
            mode_code = 1 if self.mode.upper() == "MAXIMUM" else 0
            return f"{format_code},{mode_code},1,1e-6,0,0,0.01,0,0"
        raise AssertionError(f"Unexpected ETO text query: {command}")

    async def query_block(
        self,
        command: str,
        *,
        length_multiplier: int = 1,
    ) -> bytes:
        self.commands.append(command)
        assert command == ":WAVeform:DATA?"
        assert length_multiplier == 4
        if self.empty_word_failures:
            self.empty_word_failures -= 1
            return b""
        channel = int(self.source[-1])
        codes = tuple(channel * 100 + index * 10 for index in range(4))
        return "".join(f"{code:04X}" for code in codes).encode("ascii")

    async def query_ascii_block(self, command: str) -> bytes:
        self.commands.append(command)
        assert command == ":WAVeform:DATA?"
        if self.ascii_gate is not None:
            await self.ascii_gate.wait()
        channel = int(self.source[-1])
        if self.mode.upper() == "MAXIMUM":
            return ",".join(
                str(channel + index / 10)
                for index in range(self.waveform_start, self.waveform_stop + 1)
            ).encode()
        return ",".join(str(channel + index / 10) for index in range(4)).encode()

    async def write(self, command: str) -> None:
        self.commands.append(command)
        if command == ":MENU:STOP":
            self.status = "STOP"
        elif command == ":MENU:RUN":
            self.status = "RUN"
        elif command.startswith(":WAVeform:SOURce "):
            self.source = command.rsplit(" ", maxsplit=1)[-1]
        elif command.startswith(":WAVeform:MODE "):
            self.mode = command.rsplit(" ", maxsplit=1)[-1]
        elif command.startswith(":WAVeform:FORMat "):
            self.waveform_format = command.rsplit(" ", maxsplit=1)[-1]
        elif command.startswith(":WAVeform:STARt "):
            self.waveform_start = int(command.rsplit(" ", maxsplit=1)[-1])
        elif command.startswith(":WAVeform:STOP "):
            self.waveform_stop = int(command.rsplit(" ", maxsplit=1)[-1])

    async def capture_stored_screenshot(self, *, timeout_s: float = 5.0) -> bytes:
        assert timeout_s == 15.0
        return b"\x89PNG\r\n\x1a\n" + b"eto-screenshot"

    async def query_vxi11_raw(self, command: str) -> bytes:
        raise AssertionError(f"Unexpected VXI-11 query: {command}")

    async def list_http_links(self, path: str = "/") -> tuple[str, ...]:
        del path
        return ()

    async def download_http_file(self, path: str) -> bytes:
        raise FileNotFoundError(path)

    async def close(self) -> None:
        self.close_count += 1


def _descriptor() -> MicsigDescriptor:
    return MicsigDescriptor(
        host="198.51.100.28",
        scpi_port=5025,
        screen_port=8888,
        identification=parse_identification("Micsig,ETO5004,ETO-SERIAL-1,1.2.3"),
    )


def _mho1_descriptor() -> MicsigDescriptor:
    return MicsigDescriptor(
        host="192.0.2.20",
        scpi_port=5025,
        http_port=80,
        screen_port=8888,
        identification=parse_mho1_identification("Micsig,MHO14-200,MHO-SERIAL-1,2.154.75"),
    )


def test_eto_identity_is_strictly_model_bounded() -> None:
    identification = parse_identification("Micsig,ETO5004,ETO-SERIAL-1,1.2.3")

    assert identification.model == "ETO5004"
    with pytest.raises(MicsigProtocolError, match="Unsupported ETO"):
        parse_identification("Micsig,ETO3504,ETO-SERIAL-2,1.2.3")
    with pytest.raises(MicsigProtocolError, match="Unsupported SCPI"):
        parse_mho1_identification("Micsig,ETO5004,ETO-SERIAL-1,1.2.3")
    with pytest.raises(MicsigProtocolError, match="Invalid Micsig ETO"):
        parse_identification("Micsig,ETO5004,missing-firmware")


def test_eto_device_ids_use_serials_and_distinguish_multiple_scopes() -> None:
    first = MicsigETOScope(_descriptor(), transport=FakeETOTransport())
    second_identification = parse_identification("Micsig,ETO5004,ETO-SERIAL-2,1.2.3")
    second = MicsigETOScope(
        MicsigDescriptor(
            host="198.51.100.29",
            scpi_port=5025,
            screen_port=8888,
            identification=second_identification,
        ),
        transport=FakeETOTransport(),
    )

    assert first.device_id == "micsig_eto5004_eto_serial_1"
    assert second.device_id == "micsig_eto5004_eto_serial_2"
    assert first.device_id != second.device_id


@pytest.mark.asyncio
async def test_eto_discovery_keeps_the_identification_connection() -> None:
    connection_count = 0

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        nonlocal connection_count
        connection_count += 1
        assert await reader.readline() == b"*IDN?\n"
        writer.write(b"Micsig,ETO5004,ETO-SERIAL-1,1.2.3\n")
        await writer.drain()
        assert await reader.readline() == b":TRIGger:STATus?\n"
        writer.write(b"RUN\n")
        await writer.drain()
        await reader.read()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    transports = await MicsigETOTransport.discover_connected(
        hosts=("127.0.0.1",),
        scpi_port=port,
        connect_timeout_s=0.2,
        scan_fallback=False,
    )
    try:
        assert await transports[0].query_text(":TRIGger:STATus?") == "RUN"
    finally:
        await transports[0].close()
        server.close()
        await server.wait_closed()

    assert transports[0].descriptor.model == "ETO5004"
    assert connection_count == 1


@pytest.mark.asyncio
async def test_eto_ascii_transport_reads_point_count_in_buffered_chunks() -> None:
    values = tuple(f"{12.0 + index / 1000:.6f}" for index in range(1100))
    payload = ",".join(values).encode("ascii") + b","

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        assert await reader.readline() == b"*IDN?\n"
        writer.write(b"Micsig,ETO5004,ETO-SERIAL-1,1.2.3\n")
        await writer.drain()
        assert await reader.readline() == b":WAVeform:DATA?\n"
        writer.write(b"#4" + b"1100" + payload + b"ignored-padding")
        await writer.drain()
        await reader.read()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    transport = await MicsigETOTransport.connect(
        "127.0.0.1",
        scpi_port=port,
        connect_timeout_s=0.2,
    )
    try:
        result = await asyncio.wait_for(
            transport.query_ascii_block(":WAVeform:DATA?"),
            timeout=1.0,
        )
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()

    assert result == payload.rstrip(b",")


@pytest.mark.asyncio
async def test_eto_ascii_transport_accepts_an_idle_final_numeric_token() -> None:
    payload = b"1.0,2.0,3.0,4.0"

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        assert await reader.readline() == b"*IDN?\n"
        writer.write(b"Micsig,ETO5004,ETO-SERIAL-1,1.2.3\n")
        await writer.drain()
        assert await reader.readline() == b":WAVeform:DATA?\n"
        writer.write(b"#14" + payload)
        await writer.drain()
        await reader.read()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    transport = await MicsigETOTransport.connect(
        "127.0.0.1",
        scpi_port=port,
        connect_timeout_s=0.2,
    )
    try:
        result = await asyncio.wait_for(
            transport.query_ascii_block(":WAVeform:DATA?"),
            timeout=1.0,
        )
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()

    assert result == payload


def test_eto_known_bad_timebase_mode_readback_is_model_bounded() -> None:
    affected = MicsigETOScope(
        MicsigDescriptor(
            host="198.51.100.28",
            scpi_port=5025,
            screen_port=8888,
            identification=parse_identification(
                "Micsig,ETO5004,ETO-SERIAL-1,3.392.132"
            ),
        ),
        transport=FakeETOTransport(),
    )
    unaffected = MicsigETOScope(_descriptor(), transport=FakeETOTransport())

    assert affected._normalize_timebase_mode_readback("XY") == "YT"
    assert affected._normalize_timebase_mode_readback("YT") == "YT"
    assert unaffected._normalize_timebase_mode_readback("XY") == "XY"

    preamble = parse_waveform_preamble("0,0,1,1e-6,0,0,0.1,0.2,2")
    corrected = affected._normalize_word_preamble(preamble)
    untouched = unaffected._normalize_word_preamble(preamble)
    assert corrected.y_increment == pytest.approx(0.05)
    assert corrected.y_origin == pytest.approx(0.1)
    assert corrected.y_reference == preamble.y_reference
    assert untouched == preamble


@pytest.mark.asyncio
async def test_eto_frame_uses_documented_standard_ascii_and_restores_settings() -> None:
    transport = FakeETOTransport()
    scope = MicsigETOScope(_descriptor(), transport=transport)

    assert await scope.identify() == "Micsig ETO5004 SN ETO-SERIAL-1 FW 1.2.3"
    frame = await scope.capture_frame(
        (),
        channels=("CH1", "CH2"),
        include_screenshot=False,
    )

    assert scope.waveform_transfer_method == "standard_word_hex"
    assert scope.screenshot_supported is True
    assert scope.dmm_support.hardware_present is False
    assert [item.source for item in frame.waveforms] == ["CH1", "CH2"]
    assert [item.points for item in frame.waveforms] == [4, 4]
    assert frame.waveforms[0].samples == (100, 110, 120, 130)
    assert tuple(frame.waveforms[0].voltage_at(index) for index in range(4)) == (
        1.0,
        1.1,
        1.2,
        1.3,
    )
    assert frame.waveforms[0].ascii_data == b""
    assert ":WAVeform:DATA?" in transport.commands
    assert ":WAVeform:DATA:BIN?" not in transport.commands
    assert ":WAVeform:DATA:ASCii?" not in transport.commands
    assert transport.status == "RUN"
    assert transport.source == "CH4"
    assert transport.mode == "RAW"
    assert transport.waveform_format == "WORD"
    assert transport.waveform_start == 7
    assert transport.waveform_stop == 777
    assert transport.close_count >= 3


@pytest.mark.asyncio
async def test_eto_screenshot_uses_documented_stored_capture() -> None:
    scope = MicsigETOScope(_descriptor(), transport=FakeETOTransport())

    screenshot = await scope.capture_screenshot()

    assert screenshot.image_format == "png"
    assert screenshot.data.startswith(b"\x89PNG\r\n\x1a\n")


def test_eto_word_hex_parser_is_strict() -> None:
    assert parse_word_hex_waveform(b"00F300EF") == (0x00F3, 0x00EF)
    with pytest.raises(MicsigProtocolError, match="divisible by four"):
        parse_word_hex_waveform(b"00F")
    with pytest.raises(MicsigProtocolError, match="invalid WORD"):
        parse_word_hex_waveform(b"ZZZZ")


@pytest.mark.asyncio
async def test_eto_frame_retries_one_empty_word_block_on_a_fresh_session() -> None:
    transport = FakeETOTransport()
    transport.empty_word_failures = 1
    scope = MicsigETOScope(_descriptor(), transport=transport)

    frame = await scope.capture_frame((), channels=("CH1",), include_screenshot=False)

    assert len(frame.waveforms) == 1
    assert transport.commands.count(":WAVeform:DATA?") == 2
    assert transport.close_count >= 3
    assert transport.status == "RUN"


@pytest.mark.asyncio
async def test_eto_rejects_the_unsupported_fast_binary_probe() -> None:
    scope = MicsigETOScope(_descriptor(), transport=FakeETOTransport())

    with pytest.raises(ValueError, match="does not support"):
        await scope.probe_fast_binary_waveform("CH1")


@pytest.mark.asyncio
async def test_eto_maximum_ascii_requires_stop_without_changing_memory_depth() -> None:
    transport = FakeETOTransport()
    scope = MicsigETOScope(_descriptor(), transport=transport)

    with pytest.raises(RuntimeError, match="already be in STOP"):
        await scope.maximum_ascii_capture_info()

    assert not any(command.startswith(":ACQuire:DEPSelect ") for command in transport.commands)


@pytest.mark.asyncio
async def test_eto_maximum_ascii_streams_chunks_and_restores_reader_state() -> None:
    transport = FakeETOTransport()
    transport.status = "STOP"
    scope = MicsigETOScope(_descriptor(), transport=transport)
    chunks: list[MicsigETOMaximumAsciiChunk] = []

    async def receive(chunk: MicsigETOMaximumAsciiChunk) -> None:
        chunks.append(chunk)

    info = await scope.stream_maximum_ascii(("CH1",), on_chunk=receive)

    assert info.memory_depth_points == ETO_MAXIMUM_ASCII_CHUNK_POINTS + 2
    assert [(item.start_point, item.stop_point) for item in chunks] == [
        (1, ETO_MAXIMUM_ASCII_CHUNK_POINTS),
        (ETO_MAXIMUM_ASCII_CHUNK_POINTS + 1, ETO_MAXIMUM_ASCII_CHUNK_POINTS + 2),
    ]
    assert sum(item.points for item in chunks) == info.memory_depth_points
    assert all(item.source == "CH1" for item in chunks)
    assert not any(command.startswith(":ACQuire:DEPSelect ") for command in transport.commands)
    assert transport.status == "STOP"
    assert transport.source == "CH4"
    assert transport.mode == "RAW"
    assert transport.waveform_format == "WORD"
    assert transport.waveform_start == 7
    assert transport.waveform_stop == 777


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_kind", ("eto", "mho1"))
async def test_maximum_capture_service_is_once_only_and_writes_streamed_files(
    tmp_path: Path,
    scope_kind: str,
) -> None:
    transport = FakeETOTransport()
    transport.status = "STOP"
    transport.memory_depth_points = 17
    transport.ascii_gate = asyncio.Event()
    if scope_kind == "eto":
        scope = MicsigETOScope(_descriptor(), transport=transport)
        device_kind = "micsig_eto"
        expected_prefix = "eto5004"
    else:
        scope = MicsigMHO1Scope(_mho1_descriptor(), transport=transport)
        device_kind = "micsig_mho1"
        expected_prefix = "mho1"
    registry = DeviceRegistry()
    registry.register(
        Device(
            id=scope.device_id,
            name=scope.descriptor.model,
            kind=device_kind,
            connected=True,
            capabilities=("oscilloscope", "waveform_capture"),
        ),
        scope,
    )

    class FakeScopeMeasurements:
        def __init__(self) -> None:
            self.suspended = 0

        async def suspend_live_polling(self, device_id: str) -> None:
            assert device_id == scope.device_id
            self.suspended += 1

        def resume_live_polling(self, device_id: str) -> None:
            assert device_id == scope.device_id
            self.suspended -= 1

    scope_measurements = FakeScopeMeasurements()
    capture_service = SimpleNamespace(status=lambda: SimpleNamespace(active=False))
    service = ScopeMaximumCaptureService(
        tmp_path,
        registry,
        scope_measurements,  # type: ignore[arg-type]
        capture_service,  # type: ignore[arg-type]
    )

    started = await service.start_capture(scope.device_id, channels=("CH1", "CH2"))
    assert started.active is True
    assert started.memory_depth_points == 17
    assert service.active_device_ids() == frozenset((scope.device_id,))
    with pytest.raises(RuntimeError, match="already active"):
        await service.start_capture(scope.device_id, channels=("CH1",))

    transport.ascii_gate.set()
    for _ in range(100):
        if not service.status(scope.device_id).active:
            break
        await asyncio.sleep(0.01)
    completed = service.status(scope.device_id)

    assert completed.state == "completed"
    assert completed.progress_percent == 100
    assert completed.points_completed == 34
    assert scope_measurements.suspended == 0
    assert [path.name for path in completed.artifact_files] == [
        f"{expected_prefix}_ch1_maximum_ascii.txt",
        f"{expected_prefix}_ch2_maximum_ascii.txt",
    ]
    assert all(
        len(path.read_text(encoding="ascii").strip().split(",")) == 17
        for path in completed.artifact_files
    )
    assert completed.metadata_file is not None
    assert completed.metadata_file.is_file()
    assert '"memory_depth_changed": false' in completed.metadata_file.read_text(encoding="utf-8")

    capture_service.status = lambda: SimpleNamespace(active=True)
    with pytest.raises(RuntimeError, match="Stop the common CSV recording"):
        await service.start_capture(scope.device_id, channels=("CH1",))
