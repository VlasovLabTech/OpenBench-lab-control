from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from openbench.config import Settings
from openbench.core.capabilities import AsyncClosable
from openbench.core.events import MeasurementEventBus
from openbench.core.registry import DeviceRegistry
from openbench.core.scheduler import PollingScheduler
from openbench.domain import Channel, Device
from openbench.drivers.feeltech_fy import (
    FeelTechFYGenerator,
    FeelTechSerialTransport,
    FeelTechUnavailableError,
)
from openbench.drivers.fnirsi_dps150 import (
    FNIRSIDPS150,
    DPS150Descriptor,
    DPS150SerialTransport,
    DPS150UnavailableError,
)
from openbench.drivers.itech_it6000c import (
    ITechIT6000C,
    ITechIT6000CDescriptor,
    ITechIT6000CSerialTransport,
    ITechIT6000CUnavailableError,
)
from openbench.drivers.kingst_la2016 import (
    KingstLA2016,
    SigrokCLITransport,
    SigrokUnavailableError,
)
from openbench.drivers.micsig_common import is_micsig_scope_kind
from openbench.drivers.micsig_eto import MicsigETOScope, MicsigETOTransport
from openbench.drivers.micsig_mho1 import (
    MicsigMHO1Scope,
    MicsigScpiTransport,
    MicsigUnavailableError,
)
from openbench.drivers.owon_spm import (
    OwonSPMDescriptor,
    OwonSPMInstrument,
    OwonSPMSerialTransport,
    OwonSPMUnavailableError,
)
from openbench.drivers.simulated import SimulatedMatrix, SimulatedMeter
from openbench.drivers.ut61e import (
    CH9325HidTransport,
    UT61EMeter,
    UT61EUnavailableError,
)
from openbench.drivers.ut61eplus import (
    UT61EPlusMeter,
    UT61EPlusUnavailableError,
    discover_ut61eplus_descriptors,
)
from openbench.drivers.ut197 import UT197BleTransport, UT197Meter, UT197UnavailableError
from openbench.services.bidirectional_power_supply_service import (
    BidirectionalPowerSupplyService,
)
from openbench.services.capture_service import CaptureService
from openbench.services.dc_power_supply_service import DCPowerSupplyService
from openbench.services.device_service import DeviceService
from openbench.services.instrument_settings_service import InstrumentSettingsService
from openbench.services.logic_analyzer_service import LogicAnalyzerService
from openbench.services.matrix_service import MatrixService
from openbench.services.measurement_service import MeasurementService
from openbench.services.scope_maximum_capture_service import ScopeMaximumCaptureService
from openbench.services.scope_measurement_service import ScopeMeasurementService
from openbench.services.signal_generator_service import SignalGeneratorService
from openbench.services.source_measure_unit_service import SourceMeasureUnitService
from openbench.storage import Database, InstrumentPreferenceStore

logger = logging.getLogger(__name__)
ITECH_INSTRUMENT_TYPE = ITechIT6000C


@dataclass(slots=True)
class ApplicationContext:
    settings: Settings
    database: Database
    instrument_preferences: InstrumentPreferenceStore
    event_bus: MeasurementEventBus
    registry: DeviceRegistry
    device_service: DeviceService
    measurement_service: MeasurementService
    matrix_service: MatrixService
    scheduler: PollingScheduler
    scope_measurement_service: ScopeMeasurementService
    scope_maximum_capture_service: ScopeMaximumCaptureService
    capture_service: CaptureService
    instrument_settings_service: InstrumentSettingsService
    signal_generator_service: SignalGeneratorService
    dc_power_supply_service: DCPowerSupplyService
    source_measure_unit_service: SourceMeasureUnitService
    bidirectional_power_supply_service: BidirectionalPowerSupplyService
    logic_analyzer_service: LogicAnalyzerService


def register_simulated_meter(context: ApplicationContext) -> tuple[Device, ...]:
    meter = SimulatedMeter()
    if context.registry.has_device(meter.device_id):
        return (context.registry.device(meter.device_id),)
    device = Device(
        id=meter.device_id,
        name="Simulated Output Voltage Meter",
        kind="simulated_meter",
        connected=True,
        capabilities=("dc_voltage_meter",),
    )
    channel = Channel(
        id=meter.channel_id,
        device_id=meter.device_id,
        name="Output voltage",
        capability="dc_voltage_meter",
        unit="V",
        poll_interval_s=context.instrument_settings_service.preferred_poll_interval(
            meter.device_id,
            kind=device.kind,
            default=context.settings.poll_interval_s,
        ),
    )
    context.registry.register(device, meter, (channel,))
    context.device_service.register(device, (channel,))
    context.scheduler.add_target(channel, meter)
    return (device,)


async def disconnect_device(
    context: ApplicationContext,
    device_id: str,
) -> Device:
    if context.scope_maximum_capture_service.owns_device(device_id):
        raise RuntimeError("Micsig MAXIMUM ASCII capture is active; wait for it to finish")
    device = context.registry.device(device_id)
    if device.kind == "simulated_matrix":
        raise ValueError("The relay matrix cannot be disconnected from the device panel")
    instrument = context.registry.instrument(device_id)
    channels = tuple(
        channel for channel in context.registry.channels() if channel.device_id == device_id
    )
    if device.kind == "fnirsi_dps150":
        # Confirm Output OFF while the transport and registry entry are still
        # available. If this fails, keep the device registered for recovery.
        await context.dc_power_supply_service.remove_device(device_id)
    if device.kind == "owon_spm":
        await context.source_measure_unit_service.remove_device(device_id)
    if device.kind == "itech_it6000c":
        await context.bidirectional_power_supply_service.remove_device(device_id)
    if device.kind == "kingst_la2016":
        await context.logic_analyzer_service.remove_device(device_id)
    for channel in channels:
        if channel.id in {item.id for item in context.scheduler.target_channels()}:
            await context.scheduler.remove_target(channel.id)
    if is_micsig_scope_kind(device.kind):
        await context.scope_measurement_service.remove_scope(device_id)
    if isinstance(instrument, AsyncClosable):
        context.scheduler.remove_closable(instrument)
        await instrument.close()
    context.registry.unregister(device_id)
    context.device_service.register(replace(device, connected=False), channels)
    return device


async def register_ut197_devices(context: ApplicationContext) -> tuple[Device, ...]:
    if not context.settings.ut197_enabled:
        return ()
    try:
        meter_descriptors = await UT197BleTransport.discover(
            timeout_s=context.settings.ut197_scan_timeout_s
        )
    except UT197UnavailableError:
        return ()

    discovered: list[Device] = []
    for descriptor in meter_descriptors:
        physical_meter = UT197Meter(descriptor)
        if context.registry.has_device(physical_meter.device_id):
            discovered.append(context.registry.device(physical_meter.device_id))
            continue
        physical_device = Device(
            id=physical_meter.device_id,
            name=f"UNI-T UT197 ({descriptor.address})",
            kind="ut197",
            connected=True,
            capabilities=(
                "multimeter",
                "voltage",
                "current",
                "resistance",
                "temperature",
                "frequency",
                "capacitance",
            ),
        )
        physical_channel = Channel(
            id=physical_meter.channel_id,
            device_id=physical_meter.device_id,
            name="UT197 primary display",
            capability="multimeter_reading",
            unit="V",
            poll_interval_s=context.instrument_settings_service.preferred_poll_interval(
                physical_meter.device_id,
                kind=physical_device.kind,
                default=context.settings.ut197_poll_interval_s,
            ),
        )
        context.registry.register(
            physical_device,
            physical_meter,
            (physical_channel,),
        )
        context.device_service.register(physical_device, (physical_channel,))
        context.scheduler.add_target(physical_channel, physical_meter)
        discovered.append(physical_device)
    return tuple(discovered)


async def register_micsig_devices(context: ApplicationContext) -> tuple[Device, ...]:
    if not context.settings.micsig_enabled:
        return ()
    try:
        transports = await MicsigScpiTransport.discover_connected(
            hosts=context.settings.micsig_hosts,
            subnets=context.settings.micsig_scan_subnets,
            timeout_s=context.settings.micsig_discovery_timeout_s,
            scan_fallback=context.settings.micsig_scan_fallback,
            scpi_port=context.settings.micsig_scpi_port,
            http_port=context.settings.micsig_http_port,
        )
    except (MicsigUnavailableError, ValueError):
        return ()

    discovered: list[Device] = []
    for transport in transports:
        descriptor = transport.descriptor
        scope = MicsigMHO1Scope(descriptor, transport=transport)
        if context.registry.has_device(scope.device_id):
            await transport.close()
            discovered.append(context.registry.device(scope.device_id))
            continue
        device = Device(
            id=scope.device_id,
            name=f"Micsig {descriptor.model} ({descriptor.host})",
            kind="micsig_mho1",
            connected=True,
            capabilities=(
                "oscilloscope",
                "waveform_capture",
                "screenshot_capture",
                "scalar_measurements",
                "acquisition_control",
                "scope_settings_control",
            ),
        )
        context.registry.register(device, scope)
        context.device_service.register(device, ())
        context.scheduler.add_closable(scope)
        await context.scope_measurement_service.add_scope(scope)
        discovered.append(device)
    return tuple(discovered)


async def register_micsig_eto_devices(context: ApplicationContext) -> tuple[Device, ...]:
    if not context.settings.micsig_eto_enabled:
        return ()
    try:
        transports = await MicsigETOTransport.discover_connected(
            hosts=context.settings.micsig_eto_hosts,
            subnets=context.settings.micsig_eto_scan_subnets,
            timeout_s=context.settings.micsig_eto_discovery_timeout_s,
            scan_fallback=context.settings.micsig_eto_scan_fallback,
            scpi_port=context.settings.micsig_eto_scpi_port,
            http_port=context.settings.micsig_eto_http_port,
        )
    except (MicsigUnavailableError, ValueError):
        return ()

    discovered: list[Device] = []
    for transport in transports:
        descriptor = transport.descriptor
        scope = MicsigETOScope(descriptor, transport=transport)
        if context.registry.has_device(scope.device_id):
            await transport.close()
            discovered.append(context.registry.device(scope.device_id))
            continue
        device = Device(
            id=scope.device_id,
            name=f"Micsig {descriptor.model} ({descriptor.host})",
            kind="micsig_eto",
            connected=True,
            capabilities=(
                "oscilloscope",
                "waveform_capture",
                "screenshot_capture",
                "scalar_measurements",
                "acquisition_control",
                "scope_settings_control",
            ),
        )
        context.registry.register(device, scope)
        context.device_service.register(device, ())
        context.scheduler.add_closable(scope)
        await context.scope_measurement_service.add_scope(scope)
        discovered.append(device)
    return tuple(discovered)


async def register_kingst_devices(context: ApplicationContext) -> tuple[Device, ...]:
    if not context.settings.kingst_enabled:
        return ()
    try:
        descriptors = await SigrokCLITransport.discover(
            executable=context.settings.sigrok_cli_path,
            timeout_s=context.settings.kingst_discovery_timeout_s,
        )
    except SigrokUnavailableError:
        return ()

    discovered: list[Device] = []
    for descriptor in descriptors:
        analyzer = KingstLA2016(descriptor)
        if context.registry.has_device(analyzer.device_id):
            registered = context.registry.instrument(analyzer.device_id)
            if isinstance(registered, KingstLA2016):
                registered.update_descriptor(descriptor)
            await analyzer.close()
            discovered.append(context.registry.device(analyzer.device_id))
            continue
        device = Device(
            id=analyzer.device_id,
            name=f"Kingst {descriptor.model}",
            kind="kingst_la2016",
            connected=True,
            capabilities=(
                "logic_analyzer",
                "digital_capture",
                "hardware_trigger",
                "pretrigger",
                "sigrok_session",
            ),
        )
        context.registry.register(device, analyzer)
        context.device_service.register(device, ())
        context.scheduler.add_closable(analyzer)
        context.logic_analyzer_service.add_device(analyzer)
        discovered.append(device)
    return tuple(discovered)


async def register_feeltech_devices(
    context: ApplicationContext,
) -> tuple[Device, ...]:
    if not context.settings.feeltech_enabled:
        return ()
    try:
        excluded_ports = frozenset(
            instrument.descriptor.port
            for device in context.registry.devices()
            if device.kind == "owon_spm"
            for instrument in (context.registry.instrument(device.id),)
            if isinstance(instrument, OwonSPMInstrument)
        )
        descriptors = await asyncio.to_thread(
            FeelTechSerialTransport.discover,
            excluded_ports=excluded_ports,
        )
    except FeelTechUnavailableError:
        return ()

    discovered: list[Device] = []
    for descriptor in descriptors:
        generator = FeelTechFYGenerator(descriptor)
        if context.registry.has_device(generator.device_id):
            discovered.append(context.registry.device(generator.device_id))
            continue
        try:
            await generator.identify()
            first_channel_id = generator.parameters[0][0]
            await generator.read_meter(first_channel_id)
        except (OSError, TimeoutError, ValueError) as exc:
            logger.warning(
                "FeelTech probe failed for %s (%04X:%04X): %s",
                descriptor.port,
                descriptor.vid,
                descriptor.pid,
                exc,
            )
            await generator.close()
            continue

        device = Device(
            id=generator.device_id,
            name=generator.model,
            kind="feeltech_fy",
            connected=True,
            capabilities=(
                "signal_generator",
                "dual_channel",
                "waveform",
                "frequency",
                "amplitude",
                "offset",
                "duty_cycle",
                "phase",
                "output_state",
                "external_counter",
            ),
        )
        poll_interval_s = context.instrument_settings_service.preferred_poll_interval(
            generator.device_id,
            kind=device.kind,
            default=context.settings.feeltech_poll_interval_s,
        )
        channels = tuple(
            Channel(
                id=channel_id,
                device_id=generator.device_id,
                name=parameter.name,
                capability=(
                    f"external_counter_{parameter.key}"
                    if parameter.channel == 0
                    else f"signal_generator_{parameter.key}"
                ),
                unit=parameter.unit,
                poll_interval_s=poll_interval_s,
            )
            for channel_id, parameter in generator.parameters
        )
        context.registry.register(device, generator, channels)
        context.device_service.register(device, channels)
        for channel in channels:
            context.scheduler.add_target(channel, generator)
        discovered.append(device)
    return tuple(discovered)


async def register_dps150_devices(
    context: ApplicationContext,
) -> tuple[Device, ...]:
    if not context.settings.dps150_enabled:
        return ()
    descriptors: tuple[DPS150Descriptor, ...] = ()
    for attempt in range(2):
        try:
            descriptors = await asyncio.to_thread(DPS150SerialTransport.discover)
        except (DPS150UnavailableError, OSError):
            descriptors = ()
        if descriptors or attempt:
            break
        await asyncio.sleep(0.3)

    discovered: list[Device] = []
    for descriptor in descriptors:
        candidate = FNIRSIDPS150(descriptor)
        if context.registry.has_device(candidate.device_id):
            discovered.append(context.registry.device(candidate.device_id))
            continue
        supply: FNIRSIDPS150 | None = None
        for attempt in range(2):
            try:
                await candidate.identify()
                await candidate.read_state(force=True)
                supply = candidate
                break
            except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                await candidate.close()
                if attempt:
                    logger.warning(
                        "DPS-150 probe failed for %s (%04X:%04X): %s",
                        descriptor.port,
                        descriptor.vid,
                        descriptor.pid,
                        exc,
                    )
                    break
                await asyncio.sleep(0.3)
                candidate = FNIRSIDPS150(descriptor)
        if supply is None:
            continue

        identity = supply.identity
        device = Device(
            id=supply.device_id,
            name=identity.model if identity is not None else "DPS-150",
            kind="fnirsi_dps150",
            connected=True,
            capabilities=(
                "dc_power_supply",
                "voltage_setpoint",
                "current_limit",
                "output_state",
                "live_voltage",
                "live_current",
                "live_power",
                "protections",
                "presets",
                "metering",
                "program_sequence",
                "voltage_sweep",
                "current_sweep",
            ),
        )
        poll_interval_s = context.instrument_settings_service.preferred_poll_interval(
            supply.device_id,
            kind=device.kind,
            default=context.settings.dps150_poll_interval_s,
        )
        channels = tuple(
            Channel(
                id=channel_id,
                device_id=supply.device_id,
                name=parameter.name,
                capability=parameter.capability,
                unit=parameter.unit,
                poll_interval_s=poll_interval_s,
            )
            for channel_id, parameter in supply.parameters
        )
        context.registry.register(device, supply, channels)
        context.device_service.register(device, channels)
        for channel in channels:
            context.scheduler.add_target(channel, supply)
        discovered.append(device)
    return tuple(discovered)


async def register_owon_spm_devices(
    context: ApplicationContext,
) -> tuple[Device, ...]:
    if not context.settings.owon_spm_enabled:
        return ()
    excluded_ports = frozenset(
        instrument.descriptor.port
        for device in context.registry.devices()
        if device.kind == "feeltech_fy"
        for instrument in (context.registry.instrument(device.id),)
        if isinstance(instrument, FeelTechFYGenerator)
    )
    descriptors: tuple[OwonSPMDescriptor, ...] = ()
    for attempt in range(2):
        try:
            descriptors = await asyncio.to_thread(
                OwonSPMSerialTransport.discover,
                excluded_ports=excluded_ports,
            )
        except (OwonSPMUnavailableError, OSError):
            descriptors = ()
        if descriptors or attempt:
            break
        await asyncio.sleep(0.25)

    discovered: list[Device] = []
    for descriptor in descriptors:
        instrument = OwonSPMInstrument(descriptor)
        if context.registry.has_device(instrument.device_id):
            discovered.append(context.registry.device(instrument.device_id))
            continue
        try:
            await instrument.identify()
            await instrument.read_state(force=True)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            logger.warning(
                "OWON SPM probe failed for %s (%04X:%04X): %s",
                descriptor.port,
                descriptor.vid,
                descriptor.pid,
                exc,
            )
            await instrument.close()
            continue
        identity = instrument.identity
        device = Device(
            id=instrument.device_id,
            name=f"OWON {identity.model}",
            kind="owon_spm",
            connected=True,
            capabilities=(
                "source_measure_unit",
                "dc_power_supply",
                "multimeter",
                "voltage_setpoint",
                "current_limit",
                "output_state",
                "live_voltage",
                "live_current",
                "live_power",
                "protections",
                "dmm_function",
            ),
        )
        poll_interval_s = context.instrument_settings_service.preferred_poll_interval(
            instrument.device_id,
            kind=device.kind,
            default=context.settings.owon_spm_poll_interval_s,
        )
        channels = tuple(
            Channel(
                id=channel_id,
                device_id=instrument.device_id,
                name=parameter.name,
                capability=parameter.capability,
                unit=parameter.unit,
                poll_interval_s=poll_interval_s,
            )
            for channel_id, parameter in instrument.parameters
        )
        context.registry.register(device, instrument, channels)
        context.device_service.register(device, channels)
        for channel in channels:
            context.scheduler.add_target(channel, instrument)
        discovered.append(device)
    return tuple(discovered)


async def register_itech_it6000c_devices(
    context: ApplicationContext,
) -> tuple[Device, ...]:
    if not context.settings.itech_it6000c_enabled:
        return ()
    descriptors: tuple[ITechIT6000CDescriptor, ...] = ()
    for attempt in range(2):
        try:
            descriptors = await asyncio.to_thread(ITechIT6000CSerialTransport.discover)
        except (ITechIT6000CUnavailableError, OSError):
            descriptors = ()
        if descriptors or attempt:
            break
        await asyncio.sleep(0.25)

    discovered: list[Device] = []
    for descriptor in descriptors:
        instrument = ITechIT6000C(descriptor)
        if context.registry.has_device(instrument.device_id):
            existing_device = context.registry.device(instrument.device_id)
            if context.scheduler.device_connected(
                instrument.device_id,
                default=existing_device.connected,
            ):
                discovered.append(existing_device)
                continue
        try:
            await instrument.identify()
            await instrument.read_state(force=True, full=True)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            logger.warning(
                "ITECH IT6000C probe failed for %s (%04X:%04X at %d baud): %s",
                descriptor.port,
                descriptor.vid,
                descriptor.pid,
                descriptor.baud_rate,
                exc,
            )
            # ITECH discovery is read-only. In particular, do not route cleanup
            # through close(), which deliberately writes OUTP OFF for active use.
            await instrument.release_transport_for_reconnect()
            continue
        if context.registry.has_device(instrument.device_id):
            existing_instrument = context.registry.instrument(instrument.device_id)
            if not isinstance(existing_instrument, ITECH_INSTRUMENT_TYPE):
                await instrument.release_transport_for_reconnect()
                raise RuntimeError(
                    f"Registered ITECH device {instrument.device_id} has an invalid driver"
                )
            existing_channels = tuple(
                channel
                for channel in context.registry.channels()
                if channel.device_id == instrument.device_id
            )
            for channel in existing_channels:
                if channel.id in {item.id for item in context.scheduler.target_channels()}:
                    await context.scheduler.remove_target(channel.id)
            context.scheduler.remove_closable(existing_instrument)
            await existing_instrument.release_transport_for_reconnect()
            context.registry.unregister(instrument.device_id)
        identity = instrument.identity
        device = Device(
            id=instrument.device_id,
            name=f"ITECH {identity.model}",
            kind="itech_it6000c",
            connected=True,
            capabilities=(
                "bidirectional_power_supply",
                "source",
                "sink",
                "constant_voltage",
                "constant_current",
                "voltage_setpoint",
                "current_setpoint",
                "positive_negative_limits",
                "output_state",
                "live_voltage",
                "live_current",
                "live_power",
                "protections",
            ),
        )
        poll_interval_s = context.instrument_settings_service.preferred_poll_interval(
            instrument.device_id,
            kind=device.kind,
            default=context.settings.itech_it6000c_poll_interval_s,
        )
        channels = tuple(
            Channel(
                id=channel_id,
                device_id=instrument.device_id,
                name=parameter.name,
                capability=parameter.capability,
                unit=parameter.unit,
                poll_interval_s=poll_interval_s,
            )
            for channel_id, parameter in instrument.parameters
        )
        context.registry.register(device, instrument, channels)
        context.device_service.register(device, channels)
        for channel in channels:
            context.scheduler.add_target(channel, instrument)
        discovered.append(device)
    return tuple(discovered)


async def _register_original_ut61_devices(
    context: ApplicationContext,
    *,
    model: str,
) -> tuple[Device, ...]:
    if not context.settings.ut61e_enabled:
        return ()
    try:
        meter_descriptors = await asyncio.to_thread(CH9325HidTransport.discover)
    except UT61EUnavailableError:
        return ()

    discovered: list[Device] = []
    for descriptor in meter_descriptors:
        baud_rates = (2400, 19200) if model == "UT61D" else (19200,)
        candidates = tuple(
            UT61EMeter(descriptor, model=model, baud_rate=baud_rate) for baud_rate in baud_rates
        )
        if context.registry.has_device(candidates[0].device_id):
            discovered.append(context.registry.device(candidates[0].device_id))
            continue
        physical_meter: UT61EMeter | None = None
        for candidate in candidates:
            try:
                await candidate.read_meter(candidate.channel_id)
            except (OSError, TimeoutError, ValueError) as exc:
                logger.warning(
                    "%s probe failed for %s (%04X:%04X) at %d baud: %s",
                    model,
                    descriptor.serial_number or descriptor.path,
                    descriptor.vid,
                    descriptor.pid,
                    candidate.baud_rate,
                    exc,
                )
                await candidate.close()
                continue
            physical_meter = candidate
            break
        if physical_meter is None:
            continue
        physical_device = Device(
            id=physical_meter.device_id,
            name=f"UNI-T {model}",
            kind=model.casefold(),
            connected=True,
            capabilities=(
                "multimeter",
                "voltage",
                "current",
                "resistance",
                "frequency",
                "capacitance",
            ),
        )
        physical_channel = Channel(
            id=physical_meter.channel_id,
            device_id=physical_meter.device_id,
            name=f"{model} primary display",
            capability="multimeter_reading",
            unit="V",
            poll_interval_s=context.instrument_settings_service.preferred_poll_interval(
                physical_meter.device_id,
                kind=physical_device.kind,
                default=context.settings.ut61e_poll_interval_s,
            ),
        )
        context.registry.register(physical_device, physical_meter, (physical_channel,))
        context.device_service.register(physical_device, (physical_channel,))
        context.scheduler.add_target(physical_channel, physical_meter)
        discovered.append(physical_device)
    return tuple(discovered)


async def register_ut61d_devices(
    context: ApplicationContext,
) -> tuple[Device, ...]:
    return await _register_original_ut61_devices(context, model="UT61D")


async def register_ut61e_devices(
    context: ApplicationContext,
) -> tuple[Device, ...]:
    return await _register_original_ut61_devices(context, model="UT61E")


async def register_ut61eplus_devices(
    context: ApplicationContext,
) -> tuple[Device, ...]:
    if not context.settings.ut61eplus_enabled:
        return ()
    try:
        meter_descriptors = await asyncio.to_thread(discover_ut61eplus_descriptors)
    except UT61EPlusUnavailableError:
        return ()

    discovered: list[Device] = []
    for descriptor in meter_descriptors:
        physical_meter = UT61EPlusMeter(descriptor)
        if context.registry.has_device(physical_meter.device_id):
            discovered.append(context.registry.device(physical_meter.device_id))
            continue
        try:
            await physical_meter.read_meter(physical_meter.channel_id)
        except (OSError, TimeoutError, ValueError) as exc:
            logger.warning(
                "UT61E+ probe failed for %s (%04X:%04X, %s): %s",
                descriptor.serial_number or descriptor.path,
                descriptor.vid,
                descriptor.pid,
                descriptor.transport,
                exc,
            )
            continue
        physical_device = Device(
            id=physical_meter.device_id,
            name=f"UNI-T UT61E+ ({descriptor.serial_number or descriptor.transport})",
            kind="ut61eplus",
            connected=True,
            capabilities=(
                "multimeter",
                "voltage",
                "current",
                "resistance",
                "temperature",
                "frequency",
                "capacitance",
            ),
        )
        physical_channel = Channel(
            id=physical_meter.channel_id,
            device_id=physical_meter.device_id,
            name="UT61E+ primary display",
            capability="multimeter_reading",
            unit="V",
            poll_interval_s=context.instrument_settings_service.preferred_poll_interval(
                physical_meter.device_id,
                kind=physical_device.kind,
                default=context.settings.ut61eplus_poll_interval_s,
            ),
        )
        context.registry.register(physical_device, physical_meter, (physical_channel,))
        context.device_service.register(physical_device, (physical_channel,))
        context.scheduler.add_target(physical_channel, physical_meter)
        discovered.append(physical_device)
    return tuple(discovered)


def create_context(settings: Settings) -> ApplicationContext:
    database = Database(settings.database_url)
    database.create_schema()
    instrument_preferences = InstrumentPreferenceStore(database)

    event_bus = MeasurementEventBus(queue_size=32)
    registry = DeviceRegistry()
    device_service = DeviceService(database)
    measurement_service = MeasurementService(database)

    matrix = SimulatedMatrix()
    matrix_device = Device(
        id=matrix.device_id,
        name="Simulated Relay Matrix",
        kind="simulated_matrix",
        connected=True,
        capabilities=("relay_matrix",),
    )
    registry.register(matrix_device, matrix)
    device_service.register(matrix_device, ())
    matrix_service = MatrixService(database, matrix)
    matrix_service.initialize()

    scheduler = PollingScheduler(measurement_service, event_bus)
    scope_measurement_service = ScopeMeasurementService(
        event_bus,
        preferences=instrument_preferences,
    )
    capture_service = CaptureService(
        Path(settings.capture_directory),
        event_bus,
        scheduler,
        registry,
        scope_measurement_service,
        instrument_preferences,
    )
    scope_maximum_capture_service = ScopeMaximumCaptureService(
        Path(settings.capture_directory),
        registry,
        scope_measurement_service,
        capture_service,
    )
    logic_analyzer_service = LogicAnalyzerService(
        Path(settings.capture_directory),
        registry,
        capture_service,
        instrument_preferences,
    )
    capture_service.add_recording_listener(
        started=logic_analyzer_service.recording_started,
        stopped=logic_analyzer_service.recording_stopped,
    )
    instrument_settings_service = InstrumentSettingsService(
        registry,
        scheduler,
        device_service,
        scope_measurement_service,
        capture_service,
        instrument_preferences,
    )
    signal_generator_service = SignalGeneratorService(registry, matrix_service)
    dc_power_supply_service = DCPowerSupplyService(registry, matrix_service)
    source_measure_unit_service = SourceMeasureUnitService(registry, matrix_service)
    bidirectional_power_supply_service = BidirectionalPowerSupplyService(
        registry,
        matrix_service,
        scheduler,
    )
    capture_service.set_device_exclusion_provider(
        scope_maximum_capture_service.active_device_ids
    )
    context = ApplicationContext(
        settings=settings,
        database=database,
        instrument_preferences=instrument_preferences,
        event_bus=event_bus,
        registry=registry,
        device_service=device_service,
        measurement_service=measurement_service,
        matrix_service=matrix_service,
        scheduler=scheduler,
        scope_measurement_service=scope_measurement_service,
        scope_maximum_capture_service=scope_maximum_capture_service,
        capture_service=capture_service,
        instrument_settings_service=instrument_settings_service,
        signal_generator_service=signal_generator_service,
        dc_power_supply_service=dc_power_supply_service,
        source_measure_unit_service=source_measure_unit_service,
        bidirectional_power_supply_service=bidirectional_power_supply_service,
        logic_analyzer_service=logic_analyzer_service,
    )
    register_simulated_meter(context)
    return context
