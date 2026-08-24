from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _locked_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name = re.split(r"[<>=!~]", stripped, maxsplit=1)[0]
        names.add(name.casefold().replace("_", "-"))
    return names


def test_windows_locks_cover_declared_runtime_and_development_dependencies() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    runtime_lock = _locked_names(PROJECT_ROOT / "requirements/windows-runtime.lock")
    dev_lock = _locked_names(PROJECT_ROOT / "requirements/windows-dev.lock")

    runtime_requirements = [
        *project["dependencies"],
        *project["optional-dependencies"]["hardware"],
    ]
    development_requirements = [
        *runtime_requirements,
        *project["optional-dependencies"]["dev"],
    ]
    runtime_names = {
        Requirement(item).name.casefold().replace("_", "-")
        for item in runtime_requirements
    }
    development_names = {
        Requirement(item).name.casefold().replace("_", "-")
        for item in development_requirements
    }

    assert runtime_names <= runtime_lock
    assert development_names <= dev_lock


def test_kingst_source_lock_matches_tracked_patch() -> None:
    lock = json.loads(
        (PROJECT_ROOT / "scripts/kingst-runtime.lock.json").read_text(encoding="utf-8")
    )
    patch = PROJECT_ROOT / lock["patch"]["path"]

    assert lock["schema_version"] == 1
    assert _sha256(patch) == lock["patch"]["sha256"]
    assert len(lock["sources"]["libsigrok"]["commit"]) == 40
    assert len(lock["sources"]["sigrok_cli"]["commit"]) == 40
    assert len(lock["sources"]["sigrok_util"]["commit"]) == 40
    assert {item["name"] for item in lock["validated_firmware"]} >= {
        "kingst-la-01a2.fw",
        "kingst-la2016a1-fpga.bitstream",
    }


def test_dashboard_uses_the_vendored_htmx_asset() -> None:
    template = (PROJECT_ROOT / "src/openbench/web/templates/base.html").read_text(
        encoding="utf-8"
    )
    asset = PROJECT_ROOT / "src/openbench/web/static/htmx-2.0.4.min.js"
    license_file = PROJECT_ROOT / "src/openbench/web/static/htmx-LICENSE.txt"

    assert "unpkg.com" not in template
    assert "htmx-2.0.4.min.js" in template
    assert _sha256(asset) == "E209DDA5C8235479F3166DEFC7750E1DBCD5A5C1808B7792FC2E6733768FB447"
    assert license_file.is_file()


def test_clean_machine_entrypoints_and_context_are_tracked() -> None:
    expected = (
        "Setup OpenBench.cmd",
        "Install Codex Skill.cmd",
        "Start OpenBench.cmd",
        "Install Kingst Firmware.cmd",
        "Build Kingst Runtime.cmd",
        "LICENSE",
        "scripts/setup-openbench.ps1",
        "scripts/install-openbench-skill.ps1",
        "scripts/diagnose-openbench.ps1",
        "scripts/install-kingst-firmware.ps1",
        "scripts/build-kingst-sigrok.ps1",
        "scripts/audit-publication.ps1",
        "scripts/test-package-install.ps1",
        "docs/portability.md",
        "docs/driver-development.md",
        "docs/system-concept-ru.md",
    )
    assert all((PROJECT_ROOT / item).is_file() for item in expected)
    assert not (PROJECT_ROOT / "VlasovLab_OpenBench_System_Concept.md").exists()
    assert not (PROJECT_ROOT / "VlasovLab_OpenBench_Codex_Startup_Prompt.md").exists()


def test_default_runtime_state_is_consolidated_under_ignored_openbench_directory() -> None:
    from openbench.config import DEFAULT_CAPTURE_DIRECTORY, DEFAULT_DATABASE_URL

    assert DEFAULT_DATABASE_URL == "sqlite:///./.openbench/data/openbench.db"
    assert DEFAULT_CAPTURE_DIRECTORY == ".openbench/data/captures/sessions"
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".openbench/" in ignore
    assert not (PROJECT_ROOT / "openbench.db").exists()
    assert not (PROJECT_ROOT / "captures").exists()


def test_public_text_has_no_email_user_path_or_private_bench_address() -> None:
    ignored_roots = {".git", ".openbench", ".venv"}
    text_suffixes = {
        ".cmd",
        ".html",
        ".json",
        ".md",
        ".ps1",
        ".py",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
    email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    user_path = re.compile(r"(?:[A-Za-z]:\\Users\\[^\\\s]+|/Users/[^/\s]+|/home/[^/\s]+)")
    private_address = re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    )

    for path in PROJECT_ROOT.rglob("*"):
        relative = path.relative_to(PROJECT_ROOT)
        if not path.is_file() or relative.parts[0] in ignored_roots:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.suffix.casefold() not in text_suffixes:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert email.search(content) is None, f"email address in {relative}"
        assert user_path.search(content) is None, f"machine-specific user path in {relative}"
        assert private_address.search(content) is None, (
            f"private bench address in public file: {relative}"
        )


def test_canonical_openbench_skill_is_packaged() -> None:
    skill_root = PROJECT_ROOT / "skills/openbench"
    expected = (
        "SKILL.md",
        "agents/openai.yaml",
        "references/automation-api.md",
        "scripts/openbench_api.py",
    )

    assert all((skill_root / item).is_file() for item in expected)
    frontmatter = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    assert frontmatter.startswith("---\nname: openbench\n")


def test_openbench_skill_exposes_itech_reservation_commands(monkeypatch) -> None:
    helper_path = PROJECT_ROOT / "skills/openbench/scripts/openbench_api.py"
    spec = importlib.util.spec_from_file_location("openbench_skill_api", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    calls: list[tuple[str, str]] = []

    def request_json(
        _base_url: str,
        method: str,
        path: str,
        _payload=None,
        **_kwargs,
    ) -> dict[str, bool]:
        calls.append((method, path))
        return {"active": method == "POST"}

    monkeypatch.setattr(helper, "request_json", request_json)
    for command in ("itech-reserve", "itech-release"):
        args = helper.build_parser().parse_args([command, "itech serial/1"])
        helper.execute(args)

    assert calls == [
        (
            "POST",
            "/bidirectional-power-supplies/itech%20serial%2F1/experiment-reservation",
        ),
        (
            "DELETE",
            "/bidirectional-power-supplies/itech%20serial%2F1/experiment-reservation",
        ),
    ]
