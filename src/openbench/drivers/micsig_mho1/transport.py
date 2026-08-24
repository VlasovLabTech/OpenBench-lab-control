from __future__ import annotations

import asyncio
import html
import http.client
import ipaddress
import random
import re
import socket
import struct
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from openbench.drivers.micsig_mho1.protocol import (
    MicsigIdentification,
    MicsigProtocolError,
    parse_identification,
)

DEFAULT_SCPI_PORT = 5025
DEFAULT_SCREEN_PORT = 8888
DEFAULT_HTTP_PORT = 80
DEFAULT_DISCOVERY_TIMEOUT_S = 2.0
DEFAULT_CONNECT_TIMEOUT_S = 0.5
DEFAULT_RESPONSE_TIMEOUT_S = 5.0
DEFAULT_BLOCK_TIMEOUT_S = 20.0
DEFAULT_MAX_BLOCK_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_SCAN_HOSTS = 1024

PORTMAPPER_PROGRAM = 100000
PORTMAPPER_VERSION = 2
PORTMAPPER_GETPORT = 3
VXI11_CORE_PROGRAM = 0x0607AF
VXI11_CORE_VERSION = 1
IP_PROTOCOL_TCP = 6

SCREEN_PACKET_HEADER_BYTES = 8
SCREEN_PACKET_MAGIC = b"XU\xaa"
MAX_SCREEN_PACKET_BYTES = 32 * 1024 * 1024


class MicsigUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MicsigDescriptor:
    host: str
    scpi_port: int
    screen_port: int
    identification: MicsigIdentification
    vxi11_port: int | None = None
    http_port: int = DEFAULT_HTTP_PORT
    screen_width: int = 1280
    screen_height: int = 800

    @property
    def manufacturer(self) -> str:
        return self.identification.manufacturer

    @property
    def model(self) -> str:
        return self.identification.model

    @property
    def serial_number(self) -> str:
        return self.identification.serial_number

    @property
    def firmware_version(self) -> str:
        return self.identification.firmware_version


def _build_portmapper_getport_request(xid: int) -> bytes:
    return struct.pack(
        ">14I",
        xid,
        0,  # CALL
        2,  # RPC version
        PORTMAPPER_PROGRAM,
        PORTMAPPER_VERSION,
        PORTMAPPER_GETPORT,
        0,  # AUTH_NULL credentials
        0,
        0,  # AUTH_NULL verifier
        0,
        VXI11_CORE_PROGRAM,
        VXI11_CORE_VERSION,
        IP_PROTOCOL_TCP,
        0,
    )


def _parse_portmapper_getport_response(data: bytes, xid: int) -> int | None:
    if len(data) < 28:
        return None
    response_xid, message_type, reply_status = struct.unpack_from(">3I", data)
    if response_xid != xid or message_type != 1 or reply_status != 0:
        return None

    verifier_length = struct.unpack_from(">I", data, 16)[0]
    accepted_status_offset = 20 + ((verifier_length + 3) & ~3)
    port_offset = accepted_status_offset + 4
    if len(data) < port_offset + 4:
        return None
    accepted_status = struct.unpack_from(">I", data, accepted_status_offset)[0]
    if accepted_status != 0:
        return None
    return struct.unpack_from(">I", data, port_offset)[0]


def _local_ipv4_networks() -> tuple[ipaddress.IPv4Network, ...]:
    addresses: set[ipaddress.IPv4Address] = set()
    try:
        records = socket.getaddrinfo(
            socket.gethostname(),
            None,
            socket.AF_INET,
            socket.SOCK_STREAM,
        )
    except OSError:
        return ()
    for record in records:
        try:
            address = ipaddress.IPv4Address(record[4][0])
        except ipaddress.AddressValueError:
            continue
        if address.is_loopback or address.is_link_local or not address.is_private:
            continue
        addresses.add(address)

    networks = {ipaddress.IPv4Network(f"{address}/24", strict=False) for address in addresses}

    def rank(network: ipaddress.IPv4Network) -> tuple[int, int]:
        first = int(str(network.network_address).split(".", maxsplit=1)[0])
        if first == 192:
            priority = 0
        elif first == 10:
            priority = 1
        else:
            priority = 2
        return priority, int(network.network_address)

    return tuple(sorted(networks, key=rank))


def _vxi11_discover_sync(timeout_s: float) -> dict[str, int]:
    if timeout_s <= 0:
        return {}

    xid = random.getrandbits(32)
    request = _build_portmapper_getport_request(xid)
    targets = {"255.255.255.255"}
    targets.update(str(network.broadcast_address) for network in _local_ipv4_networks())

    responses: dict[str, int] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))
        for target in targets:
            try:
                sock.sendto(request, (target, 111))
            except OSError:
                continue

        deadline = time.monotonic() + timeout_s
        last_response_at: float | None = None
        while time.monotonic() < deadline:
            if last_response_at is not None and time.monotonic() - last_response_at >= 0.2:
                break
            remaining = deadline - time.monotonic()
            sock.settimeout(max(0.01, min(0.1, remaining)))
            try:
                data, address = sock.recvfrom(4096)
            except TimeoutError:
                continue
            port = _parse_portmapper_getport_response(data, xid)
            if port is None or port == 0:
                continue
            responses[address[0]] = port
            last_response_at = time.monotonic()
    finally:
        sock.close()
    return responses


def _parse_scan_networks(subnets: Iterable[str]) -> tuple[ipaddress.IPv4Network, ...]:
    networks: list[ipaddress.IPv4Network] = []
    for subnet in subnets:
        try:
            network = ipaddress.ip_network(subnet.strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid Micsig discovery subnet: {subnet!r}") from exc
        if not isinstance(network, ipaddress.IPv4Network):
            raise ValueError(f"Micsig discovery supports IPv4 subnets only: {subnet!r}")
        networks.append(network)
    return tuple(networks)


async def _probe_identification(
    host: str,
    port: int,
    timeout_s: float,
    identification_parser: Callable[[str], MicsigIdentification] = parse_identification,
) -> MicsigIdentification | None:
    writer: asyncio.StreamWriter | None = None
    try:
        reader, connected_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout_s,
        )
        writer = connected_writer
        writer.write(b"*IDN?\n")
        await asyncio.wait_for(writer.drain(), timeout=timeout_s)
        response = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
        if not response:
            return None
        return identification_parser(response.decode("ascii", errors="replace"))
    except (OSError, TimeoutError, MicsigProtocolError, UnicodeError):
        return None
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def _validated_descriptors(
    hosts: Iterable[str],
    *,
    scpi_port: int,
    screen_port: int,
    http_port: int,
    connect_timeout_s: float,
    vxi11_ports: dict[str, int] | None = None,
    identification_parser: Callable[[str], MicsigIdentification] = parse_identification,
) -> tuple[MicsigDescriptor, ...]:
    unique_hosts = tuple(dict.fromkeys(host.strip() for host in hosts if host.strip()))
    identifications = await asyncio.gather(
        *(
            _probe_identification(
                host,
                scpi_port,
                connect_timeout_s,
                identification_parser,
            )
            for host in unique_hosts
        )
    )
    descriptors = []
    seen_serials: set[str] = set()
    for host, identification in zip(unique_hosts, identifications, strict=True):
        if identification is None or identification.serial_number in seen_serials:
            continue
        seen_serials.add(identification.serial_number)
        descriptors.append(
            MicsigDescriptor(
                host=host,
                scpi_port=scpi_port,
                screen_port=screen_port,
                identification=identification,
                vxi11_port=(vxi11_ports or {}).get(host),
                http_port=http_port,
            )
        )
    return tuple(descriptors)


def _http_get_sync(host: str, port: int, path: str, timeout_s: float) -> bytes:
    connection = http.client.HTTPConnection(host, port, timeout=timeout_s)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        data = response.read()
        if response.status != 200:
            raise MicsigUnavailableError(
                f"Micsig HTTP GET {path} returned status {response.status}"
            )
        return data
    finally:
        connection.close()


def _vxi11_query_raw_sync(host: str, command: str, timeout_s: float) -> bytes:
    import vxi11  # type: ignore

    instrument = vxi11.Instrument(host)
    instrument.timeout = timeout_s
    try:
        return instrument.ask_raw(command.encode("ascii"))
    finally:
        instrument.close()


def _list_screenshots_sync(
    host: str,
    port: int,
    timeout_s: float,
) -> tuple[str, ...]:
    body = _http_get_sync(
        host,
        port,
        "/pictures/Screenshots",
        timeout_s,
    ).decode("utf-8", errors="replace")
    return tuple(
        re.findall(
            r'href="(/pictures/Screenshots/[^"]+\.(?:png|jpg|jpeg))"',
            body,
            flags=re.IGNORECASE,
        )
    )


def _list_http_links_sync(
    host: str,
    port: int,
    path: str,
    timeout_s: float,
) -> tuple[str, ...]:
    body = _http_get_sync(host, port, path, timeout_s).decode(
        "utf-8",
        errors="replace",
    )
    base = f"http://{host}:{port}{path}"
    links: list[str] = []
    for raw_href in re.findall(r'href=["\']([^"\']+)["\']', body, flags=re.IGNORECASE):
        parsed = urlsplit(urljoin(base, html.unescape(raw_href)))
        if parsed.hostname != host or (parsed.port or 80) != port:
            continue
        if not parsed.path.startswith("/") or parsed.path in links:
            continue
        links.append(parsed.path)
    return tuple(links)


class MicsigScpiTransport:
    instrument_label = "Micsig MHO1"

    @staticmethod
    def parse_identification_response(response: str) -> MicsigIdentification:
        return parse_identification(response)

    def __init__(
        self,
        descriptor: MicsigDescriptor,
        *,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        response_timeout_s: float = DEFAULT_RESPONSE_TIMEOUT_S,
        block_timeout_s: float = DEFAULT_BLOCK_TIMEOUT_S,
        max_block_bytes: int = DEFAULT_MAX_BLOCK_BYTES,
    ) -> None:
        self.descriptor = descriptor
        self.connect_timeout_s = connect_timeout_s
        self.response_timeout_s = response_timeout_s
        self.block_timeout_s = block_timeout_s
        self.max_block_bytes = max_block_bytes
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @classmethod
    async def connect(
        cls,
        host: str,
        *,
        scpi_port: int = DEFAULT_SCPI_PORT,
        screen_port: int = DEFAULT_SCREEN_PORT,
        http_port: int = DEFAULT_HTTP_PORT,
        vxi11_port: int | None = None,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
    ) -> MicsigScpiTransport:
        writer: asyncio.StreamWriter | None = None
        try:
            reader, connected_writer = await asyncio.wait_for(
                asyncio.open_connection(host, scpi_port),
                timeout=connect_timeout_s,
            )
            writer = connected_writer
            await cls._send(connected_writer, "*IDN?", connect_timeout_s)
            response = await asyncio.wait_for(
                reader.readline(),
                timeout=connect_timeout_s,
            )
            identification = cls.parse_identification_response(
                response.decode("ascii", errors="replace")
            )
        except (OSError, TimeoutError, UnicodeError, MicsigProtocolError) as exc:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            raise MicsigUnavailableError(
                f"No supported {cls.instrument_label} at {host}:{scpi_port}"
            ) from exc

        descriptor = MicsigDescriptor(
            host=host,
            scpi_port=scpi_port,
            screen_port=screen_port,
            http_port=http_port,
            vxi11_port=vxi11_port,
            identification=identification,
        )
        transport = cls(
            descriptor,
            connect_timeout_s=connect_timeout_s,
        )
        transport._reader = reader
        transport._writer = connected_writer
        return transport

    @classmethod
    async def discover_connected(
        cls,
        *,
        hosts: tuple[str, ...] = (),
        subnets: tuple[str, ...] = (),
        timeout_s: float = DEFAULT_DISCOVERY_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        scan_fallback: bool = True,
        scpi_port: int = DEFAULT_SCPI_PORT,
        screen_port: int = DEFAULT_SCREEN_PORT,
        http_port: int = DEFAULT_HTTP_PORT,
        max_scan_hosts: int = DEFAULT_MAX_SCAN_HOSTS,
        vxi11_discoverer: Callable[[float], dict[str, int]] | None = None,
    ) -> tuple[MicsigScpiTransport, ...]:
        vxi11_ports: dict[str, int] = {}
        candidates = tuple(dict.fromkeys(host.strip() for host in hosts if host.strip()))
        if not candidates:
            discoverer = vxi11_discoverer or _vxi11_discover_sync
            try:
                vxi11_ports = await asyncio.to_thread(discoverer, timeout_s)
            except OSError:
                vxi11_ports = {}
            candidates = tuple(vxi11_ports)

        async def try_connect(host: str) -> MicsigScpiTransport | None:
            try:
                return await cls.connect(
                    host,
                    scpi_port=scpi_port,
                    screen_port=screen_port,
                    http_port=http_port,
                    vxi11_port=vxi11_ports.get(host),
                    connect_timeout_s=connect_timeout_s,
                )
            except MicsigUnavailableError:
                return None

        connected = tuple(
            transport
            for transport in await asyncio.gather(*(try_connect(host) for host in candidates))
            if transport is not None
        )
        if connected or not scan_fallback:
            return connected

        networks = _parse_scan_networks(subnets) if subnets else _local_ipv4_networks()
        scanned = 0
        for network in networks:
            network_hosts = tuple(str(address) for address in network.hosts())
            scanned += len(network_hosts)
            if scanned > max_scan_hosts:
                raise MicsigUnavailableError(
                    f"Micsig subnet fallback exceeds {max_scan_hosts} hosts"
                )
            connected = tuple(
                transport
                for transport in await asyncio.gather(
                    *(try_connect(host) for host in network_hosts)
                )
                if transport is not None
            )
            if connected:
                return connected
        return ()

    @classmethod
    async def discover(
        cls,
        *,
        hosts: tuple[str, ...] = (),
        subnets: tuple[str, ...] = (),
        timeout_s: float = DEFAULT_DISCOVERY_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        scan_fallback: bool = True,
        scpi_port: int = DEFAULT_SCPI_PORT,
        screen_port: int = DEFAULT_SCREEN_PORT,
        http_port: int = DEFAULT_HTTP_PORT,
        max_scan_hosts: int = DEFAULT_MAX_SCAN_HOSTS,
        vxi11_discoverer: Callable[[float], dict[str, int]] | None = None,
    ) -> tuple[MicsigDescriptor, ...]:
        if hosts:
            explicit = await _validated_descriptors(
                hosts,
                scpi_port=scpi_port,
                screen_port=screen_port,
                http_port=http_port,
                connect_timeout_s=connect_timeout_s,
                identification_parser=cls.parse_identification_response,
            )
            if explicit:
                return explicit

        discoverer = vxi11_discoverer or _vxi11_discover_sync
        try:
            vxi11_ports = await asyncio.to_thread(discoverer, timeout_s)
        except OSError:
            vxi11_ports = {}
        discovered = await _validated_descriptors(
            vxi11_ports,
            scpi_port=scpi_port,
            screen_port=screen_port,
            http_port=http_port,
            connect_timeout_s=connect_timeout_s,
            vxi11_ports=vxi11_ports,
            identification_parser=cls.parse_identification_response,
        )
        if discovered or not scan_fallback:
            return discovered

        networks = _parse_scan_networks(subnets) if subnets else _local_ipv4_networks()
        scanned = 0
        for network in networks:
            network_hosts = tuple(str(address) for address in network.hosts())
            scanned += len(network_hosts)
            if scanned > max_scan_hosts:
                raise MicsigUnavailableError(
                    f"Micsig subnet fallback exceeds {max_scan_hosts} hosts"
                )
            descriptors = await _validated_descriptors(
                network_hosts,
                scpi_port=scpi_port,
                screen_port=screen_port,
                http_port=http_port,
                connect_timeout_s=connect_timeout_s,
                identification_parser=cls.parse_identification_response,
            )
            if descriptors:
                return descriptors
        return ()

    async def _ensure_connected(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        writer = self._writer
        if self._reader is not None and writer is not None and not writer.is_closing():
            return self._reader, writer
        try:
            reader, connected_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.descriptor.host,
                    self.descriptor.scpi_port,
                ),
                timeout=self.connect_timeout_s,
            )
        except (OSError, TimeoutError) as exc:
            raise MicsigUnavailableError(
                f"Cannot connect to {self.descriptor.model} at "
                f"{self.descriptor.host}:{self.descriptor.scpi_port}: {exc}"
            ) from exc
        self._reader = reader
        self._writer = connected_writer
        return reader, connected_writer

    async def _reset_connection(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, command: str, timeout_s: float) -> None:
        if "\n" in command or "\r" in command:
            raise ValueError("SCPI command must be a single line")
        writer.write(command.encode("ascii") + b"\n")
        await asyncio.wait_for(writer.drain(), timeout=timeout_s)

    async def _consume_block_terminator(self, reader: asyncio.StreamReader) -> None:
        try:
            first = await asyncio.wait_for(reader.readexactly(1), timeout=0.05)
        except TimeoutError:
            return
        if first == b"\n":
            return
        if first != b"\r":
            raise MicsigProtocolError(f"Unexpected byte after Micsig SCPI block: {first!r}")
        try:
            second = await asyncio.wait_for(reader.readexactly(1), timeout=0.05)
        except TimeoutError:
            return
        if second != b"\n":
            raise MicsigProtocolError(f"Unexpected byte after Micsig SCPI block CR: {second!r}")

    async def query_text(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
        attempts: int = 2,
    ) -> str:
        effective_timeout_s = self.response_timeout_s if timeout_s is None else timeout_s
        if effective_timeout_s <= 0:
            raise ValueError("Micsig query timeout must be positive")
        if attempts not in {1, 2}:
            raise ValueError("Micsig query attempts must be 1 or 2")
        async with self._lock:
            for attempt in range(attempts):
                try:
                    reader, writer = await self._ensure_connected()
                    await self._send(writer, command, effective_timeout_s)
                    response = await asyncio.wait_for(
                        reader.readline(),
                        timeout=effective_timeout_s,
                    )
                    if not response:
                        raise MicsigUnavailableError("Micsig closed the SCPI connection")
                    return response.decode("ascii", errors="replace").strip()
                except asyncio.CancelledError:
                    raise
                except (OSError, TimeoutError, MicsigUnavailableError):
                    await self._reset_connection()
                    if attempt + 1 >= attempts:
                        raise
            raise AssertionError("unreachable")

    async def query_block(
        self,
        command: str,
        *,
        length_multiplier: int = 1,
    ) -> bytes:
        if length_multiplier <= 0:
            raise ValueError("SCPI block length multiplier must be positive")
        async with self._lock:
            for attempt in range(2):
                try:
                    reader, writer = await self._ensure_connected()
                    await self._send(writer, command, self.block_timeout_s)
                    marker = await asyncio.wait_for(
                        reader.readexactly(1),
                        timeout=self.block_timeout_s,
                    )
                    if marker != b"#":
                        remainder = await asyncio.wait_for(
                            reader.readline(),
                            timeout=self.response_timeout_s,
                        )
                        response = (marker + remainder).decode(
                            "ascii",
                            errors="replace",
                        )
                        raise MicsigProtocolError(
                            f"Expected SCPI definite-length block, got {response!r}"
                        )
                    digits_raw = await asyncio.wait_for(
                        reader.readexactly(1),
                        timeout=self.block_timeout_s,
                    )
                    if not digits_raw.isdigit() or digits_raw == b"0":
                        raise MicsigProtocolError(
                            f"Invalid SCPI block length descriptor: {digits_raw!r}"
                        )
                    digits = int(digits_raw)
                    length_raw = await asyncio.wait_for(
                        reader.readexactly(digits),
                        timeout=self.block_timeout_s,
                    )
                    if not length_raw.isdigit():
                        raise MicsigProtocolError(f"Invalid SCPI block length: {length_raw!r}")
                    declared_length = int(length_raw)
                    length = declared_length * length_multiplier
                    if length > self.max_block_bytes:
                        raise MicsigProtocolError(
                            f"SCPI block payload length {length} exceeds limit "
                            f"{self.max_block_bytes}"
                        )
                    if length == 0:
                        payload = b""
                    else:
                        payload = await asyncio.wait_for(
                            reader.readexactly(length),
                            timeout=self.block_timeout_s,
                        )
                    await self._consume_block_terminator(reader)
                    return payload
                except asyncio.CancelledError:
                    raise
                except MicsigProtocolError:
                    raise
                except (OSError, TimeoutError, asyncio.IncompleteReadError):
                    await self._reset_connection()
                    if attempt:
                        raise
            raise AssertionError("unreachable")

    async def query_ascii_block(self, command: str) -> bytes:
        """Read fast ASCII data whose firmware header length is a point count."""
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
                        raise MicsigProtocolError(f"Expected SCPI ASCII block, got {response!r}")
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
                    if declared_points == 0:
                        await self._reset_connection()
                        return b""
                    payload = bytearray()
                    completed_points = 0
                    in_number = False
                    while completed_points < declared_points:
                        value = await asyncio.wait_for(
                            reader.readexactly(1), timeout=self.block_timeout_s
                        )
                        if value[0] in b"0123456789+-.eE":
                            payload.extend(value)
                            in_number = True
                        elif value[0] in b", \t\r\n":
                            if in_number:
                                completed_points += 1
                                in_number = False
                                if completed_points >= declared_points:
                                    break
                            payload.extend(value)
                        else:
                            raise MicsigProtocolError(
                                "Invalid byte in Micsig ASCII waveform"
                            )
                        if len(payload) > self.max_block_bytes:
                            raise MicsigProtocolError(
                                "Micsig ASCII waveform exceeds the configured size limit"
                            )
                    result = bytes(payload).rstrip(b" ,\t\r\n")
                    # This firmware pads the response after the declared point
                    # count inconsistently. Close the waveform connection so
                    # those bytes cannot be mistaken for the next SCPI reply.
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

    async def write(self, command: str) -> None:
        async with self._lock:
            try:
                _, writer = await self._ensure_connected()
                await self._send(writer, command, self.response_timeout_s)
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError):
                await self._reset_connection()
                raise

    async def read_screen_h264(self) -> bytes:
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.descriptor.host,
                    self.descriptor.screen_port,
                ),
                timeout=self.connect_timeout_s,
            )
        except (OSError, TimeoutError) as exc:
            raise MicsigUnavailableError(
                f"Cannot connect to Micsig screen stream at "
                f"{self.descriptor.host}:{self.descriptor.screen_port}: {exc}"
            ) from exc

        payloads: list[bytes] = []
        try:
            for _ in range(4):
                header = await asyncio.wait_for(
                    reader.readexactly(SCREEN_PACKET_HEADER_BYTES),
                    timeout=self.block_timeout_s,
                )
                packet_type = header[0]
                if header[1:4] != SCREEN_PACKET_MAGIC:
                    raise MicsigProtocolError(
                        f"Invalid Micsig screen packet header: {header.hex(' ')}"
                    )
                length = struct.unpack_from("<I", header, 4)[0]
                if length == 0 or length > MAX_SCREEN_PACKET_BYTES:
                    raise MicsigProtocolError(f"Invalid Micsig screen packet length: {length}")
                payload = await asyncio.wait_for(
                    reader.readexactly(length),
                    timeout=self.block_timeout_s,
                )
                payloads.append(payload)
                if packet_type == 2:
                    break
            data = b"".join(payloads)
            if b"\x00\x00\x00\x01" not in data:
                raise MicsigProtocolError("Micsig screen stream is not Annex-B H.264")
            return data
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def query_vxi11_raw(self, command: str) -> bytes:
        if command != ":SYS:SCR?":
            raise ValueError("Only the bounded screenshot query is supported over VXI-11")
        return await asyncio.to_thread(
            _vxi11_query_raw_sync,
            self.descriptor.host,
            command,
            self.block_timeout_s,
        )

    async def capture_stored_screenshot(self, *, timeout_s: float = 5.0) -> bytes:
        before = set(
            await asyncio.to_thread(
                _list_screenshots_sync,
                self.descriptor.host,
                self.descriptor.http_port,
                self.response_timeout_s,
            )
        )
        await self.write(":STORage:CAPTure:STARt")

        deadline = time.monotonic() + timeout_s
        newest_path: str | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            current = set(
                await asyncio.to_thread(
                    _list_screenshots_sync,
                    self.descriptor.host,
                    self.descriptor.http_port,
                    self.response_timeout_s,
                )
            )
            candidates = current - before
            completed = sorted(path for path in candidates if "/.pending-" not in path)
            pending = sorted(path for path in candidates if "/.pending-" in path)
            if completed:
                newest_path = completed[-1]
                break
            if pending:
                newest_path = pending[-1]
                # Pending files are already readable, but give the firmware one
                # more poll interval to atomically publish the final filename.
                continue
        if newest_path is None:
            raise MicsigUnavailableError(
                "Micsig did not publish a stored screenshot before timeout"
            )
        return await asyncio.to_thread(
            _http_get_sync,
            self.descriptor.host,
            self.descriptor.http_port,
            newest_path,
            self.block_timeout_s,
        )

    async def list_http_links(self, path: str = "/") -> tuple[str, ...]:
        if path != "/" and (
            not path.startswith(("/files", "/pictures"))
            or ".." in path
            or "?" in path
            or "#" in path
        ):
            raise ValueError("Micsig storage browsing is limited to /files and /pictures")
        return await asyncio.to_thread(
            _list_http_links_sync,
            self.descriptor.host,
            self.descriptor.http_port,
            path,
            self.response_timeout_s,
        )

    async def download_http_file(self, path: str) -> bytes:
        if not path.startswith("/files/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("Micsig waveform download is limited to /files")
        data = await asyncio.to_thread(
            _http_get_sync,
            self.descriptor.host,
            self.descriptor.http_port,
            path,
            self.block_timeout_s,
        )
        if len(data) > self.max_block_bytes:
            raise MicsigUnavailableError(
                f"Micsig HTTP file is larger than {self.max_block_bytes} bytes"
            )
        return data

    async def close(self) -> None:
        async with self._lock:
            await self._reset_connection()
