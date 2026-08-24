from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import uvicorn

from openbench.api import create_app
from openbench.bootstrap import (
    ApplicationContext,
    create_context,
    register_feeltech_devices,
    register_micsig_devices,
    register_ut61d_devices,
    register_ut61eplus_devices,
    register_ut197_devices,
)
from openbench.config import DEFAULT_CAPTURE_DIRECTORY as DEFAULT_SESSION_DIRECTORY
from openbench.config import Settings
from openbench.drivers.micsig_mho1 import (
    MicsigMHO1Scope,
    MicsigScpiTransport,
    MicsigSnapshot,
)
from openbench.services.matrix_service import (
    MatrixNotFoundError,
    MatrixValidationError,
    SafetyInterlockError,
)

app = typer.Typer(
    name="openbench",
    help="VlasovLab OpenBench local laboratory automation prototype.",
    no_args_is_help=True,
)
matrix_app = typer.Typer(help="Inspect and control matrix profiles.", no_args_is_help=True)
scope_app = typer.Typer(
    help="Capture an already configured Micsig MHO1.",
    no_args_is_help=True,
)
app.add_typer(matrix_app, name="matrix")
app.add_typer(scope_app, name="scope")
DEFAULT_CAPTURE_DIRECTORY = Path(DEFAULT_SESSION_DIRECTORY).parent


def _context() -> ApplicationContext:
    return create_context(Settings.from_env())


async def _discover_scope(host: str | None) -> MicsigMHO1Scope:
    settings = Settings.from_env()
    transports = await MicsigScpiTransport.discover_connected(
        hosts=(host,) if host is not None else settings.micsig_hosts,
        subnets=settings.micsig_scan_subnets,
        timeout_s=settings.micsig_discovery_timeout_s,
        scan_fallback=settings.micsig_scan_fallback,
        scpi_port=settings.micsig_scpi_port,
        http_port=settings.micsig_http_port,
    )
    if not transports:
        raise RuntimeError("Micsig MHO1 was not found")
    if len(transports) > 1:
        hosts = ", ".join(transport.descriptor.host for transport in transports)
        for transport in transports:
            await transport.close()
        raise RuntimeError(f"Multiple Micsig scopes found ({hosts}); select one with --host")
    transport = transports[0]
    return MicsigMHO1Scope(transport.descriptor, transport=transport)


def _scope_error(exc: Exception) -> NoReturn:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1) from exc


@app.command()
def serve(
    host: str | None = typer.Option(
        None,
        help="Bind address (OPENBENCH_HOST or localhost by default).",
    ),
    port: int | None = typer.Option(
        None,
        min=1,
        max=65535,
        help="TCP port (OPENBENCH_PORT or 8000 by default).",
    ),
    reload: bool = typer.Option(False, help="Enable Uvicorn development reload."),
) -> None:
    """Run the local OpenBench server."""
    configured = Settings.from_env()
    settings = replace(
        configured,
        host=configured.host if host is None else host,
        port=configured.port if port is None else port,
    )
    if reload:
        uvicorn.run(
            "openbench.main:app",
            host=settings.host,
            port=settings.port,
            reload=True,
        )
    else:
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


@app.command()
def devices() -> None:
    """List registered instruments."""
    context = _context()
    try:
        asyncio.run(register_feeltech_devices(context))
        asyncio.run(register_ut61d_devices(context))
        asyncio.run(register_ut61eplus_devices(context))
        asyncio.run(register_ut197_devices(context))
        asyncio.run(register_micsig_devices(context))
        for device in context.registry.devices():
            capabilities = ", ".join(device.capabilities)
            state = "connected" if device.connected else "offline"
            typer.echo(f"{device.id}\t{state}\t{capabilities}\t{device.name}")
    finally:
        asyncio.run(context.scheduler.stop())
        context.database.dispose()


@scope_app.command("start")
def scope_start(
    host: str | None = typer.Option(None, help="Scope IP; otherwise auto-discover."),
) -> None:
    """Start continuous acquisition without changing scope settings."""

    async def operation() -> None:
        scope = await _discover_scope(host)
        try:
            await scope.start()
            typer.echo(f"Started {scope.descriptor.model} at {scope.descriptor.host}")
        finally:
            await scope.close()

    try:
        asyncio.run(operation())
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _scope_error(exc)


@scope_app.command("stop")
def scope_stop(
    host: str | None = typer.Option(None, help="Scope IP; otherwise auto-discover."),
) -> None:
    """Stop acquisition and wait until the stopped frame is stable."""

    async def operation() -> None:
        scope = await _discover_scope(host)
        try:
            await scope.stop()
            typer.echo(f"Stopped {scope.descriptor.model} at {scope.descriptor.host}")
        finally:
            await scope.close()

    try:
        asyncio.run(operation())
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _scope_error(exc)


@scope_app.command("single")
def scope_single(
    host: str | None = typer.Option(None, help="Scope IP; otherwise auto-discover."),
    wait_timeout: float | None = typer.Option(
        None,
        min=0.01,
        help="Optionally wait this many seconds for the trigger.",
    ),
) -> None:
    """Arm one acquisition using the trigger already configured on the scope."""

    async def operation() -> None:
        scope = await _discover_scope(host)
        try:
            await scope.single(wait_timeout_s=wait_timeout)
            state = "completed" if wait_timeout is not None else "armed"
            typer.echo(
                f"Single acquisition {state} on {scope.descriptor.model} at {scope.descriptor.host}"
            )
        finally:
            await scope.close()

    try:
        asyncio.run(operation())
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _scope_error(exc)


@scope_app.command("snapshot")
def scope_snapshot(
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Directory for measurement CSV and PNG.",
        ),
    ] = DEFAULT_CAPTURE_DIRECTORY,
    host: str | None = typer.Option(None, help="Scope IP; otherwise auto-discover."),
    screenshot: bool = typer.Option(True, help="Also save a PNG screen capture."),
    resume: bool = typer.Option(
        False,
        help="Resume RUN after capture; default leaves the scope stopped.",
    ),
) -> None:
    """Stop and save a scope PNG plus direct scalar measurements."""

    async def operation() -> tuple[MicsigSnapshot, str]:
        scope = await _discover_scope(host)
        try:
            snapshot = await scope.capture_snapshot(
                include_screenshot=screenshot,
                resume=resume,
            )
            return snapshot, scope.descriptor.serial_number
        finally:
            await scope.close()

    try:
        snapshot, serial_number = asyncio.run(operation())
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _scope_error(exc)

    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    stem = f"{timestamp}_{serial_number}"
    output.mkdir(parents=True, exist_ok=True)
    measurements_path = output / f"{stem}_measurements.csv"
    measurements_path.write_bytes(snapshot.measurements_csv)
    typer.echo(f"Saved direct scalar measurements:\n  {measurements_path.resolve()}")
    if snapshot.screenshot is not None:
        screenshot_path = output / f"{stem}_screen.{snapshot.screenshot.image_format}"
        screenshot_path.write_bytes(snapshot.screenshot.data)
        typer.echo(f"  {screenshot_path.resolve()}")
    elif snapshot.screenshot_error is not None:
        typer.echo(
            f"Warning: screenshot was not saved: {snapshot.screenshot_error}",
            err=True,
        )


@matrix_app.command("list")
def matrix_list() -> None:
    """List saved matrix profiles."""
    context = _context()
    try:
        active = context.matrix_service.active()
        for profile in context.matrix_service.list_profiles():
            marker = "*" if active.profile_id == profile.id else " "
            typer.echo(
                f"{marker} {profile.id}\tv{profile.version}\t"
                f"{len(profile.connections)} routes\t{profile.name}"
            )
    finally:
        context.database.dispose()


@matrix_app.command("apply")
def matrix_apply(profile: str = typer.Argument(help="Profile ID or exact name.")) -> None:
    """Validate and atomically apply a profile."""
    context = _context()
    try:
        try:
            result = context.matrix_service.apply_profile(profile)
        except (MatrixNotFoundError, MatrixValidationError, SafetyInterlockError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(
            json.dumps(
                {
                    "success": result.success,
                    "profile_id": result.profile_id,
                    "profile_name": result.profile_name,
                    "active_routes": len(result.active_connections),
                    "message": result.message,
                },
                ensure_ascii=False,
            )
        )
    finally:
        context.database.dispose()


@matrix_app.command("open-all")
def matrix_open_all() -> None:
    """Open every matrix route."""
    context = _context()
    try:
        result = context.matrix_service.open_all(reason="CLI request")
        typer.echo(result.message)
    finally:
        context.database.dispose()


if __name__ == "__main__":
    app()
