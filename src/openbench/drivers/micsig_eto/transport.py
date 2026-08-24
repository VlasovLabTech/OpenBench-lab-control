from __future__ import annotations

import asyncio
import time

from openbench.drivers.micsig_eto.protocol import parse_identification
from openbench.drivers.micsig_mho1.protocol import MicsigIdentification, MicsigProtocolError
from openbench.drivers.micsig_mho1.transport import (
    MicsigScpiTransport,
    MicsigUnavailableError,
    _http_get_sync,
)

ETO_ASCII_READ_CHUNK_BYTES = 64 * 1024
ETO_ASCII_FINAL_TOKEN_IDLE_S = 0.25
_ASCII_NUMBER_BYTES = b"0123456789+-.eE"
_ASCII_DELIMITER_BYTES = b", \t\r\n"


class MicsigETOTransport(MicsigScpiTransport):
    """Raw TCP/VXI-11 discovery transport restricted to Micsig ETO5004."""

    instrument_label = "Micsig ETO5004"

    @staticmethod
    def parse_identification_response(response: str) -> MicsigIdentification:
        return parse_identification(response)

    async def capture_stored_screenshot(self, *, timeout_s: float = 15.0) -> bytes:
        """Create one documented scope-side screenshot and download it."""

        screenshot_directory = "/pictures/Screenshots"
        try:
            before = set(await self.list_http_links(screenshot_directory))
        except (OSError, RuntimeError, TimeoutError):
            # A new ETO has no Screenshots directory until the first capture.
            before = set()

        await self.write(":STORage:CAPTure:STARt")
        deadline = time.monotonic() + timeout_s
        newest_path: str | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            try:
                current = set(await self.list_http_links(screenshot_directory))
            except (OSError, RuntimeError, TimeoutError):
                continue
            candidates = sorted(
                path
                for path in current - before
                if path.casefold().endswith((".png", ".jpg", ".jpeg"))
            )
            completed = [path for path in candidates if "/.pending-" not in path]
            if completed:
                newest_path = completed[-1]
                break
            if candidates:
                newest_path = candidates[-1]

        if newest_path is None:
            raise MicsigUnavailableError(
                "Micsig ETO did not publish a stored screenshot before timeout"
            )
        return await self.download_http_file(newest_path)

    async def download_http_file(self, path: str) -> bytes:
        if path.startswith("/files/"):
            return await super().download_http_file(path)
        if (
            not path.startswith("/pictures/Screenshots/")
            or not path.casefold().endswith((".png", ".jpg", ".jpeg"))
            or ".." in path
            or "?" in path
            or "#" in path
        ):
            raise ValueError(
                "Micsig ETO download is limited to /files and screenshot images"
            )
        data = await asyncio.to_thread(
            _http_get_sync,
            self.descriptor.host,
            self.descriptor.http_port,
            path,
            self.block_timeout_s,
        )
        if len(data) > self.max_block_bytes:
            raise MicsigUnavailableError(
                f"Micsig ETO HTTP file is larger than {self.max_block_bytes} bytes"
            )
        return data

    async def query_ascii_block(self, command: str) -> bytes:
        """Read ETO point-count ASCII blocks without one await per byte.

        ETO firmware describes the SCPI header length as a number of samples,
        not a payload byte count. The shared conservative reader therefore has
        to find the requested number of numeric tokens. Reading each byte with
        a separate ``await`` made a 1,100-point screen trace take roughly
        45 seconds on the physical ETO5004. This model-specific reader keeps
        the same bounded token validation while consuming buffered chunks.
        """

        async with self._lock:
            for attempt in range(2):
                try:
                    reader, writer = await self._ensure_connected()
                    await self._send(writer, command, self.block_timeout_s)
                    marker = await asyncio.wait_for(
                        reader.readexactly(1), timeout=self.block_timeout_s
                    )
                    if marker != b"#":
                        remainder = await asyncio.wait_for(
                            reader.readline(), timeout=self.response_timeout_s
                        )
                        response = (marker + remainder).decode("ascii", errors="replace")
                        raise MicsigProtocolError(
                            f"Expected SCPI ASCII block, got {response!r}"
                        )
                    digits_raw = await asyncio.wait_for(
                        reader.readexactly(1), timeout=self.block_timeout_s
                    )
                    if not digits_raw.isdigit() or digits_raw == b"0":
                        raise MicsigProtocolError(
                            f"Invalid SCPI ASCII block length descriptor: {digits_raw!r}"
                        )
                    digits = int(digits_raw)
                    length_raw = await asyncio.wait_for(
                        reader.readexactly(digits), timeout=self.block_timeout_s
                    )
                    if not length_raw.isdigit() or int(length_raw) > self.max_block_bytes:
                        raise MicsigProtocolError(
                            f"Invalid SCPI ASCII block point count: {length_raw!r}"
                        )
                    declared_points = int(length_raw)
                    if declared_points <= 0:
                        raise MicsigProtocolError(
                            "Micsig ETO returned an empty ASCII waveform"
                        )

                    payload = bytearray()
                    token_open = False
                    points = 0
                    while points < declared_points:
                        # ETO5004 firmware 3.392.132 sometimes leaves the final
                        # numeric token open and keeps the SCPI socket alive.
                        # Once every preceding point is complete, a short idle
                        # boundary safely terminates that last token. Waiting
                        # for the generic block timeout here added 20-40 s.
                        final_token_open = (
                            token_open and points + 1 == declared_points
                        )
                        read_timeout_s = (
                            ETO_ASCII_FINAL_TOKEN_IDLE_S
                            if final_token_open
                            else self.block_timeout_s
                        )
                        try:
                            chunk = await asyncio.wait_for(
                                reader.read(ETO_ASCII_READ_CHUNK_BYTES),
                                timeout=read_timeout_s,
                            )
                        except TimeoutError:
                            if final_token_open:
                                points += 1
                                token_open = False
                                break
                            raise
                        if not chunk:
                            if final_token_open:
                                points += 1
                                token_open = False
                                break
                            raise asyncio.IncompleteReadError(bytes(payload), None)

                        consumed = 0
                        for value in chunk:
                            consumed += 1
                            if value in _ASCII_NUMBER_BYTES:
                                token_open = True
                            elif value in _ASCII_DELIMITER_BYTES:
                                if token_open:
                                    points += 1
                                    token_open = False
                                    if points == declared_points:
                                        break
                            else:
                                raise MicsigProtocolError(
                                    "Invalid byte in Micsig ETO ASCII waveform: "
                                    f"{bytes((value,))!r}"
                                )
                        payload.extend(chunk[:consumed])
                        if len(payload) > self.max_block_bytes:
                            raise MicsigProtocolError(
                                "Micsig ETO ASCII waveform exceeds the configured size limit"
                            )

                    result = bytes(payload).rstrip(_ASCII_DELIMITER_BYTES)
                    # Firmware can append inconsistent padding after the final
                    # token. Discard it with the connection, exactly as the
                    # conservative shared reader does.
                    await self._reset_connection()
                    return result
                except asyncio.CancelledError:
                    raise
                except MicsigProtocolError:
                    await self._reset_connection()
                    raise
                except (OSError, TimeoutError, asyncio.IncompleteReadError):
                    await self._reset_connection()
                    if attempt:
                        raise
            raise AssertionError("unreachable")
