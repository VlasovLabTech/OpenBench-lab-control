from pytest import MonkeyPatch

import openbench.cli as cli
from openbench.config import Settings


def test_serve_uses_environment_host_and_port(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENBENCH_HOST", "127.0.0.2")
    monkeypatch.setenv("OPENBENCH_PORT", "18117")
    calls: list[tuple[Settings, str, int]] = []

    monkeypatch.setattr(cli, "create_app", lambda settings: settings)

    def fake_run(app: Settings, *, host: str, port: int) -> None:
        calls.append((app, host, port))

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    cli.serve(host=None, port=None, reload=False)

    assert len(calls) == 1
    assert calls[0][0].host == "127.0.0.2"
    assert calls[0][0].port == 18117


def test_serve_command_line_values_override_environment(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENBENCH_HOST", "127.0.0.2")
    monkeypatch.setenv("OPENBENCH_PORT", "18117")
    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(cli, "create_app", lambda settings: settings)

    def fake_run(_app: Settings, *, host: str, port: int) -> None:
        calls.append((host, port))

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    cli.serve(host="127.0.0.3", port=18118, reload=False)

    assert calls == [("127.0.0.3", 18118)]
