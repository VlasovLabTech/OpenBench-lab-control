from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import unquote

from fastapi import FastAPI, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from openbench.api.bidirectional_power_supplies import (
    router as bidirectional_power_supplies_router,
)
from openbench.api.captures import router as captures_router
from openbench.api.dependencies import ContextDep
from openbench.api.devices import router as devices_router
from openbench.api.generators import router as generators_router
from openbench.api.logic_analyzers import router as logic_analyzers_router
from openbench.api.matrix import router as matrix_router
from openbench.api.oscilloscopes import router as oscilloscopes_router
from openbench.api.power_supplies import router as power_supplies_router
from openbench.api.schemas import (
    EmergencyStopOut,
    EmergencyStopRequest,
    HealthOut,
    MatrixApplyResultOut,
    SafetyStateOut,
)
from openbench.api.source_measure_units import router as source_measure_units_router
from openbench.api.websocket import router as websocket_router
from openbench.bootstrap import (
    create_context,
    register_dps150_devices,
    register_feeltech_devices,
    register_itech_it6000c_devices,
    register_kingst_devices,
    register_micsig_devices,
    register_micsig_eto_devices,
    register_owon_spm_devices,
    register_ut61d_devices,
    register_ut61eplus_devices,
    register_ut197_devices,
)
from openbench.config import Settings
from openbench.web.routes import STATIC_DIR
from openbench.web.routes import router as web_router


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        context = create_context(active_settings)
        app.state.context = context
        if active_settings.auto_discover:
            await register_dps150_devices(context)
            await register_owon_spm_devices(context)
            if active_settings.itech_it6000c_auto_discover:
                await register_itech_it6000c_devices(context)
            await register_feeltech_devices(context)
            await register_ut61d_devices(context)
            await register_ut61eplus_devices(context)
            await register_ut197_devices(context)
            await register_micsig_devices(context)
            if active_settings.micsig_eto_auto_discover:
                await register_micsig_eto_devices(context)
            await register_kingst_devices(context)
        await context.scope_measurement_service.start()
        await context.scheduler.start()
        try:
            yield
        finally:
            await context.scope_maximum_capture_service.close()
            await context.logic_analyzer_service.close()
            await context.capture_service.close()
            await context.dc_power_supply_service.close()
            await context.source_measure_unit_service.close()
            await context.bidirectional_power_supply_service.close()
            await context.scope_measurement_service.stop()
            await context.scheduler.stop()
            context.database.dispose()

    app = FastAPI(
        title="VlasovLab OpenBench",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def protect_reserved_instruments(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path_parts = request.url.path.split("/")
        mutating = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        device_id: str | None = None
        if mutating and len(path_parts) >= 4 and path_parts[1:3] == ["ui", "devices"]:
            device_id = unquote(path_parts[3])
        elif (
            mutating
            and len(path_parts) >= 5
            and path_parts[1:3] == ["api", "v1"]
            and path_parts[3] in {"devices", "oscilloscopes"}
        ):
            device_id = unquote(path_parts[4])
        if device_id is not None:
            context = getattr(request.app.state, "context", None)
            if context is not None and context.scope_maximum_capture_service.owns_device(device_id):
                return PlainTextResponse(
                    "Micsig MAXIMUM ASCII capture is active; wait for it to finish.",
                    status_code=409,
                )
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(bidirectional_power_supplies_router)
    app.include_router(captures_router)
    app.include_router(devices_router)
    app.include_router(generators_router)
    app.include_router(logic_analyzers_router)
    app.include_router(power_supplies_router)
    app.include_router(source_measure_units_router)
    app.include_router(matrix_router)
    app.include_router(oscilloscopes_router)
    app.include_router(websocket_router)
    app.include_router(web_router)

    @app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
    def api_documentation(request: Request) -> HTMLResponse:
        generated = get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} API",
            swagger_favicon_url="/static/favicon.svg?v=20260730-2",
        )
        html = bytes(generated.body).decode("utf-8")
        language = "ru" if request.cookies.get("openbench_language") == "ru" else "en"
        navigation = """
        <script src="/static/i18n.js?v=20260824-5" defer></script>
        <style>
          .openbench-docs-controls {
            position: fixed;
            z-index: 10000;
            top: 12px;
            left: 12px;
            display: flex;
            gap: 8px;
          }
          .openbench-docs-controls a,
          .openbench-docs-controls button {
            padding: 9px 13px;
            border-radius: 8px;
            color: #fff;
            background: #167966;
            font: 700 13px system-ui, sans-serif;
            text-decoration: none;
            border: 0;
            cursor: pointer;
            box-shadow: 0 6px 18px rgba(0,0,0,.18);
          }
          .openbench-docs-controls button { background: #1d3b49; }
          .openbench-docs-controls a[aria-current="true"] { background: #1d3b49; }
          .swagger-ui .topbar { padding-left: 226px; }
          html[data-theme="dark"] body,
          html[data-theme="dark"] .swagger-ui {
            color: #e8f0f3;
            background: #0b1116;
          }
          html[data-theme="dark"] .swagger-ui .info .title,
          html[data-theme="dark"] .swagger-ui .info p,
          html[data-theme="dark"] .swagger-ui .info li,
          html[data-theme="dark"] .swagger-ui .opblock-tag,
          html[data-theme="dark"] .swagger-ui .opblock-description-wrapper p,
          html[data-theme="dark"] .swagger-ui .response-col_status,
          html[data-theme="dark"] .swagger-ui .response-col_description,
          html[data-theme="dark"] .swagger-ui table thead tr td,
          html[data-theme="dark"] .swagger-ui table thead tr th {
            color: #e8f0f3;
          }
          html[data-theme="dark"] .swagger-ui .scheme-container,
          html[data-theme="dark"] .swagger-ui section.models,
          html[data-theme="dark"] .swagger-ui section.models .model-container {
            background: #121b22;
            box-shadow: none;
          }
        </style>
        <script>
          (() => {
            const saved = localStorage.getItem("openbench-theme");
            const preferred = matchMedia("(prefers-color-scheme: dark)").matches
              ? "dark"
              : "light";
            document.documentElement.dataset.theme = saved || preferred;
            window.toggleOpenBenchDocsTheme = () => {
              const next = document.documentElement.dataset.theme === "dark"
                ? "light"
                : "dark";
              document.documentElement.dataset.theme = next;
              localStorage.setItem("openbench-theme", next);
            };
          })();
        </script>
        <div class="openbench-docs-controls">
          <a class="openbench-docs-back" href="/" aria-label="Back to OpenBench">
            ← OpenBench
          </a>
          <a href="/ui/language/en?next=/docs" __EN_CURRENT__>EN</a>
          <a href="/ui/language/ru?next=/docs" __RU_CURRENT__>RU</a>
          <button type="button" onclick="toggleOpenBenchDocsTheme()"
                  aria-label="Switch API color theme">Light / dark</button>
        </div>
        """
        navigation = navigation.replace(
            "__EN_CURRENT__",
            'aria-current="true"' if language == "en" else "",
        ).replace(
            "__RU_CURRENT__",
            'aria-current="true"' if language == "ru" else "",
        )
        html = html.replace(
            "<html>",
            f'<html lang="{language}" data-language="{language}">',
            1,
        )
        return HTMLResponse(html.replace("<body>", f"<body>{navigation}", 1))

    @app.get("/api/v1/health", response_model=HealthOut, tags=["system"])
    def health(context: ContextDep) -> HealthOut:
        safety = context.matrix_service.safety_state()
        devices = context.registry.devices()
        return HealthOut(
            status="ok",
            safety_state=safety.state,
            scheduler_running=context.scheduler.running,
            devices=len(devices),
        )

    @app.post("/api/v1/emergency-stop", response_model=EmergencyStopOut, tags=["system"])
    async def emergency_stop(
        context: ContextDep,
        payload: EmergencyStopRequest | None = None,
    ) -> EmergencyStopOut:
        reason = payload.reason if payload is not None else "operator request"
        matrix = context.matrix_service.emergency_stop(reason)
        generator_errors = await context.signal_generator_service.all_outputs_off()
        power_supply_errors = await context.dc_power_supply_service.all_outputs_off()
        source_measure_unit_errors = await context.source_measure_unit_service.all_outputs_off()
        bidirectional_power_supply_errors = (
            await context.bidirectional_power_supply_service.all_outputs_off()
        )
        safety = context.matrix_service.safety_state()
        return EmergencyStopOut(
            success=(
                not generator_errors
                and not power_supply_errors
                and not source_measure_unit_errors
                and not bidirectional_power_supply_errors
            ),
            safety=SafetyStateOut.from_domain(safety),
            matrix=MatrixApplyResultOut.from_domain(matrix),
            generator_errors=list(generator_errors),
            power_supply_errors=list(power_supply_errors),
            source_measure_unit_errors=list(source_measure_unit_errors),
            bidirectional_power_supply_errors=list(bidirectional_power_supply_errors),
        )

    @app.post(
        "/api/v1/simulation/reset-safety",
        response_model=SafetyStateOut,
        tags=["simulation"],
        summary="Reset simulated safety state (non-production only)",
    )
    def reset_simulated_safety(
        context: ContextDep,
    ) -> SafetyStateOut:
        return SafetyStateOut.from_domain(context.matrix_service.reset_simulated_safety())

    return app
