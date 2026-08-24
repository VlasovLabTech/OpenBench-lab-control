from __future__ import annotations

from openbench.drivers.micsig_common import (
    MICSIG_MAXIMUM_ASCII_CHUNK_POINTS,
    MICSIG_MAXIMUM_MEMORY_POINTS,
    MicsigMaximumAsciiChunk,
    MicsigMaximumCaptureInfo,
)
from openbench.drivers.micsig_mho1.protocol import (
    MicsigIdentification,
    MicsigProtocolError,
)

SUPPORTED_ETO_MODELS = frozenset(("ETO5004",))
ETO_MAXIMUM_ASCII_CHUNK_POINTS = MICSIG_MAXIMUM_ASCII_CHUNK_POINTS
ETO_MAXIMUM_MEMORY_POINTS = MICSIG_MAXIMUM_MEMORY_POINTS
MicsigETOMaximumCaptureInfo = MicsigMaximumCaptureInfo
MicsigETOMaximumAsciiChunk = MicsigMaximumAsciiChunk


def parse_identification(response: str) -> MicsigIdentification:
    """Parse and strictly validate the ETO5004 IEEE 488.2 identity reply."""
    fields = tuple(field.strip() for field in response.strip().split(","))
    if len(fields) != 4 or any(not field for field in fields):
        raise MicsigProtocolError(f"Invalid Micsig ETO *IDN? response: {response!r}")
    identification = MicsigIdentification(*fields)
    if (
        identification.manufacturer.casefold() != "micsig"
        or identification.model.upper() not in SUPPORTED_ETO_MODELS
    ):
        raise MicsigProtocolError(
            f"Unsupported ETO SCPI instrument: {identification.manufacturer},{identification.model}"
        )
    return identification


def parse_word_hex_waveform(payload: bytes) -> tuple[int, ...]:
    """Parse ETO ``FORMAT WORD`` data encoded as four ASCII hex digits/point."""

    if not payload:
        raise MicsigProtocolError("Micsig ETO returned an empty WORD waveform")
    if len(payload) % 4:
        raise MicsigProtocolError(
            f"Micsig ETO WORD payload length must be divisible by four, got {len(payload)}"
        )
    try:
        text = payload.decode("ascii")
        return tuple(int(text[index : index + 4], 16) for index in range(0, len(text), 4))
    except (UnicodeError, ValueError) as exc:
        raise MicsigProtocolError("Micsig ETO returned invalid WORD hex data") from exc
