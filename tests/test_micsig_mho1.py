from __future__ import annotations

import asyncio
import csv
import io
import struct
from dataclasses import replace
from typing import ClassVar

import pytest
from PIL import Image, ImageDraw

from openbench.drivers.micsig_common import (
    MICSIG_MAXIMUM_ASCII_CHUNK_POINTS,
    MicsigMaximumAsciiChunk,
)
from openbench.drivers.micsig_mho1 import (
    MAX_SCALAR_MEASUREMENTS,
    MicsigChannelUpdate,
    MicsigDescriptor,
    MicsigIdentification,
    MicsigMHO1Scope,
    MicsigScalarMeasurementSpec,
    MicsigScopeUpdate,
    MicsigScpiTransport,
    MicsigTriggerUpdate,
    normalize_screenshot_image,
    parse_ascii_waveform,
    parse_fast_binary_waveform,
    parse_identification,
)
from openbench.drivers.micsig_mho1.transport import (
    _build_portmapper_getport_request,
    _parse_portmapper_getport_response,
)


class FakeMicsigTransport:
    instances: ClassVar[list[FakeMicsigTransport]] = []

    def __init__(self) -> None:
        self.status = "RUN"
        self.source = "CH2"
        self.mode = "NORMal"
        self.waveform_format = "WORD"
        self.waveform_start = 1
        self.waveform_stop = 1100
        self.storage_source = "CH2"
        self.storage_location = "LOCAL"
        self.storage_type = "WAV"
        self.storage_filename = "DefaultSaveName"
        self.storage_files: dict[str, bytes] = {}
        self.screenshot_queries = 0
        self.empty_first_screenshot = True
        self.empty_ascii_sources: set[str] = set()
        self.stored_screenshot_captures = 0
        self.writes: list[str] = []
        self.commands: list[str] = []
        self.closed = False
        self.close_count = 0
        self.__class__.instances.append(self)

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
            return "Micsig,MHO14-200,MHO1-DEMO-0001,2.154.75"
        if command == ":TRIGger:STATus?":
            return self.status
        if command == ":ACQuire:TYPE?":
            return "NORMAL"
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
        if command == ":WAVeform:DATA?":
            channel = int(self.source[-1])
            return ",".join(str(channel + index / 10) for index in range(4))
        if command == ":WAVeform:PREamble?":
            channel = int(self.source[-1])
            format_code = 2 if self.waveform_format == "ASCII" else 0
            return f"{format_code},0,1,1e-6,0.01,0,{channel / 1000},0,0"
        if command == ":STORage:SAVE:SOURce?":
            return self.storage_source
        if command == ":STORage:SAVE:LOCAtion?":
            return self.storage_location
        if command == ":STORage:SAVE:TYPE?":
            return self.storage_type
        if command == ":STORage:SAVE:FILename?":
            return self.storage_filename
        if command == ":TIMebase:EXTent?":
            return "0.01"
        if command == ":TIMebase:POSition?":
            return "0.0491"
        if command.startswith(":CHANnel") and command.endswith(":SCALe?"):
            return "1"
        if command.startswith(":CHANnel") and command.endswith(":POSition?"):
            channel = int(command[len(":CHANnel")])
            zero_y = (120, 220, 320, 620)[channel - 1]
            return str((380 - zero_y) / 60)
        if command.startswith(":MEASure:"):
            if ":STATistic:CURRent:VIEW?" in command:
                return "--"
            if ":PDUTy?" in command:
                return "0.4827"
            if ":FREQ?" in command and command.endswith("CH4"):
                return "--"
            if ":PHASe?" in command:
                return "2.25"
            if ":DELay?" in command:
                return "1e-9"
            channel = int(command[-1])
            return str(channel + 0.25)
        raise AssertionError(f"Unexpected text query: {command}")

    async def query_block(
        self,
        command: str,
        *,
        length_multiplier: int = 1,
    ) -> bytes:
        self.commands.append(command)
        assert length_multiplier in {1, 4}
        if command == ":SYS:SCR?":
            self.screenshot_queries += 1
            if self.empty_first_screenshot and self.screenshot_queries == 1:
                return b""
            return b"\x89PNG\r\n\x1a\ndirect-screenshot"
        if command == ":WAVeform:DATA:BIN?":
            channel = int(self.source[-1])
            return struct.pack("<4i", *(channel * 100 + index for index in range(4)))
        if command == ":WAVeform:DATA:ASCii?":
            channel = int(self.source[-1])
            return ",".join(str(channel * 100 + index) for index in range(4)).encode()
        raise AssertionError(f"Unexpected block query: {command}")

    async def query_ascii_block(self, command: str) -> bytes:
        self.commands.append(command)
        assert command == ":WAVeform:DATA:ASCii?"
        if self.source in self.empty_ascii_sources:
            return b""
        channel = int(self.source[-1])
        return " ".join(str(channel * 100 + index) for index in range(4)).encode()

    async def write(self, command: str) -> None:
        self.commands.append(command)
        self.writes.append(command)
        if command == ":MENU:RUN":
            self.status = "RUN"
        elif command == ":MENU:STOP":
            self.status = "STOP"
        elif command == ":MENU:SINGle":
            self.status = "WAIT"
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
        elif command.startswith(":STORage:SAVE:SOURce "):
            self.storage_source = command.rsplit(" ", maxsplit=1)[-1]
        elif command.startswith(":STORage:SAVE:LOCAtion "):
            self.storage_location = command.rsplit(" ", maxsplit=1)[-1].upper()
        elif command.startswith(":STORage:SAVE:TYPE "):
            self.storage_type = command.rsplit(" ", maxsplit=1)[-1].upper()
        elif command.startswith(":STORage:SAVE:FILename "):
            self.storage_filename = command.split(" ", maxsplit=1)[-1].strip('"')
        elif command.startswith(":STORage:SAVE CH"):
            self.storage_source = command.rsplit(" ", maxsplit=1)[-1]
        elif command == ":STORage:SAVE:STARt":
            extension = self.storage_type.lower()
            directory = "/files/csvwave" if self.storage_type == "CSV" else "/files/binwave"
            suffix = (
                "" if self.storage_filename.lower().endswith(f".{extension}") else f".{extension}"
            )
            path = f"{directory}/{self.storage_filename}{suffix}"
            self.storage_files[path] = b"time_s,voltage_v\n0,1\n1e-6,2\n"

    async def capture_stored_screenshot(self, *, timeout_s: float = 5.0) -> bytes:
        assert timeout_s > 0
        self.stored_screenshot_captures += 1
        image = Image.new("RGB", (1280, 800), "black")
        draw = ImageDraw.Draw(image)
        colors = ("yellow", "cyan", "magenta", "lime")
        for y, color in zip((120, 220, 320, 620), colors, strict=True):
            draw.line((80, y, 1179, y), fill=color, width=1)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    async def query_vxi11_raw(self, command: str) -> bytes:
        assert command == ":SYS:SCR?"
        payload = b"\x89PNG\r\n\x1a\nprobe"
        return b"#2" + str(len(payload)).encode().zfill(2) + payload

    async def list_http_links(self, path: str = "/") -> tuple[str, ...]:
        if path == "/files":
            return ("/files/default", "/files/refwave", "/files/csvwave")
        return tuple(sorted(item for item in self.storage_files if item.startswith(f"{path}/")))

    async def download_http_file(self, path: str) -> bytes:
        try:
            return self.storage_files[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    async def close(self) -> None:
        self.closed = True
        self.close_count += 1


class EmptyScreenMicsigTransport(FakeMicsigTransport):
    async def capture_stored_screenshot(self, *, timeout_s: float = 5.0) -> bytes:
        assert timeout_s > 0
        image = Image.new("RGB", (1280, 800), "black")
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


class ControlMicsigTransport(FakeMicsigTransport):
    def __init__(self) -> None:
        super().__init__()
        self.acquisition_type = "NORMal"
        self.averaging_count = 4
        self.memory_depth = "11000"
        self.timebase = 0.001
        self.timebase_mode = "YT"
        self.displayed = {channel: True for channel in range(1, 5)}
        self.coupling = {channel: "DC" for channel in range(1, 5)}
        self.probe = {channel: 1.0 for channel in range(1, 5)}
        self.impedance = {channel: "MEGA" for channel in range(1, 5)}
        self.trigger_type = "EDGE"
        self.trigger_mode = "AUTO"
        self.trigger_source = "CH1"
        self.trigger_slope = "RISE"
        self.trigger_level = 0.0
        self.trigger_coupling = "DC"

    async def query_text(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
        attempts: int = 2,
    ) -> str:
        fixed = {
            ":ACQuire:TYPE?": self.acquisition_type,
            ":ACQuire:MEAN?": str(self.averaging_count),
            ":ACQuire:SRATe?": "100000000",
            ":ACQuire:DEPSelect?": self.memory_depth,
            ":ACQuire:DEPTh?": self.memory_depth,
            ":TIMebase:MODE?": self.timebase_mode,
            ":TRIGger:TYPE?": self.trigger_type,
            ":TRIGger:MODE?": self.trigger_mode,
            ":TRIGger:EDGE:SOURce?": self.trigger_source,
            ":TRIGger:EDGE:SLOPe?": self.trigger_slope,
            ":TRIGger:EDGE:LEVel?": str(self.trigger_level),
            ":TRIGger:EDGE:COUPle?": self.trigger_coupling,
        }
        if command in fixed:
            return fixed[command]
        if command == ":TIMebase:EXTent?":
            self.commands.append(command)
            return str(self.timebase)
        if command.startswith(":CHANnel"):
            channel = int(command[len(":CHANnel")])
            if command.endswith(":DISPlay?"):
                return "1" if self.displayed[channel] else "0"
            if command.endswith(":COUPle?"):
                return self.coupling[channel]
            if command.endswith(":PROBe?"):
                return str(self.probe[channel])
            if command.endswith(":INPutres?"):
                return self.impedance[channel]
        return await super().query_text(
            command,
            timeout_s=timeout_s,
            attempts=attempts,
        )

    async def write(self, command: str) -> None:
        await super().write(command)
        if " " not in command:
            return
        key, value = command.rsplit(" ", maxsplit=1)
        if key == ":ACQuire:TYPE":
            self.acquisition_type = value
        elif key == ":ACQuire:MEAN":
            self.averaging_count = int(value)
        elif key == ":ACQuire:DEPSelect":
            self.memory_depth = value
        elif key == ":TIMebase:EXTent":
            self.timebase = float(value)
        elif key == ":TIMebase:MODE":
            self.timebase_mode = value
        elif key == ":TRIGger:TYPE":
            self.trigger_type = value
        elif key == ":TRIGger:MODE":
            self.trigger_mode = value
        elif key == ":TRIGger:EDGE:SOURce":
            self.trigger_source = value
        elif key == ":TRIGger:EDGE:SLOPe":
            self.trigger_slope = value
        elif key == ":TRIGger:EDGE:LEVel":
            self.trigger_level = float(value)
        elif key == ":TRIGger:EDGE:COUPle":
            self.trigger_coupling = value
        elif key.startswith(":CHANnel"):
            channel = int(key[len(":CHANnel")])
            if key.endswith(":DISPlay"):
                self.displayed[channel] = value == "ON"
            elif key.endswith(":COUPle"):
                self.coupling[channel] = value
            elif key.endswith(":PROBe"):
                self.probe[channel] = float(value)
            elif key.endswith(":INPutres"):
                self.impedance[channel] = value


class MaximumMicsigTransport(ControlMicsigTransport):
    def __init__(self) -> None:
        super().__init__()
        self.status = "STOP"
        self.memory_depth = str(MICSIG_MAXIMUM_ASCII_CHUNK_POINTS + 2)
        self.source = "CH4"
        self.mode = "RAW"
        self.waveform_format = "WORD"
        self.waveform_start = 7
        self.waveform_stop = 777

    async def query_text(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
        attempts: int = 2,
    ) -> str:
        if command == ":WAVeform:PREamble?":
            self.commands.append(command)
            format_code = 2 if self.waveform_format.upper() == "ASCII" else 0
            mode_code = 1 if self.mode.upper() == "MAXIMUM" else 0
            return f"{format_code},{mode_code},1,1e-6,0,0,0.01,0,0"
        return await super().query_text(
            command,
            timeout_s=timeout_s,
            attempts=attempts,
        )

    async def query_ascii_block(self, command: str) -> bytes:
        if command != ":WAVeform:DATA?":
            return await super().query_ascii_block(command)
        self.commands.append(command)
        channel = int(self.source[-1])
        return ",".join(
            str(channel + index / 10)
            for index in range(self.waveform_start, self.waveform_stop + 1)
        ).encode()


def _descriptor() -> MicsigDescriptor:
    return MicsigDescriptor(
        host="192.0.2.15",
        scpi_port=5025,
        screen_port=8888,
        identification=MicsigIdentification(
            manufacturer="Micsig",
            model="MHO14-200",
            serial_number="MHO1-DEMO-0001",
            firmware_version="2.154.75",
        ),
    )


def test_parses_mho1_identification_and_fast_waveform() -> None:
    identification = parse_identification("Micsig,MHO14-200,MHO1-DEMO-0001,2.154.75")
    samples = parse_fast_binary_waveform(struct.pack("<4i", -2048, -1, 0, 2047))

    assert identification.is_supported_mho1 is True
    assert identification.has_integrated_multimeter is True
    assert samples == (-2048, -1, 0, 2047)


def test_parses_ascii_waveform_with_optional_block_header() -> None:
    assert parse_ascii_waveform("1.25,-2.5,0") == (1.25, -2.5, 0.0)
    assert parse_ascii_waveform("#2111.25,-2.5,0") == (1.25, -2.5, 0.0)
    assert parse_ascii_waveform("1.25 -2.5 0") == (1.25, -2.5, 0.0)


@pytest.mark.asyncio
async def test_mho1_maximum_ascii_requires_existing_stop_without_depth_write() -> None:
    transport = MaximumMicsigTransport()
    transport.status = "RUN"
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    with pytest.raises(RuntimeError, match="already be in STOP"):
        await scope.maximum_ascii_capture_info()

    assert ":MENU:STOP" not in transport.commands
    assert ":MENU:RUN" not in transport.commands
    assert not any(command.startswith(":ACQuire:DEPSelect ") for command in transport.commands)


@pytest.mark.asyncio
async def test_mho1_maximum_ascii_streams_chunks_and_restores_reader_context() -> None:
    transport = MaximumMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)
    chunks: list[MicsigMaximumAsciiChunk] = []

    async def receive(chunk: MicsigMaximumAsciiChunk) -> None:
        chunks.append(chunk)

    info = await scope.stream_maximum_ascii(("CH1",), on_chunk=receive)

    assert info.memory_depth_points == MICSIG_MAXIMUM_ASCII_CHUNK_POINTS + 2
    assert [(item.start_point, item.stop_point) for item in chunks] == [
        (1, MICSIG_MAXIMUM_ASCII_CHUNK_POINTS),
        (MICSIG_MAXIMUM_ASCII_CHUNK_POINTS + 1, MICSIG_MAXIMUM_ASCII_CHUNK_POINTS + 2),
    ]
    assert sum(item.points for item in chunks) == info.memory_depth_points
    assert not any(command.startswith(":ACQuire:DEPSelect ") for command in transport.commands)
    assert ":MENU:STOP" not in transport.commands
    assert ":MENU:RUN" not in transport.commands
    assert transport.status == "STOP"
    assert transport.source == "CH4"
    assert transport.mode == "RAW"
    assert transport.waveform_format == "WORD"
    assert transport.waveform_start == 7
    assert transport.waveform_stop == 777
    assert transport.close_count > 10


@pytest.mark.asyncio
async def test_firmware_2_154_75_uses_fast_ascii_waveform_transfer() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    captures = await scope.capture_waveforms(("CH1", "CH2"))

    assert scope.waveform_transfer_method == "fast_ascii"
    assert [capture.points for capture in captures] == [4, 4]
    assert captures[0].voltage_at(0) == pytest.approx(100)
    assert captures[1].voltage_at(3) == pytest.approx(203)
    assert transport.waveform_format == "WORD"
    assert transport.waveform_start == 1
    assert transport.waveform_stop == 1100
    assert transport.status == "RUN"


@pytest.mark.asyncio
async def test_replaces_displayed_scalar_measurements() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    values = await scope.replace_scalar_measurements(
        (("CH1", "amplitude"), ("CH2", "peak_to_peak"))
    )

    assert transport.writes[-3:] == [
        ":MEASure:CLEar all",
        ":MEASure:OPEN AMP,CH1",
        ":MEASure:OPEN PKPK,CH2",
    ]
    assert [(item.channel, item.item) for item in values] == [
        ("CH1", "amplitude"),
        ("CH2", "peak_to_peak"),
    ]


@pytest.mark.asyncio
async def test_replaces_and_reads_two_channel_phase_and_delay() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)
    profile = (
        MicsigScalarMeasurementSpec("CH1", "phase", "CH2"),
        MicsigScalarMeasurementSpec("CH1", "delay", "CH2", "FRISe", "FFALL"),
    )

    values = await scope.replace_scalar_measurements(profile)

    assert transport.writes[-3:] == [
        ":MEASure:CLEar all",
        ":MEASure:OPEN PHASe,CH1,CH2",
        ":MEASure:OPEN DELay,CH1,CH2,FRISe,FFALL",
    ]
    assert ":MEASure:PHASe? CH1,CH2" in transport.commands
    assert ":MEASure:DELay? CH1,CH2,FRISe,FFALL" in transport.commands
    assert values[0].value == pytest.approx(2.25)
    assert values[0].unit == "deg"
    assert values[1].value == pytest.approx(1e-9)
    assert values[1].unit == "s"


@pytest.mark.asyncio
async def test_reads_ten_measurements_without_reconfiguring_front_panel() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)
    names = (
        "amplitude",
        "peak_to_peak",
        "rms",
        "frequency",
        "maximum",
        "minimum",
        "mean",
        "cycle_mean",
        "cycle_rms",
        "ac_rms",
    )
    profile = tuple(("CH1", item) for item in names)

    values = await scope.read_scalar_measurement_profile(profile)

    assert len(values) == MAX_SCALAR_MEASUREMENTS
    assert transport.writes == []
    assert transport.close_count == 1
    with pytest.raises(ValueError, match="No more than 10"):
        await scope.read_scalar_measurement_profile((*profile, ("CH2", "amplitude")))


@pytest.mark.asyncio
async def test_probes_vxi11_screenshot_without_stored_fallback() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    result = await scope.probe_direct_screenshot("vxi11")

    assert result.image_format == "png"
    assert result.payload_bytes == 13
    assert result.error is None


@pytest.mark.asyncio
async def test_probes_fast_binary_without_ascii_fallback() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    result = await scope.probe_fast_binary_waveform("CH1")

    assert result.source == "CH1"
    assert result.payload_bytes == 16
    assert len(result.data) == 16
    assert result.points == 4
    assert result.error is None
    assert transport.source == "CH2"


def test_repairs_mho1_malformed_jfif_marker() -> None:
    output = io.BytesIO()
    Image.new("RGB", (16, 12), "navy").save(output, format="JPEG")
    valid = output.getvalue()
    malformed = valid[:2] + b"\x58\x00" + valid[4:]

    repaired, image_format = normalize_screenshot_image(malformed)

    assert image_format == "jpeg"
    assert repaired[:10] == b"\xff\xd8\xff\xe0\x00\x10JFIF"
    Image.open(io.BytesIO(repaired)).verify()


@pytest.mark.asyncio
async def test_reuses_screenshot_within_one_second() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    first = await scope.capture_screenshot()
    second = await scope.capture_screenshot()

    assert second is first
    assert transport.screenshot_queries == 2
    assert transport.stored_screenshot_captures == 0


@pytest.mark.asyncio
async def test_captures_verified_ascii_frame_without_state_queries_or_measurement_setup() -> None:
    transport = FakeMicsigTransport()
    transport.empty_first_screenshot = False
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    frame = await scope.capture_frame(
        (("CH1", "amplitude"), ("CH2", "frequency")),
    )

    assert frame.elapsed_s > 0
    assert [item.source for item in frame.waveforms] == ["CH1", "CH2", "CH3", "CH4"]
    assert [item.points for item in frame.waveforms] == [4, 4, 4, 4]
    assert all(item.ascii_data for item in frame.waveforms)
    assert frame.waveforms[0].ascii_data == b"100 101 102 103"
    assert frame.waveforms[0].preamble_text == "0,0,1,1e-6,0.01,0,0.001,0,0"
    assert frame.waveforms[0].reported_preamble is not None
    assert frame.waveforms[0].reported_preamble.format_code == 0
    assert frame.waveforms[0].preamble.format_code == 2
    assert [(item.channel, item.item) for item in frame.measurements] == [
        ("CH1", "amplitude"),
        ("CH2", "frequency"),
    ]
    assert frame.screenshot is not None
    assert frame.screenshot_error is None
    assert frame.waveform_csv.startswith(
        b"sample_index,time_s,ch1_v,ch2_v,ch3_v,ch4_v\n"
    )
    assert transport.commands == [
        ":MENU:STOP",
        ":WAVeform:SOURce CH1",
        ":WAVeform:MODE NORMal",
        ":WAVeform:PREamble?",
        ":WAVeform:DATA:ASCii?",
        ":WAVeform:SOURce CH2",
        ":WAVeform:MODE NORMal",
        ":WAVeform:DATA:ASCii?",
        ":WAVeform:SOURce CH3",
        ":WAVeform:MODE NORMal",
        ":WAVeform:DATA:ASCii?",
        ":WAVeform:SOURce CH4",
        ":WAVeform:MODE NORMal",
        ":WAVeform:DATA:ASCii?",
        ":SYS:SCR?",
        ":MEASure:AMP? CH1",
        ":MEASure:FREQ? CH2",
        ":MENU:RUN",
    ]
    assert transport.status == "RUN"


@pytest.mark.asyncio
async def test_measurement_only_frame_does_not_stop_or_request_optional_artifacts() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    frame = await scope.capture_frame(
        (("CH1", "amplitude"), ("CH2", "frequency")),
        channels=(),
        include_screenshot=False,
    )

    assert frame.waveforms == ()
    assert frame.waveform_csv == b""
    assert frame.screenshot is None
    assert transport.commands == [
        ":MEASure:AMP? CH1",
        ":MEASure:FREQ? CH2",
    ]
    assert transport.status == "RUN"


@pytest.mark.asyncio
async def test_ascii_frame_reads_only_selected_channels_and_skips_screenshot() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    frame = await scope.capture_frame(
        (("CH1", "amplitude"),),
        channels=("CH2", "CH4"),
        include_screenshot=False,
    )

    assert [item.source for item in frame.waveforms] == ["CH2", "CH4"]
    assert frame.screenshot is None
    assert transport.commands == [
        ":MENU:STOP",
        ":WAVeform:SOURce CH2",
        ":WAVeform:MODE NORMal",
        ":WAVeform:PREamble?",
        ":WAVeform:DATA:ASCii?",
        ":WAVeform:SOURce CH4",
        ":WAVeform:MODE NORMal",
        ":WAVeform:DATA:ASCii?",
        ":MEASure:AMP? CH1",
        ":MENU:RUN",
    ]


@pytest.mark.asyncio
async def test_triggered_frame_reads_existing_stop_without_extra_stop_or_run() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    await scope.single()
    assert transport.status == "WAIT"
    wait_task = asyncio.create_task(scope.wait_for_trigger(timeout_s=0.25, poll_interval_s=0.01))
    await asyncio.sleep(0.02)
    transport.status = "STOP"
    assert await wait_task == "STOP"
    command_count = len(transport.commands)

    frame = await scope.capture_frame(
        (("CH1", "amplitude"),),
        channels=("CH1",),
        include_screenshot=False,
        stop_before_capture=False,
        resume_after=False,
    )

    assert frame.waveforms[0].source == "CH1"
    frame_commands = transport.commands[command_count:]
    assert ":MENU:STOP" not in frame_commands
    assert ":MENU:RUN" not in frame_commands
    assert transport.status == "STOP"


@pytest.mark.asyncio
async def test_waveform_failure_does_not_block_scalar_measurements() -> None:
    transport = FakeMicsigTransport()
    transport.empty_ascii_sources.add("CH2")
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    frame = await scope.capture_frame(
        (("CH1", "amplitude"),),
        channels=("CH2",),
        include_screenshot=False,
    )

    assert frame.waveforms == ()
    assert frame.waveform_error is not None
    assert [(item.channel, item.item) for item in frame.measurements] == [("CH1", "amplitude")]
    assert transport.commands[-2:] == [":MEASure:AMP? CH1", ":MENU:RUN"]
    assert transport.status == "RUN"


@pytest.mark.asyncio
async def test_saves_and_downloads_scope_csv_without_screen_pixels() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    stored = await scope.save_waveform_file(
        "CH1",
        file_type="CSV",
        filename="openbench_test_ch1",
        timeout_s=1,
    )

    assert stored.source == "CH1"
    assert stored.file_type == "CSV"
    assert stored.http_path == "/files/csvwave/openbench_test_ch1.csv"
    assert stored.data.startswith(b"time_s,voltage_v")
    assert transport.storage_source == "CH1"
    assert transport.storage_location == "LOCAL"
    assert transport.storage_type == "CSV"
    assert ":STORage:SAVE:SOURce CH1" not in transport.writes
    assert ":STORage:SAVE:FILename openbench_test_ch1" in transport.writes
    assert ":STORage:SAVE CH1" in transport.writes
    assert ":STORage:SAVE:STARt" in transport.writes


def test_builds_and_parses_vxi11_portmapper_packet() -> None:
    xid = 0x12345678
    request = _build_portmapper_getport_request(xid)
    response = struct.pack(
        ">7I",
        xid,
        1,  # REPLY
        0,  # MSG_ACCEPTED
        0,  # AUTH_NULL
        0,  # verifier length
        0,  # SUCCESS
        40223,
    )

    assert len(request) == 56
    assert _parse_portmapper_getport_response(response, xid) == 40223


@pytest.mark.asyncio
async def test_discovers_explicit_micsig_host() -> None:
    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        assert await reader.readline() == b"*IDN?\n"
        writer.write(b"Micsig,MHO14-200,MHO1-DEMO-0001,2.154.75\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        descriptors = await MicsigScpiTransport.discover(
            hosts=("127.0.0.1",),
            scpi_port=port,
            connect_timeout_s=0.2,
            scan_fallback=False,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert len(descriptors) == 1
    assert descriptors[0].host == "127.0.0.1"
    assert descriptors[0].model == "MHO14-200"


@pytest.mark.asyncio
async def test_connected_discovery_reuses_identification_connection() -> None:
    connection_count = 0

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        nonlocal connection_count
        connection_count += 1
        assert await reader.readline() == b"*IDN?\n"
        writer.write(b"Micsig,MHO14-200,MHO1-DEMO-0001,2.154.75\n")
        await writer.drain()
        assert await reader.readline() == b":TRIGger:STATus?\n"
        writer.write(b"RUN\n")
        await writer.drain()
        await reader.read()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    transports = await MicsigScpiTransport.discover_connected(
        hosts=("127.0.0.1",),
        scpi_port=port,
        connect_timeout_s=0.2,
        scan_fallback=False,
    )
    try:
        status = await transports[0].query_text(":TRIGger:STATus?")
    finally:
        await transports[0].close()
        server.close()
        await server.wait_closed()

    assert status == "RUN"
    assert connection_count == 1


@pytest.mark.asyncio
async def test_transport_consumes_binary_block_line_ending() -> None:
    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        assert await reader.readline() == b":WAVeform:DATA:BIN?\n"
        writer.write(b"#11data\r\n")
        await writer.drain()
        assert await reader.readline() == b":WAVeform:PREamble?\n"
        writer.write(b"0,0,1,1e-6,0,0,0.01,0,0\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    descriptor = MicsigDescriptor(
        host="127.0.0.1",
        scpi_port=port,
        screen_port=8888,
        identification=_descriptor().identification,
    )
    transport = MicsigScpiTransport(descriptor)
    try:
        payload = await transport.query_block(
            ":WAVeform:DATA:BIN?",
            length_multiplier=4,
        )
        preamble = await transport.query_text(":WAVeform:PREamble?")
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()

    assert payload == b"data"
    assert preamble == "0,0,1,1e-6,0,0,0.01,0,0"


@pytest.mark.asyncio
async def test_start_stop_and_single_use_existing_scope_settings() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    await scope.stop()
    await scope.start()
    await scope.single()

    assert transport.writes == [":MENU:STOP", ":MENU:RUN", ":MENU:SINGle"]
    assert transport.status == "WAIT"


@pytest.mark.asyncio
async def test_snapshot_saves_png_and_direct_measurements_without_pixel_traces() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    snapshot = await scope.capture_snapshot()

    measurement_rows = snapshot.measurements_csv.decode().splitlines()
    parsed_measurements = list(csv.DictReader(io.StringIO(snapshot.measurements_csv.decode())))
    assert snapshot.channels == ("CH1", "CH2", "CH3", "CH4")
    assert len(measurement_rows) == 25
    assert measurement_rows[0] == (
        "channel,measurement,value,unit,status,secondary_channel,source_edge,target_edge"
    )
    duty = next(
        row
        for row in parsed_measurements
        if row["channel"] == "CH3" and row["measurement"] == "positive_duty"
    )
    assert float(duty["value"]) == pytest.approx(48.27)
    assert snapshot.screenshot is not None
    assert snapshot.screenshot.image_format == "png"
    assert snapshot.screenshot_error is None
    assert transport.status == "STOP"
    assert transport.writes.count(":MENU:STOP") == 1
    assert ":MENU:RUN" not in transport.writes


@pytest.mark.asyncio
async def test_snapshot_can_resume_only_when_requested() -> None:
    transport = FakeMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    await scope.capture_snapshot(include_screenshot=False, resume=True)

    assert transport.status == "RUN"
    assert transport.writes[-1] == ":MENU:RUN"


@pytest.mark.asyncio
async def test_snapshot_does_not_stop_an_already_stopped_scope_again() -> None:
    transport = FakeMicsigTransport()
    transport.status = "STOP"
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    await scope.capture_snapshot(include_screenshot=False)

    assert ":MENU:STOP" not in transport.writes


@pytest.mark.asyncio
async def test_snapshot_scalars_do_not_depend_on_screen_trace_pixels() -> None:
    transport = EmptyScreenMicsigTransport()
    scope = MicsigMHO1Scope(_descriptor(), transport=transport)

    snapshot = await scope.capture_snapshot()

    rows = list(csv.DictReader(io.StringIO(snapshot.measurements_csv.decode())))
    reported = [row for row in rows if row["value"]]
    assert reported
    assert {row["status"] for row in reported} == {"ok"}


@pytest.mark.asyncio
async def test_scope_control_reads_applies_restores_and_transfers_waveforms() -> None:
    transport = ControlMicsigTransport()
    descriptor = _descriptor()
    descriptor = replace(
        descriptor,
        identification=replace(descriptor.identification, firmware_version="test"),
    )
    scope = MicsigMHO1Scope(descriptor, transport=transport)

    initial = await scope.read_state()
    updated = await scope.apply_update(
        MicsigScopeUpdate(
            channels=(MicsigChannelUpdate(channel=2, displayed=False, coupling="AC"),),
            timebase_s_per_div=0.0001,
            trigger=MicsigTriggerUpdate(mode="NORMAL", source="CH2", level_v=0.25),
        )
    )
    waveforms = await scope.capture_waveforms(("CH1", "CH2"))
    await scope.restore_state(initial)

    assert updated.channels[1].displayed is False
    assert updated.channels[1].coupling == "AC"
    assert updated.timebase_s_per_div == pytest.approx(0.0001)
    assert updated.trigger.source == "CH2"
    assert [item.source for item in waveforms] == ["CH1", "CH2"]
    assert [item.points for item in waveforms] == [4, 4]
    assert transport.displayed[2] is True
    assert transport.coupling[2] == "DC"
    assert transport.timebase == pytest.approx(initial.timebase_s_per_div)
