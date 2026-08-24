from __future__ import annotations

from types import SimpleNamespace

import pytest

from openbench.drivers.feeltech_fy import (
    COUNTER_PARAMETERS,
    FEELTECH_PARAMETERS,
    FeelTechChannelUpdate,
    FeelTechDescriptor,
    FeelTechFYGenerator,
    FeelTechParameter,
    FeelTechProtocolError,
    FeelTechSerialTransport,
    encode_parameter_command,
    parse_model,
    parse_parameter,
)

REAL_RESPONSES = {
    "UMO": "FY6200-20M",
    "RMW": "0",
    "RMF": "00010000.000000",
    "RMA": "50000",
    "RMO": "0",
    "RMD": "50000",
    "RMP": "0",
    "RMN": "255",
    "RFW": "0",
    "RFF": "00010000.000000",
    "RFA": "50000",
    "RFO": "0",
    "RFD": "50000",
    "RFP": "0",
    "RFN": "0",
    "RPM": "0",
    "RPN": "10",
    "RTA": "0",
    "RTF": "0",
    "RFK": "1000",
    "RTP": "0",
    "RSS": "50000",
    "RCG": "0",
    "RCF": "1234",
    "RCC": "42",
    "RCT": "810373",
    "RC+": "405186",
    "RC-": "405187",
    "RCD": "500",
}


class StaticTransport:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.writes: list[str] = []
        self.closed = False

    def query(self, command: str) -> str:
        self.commands.append(command)
        return REAL_RESPONSES[command]

    def write(self, command: str) -> None:
        self.writes.append(command)

    def close(self) -> None:
        self.closed = True


class StatefulTransport(StaticTransport):
    def __init__(self) -> None:
        super().__init__()
        self.responses = dict(REAL_RESPONSES)
        self.sync = {index: False for index in range(5)}

    def query(self, command: str) -> str:
        self.commands.append(command)
        if command.startswith("RSA"):
            return "255" if self.sync[int(command[-1])] else "0"
        return self.responses[command]

    def write(self, command: str) -> None:
        super().write(command)
        prefix = command[:3]
        payload = command[3:]
        read_command = f"R{prefix[1:]}"
        if prefix in {"WMW", "WFW"}:
            self.responses[read_command] = str(int(payload))
        elif prefix in {"WMF", "WFF"}:
            self.responses[read_command] = str(float(payload))
        elif prefix in {"WMA", "WFA"}:
            self.responses[read_command] = str(round(float(payload) * 10_000))
        elif prefix in {"WMO", "WFO"}:
            encoded = round(float(payload) * 1_000)
            self.responses[read_command] = str(encoded % 0x1_0000_0000)
        elif prefix in {"WMD", "WFD"}:
            self.responses[read_command] = str(round(float(payload) * 1_000))
        elif prefix in {"WMP", "WFP"}:
            self.responses[read_command] = str(round(float(payload) * 1_000))
        elif prefix in {"WMN", "WFN"}:
            self.responses[read_command] = "255" if payload == "1" else "0"
        elif prefix in {"USA", "USD"}:
            self.sync[int(payload)] = prefix == "USA"
        elif prefix == "WPM":
            self.responses["RPM"] = payload
        elif prefix == "WPN":
            self.responses["RPN"] = payload
        elif prefix == "WTA":
            self.responses["RTA"] = payload
        elif prefix == "WTF":
            self.responses["RTF"] = payload
        elif prefix == "WFK":
            self.responses["RFK"] = str(int(payload) / 1_000_000)
        elif prefix == "WTP":
            self.responses["RTP"] = payload
        elif prefix == "WMS":
            self.responses["RSS"] = str(int(payload) * 10)
        elif prefix == "WCG":
            self.responses["RCG"] = payload
        elif prefix == "WCZ":
            self.responses["RCC"] = "0"


class MissFirstOutputTransport(StatefulTransport):
    def __init__(self) -> None:
        super().__init__()
        self.missed_output_write = False

    def write(self, command: str) -> None:
        if command.startswith("WMN") and not self.missed_output_write:
            StaticTransport.write(self, command)
            self.missed_output_write = True
            return
        super().write(command)


class IgnoringWriteTransport(StatefulTransport):
    def write(self, command: str) -> None:
        self.writes.append(command)


class FakeListPorts:
    @staticmethod
    def comports() -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                device="COM33",
                vid=0x1A86,
                pid=0x7523,
                serial_number=None,
                location="Port_#0004.Hub_#0001",
                description="USB-SERIAL CH340",
                manufacturer="wch.cn",
            ),
            SimpleNamespace(
                device="COM4",
                vid=None,
                pid=None,
                serial_number=None,
                location=None,
                description="Bluetooth serial",
                manufacturer="Microsoft",
            ),
        ]


class WakeOnSecondQueryPort:
    def __init__(self, response: bytes = b"FY6200-20M\n") -> None:
        self.is_open = True
        self.dtr = False
        self.rts = True
        self.timeout = 0.0
        self.writes: list[bytes] = []
        self.reads = 0
        self.closed = False
        self.response = response

    def reset_input_buffer(self) -> None:
        pass

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        pass

    def read_until(self, expected: bytes, size: int) -> bytes:
        self.reads += 1
        return b"" if self.reads == 1 else self.response

    def close(self) -> None:
        self.is_open = False
        self.closed = True


def descriptor() -> FeelTechDescriptor:
    return FeelTechDescriptor(
        port="COM33",
        vid=0x1A86,
        pid=0x7523,
        serial_number="",
        location="Port_#0004.Hub_#0001",
        description="USB-SERIAL CH340",
        manufacturer="wch.cn",
    )


def parameter(command: str) -> FeelTechParameter:
    return next(
        item for item in (*FEELTECH_PARAMETERS, *COUNTER_PARAMETERS) if item.command == command
    )


def test_parses_real_fy6200_responses() -> None:
    assert parse_model(REAL_RESPONSES["UMO"]) == "FY6200-20M"

    waveform = parse_parameter(parameter("RMW"), REAL_RESPONSES["RMW"])
    frequency = parse_parameter(parameter("RMF"), REAL_RESPONSES["RMF"])
    amplitude = parse_parameter(parameter("RMA"), REAL_RESPONSES["RMA"])
    duty = parse_parameter(parameter("RMD"), REAL_RESPONSES["RMD"])
    output = parse_parameter(parameter("RMN"), REAL_RESPONSES["RMN"])

    assert waveform.value == 0
    assert waveform.mode == "SINE"
    assert frequency.value == 10_000
    assert frequency.unit == "Hz"
    assert amplitude.value == 5
    assert amplitude.unit == "Vpp"
    assert duty.value == 50
    assert output.value == 1
    assert output.mode == "ON"


def test_parses_fy6200_signed_millivolt_offset() -> None:
    negative = parse_parameter(parameter("RFO"), "4294967046")
    positive = parse_parameter(parameter("RFO"), "250")

    assert negative.value == pytest.approx(-0.25)
    assert positive.value == pytest.approx(0.25)


def test_parses_fy6200_millidegree_phase() -> None:
    parsed = parse_parameter(parameter("RFP"), "12300")

    assert parsed.value == pytest.approx(12.3)


def test_parses_external_counter_measurements() -> None:
    frequency = parse_parameter(parameter("RCF"), "1234")
    count = parse_parameter(parameter("RCC"), "42")
    duty = parse_parameter(parameter("RCD"), "500")

    assert frequency.value == pytest.approx(1234)
    assert frequency.mode == "COUNTER FREQUENCY"
    assert count.value == 42
    assert count.unit == "pulses"
    assert duty.value == pytest.approx(50)


def test_rejects_unknown_model() -> None:
    with pytest.raises(FeelTechProtocolError, match="model response"):
        parse_model("not-a-feeltech")


def test_discovers_only_ch340_serial_adapter() -> None:
    descriptors = FeelTechSerialTransport.discover(
        list_ports_module=FakeListPorts,
    )

    assert len(descriptors) == 1
    assert descriptors[0].port == "COM33"
    assert descriptors[0].location == "Port_#0004.Hub_#0001"


def test_transport_rejects_write_commands_before_opening_port() -> None:
    transport = FeelTechSerialTransport(descriptor(), serial_module=object())

    with pytest.raises(ValueError, match="non-read-only"):
        transport.query("WMA5")


def test_encodes_documented_channel_commands() -> None:
    assert encode_parameter_command(parameter("RMW"), 2) == "WMW02"
    assert encode_parameter_command(parameter("RMF"), 123.456789) == "WMF00000123.456789"
    assert encode_parameter_command(parameter("RMA"), 2.5) == "WMA2.500"
    assert encode_parameter_command(parameter("RMO"), -0.25) == "WMO-0.250"
    assert encode_parameter_command(parameter("RMD"), 37.5) == "WMD37.500"
    assert encode_parameter_command(parameter("RMP"), 12.3) == "WMP12.30"
    assert encode_parameter_command(parameter("RMN"), True) == "WMN1"


def test_model_query_retries_once_when_device_ignores_first_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = WakeOnSecondQueryPort()
    serial_module = SimpleNamespace(Serial=lambda **_: port)
    monkeypatch.setattr("openbench.drivers.feeltech_fy.transport.time.sleep", lambda _: None)
    transport = FeelTechSerialTransport(descriptor(), serial_module=serial_module)

    response = transport.query("UMO")

    assert response == "FY6200-20M"
    assert port.writes == [b"UMO\n", b"UMO\n"]
    assert port.closed is False


def test_parameter_query_retries_once_when_device_ignores_first_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = WakeOnSecondQueryPort(b"10000.000000\n")
    serial_module = SimpleNamespace(Serial=lambda **_: port)
    monkeypatch.setattr("openbench.drivers.feeltech_fy.transport.time.sleep", lambda _: None)
    transport = FeelTechSerialTransport(descriptor(), serial_module=serial_module)

    response = transport.query("RMF")

    assert response == "10000.000000"
    assert port.writes == [b"RMF\n", b"RMF\n"]


@pytest.mark.asyncio
async def test_generator_identifies_and_caches_complete_dual_channel_state() -> None:
    transport = StaticTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)

    identity = await generator.identify()
    frequency_id = next(
        channel_id for channel_id, item in generator.parameters if item.command == "RMF"
    )
    amplitude_id = next(
        channel_id for channel_id, item in generator.parameters if item.command == "RFA"
    )
    frequency = await generator.read_meter(frequency_id)
    amplitude = await generator.read_meter(amplitude_id)
    await generator.close()

    assert identity == "FeelElec FY6200-20M on COM33"
    assert generator.model == "FY6200-20M"
    assert generator.device_id.startswith("feeltech_fy_")
    assert frequency.value == 10_000
    assert amplitude.value == 5
    assert transport.commands == [
        "UMO",
        *(item.command for item in FEELTECH_PARAMETERS),
    ]
    assert transport.closed is True


@pytest.mark.asyncio
async def test_generator_exposes_counter_as_live_channels_with_gate_scaling() -> None:
    transport = StatefulTransport()
    transport.responses["RCG"] = "1"
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()
    await generator.configure_counter(gate_code=1, coupling="dc", mode="frequency")
    frequency_id = next(
        channel_id for channel_id, item in generator.parameters if item.command == "RCF"
    )
    duty_id = next(channel_id for channel_id, item in generator.parameters if item.command == "RCD")

    frequency = await generator.read_meter(frequency_id)
    duty = await generator.read_meter(duty_id)

    assert frequency_id.endswith(".counter.frequency")
    assert frequency.value == pytest.approx(123.4)
    assert duty.value == pytest.approx(50)


@pytest.mark.asyncio
async def test_generator_hides_stale_counter_timings_when_input_is_idle() -> None:
    transport = StatefulTransport()
    transport.responses.update(
        {
            "RCF": "0",
            "RCC": "0",
            "RCT": "4935607400",
            "RC+": "1760377040",
            "RC-": "3175230360",
            "RCD": "9",
        }
    )
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()
    await generator.configure_counter(gate_code=0, coupling="dc", mode="frequency")
    period_id = next(
        channel_id
        for channel_id, item in generator.parameters
        if item.channel_suffix == "counter.period"
    )

    period = await generator.read_meter(period_id)
    advanced = await generator.read_advanced_state()

    assert period.value is None
    assert period.status == "idle"
    assert advanced.counter.period_ns is None
    assert advanced.counter.positive_width_ns is None
    assert advanced.counter.negative_width_ns is None
    assert advanced.counter.duty_percent is None


@pytest.mark.asyncio
async def test_generator_updates_off_channel_and_verifies_each_value() -> None:
    transport = StatefulTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()

    state = await generator.update_channel(
        2,
        FeelTechChannelUpdate(
            waveform_code=1,
            frequency_hz=1234.5,
            amplitude_vpp=2.5,
            offset_v=-0.25,
            duty_percent=37.5,
            phase_deg=12.3,
            output_enabled=False,
        ),
    )

    channel = state.channel(2)
    assert channel.waveform_name == "SQUARE"
    assert channel.frequency_hz == pytest.approx(1234.5)
    assert channel.amplitude_vpp == pytest.approx(2.5)
    assert channel.offset_v == pytest.approx(-0.25)
    assert channel.duty_percent == pytest.approx(37.5)
    assert channel.phase_deg == pytest.approx(12.3)
    assert channel.output_enabled is False
    assert transport.writes == [
        "WFW01",
        "WFF00001234.500000",
        "WFA2.500",
        "WFO-0.250",
        "WFD37.500",
        "WFP12.30",
    ]


def test_generator_accepts_high_frequency_dds_readback_quantization() -> None:
    parameter = next(
        item
        for item in FEELTECH_PARAMETERS
        if item.channel == 1 and item.key == "frequency"
    )

    assert FeelTechFYGenerator._matches(
        parameter,
        7_368_062.997280773,
        SimpleNamespace(value=7_368_062.931745),
    )
    assert not FeelTechFYGenerator._matches(
        parameter,
        7_368_062.997280773,
        SimpleNamespace(value=7_368_050.0),
    )


def test_generator_accepts_low_frequency_dds_readback_quantization() -> None:
    parameter = next(
        item
        for item in FEELTECH_PARAMETERS
        if item.channel == 1 and item.key == "frequency"
    )

    assert FeelTechFYGenerator._matches(
        parameter,
        1584.8931924611136,
        SimpleNamespace(value=1584.827656),
    )
    assert not FeelTechFYGenerator._matches(
        parameter,
        1584.8931924611136,
        SimpleNamespace(value=1583.0),
    )


@pytest.mark.asyncio
async def test_generator_temporarily_disables_active_output_while_reconfiguring() -> None:
    transport = StatefulTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()

    state = await generator.update_channel(
        1,
        FeelTechChannelUpdate(frequency_hz=20_000),
    )

    assert state.channel(1).output_enabled is True
    assert transport.writes == ["WMN0", "WMF00020000.000000", "WMN1"]


@pytest.mark.asyncio
async def test_generator_single_output_write_reads_only_that_output() -> None:
    transport = StatefulTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()
    transport.commands.clear()
    transport.writes.clear()

    enabled = await generator.set_channel_output(1, False)

    assert enabled is False
    assert transport.writes == ["WMN0"]
    assert transport.commands == ["RMN"]


@pytest.mark.asyncio
async def test_generator_single_output_retries_one_missed_hardware_write() -> None:
    transport = MissFirstOutputTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()
    transport.commands.clear()
    transport.writes.clear()

    enabled = await generator.set_channel_output(1, False)

    assert enabled is False
    assert transport.writes == ["WMN0", "WMN0"]
    assert transport.commands == ["RMN", "RMN"]


@pytest.mark.asyncio
async def test_generator_verifies_main_pulse_width_with_output_paused() -> None:
    transport = StatefulTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()

    state = await generator.update_channel(
        1,
        FeelTechChannelUpdate(pulse_width_ns=25_000),
    )
    advanced = await generator.read_advanced_state()

    assert state.channel(1).output_enabled is True
    assert advanced.main_pulse_width_ns == 25_000
    assert transport.writes == ["WMN0", "WMS2500", "WMN1"]


@pytest.mark.asyncio
async def test_generator_rejects_unsafe_combination_without_writing() -> None:
    transport = StatefulTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()

    with pytest.raises(ValueError, match="frequency must be"):
        await generator.update_channel(
            1,
            FeelTechChannelUpdate(waveform_code=1, frequency_hz=16_000_000),
        )

    assert transport.writes == []


@pytest.mark.asyncio
async def test_generator_rejects_hardware_clamped_amplitudes_without_writing() -> None:
    transport = StatefulTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()

    with pytest.raises(ValueError, match="Amplitude must be"):
        await generator.update_channel(
            1,
            FeelTechChannelUpdate(waveform_code=2, amplitude_vpp=19.999),
        )
    with pytest.raises(ValueError, match="Amplitude must be"):
        await generator.update_channel(
            1,
            FeelTechChannelUpdate(waveform_code=0, amplitude_vpp=20),
        )

    assert transport.writes == []


@pytest.mark.asyncio
async def test_generator_rejects_write_when_readback_did_not_change() -> None:
    transport = IgnoringWriteTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()

    with pytest.raises(RuntimeError, match="rejected"):
        await generator.update_channel(
            2,
            FeelTechChannelUpdate(frequency_hz=1234.5),
        )

    assert transport.writes == ["WFF00001234.500000"]


@pytest.mark.asyncio
async def test_generator_synchronization_is_verified() -> None:
    transport = StatefulTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()

    synchronization = await generator.set_synchronization("frequency", True)

    assert synchronization["frequency"] is True
    assert transport.writes == ["USA1"]


@pytest.mark.asyncio
async def test_generator_advanced_controls_are_read_back() -> None:
    transport = StatefulTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()

    burst = await generator.configure_burst(trigger_mode=1, cycles=68)
    keying = await generator.configure_keying(
        kind="fsk",
        mode=2,
        secondary_frequency_hz=234.5,
    )
    counter = await generator.configure_counter(gate_code=1, coupling="ac", mode="both")
    reset = await generator.reset_counter()
    paused = await generator.pause_counter()

    assert burst.burst.trigger_source == "ch2"
    assert burst.burst.cycles == 68
    assert keying.modulation.fsk_source == "manual"
    assert keying.modulation.fsk_secondary_frequency_hz == pytest.approx(234.5)
    assert counter.counter.gate_time_s == 10
    assert counter.counter.coupling == "ac"
    assert counter.counter.mode == "both"
    assert reset.counter.count == 0
    assert paused.counter.paused is True
    assert transport.writes == [
        "WPN68",
        "WPM1",
        "WFK00000234500000",
        "WTF2",
        "WCG1",
        "WCC1",
        "WCZ0",
        "WCP0",
    ]


@pytest.mark.asyncio
async def test_generator_counter_reads_only_the_selected_mode() -> None:
    transport = StatefulTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()

    transport.commands.clear()
    await generator.configure_counter(gate_code=0, coupling="dc", mode="frequency")
    assert "RCF" in transport.commands
    assert "RCC" not in transport.commands

    transport.commands.clear()
    await generator.configure_counter(gate_code=0, coupling="dc", mode="count")
    assert "RCC" in transport.commands
    assert "RCF" not in transport.commands
    assert "RCT" not in transport.commands

    transport.commands.clear()
    both = await generator.configure_counter(gate_code=0, coupling="dc", mode="both")
    assert "RCC" in transport.commands
    assert "RCF" in transport.commands
    assert both.counter.mode == "both"


@pytest.mark.asyncio
async def test_generator_sweep_is_write_only_and_reported_unverified() -> None:
    transport = StatefulTransport()
    generator = FeelTechFYGenerator(descriptor(), transport=transport)
    await generator.identify()

    sweep = await generator.configure_sweep(
        target="frequency",
        start=100,
        end=10_000,
        duration_s=2.5,
        mode="logarithmic",
        source="time",
        enabled=False,
    )

    assert sweep.enabled is False
    assert sweep.verified is False
    assert transport.writes == [
        "SBE0",
        "SOB0",
        "SST100",
        "SEN10000",
        "STI2.5",
        "SMO1",
        "SXY0",
        "SBE0",
    ]
