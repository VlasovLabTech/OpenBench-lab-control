from __future__ import annotations

from pathlib import Path

from openbench.drivers.kingst_la2016.transport import (
    KingstCaptureConfig,
    KingstDescriptor,
    SigrokCLITransport,
    StateCallback,
)


class KingstLA2016:
    def __init__(
        self,
        descriptor: KingstDescriptor,
        *,
        transport: SigrokCLITransport | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.transport = transport or SigrokCLITransport(descriptor)

    @property
    def device_id(self) -> str:
        return self.descriptor.device_id

    async def identify(self) -> str:
        return (
            f"Kingst {self.descriptor.model} "
            f"({len(self.descriptor.logic_channels)} logic channels, "
            f"{self.descriptor.max_sample_rate_hz / 1_000_000:g} MHz max)"
        )

    def update_descriptor(self, descriptor: KingstDescriptor) -> None:
        if descriptor.device_id != self.device_id:
            raise ValueError("Updated Kingst descriptor belongs to another device")
        self.descriptor = descriptor
        self.transport.descriptor = descriptor

    async def capture(
        self,
        config: KingstCaptureConfig,
        output_file: Path,
        *,
        on_state: StateCallback,
    ) -> None:
        await self.transport.capture(config, output_file, on_state=on_state)

    async def stop(self) -> None:
        await self.transport.stop()

    async def close(self) -> None:
        await self.transport.close()
