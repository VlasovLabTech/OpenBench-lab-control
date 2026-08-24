from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field
from typing import Any

from openbench.drivers.ut197.protocol import FRAME_HEADER, READ_READING_COMMAND

DEFAULT_DEVICE_NAME = "UT197"
DEFAULT_SCAN_TIMEOUT_S = 10.0
DEFAULT_RESPONSE_TIMEOUT_S = 2.0
CONTROL_SETTLE_S = 0.6

SERVICE_UUID = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
NOTIFY_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"
WRITE_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"

MAX_DECLARED_FRAME_LENGTH = 256


class UT197UnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UT197Descriptor:
    name: str
    address: str
    device: object = field(repr=False, compare=False)


class _FrameBuffer:
    def __init__(self) -> None:
        self._data = bytearray()

    def clear(self) -> None:
        self._data.clear()

    def feed(self, data: bytes | bytearray) -> tuple[bytes, ...]:
        self._data.extend(data)
        frames: list[bytes] = []
        while True:
            header_at = self._data.find(FRAME_HEADER)
            if header_at < 0:
                self._data[:] = self._data[-1:] if self._data.endswith(FRAME_HEADER[:1]) else b""
                break
            if header_at:
                del self._data[:header_at]
            if len(self._data) < 4:
                break

            declared_length = int.from_bytes(self._data[2:4], "big")
            if declared_length < 4 or declared_length > MAX_DECLARED_FRAME_LENGTH:
                del self._data[0]
                continue
            frame_length = declared_length + 4
            if len(self._data) < frame_length:
                break
            frames.append(bytes(self._data[:frame_length]))
            del self._data[:frame_length]
        return tuple(frames)


def _load_bleak() -> Any:
    try:
        return importlib.import_module("bleak")
    except ImportError as exc:
        raise UT197UnavailableError(
            "bleak is required for UT197 support; install OpenBench with [hardware]"
        ) from exc


class UT197BleTransport:
    def __init__(
        self,
        descriptor: UT197Descriptor,
        *,
        response_timeout_s: float = DEFAULT_RESPONSE_TIMEOUT_S,
        bleak_module: Any | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.response_timeout_s = response_timeout_s
        self._bleak = bleak_module or _load_bleak()
        self._client: Any | None = None
        self._subscribed = False
        self._frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        self._buffer = _FrameBuffer()
        self._lock = asyncio.Lock()

    @classmethod
    async def discover(
        cls,
        *,
        name: str = DEFAULT_DEVICE_NAME,
        timeout_s: float = DEFAULT_SCAN_TIMEOUT_S,
        bleak_module: Any | None = None,
    ) -> tuple[UT197Descriptor, ...]:
        backend = bleak_module or _load_bleak()
        try:
            devices = await backend.BleakScanner.discover(timeout=timeout_s)
        except Exception as exc:
            raise UT197UnavailableError(f"UT197 BLE discovery failed: {exc}") from exc

        descriptors = []
        for device in devices:
            device_name = str(getattr(device, "name", "") or "")
            address = str(getattr(device, "address", "") or "")
            if device_name.casefold() != name.casefold() or not address:
                continue
            descriptors.append(
                UT197Descriptor(
                    name=device_name,
                    address=address,
                    device=device,
                )
            )
        return tuple(descriptors)

    def _on_disconnect(self, client: object) -> None:
        if client is self._client:
            self._client = None
            self._subscribed = False

    def _on_notification(self, _sender: object, data: bytearray) -> None:
        for frame in self._buffer.feed(data):
            if self._frames.full():
                try:
                    self._frames.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self._frames.put_nowait(frame)

    async def _ensure_connected(self) -> Any:
        if self._client is not None and self._client.is_connected and self._subscribed:
            return self._client

        client = self._bleak.BleakClient(
            self.descriptor.device,
            disconnected_callback=self._on_disconnect,
            timeout=30.0,
            pair=False,
        )
        try:
            await client.connect()
            await client.start_notify(NOTIFY_UUID, self._on_notification)
        except Exception:
            if client.is_connected:
                await client.disconnect()
            raise
        self._client = client
        self._subscribed = True
        return client

    def _clear_pending_frames(self) -> None:
        self._buffer.clear()
        while True:
            try:
                self._frames.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _reset_connection(self) -> None:
        client = self._client
        self._client = None
        self._subscribed = False
        self._clear_pending_frames()
        if client is None or not client.is_connected:
            return
        try:
            await client.stop_notify(NOTIFY_UUID)
        except Exception:
            pass
        await client.disconnect()

    async def request_reading_frame(self) -> bytes:
        async with self._lock:
            for attempt in range(2):
                try:
                    client = await self._ensure_connected()
                    self._clear_pending_frames()
                    await client.write_gatt_char(
                        WRITE_UUID,
                        READ_READING_COMMAND,
                        response=False,
                    )
                    return await asyncio.wait_for(
                        self._frames.get(),
                        timeout=self.response_timeout_s,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._reset_connection()
                    if attempt:
                        raise
            raise AssertionError("unreachable")

    async def send_control(self, command: bytes) -> None:
        async with self._lock:
            client = await self._ensure_connected()
            self._clear_pending_frames()
            await client.write_gatt_char(WRITE_UUID, command, response=True)
            await asyncio.sleep(CONTROL_SETTLE_S)

    async def close(self) -> None:
        async with self._lock:
            await self._reset_connection()
