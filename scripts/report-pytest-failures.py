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


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: report-pytest-failures.py <junit-xml>", file=sys.stderr)
        return 2

    report_path = Path(sys.argv[1])
    root = ElementTree.parse(report_path).getroot()
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

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path and failures:
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write("\n".join(summary))

    print(f"Published {failures} pytest failure annotation(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
