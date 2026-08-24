"""Publish concise JUnit failures as GitHub Actions annotations and a summary."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from xml.etree import ElementTree


def _escape_command(value: str, *, property_value: bool = False) -> str:
    escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        escaped = escaped.replace(":", "%3A").replace(",", "%2C")
    return escaped


def _failure_text(node: ElementTree.Element) -> str:
    message = node.get("message", "Test failed").strip()
    details = (node.text or "").strip()
    if details and details not in message:
        message = f"{message}\n{details}"
    return message[:8000]


def _log_tail(log_path: Path | None) -> str:
    if log_path is None:
        return "No plain-text pytest log was supplied."
    try:
        output = log_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return f"Unable to read {log_path}: {exc}"
    if not output:
        return f"The pytest log {log_path} is empty."
    return output[-8000:]


def _publish_diagnostic(title: str, message: str) -> None:
    properties = f"title={_escape_command(title, property_value=True)}"
    print(f"::error {properties}::{_escape_command(message)}")


def _write_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write("\n".join(lines))
    except OSError as exc:
        print(f"Unable to write GitHub step summary: {exc}", file=sys.stderr)


def main() -> int:
    if len(sys.argv) not in {2, 3}:
        print(
            "Usage: report-pytest-failures.py <junit-xml> [pytest-output-log]",
            file=sys.stderr,
        )
        return 2

    report_path = Path(sys.argv[1])
    log_path = Path(sys.argv[2]) if len(sys.argv) == 3 else None
    try:
        root = ElementTree.parse(report_path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        message = f"Unable to read {report_path}: {exc}\n\n{_log_tail(log_path)}"
        _publish_diagnostic("Pytest report unavailable", message)
        _write_summary(
            [
                "## Pytest failure",
                "",
                "The JUnit report was unavailable. The end of the pytest log follows:",
                "",
                "```text",
                message.replace("```", "'''"),
                "```",
                "",
            ]
        )
        return 0

    summary: list[str] = ["## Pytest failures", ""]
    failures = 0

    for case in root.iter("testcase"):
        problem = next(iter(case.findall("failure") + case.findall("error")), None)
        if problem is None:
            continue

        failures += 1
        class_name = case.get("classname", "")
        test_name = case.get("name", "unknown test")
        identity = f"{class_name}::{test_name}" if class_name else test_name
        file_name = case.get("file", "")
        line = case.get("line", "1")
        message = _failure_text(problem)

        properties = [f"title={_escape_command(identity, property_value=True)}"]
        if file_name:
            properties.append(f"file={_escape_command(file_name, property_value=True)}")
            if line.isdigit():
                properties.append(f"line={line}")
        print(f"::error {','.join(properties)}::{_escape_command(message)}")

        summary.extend(
            [
                f"### `{identity}`",
                "",
                "```text",
                message.replace("```", "'''"),
                "```",
                "",
            ]
        )

    if failures:
        _write_summary(summary)
    else:
        message = _log_tail(log_path)
        _publish_diagnostic("Pytest failed without a JUnit failure", message)
        _write_summary(
            [
                "## Pytest failure",
                "",
                "No failed test case was present in the JUnit report. Log tail:",
                "",
                "```text",
                message.replace("```", "'''"),
                "```",
                "",
            ]
        )

    print(f"Published {failures} pytest failure annotation(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
