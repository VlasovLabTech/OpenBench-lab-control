from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from openbench.bootstrap import (
    ApplicationContext,
    disconnect_device,
    register_dps150_devices,
    register_feeltech_devices,
    register_itech_it6000c_devices,
    register_kingst_devices,
    register_micsig_devices,
    register_micsig_eto_devices,
    register_owon_spm_devices,
    register_simulated_meter,
    register_ut61d_devices,
    register_ut61e_devices,
    register_ut61eplus_devices,
    register_ut197_devices,
)
from openbench.domain import Channel, Device, MatrixConnection, MatrixProfile, Measurement
from openbench.drivers.feeltech_fy import WAVEFORM_OPTIONS, FeelTechChannelUpdate
from openbench.drivers.fnirsi_dps150 import (
    FNIRSIDPS150,
    DPS150DisplayUpdate,
    DPS150OutputUpdate,
    DPS150ProtectionUpdate,
)
from openbench.drivers.itech_it6000c import (
    ITechAdvancedUpdate,
    ITechIT6000C,
    ITechOperatingPointUpdate,
    ITechProtectionUpdate,
    safety_warnings,
)
from openbench.drivers.kingst_la2016 import (
    KINGST_SAMPLE_RATES_HZ,
    KINGST_THRESHOLDS_V,
    KingstTrigger,
)
from openbench.drivers.micsig_common import is_micsig_scope_kind
from openbench.drivers.micsig_mho1 import MICSIG_DELAY_EDGES
from openbench.drivers.owon_spm import (
    DMM_AUTO_RANGE_FUNCTIONS,
    DMM_FUNCTIONS,
    DMM_RANGES,
    DMM_RELATIVE_FUNCTIONS,
    OwonSPMDMMUpdate,
    OwonSPMInstrument,
    OwonSPMOutputUpdate,
    OwonSPMProtectionUpdate,
)
from openbench.services.dc_power_supply_service import PowerSequenceStep
from openbench.services.instrument_settings_service import (
    MAX_POLL_INTERVAL_S,
    MIN_POLL_INTERVAL_BY_KIND,
)
from openbench.services.matrix_service import (
    MatrixConflictError,
    MatrixNotFoundError,
    MatrixValidationError,
    SafetyInterlockError,
)
from openbench.services.scope_measurement_service import (
    MAX_SCOPE_MEASUREMENTS,
    SCOPE_CHANNELS,
    SCOPE_MEASUREMENT_LABELS,
    ScopeMeasurementSelection,
    scope_event_channel_id,
)

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
templates = Jinja2Templates(directory=WEB_DIR / "templates")
router = APIRouter(include_in_schema=False)


def _format_frequency(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:g} MHz"
    if value >= 1_000:
        return f"{value / 1_000:g} kHz"
    return f"{value:g} Hz"


templates.env.filters["format_frequency"] = _format_frequency

POLL_INTERVAL_OPTIONS_S = (0.1, 0.3, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 600.0)
LOGIC_SAMPLE_COUNT_OPTIONS = (
    20_000,
    200_000,
    1_000_000,
    2_000_000,
    5_000_000,
    10_000_000,
    20_000_000,
    50_000_000,
    100_000_000,
    200_000_000,
    500_000_000,
    1_000_000_000,
    2_000_000_000,
    5_000_000_000,
    10_000_000_000,
)
DEVICE_PRESENTATION = {
    "simulated_meter": ("Simulated Meter", "Multimeter"),
    "ut197": ("UT197", "Multimeter"),
    "ut61d": ("UT61D", "Multimeter"),
    "ut61e": ("UT61E", "Multimeter"),
    "ut61eplus": ("UT61E+", "Multimeter"),
    "micsig_mho1": ("MHO1", "Oscilloscope"),
    "micsig_eto": ("ETO5004", "Oscilloscope"),
    "fnirsi_dps150": ("DPS-150", "DC power supply"),
    "owon_spm": ("SPM6103", "DC power supply + multimeter"),
    "itech_it6000c": ("IT6054C-800-225", "Bidirectional DC power supply"),
    "kingst_la2016": ("LA2016", "Logic analyzer"),
}
SCOPE_MEASUREMENT_ORDER = (
    "amplitude",
    "peak_to_peak",
    "rms",
    "frequency",
    "period",
    "positive_duty",
    "negative_duty",
    "high",
    "low",
    "maximum",
    "minimum",
    "mean",
    "cycle_mean",
    "cycle_rms",
    "ac_rms",
    "rise_time",
    "fall_time",
    "positive_width",
    "negative_width",
    "positive_overshoot",
    "negative_overshoot",
    "burst_width",
    "positive_rate",
    "negative_rate",
    "phase",
    "delay",
)
SCOPE_DELAY_EDGE_LABELS = {
    "FRISe": "First rising",
    "FFALL": "First falling",
    "LRISe": "Last rising",
    "LFALL": "Last falling",
}
DISCOVERY_TARGETS = (
    {
        "id": "simulated",
        "name": "Simulated Meter",
        "transport": "Built in",
        "description": "Deterministic test source for checking the interface and CSV flow.",
    },
    {
        "id": "ut197",
        "name": "UNI-T UT197",
        "transport": "Bluetooth LE",
        "description": "Turn on the meter and its Bluetooth icon. No Windows pairing.",
    },
    {
        "id": "ut61d",
        "name": "UNI-T UT61D",
        "transport": "UT-D04 / CH9325 USB HID",
        "description": "Original UT61D. Hold USB/RS232 for 2 seconds before searching.",
    },
    {
        "id": "ut61e",
        "name": "UNI-T UT61E",
        "transport": "Legacy one-way adapter",
        "description": "Original UT61E without plus. Kept as a separate device type.",
    },
    {
        "id": "ut61eplus",
        "name": "UNI-T UT61E+",
        "transport": "CH9329 / CP2110 USB HID",
        "description": (
            "Connect any supported optical USB adapter and close the UNI-T application."
        ),
    },
    {
        "id": "micsig",
        "name": "Micsig MHO1",
        "transport": "LAN / VXI-11",
        "description": "Connect the scope to the same network and power it on.",
    },
    {
        "id": "micsig_eto",
        "name": "Micsig ETO5004",
        "transport": "Wi-Fi / LAN / VXI-11",
        "description": "Connect the ETO5004 to the same network and power it on.",
    },
    {
        "id": "feeltech",
        "name": "FeelElec FY series",
        "transport": "USB serial / CH340",
        "description": "Dual-channel generator. Detection reads its model and outputs.",
    },
    {
        "id": "dps150",
        "name": "FNIRSI DPS-150",
        "transport": "USB serial / AT32 VCP",
        "description": "Programmable 30 V / 5 A DC power supply over its Micro-USB port.",
    },
    {
        "id": "owon_spm",
        "name": "OWON SPM6103",
        "transport": "USB serial / CH340 / SCPI",
        "description": "Combined programmable DC source and 4.5-digit multimeter.",
    },
    {
        "id": "itech_it6000c",
        "name": "ITECH IT6054C-800-225",
        "transport": "USB VCP / SCPI / 115200 or 9600",
        "description": "800 V / 225 A / 54 kW bidirectional source and regenerative load.",
    },
    {
        "id": "kingst",
        "name": "Kingst LA2016",
        "transport": "USB / sigrok",
        "description": "16-channel logic analyzer. Requires sigrok firmware files.",
    },
)


def _context(request: Request) -> ApplicationContext:
    context: ApplicationContext = request.app.state.context
    return context


def _interval_label(interval_s: float) -> str:
    if interval_s < 1:
        return f"{1 / interval_s:g} Hz"
    if interval_s == 1:
        return "1 Hz"
    if interval_s < 60:
        return f"every {interval_s:g} s"
    if interval_s == 60:
        return "every minute"
    return "every 10 minutes"


def _dashboard_data(
    request: Request,
    *,
    notice: str | None = None,
    errors: tuple[str, ...] = (),
) -> dict[str, object]:
    context = _context(request)
    live_devices = tuple(
        device for device in context.registry.devices() if device.kind != "simulated_matrix"
    )
    channels = context.registry.channels()
    device_cards: list[dict[str, object]] = []
    for device in live_devices:
        device_channels = tuple(channel for channel in channels if channel.device_id == device.id)
        is_scope = is_micsig_scope_kind(device.kind)
        is_generator = device.kind == "feeltech_fy"
        is_power_supply = device.kind == "fnirsi_dps150"
        is_source_measure_unit = device.kind == "owon_spm"
        is_bidirectional_power_supply = device.kind == "itech_it6000c"
        is_logic_analyzer = device.kind == "kingst_la2016"
        maximum_capture = (
            context.scope_maximum_capture_service.status(device.id) if is_scope else None
        )
        physical_connected = (
            context.scope_measurement_service.device_connected(
                device.id,
                default=device.connected,
            )
            if is_scope
            else context.scheduler.device_connected(
                device.id,
                default=device.connected,
            )
        )
        device_connected = physical_connected
        if is_generator:
            display_name, device_type = device.name, "Signal generator"
        elif is_power_supply:
            display_name, device_type = "DPS-150", "DC power supply"
        elif is_source_measure_unit:
            display_name, device_type = DEVICE_PRESENTATION["owon_spm"]
        elif is_bidirectional_power_supply:
            display_name, device_type = DEVICE_PRESENTATION["itech_it6000c"]
        elif is_logic_analyzer:
            display_name, device_type = "LA2016", "Logic analyzer"
        else:
            display_name, device_type = DEVICE_PRESENTATION.get(
                device.kind,
                (device.name, device.kind.replace("_", " ").title()),
            )
        channel_cards: list[dict[str, object]] = []
        minimum = MIN_POLL_INTERVAL_BY_KIND.get(device.kind, 0.1)
        for channel in device_channels:
            current_interval = context.scheduler.interval_for(channel.id)
            interval_values = {value for value in POLL_INTERVAL_OPTIONS_S if value >= minimum}
            interval_values.add(current_interval)
            channel_cards.append(
                {
                    "channel": channel,
                    "latest": context.measurement_service.latest(channel.id),
                    "poll_interval_s": current_interval,
                    "polling_rate": _interval_label(current_interval),
                    "minimum_interval_s": minimum,
                    "freshness_timeout_s": context.scheduler.freshness_timeout_for(channel.id),
                    "interval_options": tuple(
                        {
                            "value": value,
                            "label": _interval_label(value),
                        }
                        for value in sorted(interval_values)
                    ),
                }
            )
        scope_measurements: list[dict[str, object]] = []
        if is_scope:
            current_device_interval = context.scope_measurement_service.interval_for(device.id)
            for selection in context.scope_measurement_service.selections(device.id):
                latest = context.scope_measurement_service.latest_for(
                    device.id,
                    selection,
                )
                scope_measurements.append(
                    {
                        "selection": selection,
                        "latest": latest,
                        "event_channel_id": scope_event_channel_id(
                            device.id,
                            selection.channel,
                            selection.item,
                            secondary_channel=selection.secondary_channel,
                            source_edge=selection.source_edge,
                            target_edge=selection.target_edge,
                        ),
                        "freshness_timeout_s": (
                            context.scope_measurement_service.freshness_timeout_for(device.id)
                        ),
                    }
                )
        elif channel_cards:
            current_device_interval = context.scheduler.interval_for(device_channels[0].id)
        else:
            current_device_interval = minimum
        device_interval_values = {value for value in POLL_INTERVAL_OPTIONS_S if value >= minimum}
        device_interval_values.add(current_device_interval)
        instrument_context = context.capture_service.instrument_context(device.id)
        context_preview = " ".join(instrument_context.split())
        if len(context_preview) > 20:
            context_preview = f"{context_preview[:20]}…"
        generator_outputs: list[dict[str, object]] = []
        generator_counter: dict[str, dict[str, object]] = {}
        active_generator_outputs: list[str] = []
        if is_generator:
            for output_number in (1, 2):
                marker = f".ch{output_number}."
                parameters: dict[str, dict[str, object]] = {}
                for channel_card in channel_cards:
                    channel = cast(Channel, channel_card["channel"])
                    if marker in channel.id:
                        parameters[channel.id.rsplit(".", 1)[-1]] = channel_card
                generator_outputs.append(
                    {
                        "number": output_number,
                        "label": f"CH{output_number}",
                        "parameters": parameters,
                    }
                )
                output = parameters.get("output")
                latest_output = cast(Measurement | None, output.get("latest")) if output else None
                if latest_output is not None and latest_output.quality == "ON":
                    active_generator_outputs.append(f"CH{output_number}")
            for channel_card in channel_cards:
                channel = cast(Channel, channel_card["channel"])
                if ".counter." in channel.id:
                    generator_counter[channel.id.rsplit(".", 1)[-1]] = channel_card
        if active_generator_outputs:
            generator_active_label = f"ACTIVE {' + '.join(active_generator_outputs)}"
        else:
            generator_active_label = "OUTPUTS OFF"
        power_supply_parameters: dict[str, dict[str, object]] = {}
        power_supply_state = None
        if is_power_supply:
            for channel_card in channel_cards:
                channel = cast(Channel, channel_card["channel"])
                power_supply_parameters[channel.id.rsplit(".", 1)[-1]] = channel_card
            power_supply_instrument = context.registry.instrument(device.id)
            if isinstance(power_supply_instrument, FNIRSIDPS150):
                power_supply_state = power_supply_instrument.cached_state
        power_output = power_supply_parameters.get("output")
        latest_power_output = (
            cast(Measurement | None, power_output.get("latest")) if power_output else None
        )
        power_supply_active_label = (
            "OUTPUT ON"
            if latest_power_output is not None and latest_power_output.quality == "ON"
            else "OUTPUT OFF"
        )
        source_measure_parameters: dict[str, dict[str, object]] = {}
        source_measure_state = None
        if is_source_measure_unit:
            for channel_card in channel_cards:
                channel = cast(Channel, channel_card["channel"])
                source_measure_parameters[channel.id.rsplit(".", 1)[-1]] = channel_card
            source_measure_instrument = context.registry.instrument(device.id)
            if isinstance(source_measure_instrument, OwonSPMInstrument):
                source_measure_state = source_measure_instrument.cached_state
        bidirectional_parameters: dict[str, dict[str, object]] = {}
        bidirectional_state = None
        bidirectional_warnings: dict[str, tuple[dict[str, str], ...]] = {}
        bidirectional_instrument = None
        if is_bidirectional_power_supply:
            for channel_card in channel_cards:
                channel = cast(Channel, channel_card["channel"])
                bidirectional_parameters[channel.id.rsplit(".", 1)[-1]] = channel_card
            candidate = context.registry.instrument(device.id)
            if isinstance(candidate, ITechIT6000C):
                bidirectional_instrument = candidate
                bidirectional_state = candidate.cached_state
                if bidirectional_state is not None:
                    grouped: dict[str, list[dict[str, str]]] = {}
                    for warning in safety_warnings(bidirectional_state):
                        grouped.setdefault(warning["field"], []).append(warning)
                    bidirectional_warnings = {
                        field: tuple(items) for field, items in grouped.items()
                    }
        device_cards.append(
            {
                "device": device,
                "display_name": display_name,
                "device_type": device_type,
                "connected": device_connected,
                "physical_connected": physical_connected,
                "channels": tuple(channel_cards),
                "simulated": device.kind.startswith("simulated"),
                "scope": is_scope,
                "generator": is_generator,
                "power_supply": is_power_supply,
                "source_measure_unit": is_source_measure_unit,
                "bidirectional_power_supply": is_bidirectional_power_supply,
                "logic_analyzer": is_logic_analyzer,
                "generator_outputs": tuple(generator_outputs),
                "generator_counter": generator_counter,
                "generator_active_label": generator_active_label,
                "generator_waveforms": WAVEFORM_OPTIONS if is_generator else (),
                "power_supply_parameters": power_supply_parameters,
                "power_supply_active_label": power_supply_active_label,
                "power_supply_state": power_supply_state,
                "power_program": (
                    context.dc_power_supply_service.program_status(device.id)
                    if is_power_supply
                    else None
                ),
                "source_measure_parameters": source_measure_parameters,
                "source_measure_state": source_measure_state,
                "source_measure_dmm_functions": tuple(DMM_FUNCTIONS),
                "source_measure_dmm_ranges": DMM_RANGES,
                "source_measure_dmm_auto_ranges": DMM_AUTO_RANGE_FUNCTIONS,
                "source_measure_dmm_relative": DMM_RELATIVE_FUNCTIONS,
                "bidirectional_parameters": bidirectional_parameters,
                "bidirectional_state": bidirectional_state,
                "bidirectional_warnings": bidirectional_warnings,
                "bidirectional_instrument": bidirectional_instrument,
                "logic_settings": (
                    context.logic_analyzer_service.settings(device.id)
                    if is_logic_analyzer
                    else None
                ),
                "logic_status": (
                    context.logic_analyzer_service.status(device.id) if is_logic_analyzer else None
                ),
                "maximum_capture": maximum_capture,
                "scope_options": (
                    context.capture_service.scope_options(device.id) if is_scope else None
                ),
                "scope_measurements": tuple(scope_measurements),
                "scope_measurement_slots": tuple(
                    scope_measurements[index] if index < len(scope_measurements) else None
                    for index in range(MAX_SCOPE_MEASUREMENTS)
                ),
                "instrument_context": instrument_context,
                "context_preview": context_preview,
                "poll_interval_s": current_device_interval,
                "polling_rate": _interval_label(current_device_interval),
                "interval_options": tuple(
                    {
                        "value": value,
                        "label": _interval_label(value),
                    }
                    for value in sorted(device_interval_values)
                ),
            }
        )

    enabled_by_driver = {
        "simulated": True,
        "ut197": context.settings.ut197_enabled,
        "ut61d": context.settings.ut61e_enabled,
        "ut61e": context.settings.ut61e_enabled,
        "ut61eplus": context.settings.ut61eplus_enabled,
        "feeltech": context.settings.feeltech_enabled,
        "dps150": context.settings.dps150_enabled,
        "owon_spm": context.settings.owon_spm_enabled,
        "itech_it6000c": context.settings.itech_it6000c_enabled,
        "micsig": context.settings.micsig_enabled,
        "micsig_eto": context.settings.micsig_eto_enabled,
        "kingst": context.settings.kingst_enabled,
    }
    connected_kinds = {
        device.kind for device in context.registry.devices() if device.kind != "simulated_matrix"
    }
    discovery_targets = tuple(
        {
            **target,
            "enabled": enabled_by_driver[target["id"]],
            "connected": (
                target["id"] in connected_kinds
                or (target["id"] == "micsig" and "micsig_mho1" in connected_kinds)
                or (target["id"] == "micsig_eto" and "micsig_eto" in connected_kinds)
                or (target["id"] == "feeltech" and "feeltech_fy" in connected_kinds)
                or (target["id"] == "dps150" and "fnirsi_dps150" in connected_kinds)
                or (target["id"] == "owon_spm" and "owon_spm" in connected_kinds)
                or (
                    target["id"] == "itech_it6000c"
                    and "itech_it6000c" in connected_kinds
                )
                or (target["id"] == "simulated" and "simulated_meter" in connected_kinds)
                or (target["id"] == "kingst" and "kingst_la2016" in connected_kinds)
            ),
        }
        for target in DISCOVERY_TARGETS
    )
    safety = context.matrix_service.safety_state()
    system_operational = safety.state == "safe"
    return {
        "request": request,
        "safety": safety,
        "system_status": "OPERATIONAL" if system_operational else "INTERLOCKED",
        "system_operational": system_operational,
        "device_cards": tuple(device_cards),
        "connected_count": sum(bool(card["connected"]) for card in device_cards),
        "known_count": len(live_devices),
        "discovery_targets": discovery_targets,
        "capture": context.capture_service.status(),
        "scope_channels": SCOPE_CHANNELS,
        "scope_delay_edges": tuple(
            {"value": edge, "label": SCOPE_DELAY_EDGE_LABELS[edge]} for edge in MICSIG_DELAY_EDGES
        ),
        "scope_measurement_options": tuple(
            {
                "value": item,
                "label": SCOPE_MEASUREMENT_LABELS[item],
            }
            for item in SCOPE_MEASUREMENT_ORDER
        ),
        "max_scope_measurements": MAX_SCOPE_MEASUREMENTS,
        "logic_sample_rates_hz": KINGST_SAMPLE_RATES_HZ,
        "logic_sample_counts": LOGIC_SAMPLE_COUNT_OPTIONS,
        "logic_thresholds_v": KINGST_THRESHOLDS_V,
        "active": context.matrix_service.active(),
        "notice": notice,
        "errors": errors,
    }


def _render_dashboard(
    request: Request,
    *,
    notice: str | None = None,
    errors: tuple[str, ...] = (),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="partials/dashboard_content.html",
        context=_dashboard_data(request, notice=notice, errors=errors),
    )


def _render_logic_analyzer_panel(
    request: Request,
    device_id: str,
    *,
    notice: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    cards = cast(tuple[dict[str, object], ...], _dashboard_data(request)["device_cards"])
    card = next(
        (
            item
            for item in cards
            if cast(Device, item["device"]).id == device_id and bool(item["logic_analyzer"])
        ),
        None,
    )
    if card is None:
        raise HTTPException(status_code=404, detail="Logic analyzer not found")
    return templates.TemplateResponse(
        request=request,
        name="partials/logic_analyzer_panel.html",
        context={
            "card": card,
            "capture": _context(request).capture_service.status(),
            "logic_notice": notice,
            "logic_error": error,
        },
    )


def _render_scope_maximum_capture_panel(
    request: Request,
    device_id: str,
    *,
    notice: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    cards = cast(tuple[dict[str, object], ...], _dashboard_data(request)["device_cards"])
    card = next(
        (
            item
            for item in cards
            if cast(Device, item["device"]).id == device_id and item["maximum_capture"] is not None
        ),
        None,
    )
    if card is None:
        raise HTTPException(status_code=404, detail="Supported Micsig oscilloscope not found")
    return templates.TemplateResponse(
        request=request,
        name="partials/scope_maximum_capture_panel.html",
        context={
            "card": card,
            "maximum_notice": notice,
            "maximum_error": error,
        },
    )


async def _render_generator_advanced(
    request: Request,
    device_id: str,
    *,
    notice: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    advanced = None
    if error is None:
        try:
            advanced = await _context(request).signal_generator_service.advanced_state(device_id)
        except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
            error = str(exc)
    return templates.TemplateResponse(
        request=request,
        name="partials/generator_advanced.html",
        context={
            "device_id": device_id,
            "advanced": advanced,
            "notice": notice,
            "error": error,
        },
    )


def _selected_profile(
    profiles: tuple[MatrixProfile, ...],
    profile_id: str | None,
) -> MatrixProfile | None:
    if profile_id:
        return next((profile for profile in profiles if profile.id == profile_id), None)
    return profiles[0] if profiles else None


def _matrix_data(
    request: Request,
    profile_id: str | None = None,
    *,
    notice: str | None = None,
    errors: tuple[str, ...] = (),
) -> dict[str, object]:
    context = _context(request)
    profiles = context.matrix_service.list_profiles()
    return {
        "request": request,
        "ports": context.matrix_service.list_ports(),
        "profiles": profiles,
        "selected": _selected_profile(profiles, profile_id),
        "active": context.matrix_service.active(),
        "safety": context.matrix_service.safety_state(),
        "notice": notice,
        "errors": errors,
    }


def _render_matrix_panel(
    request: Request,
    profile_id: str | None = None,
    *,
    notice: str | None = None,
    errors: tuple[str, ...] = (),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="partials/matrix_panel.html",
        context=_matrix_data(
            request,
            profile_id,
            notice=notice,
            errors=errors,
        ),
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=_dashboard_data(request),
    )


@router.get("/ui/language/{language}", response_class=RedirectResponse)
def set_ui_language(
    language: str,
    next_url: str = Query(default="/", alias="next"),
) -> RedirectResponse:
    if language not in {"en", "ru"}:
        raise HTTPException(status_code=404, detail="Unsupported interface language")
    destination = next_url if next_url.startswith("/") and not next_url.startswith("//") else "/"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        "openbench_language",
        language,
        max_age=31_536_000,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/ui/dashboard/content", response_class=HTMLResponse)
def dashboard_content(request: Request) -> HTMLResponse:
    return _render_dashboard(request)


@router.post("/ui/devices/discover/{driver_id}", response_class=HTMLResponse)
async def discover_device(request: Request, driver_id: str) -> HTMLResponse:
    context = _context(request)
    if driver_id == "all":
        itech_devices = await register_itech_it6000c_devices(context)
        owon_devices = await register_owon_spm_devices(context)
        physical_results = await asyncio.gather(
            register_dps150_devices(context),
            register_feeltech_devices(context),
            register_ut197_devices(context),
            register_ut61d_devices(context),
            register_ut61eplus_devices(context),
            register_micsig_devices(context),
            register_micsig_eto_devices(context),
            register_kingst_devices(context),
        )
        devices = (
            itech_devices
            + owon_devices
            + tuple(device for result in physical_results for device in result)
        )
        names = ", ".join(device.name for device in devices)
        return _render_dashboard(
            request,
            notice=f"Search complete. Active: {names}",
        )
    if driver_id == "simulated":
        devices = register_simulated_meter(context)
        return _render_dashboard(
            request,
            notice=f"Connected: {devices[0].name}",
        )
    discovery = {
        "ut197": register_ut197_devices,
        "ut61d": register_ut61d_devices,
        "ut61e": register_ut61e_devices,
        "ut61eplus": register_ut61eplus_devices,
        "feeltech": register_feeltech_devices,
        "dps150": register_dps150_devices,
        "owon_spm": register_owon_spm_devices,
        "itech_it6000c": register_itech_it6000c_devices,
        "micsig": register_micsig_devices,
        "micsig_eto": register_micsig_eto_devices,
        "kingst": register_kingst_devices,
    }.get(driver_id)
    if discovery is None:
        raise HTTPException(status_code=404, detail="Unknown device driver")
    try:
        devices = await discovery(context)
    except (OSError, RuntimeError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    if not devices:
        target = next(item for item in DISCOVERY_TARGETS if item["id"] == driver_id)
        return _render_dashboard(
            request,
            errors=(f"{target['name']} was not found. Check power and connection.",),
        )
    names = ", ".join(device.name for device in devices)
    return _render_dashboard(request, notice=f"Connected: {names}")


@router.post("/ui/devices/{device_id}/disconnect", response_class=HTMLResponse)
async def disconnect_instrument(request: Request, device_id: str) -> HTMLResponse:
    context = _context(request)
    try:
        device = await disconnect_device(context, device_id)
    except (KeyError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice=f"Disconnected: {device.name}")


@router.post("/ui/devices/{device_id}/context", response_class=Response)
async def update_instrument_context(
    request: Request,
    device_id: str,
    instrument_context: str = Form(default="", max_length=10000),
) -> Response:
    try:
        _context(request).instrument_settings_service.update_context(
            device_id,
            instrument_context,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=204)


@router.post("/ui/devices/{device_id}/polling", response_class=Response)
async def update_device_polling(
    request: Request,
    device_id: str,
    interval_s: float = Form(),
) -> Response:
    context = _context(request)
    try:
        await context.instrument_settings_service.update_poll_interval(
            device_id,
            interval_s,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=204)


@router.get(
    "/ui/devices/{device_id}/scope-maximum-capture/status",
    response_class=HTMLResponse,
)
def scope_maximum_capture_status_panel(
    request: Request,
    device_id: str,
) -> HTMLResponse:
    status = _context(request).scope_maximum_capture_service.status(device_id)
    if status.active:
        return _render_scope_maximum_capture_panel(request, device_id)
    response = _render_dashboard(request)
    response.headers["HX-Retarget"] = "#dashboard-content"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@router.post(
    "/ui/devices/{device_id}/scope-maximum-capture/start",
    response_class=HTMLResponse,
)
async def start_scope_maximum_capture(
    request: Request,
    device_id: str,
) -> HTMLResponse:
    form = await request.form()
    channels = tuple(str(value) for value in form.getlist("maximum_channel"))
    try:
        status = await _context(request).scope_maximum_capture_service.start_capture(
            device_id,
            channels=channels,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(
        request,
        notice=(
            f"Micsig MAXIMUM ASCII capture started: "
            f"{status.memory_depth_points:,} points per channel."
        ),
    )


@router.get(
    "/ui/devices/{device_id}/logic-analyzer/status",
    response_class=HTMLResponse,
)
def logic_analyzer_status_panel(request: Request, device_id: str) -> HTMLResponse:
    return _render_logic_analyzer_panel(request, device_id)


@router.post(
    "/ui/devices/{device_id}/logic-analyzer/settings",
    response_class=HTMLResponse,
)
async def update_logic_analyzer_settings(
    request: Request,
    device_id: str,
) -> HTMLResponse:
    form = await request.form()
    try:
        channels = tuple(int(str(value)) for value in form.getlist("channels"))
        service = _context(request).logic_analyzer_service
        current = service.settings(device_id)
        trigger_condition = str(form["trigger_condition"])
        if trigger_condition == "mixed":
            triggers = current.triggers
            trigger_channels = current.trigger_channels
        elif trigger_condition == "off":
            triggers = ()
            trigger_channels = ()
        else:
            trigger_channels = tuple(int(str(value)) for value in form.getlist("trigger_channels"))
            triggers = tuple(
                KingstTrigger(channel=channel, condition=trigger_condition)
                for channel in trigger_channels
            )
        channels = tuple(sorted(set(channels) | set(trigger_channels)))
        service.update_settings(
            device_id,
            channels=channels,
            sample_rate_hz=int(str(form["sample_rate_hz"])),
            sample_count=int(str(form["sample_count"])),
            threshold_v=float(str(form["threshold_v"])),
            capture_ratio_percent=int(str(form["capture_ratio_percent"])),
            triggers=triggers,
            auto_start_enabled=form.get("auto_start_enabled") == "true",
            auto_start_delay_s=float(str(form["auto_start_delay_s"])),
        )
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice="LA2016 settings applied.")


@router.post(
    "/ui/devices/{device_id}/logic-analyzer/{action}",
    response_class=HTMLResponse,
)
async def control_logic_analyzer(
    request: Request,
    device_id: str,
    action: str,
) -> HTMLResponse:
    context = _context(request)
    capture = context.capture_service.status()
    try:
        if action == "start":
            await context.logic_analyzer_service.start_capture(
                device_id,
                hardware_trigger=False,
                title=capture.current_title or capture.draft_title,
                comment=capture.current_comment or capture.draft_comment,
                source="dashboard",
                recording_file=capture.current_file,
            )
            notice = "Logic capture started."
        elif action == "arm":
            await context.logic_analyzer_service.start_capture(
                device_id,
                hardware_trigger=True,
                title=capture.current_title or capture.draft_title,
                comment=capture.current_comment or capture.draft_comment,
                source="dashboard",
                recording_file=capture.current_file,
            )
            notice = "Logic analyzer armed."
        elif action == "stop":
            await context.logic_analyzer_service.stop_capture(device_id)
            notice = "Logic capture stopped."
        elif action == "open-folder":
            await context.logic_analyzer_service.open_capture_directory(device_id)
            notice = "Logic capture folder opened."
        else:
            raise ValueError("Unknown logic analyzer action")
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        return _render_logic_analyzer_panel(request, device_id, error=str(exc))
    return _render_logic_analyzer_panel(request, device_id, notice=notice)


@router.post(
    "/ui/devices/{device_id}/generator/channels/{channel}",
    response_class=HTMLResponse,
)
async def update_generator_channel(
    request: Request,
    device_id: str,
    channel: int,
    waveform_code: int = Form(),
    frequency_hz: float = Form(),
    amplitude_vpp: float = Form(),
    offset_v: float = Form(),
    duty_percent: float = Form(),
    phase_deg: float = Form(),
    output_enabled: bool = Form(),
) -> HTMLResponse:
    try:
        await _context(request).signal_generator_service.update_channel(
            device_id,
            channel,
            FeelTechChannelUpdate(
                waveform_code=waveform_code,
                frequency_hz=frequency_hz,
                amplitude_vpp=amplitude_vpp,
                offset_v=offset_v,
                duty_percent=duty_percent,
                phase_deg=phase_deg,
                output_enabled=output_enabled,
            ),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice=f"CH{channel} settings applied and verified.")


@router.post(
    "/ui/devices/{device_id}/generator/channels/{channel}/output",
    response_class=HTMLResponse,
)
async def update_generator_output(
    request: Request,
    device_id: str,
    channel: int,
    enabled: bool = Form(),
) -> HTMLResponse:
    try:
        await _context(request).signal_generator_service.update_channel(
            device_id,
            channel,
            FeelTechChannelUpdate(output_enabled=enabled),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    state = "ON" if enabled else "OFF"
    return _render_dashboard(request, notice=f"CH{channel} output {state}.")


@router.post(
    "/ui/devices/{device_id}/power-supply/output",
    response_class=HTMLResponse,
)
async def update_power_supply_output(
    request: Request,
    device_id: str,
    voltage_v: float = Form(),
    current_a: float = Form(),
    enabled: bool = Form(False),
) -> HTMLResponse:
    try:
        await _context(request).dc_power_supply_service.update_output(
            device_id,
            DPS150OutputUpdate(
                voltage_v=voltage_v,
                current_a=current_a,
                enabled=enabled,
            ),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(
        request,
        notice=(
            f"DPS-150 set to {voltage_v:g} V / {current_a:g} A, "
            f"output {'ON' if enabled else 'OFF'}."
        ),
    )


@router.post(
    "/ui/devices/{device_id}/power-supply/output/toggle",
    response_class=HTMLResponse,
)
async def toggle_power_supply_output(
    request: Request,
    device_id: str,
    enabled: bool = Form(),
) -> HTMLResponse:
    try:
        await _context(request).dc_power_supply_service.update_output(
            device_id,
            DPS150OutputUpdate(enabled=enabled),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(
        request,
        notice=f"DPS-150 output {'ON' if enabled else 'OFF'}.",
    )


@router.post(
    "/ui/devices/{device_id}/bidirectional/operating-point",
    response_class=HTMLResponse,
)
async def update_bidirectional_operating_point(
    request: Request,
    device_id: str,
    priority: str = Form(),
    voltage_setpoint_v: float = Form(),
    current_setpoint_a: float = Form(),
    current_limit_positive_a: float = Form(),
    current_limit_negative_a: float = Form(),
    voltage_limit_positive_v: float = Form(),
    voltage_limit_negative_v: float = Form(),
    power_limit_positive_w: float = Form(),
    power_limit_negative_w: float = Form(),
    output_enabled: bool = Form(False),
    wiring_confirmed: bool = Form(False),
) -> HTMLResponse:
    try:
        await _context(request).bidirectional_power_supply_service.update_operating_point(
            device_id,
            ITechOperatingPointUpdate(
                priority=priority,
                voltage_setpoint_v=voltage_setpoint_v,
                current_setpoint_a=current_setpoint_a,
                current_limit_positive_a=current_limit_positive_a,
                current_limit_negative_a=current_limit_negative_a,
                voltage_limit_positive_v=voltage_limit_positive_v,
                voltage_limit_negative_v=voltage_limit_negative_v,
                power_limit_positive_w=power_limit_positive_w,
                power_limit_negative_w=power_limit_negative_w,
                output_enabled=output_enabled,
                wiring_confirmed=wiring_confirmed,
            ),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(
        request,
        notice=(
            f"ITECH {priority.upper()} settings applied; "
            f"output {'ON' if output_enabled else 'OFF'}."
        ),
    )


@router.post(
    "/ui/devices/{device_id}/bidirectional/output-off",
    response_class=HTMLResponse,
)
async def turn_bidirectional_output_off(request: Request, device_id: str) -> HTMLResponse:
    try:
        await _context(request).bidirectional_power_supply_service.update_operating_point(
            device_id,
            ITechOperatingPointUpdate(output_enabled=False),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice="ITECH output OFF.")


@router.post(
    "/ui/devices/{device_id}/bidirectional/protections",
    response_class=HTMLResponse,
)
async def update_bidirectional_protections(
    request: Request,
    device_id: str,
    ovp_enabled: bool = Form(),
    ovp_level_v: float = Form(),
    ovp_delay_s: float = Form(),
    ocp_enabled: bool = Form(),
    ocp_level_a: float = Form(),
    ocp_delay_s: float = Form(),
    opp_enabled: bool = Form(),
    opp_level_w: float = Form(),
    opp_delay_s: float = Form(),
    uvp_enabled: bool = Form(),
    uvp_level_v: float = Form(),
    uvp_delay_s: float = Form(),
    uvp_warmup_s: float = Form(),
    ucp_enabled: bool = Form(),
    ucp_level_a: float = Form(),
    ucp_delay_s: float = Form(),
    ucp_warmup_s: float = Form(),
) -> HTMLResponse:
    try:
        await _context(request).bidirectional_power_supply_service.update_protections(
            device_id,
            ITechProtectionUpdate(
                ovp_enabled=ovp_enabled,
                ovp_level_v=ovp_level_v,
                ovp_delay_s=ovp_delay_s,
                ocp_enabled=ocp_enabled,
                ocp_level_a=ocp_level_a,
                ocp_delay_s=ocp_delay_s,
                opp_enabled=opp_enabled,
                opp_level_w=opp_level_w,
                opp_delay_s=opp_delay_s,
                uvp_enabled=uvp_enabled,
                uvp_level_v=uvp_level_v,
                uvp_delay_s=uvp_delay_s,
                uvp_warmup_s=uvp_warmup_s,
                ucp_enabled=ucp_enabled,
                ucp_level_a=ucp_level_a,
                ucp_delay_s=ucp_delay_s,
                ucp_warmup_s=ucp_warmup_s,
            ),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice="ITECH protections applied and verified.")


@router.post(
    "/ui/devices/{device_id}/bidirectional/advanced",
    response_class=HTMLResponse,
)
async def update_bidirectional_advanced(
    request: Request,
    device_id: str,
    voltage_slew_positive_v_per_ms: float = Form(),
    voltage_slew_negative_v_per_ms: float = Form(),
    current_slew_positive_a_per_ms: float = Form(),
    current_slew_negative_a_per_ms: float = Form(),
    output_rise_delay_s: float = Form(),
    output_fall_delay_s: float = Form(),
    watchdog_enabled: bool = Form(),
    watchdog_delay_s: float = Form(),
) -> HTMLResponse:
    try:
        await _context(request).bidirectional_power_supply_service.update_advanced(
            device_id,
            ITechAdvancedUpdate(
                voltage_slew_positive_v_per_ms=voltage_slew_positive_v_per_ms,
                voltage_slew_negative_v_per_ms=voltage_slew_negative_v_per_ms,
                current_slew_positive_a_per_ms=current_slew_positive_a_per_ms,
                current_slew_negative_a_per_ms=current_slew_negative_a_per_ms,
                output_rise_delay_s=output_rise_delay_s,
                output_fall_delay_s=output_fall_delay_s,
                watchdog_enabled=watchdog_enabled,
                watchdog_delay_s=watchdog_delay_s,
            ),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice="ITECH advanced settings applied and verified.")


@router.post(
    "/ui/devices/{device_id}/source-measure/output",
    response_class=HTMLResponse,
)
async def update_source_measure_output(
    request: Request,
    device_id: str,
    voltage_v: float = Form(),
    current_a: float = Form(),
    enabled: bool = Form(False),
) -> HTMLResponse:
    try:
        await _context(request).source_measure_unit_service.update_output(
            device_id,
            OwonSPMOutputUpdate(
                voltage_v=voltage_v,
                current_a=current_a,
                enabled=enabled,
            ),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(
        request,
        notice=(
            f"SPM source set to {voltage_v:g} V / {current_a:g} A, "
            f"output {'ON' if enabled else 'OFF'}."
        ),
    )


@router.post(
    "/ui/devices/{device_id}/source-measure/output/toggle",
    response_class=HTMLResponse,
)
async def toggle_source_measure_output(
    request: Request,
    device_id: str,
    enabled: bool = Form(),
) -> HTMLResponse:
    try:
        await _context(request).source_measure_unit_service.update_output(
            device_id,
            OwonSPMOutputUpdate(enabled=enabled),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice=f"SPM source output {'ON' if enabled else 'OFF'}.")


@router.post(
    "/ui/devices/{device_id}/source-measure/protections",
    response_class=HTMLResponse,
)
async def update_source_measure_protections(
    request: Request,
    device_id: str,
    over_voltage_v: float = Form(),
    over_current_a: float = Form(),
) -> HTMLResponse:
    try:
        await _context(request).source_measure_unit_service.update_protections(
            device_id,
            OwonSPMProtectionUpdate(
                over_voltage_v=over_voltage_v,
                over_current_a=over_current_a,
            ),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice="SPM protections applied and verified.")


@router.post(
    "/ui/devices/{device_id}/source-measure/multimeter",
    response_class=HTMLResponse,
)
async def update_source_measure_multimeter(
    request: Request,
    device_id: str,
    function: str = Form(),
    range_mode: str | None = Form(None),
    range_value: float | None = Form(None),
    relative_enabled: bool | None = Form(None),
    hold_enabled: bool | None = Form(None),
) -> HTMLResponse:
    try:
        await _context(request).source_measure_unit_service.update_multimeter(
            device_id,
            OwonSPMDMMUpdate(
                function=function,
                range_mode=range_mode,
                range_value=range_value,
                relative_enabled=relative_enabled,
                hold_enabled=hold_enabled,
            ),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice="SPM multimeter function applied and verified.")


@router.post(
    "/ui/devices/{device_id}/power-supply/protections",
    response_class=HTMLResponse,
)
async def update_power_supply_protections(
    request: Request,
    device_id: str,
    over_voltage_v: float = Form(),
    over_current_a: float = Form(),
    over_power_w: float = Form(),
    over_temperature_c: float = Form(),
    low_input_voltage_v: float = Form(),
) -> HTMLResponse:
    try:
        await _context(request).dc_power_supply_service.update_protections(
            device_id,
            DPS150ProtectionUpdate(
                over_voltage_v=over_voltage_v,
                over_current_a=over_current_a,
                over_power_w=over_power_w,
                over_temperature_c=over_temperature_c,
                low_input_voltage_v=low_input_voltage_v,
            ),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice="DPS-150 protections applied and verified.")


@router.post(
    "/ui/devices/{device_id}/power-supply/display",
    response_class=HTMLResponse,
)
async def update_power_supply_display(
    request: Request,
    device_id: str,
    brightness: int = Form(),
    volume: int = Form(),
) -> HTMLResponse:
    try:
        await _context(request).dc_power_supply_service.update_display(
            device_id,
            DPS150DisplayUpdate(brightness=brightness, volume=volume),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice="DPS-150 display settings applied.")


@router.post(
    "/ui/devices/{device_id}/power-supply/metering",
    response_class=HTMLResponse,
)
async def update_power_supply_metering(
    request: Request,
    device_id: str,
    action: str = Form(),
) -> HTMLResponse:
    try:
        if action in {"start", "stop"}:
            await _context(request).dc_power_supply_service.set_metering(
                device_id,
                action == "start",
            )
            notice = (
                "DPS-150 Ah/Wh metering started."
                if action == "start"
                else "DPS-150 Ah/Wh metering stopped."
            )
        else:
            raise ValueError("Metering action must be start or stop")
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice=notice)


@router.post(
    "/ui/devices/{device_id}/power-supply/preset/save",
    response_class=HTMLResponse,
)
async def save_power_supply_preset(
    request: Request,
    device_id: str,
    slot: int = Form(),
    voltage_v: float = Form(),
    current_a: float = Form(),
) -> HTMLResponse:
    try:
        await _context(request).dc_power_supply_service.save_preset(
            device_id,
            slot,
            voltage_v=voltage_v,
            current_a=current_a,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice=f"DPS-150 preset {slot} saved.")


@router.post(
    "/ui/devices/{device_id}/power-supply/preset/apply",
    response_class=HTMLResponse,
)
async def apply_power_supply_preset(
    request: Request,
    device_id: str,
    slot: int = Form(),
) -> HTMLResponse:
    try:
        await _context(request).dc_power_supply_service.apply_preset(
            device_id,
            slot,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(
        request,
        notice=f"DPS-150 preset {slot} applied; output state preserved.",
    )


@router.post(
    "/ui/devices/{device_id}/power-supply/program/sweep",
    response_class=HTMLResponse,
)
async def start_power_supply_sweep(
    request: Request,
    device_id: str,
    parameter: str = Form(),
    start: float = Form(),
    end: float = Form(),
    step: float = Form(),
    fixed_value: float = Form(),
    dwell_s: float = Form(),
    loops: int = Form(1),
) -> HTMLResponse:
    try:
        await _context(request).dc_power_supply_service.start_sweep(
            device_id,
            parameter=parameter,
            start=start,
            end=end,
            step=step,
            fixed_value=fixed_value,
            dwell_s=dwell_s,
            loops=loops,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice=f"DPS-150 {parameter} sweep started.")


@router.post(
    "/ui/devices/{device_id}/power-supply/program/sequence",
    response_class=HTMLResponse,
)
async def start_power_supply_sequence(
    request: Request,
    device_id: str,
    steps: str = Form(),
    loops: int = Form(1),
) -> HTMLResponse:
    try:
        parsed_steps = []
        for line_number, line in enumerate(steps.splitlines(), start=1):
            normalized = line.strip()
            if not normalized:
                continue
            fields = [field.strip() for field in normalized.split(",")]
            if len(fields) != 3:
                raise ValueError(f"Sequence line {line_number} must be voltage,current,dwell")
            parsed_steps.append(PowerSequenceStep(*(float(field) for field in fields)))
        await _context(request).dc_power_supply_service.start_sequence(
            device_id,
            steps=tuple(parsed_steps),
            loops=loops,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice="DPS-150 sequence started.")


@router.post(
    "/ui/devices/{device_id}/power-supply/program/{action}",
    response_class=HTMLResponse,
)
async def control_power_supply_program(
    request: Request,
    device_id: str,
    action: str,
) -> HTMLResponse:
    service = _context(request).dc_power_supply_service
    try:
        if action == "pause":
            await service.pause_program(device_id)
        elif action == "resume":
            await service.resume_program(device_id)
        elif action == "stop":
            await service.stop_program(device_id, output_off=True)
        else:
            raise ValueError("Program action must be pause, resume, or stop")
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request, notice=f"DPS-150 program {action} complete.")


@router.post(
    "/ui/devices/{device_id}/generator/synchronization",
    response_class=HTMLResponse,
)
async def update_generator_synchronization(
    request: Request,
    device_id: str,
    parameter: str = Form(),
    enabled: bool = Form(),
) -> HTMLResponse:
    try:
        await _context(request).signal_generator_service.set_synchronization(
            device_id,
            parameter,
            enabled,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    state = "enabled" if enabled else "disabled"
    return _render_dashboard(
        request,
        notice=f"{parameter.capitalize()} synchronization {state}.",
    )


@router.post(
    "/ui/devices/{device_id}/generator/preset",
    response_class=HTMLResponse,
)
async def generator_preset(
    request: Request,
    device_id: str,
    slot: int = Form(),
    action: str = Form(),
) -> HTMLResponse:
    try:
        if action == "save":
            await _context(request).signal_generator_service.save_preset(device_id, slot)
        elif action == "load":
            await _context(request).signal_generator_service.load_preset(device_id, slot)
        else:
            raise ValueError("Preset action must be save or load")
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    past_tense = "saved" if action == "save" else "loaded"
    return _render_dashboard(request, notice=f"Preset {slot} {past_tense}.")


@router.get(
    "/ui/devices/{device_id}/generator/advanced",
    response_class=HTMLResponse,
)
async def generator_advanced(
    request: Request,
    device_id: str,
) -> HTMLResponse:
    return await _render_generator_advanced(request, device_id)


@router.post(
    "/ui/devices/{device_id}/generator/burst",
    response_class=HTMLResponse,
)
async def generator_burst(
    request: Request,
    device_id: str,
    source: str = Form(),
    cycles: int = Form(),
) -> HTMLResponse:
    trigger_modes = {"off": 0, "ch2": 1, "external": 2}
    try:
        trigger_mode = trigger_modes[source]
        await _context(request).signal_generator_service.configure_burst(
            device_id,
            trigger_mode=trigger_mode,
            cycles=cycles,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return await _render_generator_advanced(request, device_id, error=str(exc))
    return await _render_generator_advanced(
        request,
        device_id,
        notice="Burst settings applied.",
    )


@router.post(
    "/ui/devices/{device_id}/generator/pulse-width",
    response_class=HTMLResponse,
)
async def generator_pulse_width(
    request: Request,
    device_id: str,
    pulse_width_ns: float = Form(),
) -> HTMLResponse:
    try:
        await _context(request).signal_generator_service.update_channel(
            device_id,
            1,
            FeelTechChannelUpdate(pulse_width_ns=pulse_width_ns),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return await _render_generator_advanced(request, device_id, error=str(exc))
    return await _render_generator_advanced(
        request,
        device_id,
        notice=f"CH1 pulse width set to {pulse_width_ns:g} ns.",
    )


@router.post(
    "/ui/devices/{device_id}/generator/burst/trigger",
    response_class=HTMLResponse,
)
async def generator_trigger_once(
    request: Request,
    device_id: str,
    cycles: int = Form(),
) -> HTMLResponse:
    try:
        await _context(request).signal_generator_service.trigger_once(
            device_id,
            cycles=cycles,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return await _render_generator_advanced(request, device_id, error=str(exc))
    return await _render_generator_advanced(
        request,
        device_id,
        notice=f"Manual burst triggered ({cycles} cycles).",
    )


@router.post(
    "/ui/devices/{device_id}/generator/keying",
    response_class=HTMLResponse,
)
async def generator_keying(
    request: Request,
    device_id: str,
    kind: str = Form(),
    source: str = Form(),
    secondary_frequency_hz: float | None = Form(default=None),
) -> HTMLResponse:
    modes = {"off": 0, "external": 1, "manual": 2}
    try:
        await _context(request).signal_generator_service.configure_keying(
            device_id,
            kind=kind,
            mode=modes[source],
            secondary_frequency_hz=(secondary_frequency_hz if kind.casefold() == "fsk" else None),
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return await _render_generator_advanced(request, device_id, error=str(exc))
    return await _render_generator_advanced(
        request,
        device_id,
        notice=f"{kind.upper()} command applied.",
    )


@router.post(
    "/ui/devices/{device_id}/generator/counter",
    response_class=HTMLResponse,
)
async def generator_counter(
    request: Request,
    device_id: str,
    gate_time_s: int = Form(),
    coupling: str = Form(),
    mode: str = Form("frequency"),
) -> HTMLResponse:
    gate_codes = {1: 0, 10: 1, 100: 2}
    try:
        await _context(request).signal_generator_service.configure_counter(
            device_id,
            gate_code=gate_codes[gate_time_s],
            coupling=coupling,
            mode=mode,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return await _render_generator_advanced(request, device_id, error=str(exc))
    return await _render_generator_advanced(
        request,
        device_id,
        notice=f"Counter {mode} mode started.",
    )


@router.post(
    "/ui/devices/{device_id}/generator/counter/pause",
    response_class=HTMLResponse,
)
async def generator_counter_pause(
    request: Request,
    device_id: str,
) -> HTMLResponse:
    try:
        await _context(request).signal_generator_service.pause_counter(device_id)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return await _render_generator_advanced(request, device_id, error=str(exc))
    return await _render_generator_advanced(
        request,
        device_id,
        notice="External counter paused.",
    )


@router.post(
    "/ui/devices/{device_id}/generator/counter/reset",
    response_class=HTMLResponse,
)
async def generator_counter_reset(
    request: Request,
    device_id: str,
) -> HTMLResponse:
    try:
        await _context(request).signal_generator_service.reset_counter(device_id)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return await _render_generator_advanced(request, device_id, error=str(exc))
    return await _render_generator_advanced(
        request,
        device_id,
        notice="External counter reset.",
    )


@router.post(
    "/ui/devices/{device_id}/generator/sweep",
    response_class=HTMLResponse,
)
async def generator_sweep(
    request: Request,
    device_id: str,
    target: str = Form(),
    start: float = Form(),
    end: float = Form(),
    duration_s: float = Form(),
    mode: str = Form(),
    source: str = Form(),
    enabled: bool = Form(),
) -> HTMLResponse:
    try:
        await _context(request).signal_generator_service.configure_sweep(
            device_id,
            target=target,
            start=start,
            end=end,
            duration_s=duration_s,
            mode=mode,
            source=source,
            enabled=enabled,
        )
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        return await _render_generator_advanced(request, device_id, error=str(exc))
    verification = "enabled" if enabled else "configured OFF"
    return await _render_generator_advanced(
        request,
        device_id,
        notice=f"Sweep {verification}; this firmware has no sweep read-back.",
    )


@router.post(
    "/ui/devices/{device_id}/scope-capture",
    response_class=HTMLResponse,
)
async def update_scope_capture(
    request: Request,
    device_id: str,
) -> HTMLResponse:
    form = await request.form()
    screen = form.get("screen") is not None
    data = form.get("data") is not None
    wait_for_trigger = form.get("wait_for_trigger") is not None
    channels = tuple(str(value) for value in form.getlist("scope_channel"))
    try:
        _context(request).instrument_settings_service.update_scope_capture(
            device_id,
            screen=screen,
            data=data,
            channels=channels,
            wait_for_trigger=wait_for_trigger,
        )
    except (KeyError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request)


@router.post(
    "/ui/devices/{device_id}/scope-measurements/apply",
    response_class=HTMLResponse,
)
async def apply_scope_measurements(
    request: Request,
    device_id: str,
) -> HTMLResponse:
    form = await request.form()
    channels = tuple(str(value) for value in form.getlist("measurement_channel"))
    items = tuple(str(value) for value in form.getlist("measurement_item"))
    secondary_channels = tuple(
        str(value) for value in form.getlist("measurement_secondary_channel")
    ) or ("",) * len(channels)
    source_edges = tuple(str(value) for value in form.getlist("measurement_source_edge")) or (
        "",
    ) * len(channels)
    target_edges = tuple(str(value) for value in form.getlist("measurement_target_edge")) or (
        "",
    ) * len(channels)
    selections = tuple(
        ScopeMeasurementSelection(
            channel=channel,
            item=item,
            secondary_channel=secondary_channel or None,
            source_edge=source_edge or None,
            target_edge=target_edge or None,
        )
        for channel, item, secondary_channel, source_edge, target_edge in zip(
            channels,
            items,
            secondary_channels,
            source_edges,
            target_edges,
            strict=False,
        )
        if item
    )
    capture_present = form.get("scope_capture_present") is not None
    screen = form.get("screen") is not None
    data = form.get("data") is not None
    wait_for_trigger = form.get("wait_for_trigger") is not None
    capture_channels = tuple(str(value) for value in form.getlist("scope_channel"))
    valid_scope_channels = {"CH1", "CH2", "CH3", "CH4"}
    if capture_present and any(
        channel.strip().upper() not in valid_scope_channels for channel in capture_channels
    ):
        return _render_dashboard(request, errors=("Unknown oscilloscope channel.",))
    if capture_present and data and not capture_channels:
        return _render_dashboard(
            request,
            errors=("Select at least one oscilloscope channel when waveform data is enabled",),
        )
    try:
        await _context(request).scope_measurement_service.replace_selections(
            device_id,
            selections,
        )
        if capture_present:
            _context(request).instrument_settings_service.update_scope_capture(
                device_id,
                screen=screen,
                data=data,
                channels=capture_channels,
                wait_for_trigger=wait_for_trigger,
            )
    except (KeyError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(
        request,
        notice=(
            "Oscilloscope settings applied: "
            f"{len(selections)}/{MAX_SCOPE_MEASUREMENTS} measurements."
        ),
    )


@router.post(
    "/ui/devices/{device_id}/scope-measurements/add",
    response_class=HTMLResponse,
)
async def add_scope_measurement(
    request: Request,
    device_id: str,
    channel: str = Form(),
    item: str = Form(),
) -> HTMLResponse:
    try:
        await _context(request).scope_measurement_service.add_selection(
            device_id,
            channel=channel,
            item=item,
        )
    except (KeyError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request)


@router.post(
    "/ui/devices/{device_id}/scope-measurements/remove",
    response_class=HTMLResponse,
)
async def remove_scope_measurement(
    request: Request,
    device_id: str,
    channel: str = Form(),
    item: str = Form(),
) -> HTMLResponse:
    try:
        await _context(request).scope_measurement_service.remove_selection(
            device_id,
            channel=channel,
            item=item,
        )
    except (KeyError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(request)


@router.post(
    "/ui/channels/{channel_id}/polling",
    response_class=HTMLResponse,
)
async def update_polling(
    request: Request,
    channel_id: str,
    interval_s: float = Form(),
) -> HTMLResponse:
    context = _context(request)
    try:
        channel = context.registry.channel(channel_id)
        device = context.registry.device(channel.device_id)
    except KeyError as exc:
        return _render_dashboard(request, errors=(str(exc),))
    minimum = MIN_POLL_INTERVAL_BY_KIND.get(device.kind, 0.1)
    if not minimum <= interval_s <= MAX_POLL_INTERVAL_S:
        return _render_dashboard(
            request,
            errors=(
                f"{device.name} polling interval must be between "
                f"{minimum:g} and {MAX_POLL_INTERVAL_S:g} seconds.",
            ),
        )
    try:
        updated = await context.scheduler.update_interval(channel_id, interval_s)
    except (KeyError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    context.registry.update_channel(updated)
    context.device_service.register(
        device,
        tuple(item for item in context.registry.channels() if item.device_id == device.id),
    )
    return _render_dashboard(
        request,
        notice=f"{device.name}: polling set to {_interval_label(interval_s)}.",
    )


@router.post("/ui/captures/run", response_class=HTMLResponse)
async def run_capture(
    request: Request,
    title: str = Form(default="", max_length=120),
    comment: str = Form(default="", max_length=10000),
    capture_mode: str = Form(default="once"),
) -> HTMLResponse:
    context = _context(request)
    if capture_mode == "once":
        try:
            path, measurements = await context.capture_service.snapshot(
                title=title,
                comment=comment,
            )
        except ValueError as exc:
            return _render_dashboard(request, errors=(str(exc),))
        return _render_dashboard(
            request,
            notice=f"Snapshot saved: {path.name} ({len(measurements)} channels).",
        )
    try:
        duration_s = float(capture_mode)
    except ValueError:
        return _render_dashboard(request, errors=("Unknown capture mode.",))
    try:
        status = await context.capture_service.start_recording(
            title=title,
            comment=comment,
            duration_s=None if duration_s == 0 else duration_s,
        )
    except (RuntimeError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    assert status.current_file is not None
    return _render_dashboard(
        request,
        notice=f"CSV recording started: {status.current_file.name}",
    )


@router.post("/ui/captures/snapshot", response_class=HTMLResponse)
async def capture_snapshot(
    request: Request,
    title: str = Form(default="", max_length=120),
    comment: str = Form(default="", max_length=10000),
) -> HTMLResponse:
    context = _context(request)
    try:
        path, measurements = await context.capture_service.snapshot(
            title=title,
            comment=comment,
        )
    except ValueError as exc:
        return _render_dashboard(request, errors=(str(exc),))
    return _render_dashboard(
        request,
        notice=f"Snapshot saved: {path.name} ({len(measurements)} channels).",
    )


@router.post("/ui/captures/recording/start", response_class=HTMLResponse)
async def start_recording(
    request: Request,
    title: str = Form(default="", max_length=120),
    comment: str = Form(default="", max_length=10000),
    duration_s: float = Form(default=0),
) -> HTMLResponse:
    try:
        status = await _context(request).capture_service.start_recording(
            title=title,
            comment=comment,
            duration_s=None if duration_s == 0 else duration_s,
        )
    except (RuntimeError, ValueError) as exc:
        return _render_dashboard(request, errors=(str(exc),))
    assert status.current_file is not None
    return _render_dashboard(
        request,
        notice=f"CSV recording started: {status.current_file.name}",
    )


@router.post("/ui/captures/recording/stop", response_class=HTMLResponse)
async def stop_recording(request: Request) -> HTMLResponse:
    try:
        status = await _context(request).capture_service.stop_recording()
    except RuntimeError as exc:
        return _render_dashboard(request, errors=(str(exc),))
    assert status.last_recording_file is not None
    return _render_dashboard(
        request,
        notice=(
            f"CSV recording stopped: {status.last_recording_file.name} "
            f"({status.samples_written} samples)."
        ),
    )


@router.post("/ui/captures/open-folder", response_class=HTMLResponse)
async def open_capture_folder(request: Request) -> HTMLResponse:
    try:
        directory = await _context(request).capture_service.open_output_directory()
    except OSError as exc:
        return _render_dashboard(request, errors=(f"Could not open capture folder: {exc}",))
    return _render_dashboard(request, notice=f"Opened capture folder: {directory}")


@router.get("/captures/{filename}", response_class=FileResponse)
def download_capture(request: Request, filename: str) -> FileResponse:
    try:
        path = _context(request).capture_service.resolve_artifact(filename)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Capture file not found") from exc
    return FileResponse(path, media_type="text/csv", filename=path.name)


@router.get("/matrix", response_class=HTMLResponse)
def matrix_page(
    request: Request,
    profile_id: str | None = Query(default=None),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="matrix.html",
        context=_matrix_data(request, profile_id),
    )


@router.get("/ui/matrix/panel", response_class=HTMLResponse)
def matrix_panel(
    request: Request,
    profile_id: str | None = Query(default=None),
) -> HTMLResponse:
    return _render_matrix_panel(request, profile_id)


@router.post("/ui/matrix/profiles", response_class=HTMLResponse)
def create_profile(
    request: Request,
    name: str = Form(),
    from_port: str = Form(default=""),
    to_port: str = Form(default=""),
) -> HTMLResponse:
    context = _context(request)
    connections = (
        (MatrixConnection(from_port=from_port, to_port=to_port),) if from_port and to_port else ()
    )
    try:
        profile = context.matrix_service.create_profile(name, connections)
        return _render_matrix_panel(
            request,
            profile.id,
            notice=f'Profile "{profile.name}" created',
        )
    except (MatrixValidationError, MatrixConflictError) as exc:
        errors = exc.errors if isinstance(exc, MatrixValidationError) else (str(exc),)
        return _render_matrix_panel(request, errors=errors)


@router.post(
    "/ui/matrix/profiles/{profile_id}/connections",
    response_class=HTMLResponse,
)
def add_connection(
    request: Request,
    profile_id: str,
    from_port: str = Form(),
    to_port: str = Form(),
) -> HTMLResponse:
    context = _context(request)
    try:
        profile = context.matrix_service.get_profile(profile_id)
        connections = (*profile.connections, MatrixConnection(from_port, to_port))
        updated = context.matrix_service.update_profile(
            profile.id,
            profile.name,
            connections,
        )
        return _render_matrix_panel(
            request,
            updated.id,
            notice="Connection added and profile saved",
        )
    except (MatrixValidationError, MatrixConflictError, MatrixNotFoundError) as exc:
        errors = exc.errors if isinstance(exc, MatrixValidationError) else (str(exc),)
        return _render_matrix_panel(request, profile_id, errors=errors)


@router.post("/ui/matrix/profiles/{profile_id}/validate", response_class=HTMLResponse)
def validate_profile(request: Request, profile_id: str) -> HTMLResponse:
    context = _context(request)
    try:
        errors = context.matrix_service.validate_profile(profile_id)
        notice = "Profile is valid" if not errors else None
        return _render_matrix_panel(request, profile_id, notice=notice, errors=errors)
    except MatrixNotFoundError as exc:
        return _render_matrix_panel(request, errors=(str(exc),))


@router.post("/ui/matrix/profiles/{profile_id}/apply", response_class=HTMLResponse)
def apply_profile(request: Request, profile_id: str) -> HTMLResponse:
    context = _context(request)
    try:
        result = context.matrix_service.apply_profile(profile_id)
        return _render_matrix_panel(request, profile_id, notice=result.message)
    except (
        MatrixValidationError,
        MatrixNotFoundError,
        MatrixConflictError,
        SafetyInterlockError,
    ) as exc:
        errors = exc.errors if isinstance(exc, MatrixValidationError) else (str(exc),)
        return _render_matrix_panel(request, profile_id, errors=errors)


@router.post("/ui/matrix/open-all", response_class=HTMLResponse)
def open_all(request: Request) -> HTMLResponse:
    result = _context(request).matrix_service.open_all()
    return _render_matrix_panel(request, notice=result.message)


@router.post("/ui/emergency-stop", response_class=HTMLResponse)
async def emergency_stop(request: Request) -> HTMLResponse:
    context = _context(request)
    context.matrix_service.emergency_stop("dashboard operator request")
    generator_errors = await context.signal_generator_service.all_outputs_off()
    power_supply_errors = await context.dc_power_supply_service.all_outputs_off()
    source_measure_unit_errors = await context.source_measure_unit_service.all_outputs_off()
    bidirectional_errors = await context.bidirectional_power_supply_service.all_outputs_off()
    output_errors = (
        *generator_errors,
        *power_supply_errors,
        *source_measure_unit_errors,
        *bidirectional_errors,
    )
    return _render_dashboard(
        request,
        errors=output_errors,
        notice=None if output_errors else "All controlled outputs are off.",
    )


@router.post("/ui/simulation/reset-safety", response_class=HTMLResponse)
def reset_safety(request: Request) -> HTMLResponse:
    _context(request).matrix_service.reset_simulated_safety()
    return _render_dashboard(request)
