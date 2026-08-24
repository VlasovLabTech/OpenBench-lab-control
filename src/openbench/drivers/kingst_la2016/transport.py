from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

if sys.platform == "win32":
    from signal import CTRL_BREAK_EVENT as _CTRL_BREAK_EVENT
    from subprocess import CREATE_NEW_PROCESS_GROUP as _CREATE_NEW_PROCESS_GROUP
    from subprocess import CREATE_NO_WINDOW as _CREATE_NO_WINDOW
else:
    _CREATE_NO_WINDOW = 0
    _CREATE_NEW_PROCESS_GROUP = 0
    _CTRL_BREAK_EVENT = signal.SIGTERM

KINGST_SAMPLE_RATES_HZ = (
    20_000,
    50_000,
    100_000,
    200_000,
    500_000,
    1_000_000,
    2_000_000,
    5_000_000,
    10_000_000,
    20_000_000,
    50_000_000,
    100_000_000,
    200_000_000,
)
KINGST_THRESHOLDS_V = (0.4, 0.6, 0.9, 1.2, 1.4, 2.0, 2.5, 4.0)
KINGST_MAX_SAMPLES = 10_000_000_000
KINGST_LOGIC_CHANNELS = tuple(f"CH{index}" for index in range(16))

_SCAN_PATTERN = re.compile(
    r"^kingst-la2016:conn=(?P<connection>\S+)\s+-\s+Kingst\s+"
    r"(?P<model>LA\d+)\s+with\s+\d+\s+channels:\s+(?P<channels>.+)$",
    re.MULTILINE,
)
_USB_PATH_PATTERN = re.compile(
    r"USB enum found\s+[0-9a-f]+:[0-9a-f]+\s+at path\s+"
    r"(?P<port_path>usb/[0-9.-]+),\s+(?P<connection>\d+\.\d+)\.",
    re.IGNORECASE,
)


class SigrokUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KingstDescriptor:
    connection: str
    model: str
    logic_channels: tuple[str, ...]
    max_sample_rate_hz: int
    port_path: str = ""

    @property
    def device_id(self) -> str:
        identity = self.port_path or self.connection
        identity = re.sub(r"[^a-zA-Z0-9]+", "-", identity).strip("-").casefold()
        return f"kingst_{self.model.casefold()}_{identity}"


@dataclass(frozen=True, slots=True)
class KingstTrigger:
    channel: int
    condition: str

    def __post_init__(self) -> None:
        if not 0 <= self.channel < 16:
            raise ValueError("Trigger channel must be between CH0 and CH15")
        if self.condition not in {"low", "high", "rising", "falling"}:
            raise ValueError("Trigger condition must be low, high, rising, or falling")

    @property
    def sigrok_value(self) -> str:
        return {
            "low": "0",
            "high": "1",
            "rising": "r",
            "falling": "f",
        }[self.condition]


@dataclass(frozen=True, slots=True)
class KingstCaptureConfig:
    channels: tuple[int, ...] = tuple(range(16))
    sample_rate_hz: int = 1_000_000
    sample_count: int = 1_000_000
    threshold_v: float = 1.4
    capture_ratio_percent: int = 50
    triggers: tuple[KingstTrigger, ...] = ()

    def __post_init__(self) -> None:
        if not self.channels:
            raise ValueError("At least one logic channel must be enabled")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("Logic channels must not be duplicated")
        if any(channel < 0 or channel >= 16 for channel in self.channels):
            raise ValueError("Logic channels must be between CH0 and CH15")
        if self.sample_rate_hz not in KINGST_SAMPLE_RATES_HZ:
            raise ValueError("Unsupported LA2016 sample rate")
        if not 1 <= self.sample_count <= KINGST_MAX_SAMPLES:
            raise ValueError("Sample count must be between 1 and 10,000,000,000")
        if self.threshold_v not in KINGST_THRESHOLDS_V:
            raise ValueError("Unsupported LA2016 input threshold")
        if not 0 <= self.capture_ratio_percent <= 100:
            raise ValueError("Capture ratio must be between 0 and 100 percent")
        if any(trigger.channel not in self.channels for trigger in self.triggers):
            raise ValueError("Every trigger channel must be enabled")
        edge_count = sum(trigger.condition in {"rising", "falling"} for trigger in self.triggers)
        if edge_count > 1:
            raise ValueError("LA2016 supports at most one edge trigger")
        trigger_channels = [trigger.channel for trigger in self.triggers]
        if len(set(trigger_channels)) != len(trigger_channels):
            raise ValueError("A logic channel can have only one trigger condition")

    @property
    def duration_s(self) -> float:
        return self.sample_count / self.sample_rate_hz

    @property
    def post_trigger_duration_s(self) -> float:
        if not self.triggers:
            return self.duration_s
        return self.duration_s * (100 - self.capture_ratio_percent) / 100


StateCallback = Callable[[str], Awaitable[None]]


class SigrokCLITransport:
    _WINDOWS_ACCESS_VIOLATION = 0xC0000005

    def __init__(
        self,
        descriptor: KingstDescriptor,
        *,
        executable: str | Path | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.executable = self.resolve_executable(executable)
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def resolve_executable(value: str | Path | None = None) -> Path:
        candidates: list[Path] = []
        if value:
            candidates.append(Path(value).expanduser())
        configured = os.getenv("OPENBENCH_SIGROK_CLI")
        if configured:
            candidates.append(Path(configured).expanduser())
        project_root = Path(__file__).resolve().parents[4]
        candidates.append(
            project_root / ".openbench" / "tools" / "sigrok-modern" / "sigrok-cli.exe"
        )
        located = shutil.which("sigrok-cli")
        if located:
            candidates.append(Path(located))
        if os.name == "nt":
            candidates.extend(
                (
                    Path(r"C:\Program Files\sigrok\sigrok-cli\sigrok-cli.exe"),
                    Path(r"C:\Program Files\sigrok\PulseView\sigrok-cli.exe"),
                )
            )
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_file():
                return resolved
        raise SigrokUnavailableError(
            "sigrok-cli was not found. Install the official sigrok Windows package."
        )

    @classmethod
    async def discover(
        cls,
        *,
        executable: str | Path | None = None,
        timeout_s: float = 12.0,
        attempts: int = 3,
        retry_delay_s: float = 0.5,
    ) -> tuple[KingstDescriptor, ...]:
        binary = cls.resolve_executable(executable)
        if attempts < 1:
            raise ValueError("Discovery attempts must be at least one")

        def scan() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    str(binary),
                    "--driver",
                    "kingst-la2016",
                    "--scan",
                    "--loglevel",
                    "4",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                check=False,
                creationflags=_CREATE_NO_WINDOW,
            )

        last_timeout: subprocess.TimeoutExpired | None = None
        for attempt in range(attempts):
            try:
                result = await asyncio.to_thread(scan)
            except subprocess.TimeoutExpired as exc:
                last_timeout = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(retry_delay_s)
                    continue
                raise SigrokUnavailableError("sigrok device discovery timed out") from exc
            combined = f"{result.stdout}\n{result.stderr}"
            port_paths = {
                match.group("connection"): match.group("port_path")
                for match in _USB_PATH_PATTERN.finditer(combined)
            }
            descriptors: list[KingstDescriptor] = []
            for match in _SCAN_PATTERN.finditer(combined):
                channels = tuple(
                    name
                    for name in match.group("channels").split()
                    if name in KINGST_LOGIC_CHANNELS
                )
                model = match.group("model")
                connection = match.group("connection")
                descriptors.append(
                    KingstDescriptor(
                        connection=connection,
                        model=model,
                        logic_channels=channels,
                        max_sample_rate_hz=(200_000_000 if model == "LA2016" else 100_000_000),
                        port_path=port_paths.get(connection, ""),
                    )
                )
            unique = {descriptor.device_id: descriptor for descriptor in descriptors}
            if unique:
                return tuple(unique.values())
            if attempt + 1 < attempts:
                await asyncio.sleep(retry_delay_s)
        if last_timeout is not None:
            raise SigrokUnavailableError("sigrok device discovery timed out") from last_timeout
        return ()

    async def refresh_connection(self) -> KingstDescriptor:
        descriptors = await self.discover(executable=self.executable)
        matching = next(
            (
                descriptor
                for descriptor in descriptors
                if descriptor.device_id == self.descriptor.device_id
            ),
            None,
        )
        if matching is None and not self.descriptor.port_path:
            same_model = [
                descriptor
                for descriptor in descriptors
                if descriptor.model == self.descriptor.model
            ]
            if len(same_model) == 1:
                matching = same_model[0]
        if matching is None:
            raise SigrokUnavailableError(
                f"Kingst {self.descriptor.model} is not available on "
                f"{self.descriptor.port_path or self.descriptor.connection}"
            )
        self.descriptor = matching
        return matching

    def command(
        self,
        config: KingstCaptureConfig,
        output_file: Path,
    ) -> tuple[str, ...]:
        command = [
            str(self.executable),
            "--driver",
            f"kingst-la2016:conn={self.descriptor.connection}",
            "--channels",
            ",".join(f"CH{channel}" for channel in config.channels),
            "--samples",
            str(config.sample_count),
            "--config",
            f"samplerate={config.sample_rate_hz}",
            "--config",
            f"voltage_threshold={config.threshold_v:g}-{config.threshold_v:g}",
            "--config",
            f"captureratio={config.capture_ratio_percent}",
            "--loglevel",
            "4",
            "--output-file",
            str(output_file),
        ]
        if config.triggers:
            command.extend(
                (
                    "--triggers",
                    ",".join(
                        f"CH{trigger.channel}={trigger.sigrok_value}" for trigger in config.triggers
                    ),
                )
            )
        return tuple(command)

    async def capture(
        self,
        config: KingstCaptureConfig,
        output_file: Path,
        *,
        on_state: StateCallback,
    ) -> None:
        await self.refresh_connection()
        await asyncio.to_thread(output_file.parent.mkdir, parents=True, exist_ok=True)
        if await asyncio.to_thread(output_file.exists):
            raise FileExistsError(output_file)
        async with self._lock:
            if self._process is not None:
                raise RuntimeError("A logic capture is already active on this analyzer")
            process = await asyncio.create_subprocess_exec(
                *self.command(config, output_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_CREATE_NEW_PROCESS_GROUP,
            )
            self._process = process
        try:
            assert process.stderr is not None
            stderr_lines: list[str] = []
            acquisition_started = False
            while True:
                raw_line = await process.stderr.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                stderr_lines.append(line)
                normalized = line.casefold()
                if "pre-trigger sampling" in normalized:
                    acquisition_started = True
                    await on_state("pretrigger")
                elif "sampling, waiting for trigger" in normalized:
                    acquisition_started = True
                    await on_state("armed")
                elif "post-trigger sampling" in normalized:
                    acquisition_started = True
                    await on_state("posttrigger")
                elif acquisition_started and "run state:" in normalized and "(idle)" in normalized:
                    await on_state("downloading")
            return_code = await process.wait()
            session_valid = await asyncio.to_thread(
                self._is_valid_session_file,
                output_file,
            )
            cleanup_crash_after_valid_capture = (
                os.name == "nt" and return_code == self._WINDOWS_ACCESS_VIOLATION and session_valid
            )
            if return_code != 0 and not cleanup_crash_after_valid_capture:
                detail = "\n".join(stderr_lines[-60:]).strip()
                if "LIBUSB_ERROR_NOT_SUPPORTED" in detail:
                    detail = (
                        "The installed sigrok-cli cannot reopen the Kingst WinUSB "
                        "interface. Keep the native Kingst WinUSB driver; use an "
                        "OpenBench-compatible sigrok build with a current libusb.\n"
                        f"{detail}"
                    )
                raise SigrokUnavailableError(
                    f"sigrok-cli exited with code {return_code}" + (f"\n{detail}" if detail else "")
                )
            if not session_valid:
                detail = "\n".join(stderr_lines[-12:]).strip()
                raise SigrokUnavailableError(
                    "sigrok-cli did not create a valid capture file"
                    + (f"\n{detail}" if detail else "")
                )
        finally:
            async with self._lock:
                if self._process is process:
                    self._process = None

    async def stop(self) -> None:
        async with self._lock:
            process = self._process
        if process is None or process.returncode is not None:
            return
        if os.name == "nt":
            try:
                os.kill(process.pid, _CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                process.terminate()
        else:
            process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def close(self) -> None:
        await self.stop()

    @staticmethod
    def _output_size(output_file: Path) -> int:
        return output_file.stat().st_size if output_file.is_file() else 0

    @classmethod
    def _is_valid_session_file(cls, output_file: Path) -> bool:
        if cls._output_size(output_file) <= 0:
            return False
        try:
            with zipfile.ZipFile(output_file) as session:
                entries = {item.filename: item for item in session.infolist()}
                logic_entries = [
                    item
                    for name, item in entries.items()
                    if name.startswith("logic-") and item.file_size > 0
                ]
                return (
                    "version" in entries
                    and "metadata" in entries
                    and bool(logic_entries)
                    and session.testzip() is None
                )
        except (OSError, zipfile.BadZipFile):
            return False
