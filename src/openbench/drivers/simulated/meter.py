from __future__ import annotations

import asyncio
import math
import time


class SimulatedMeter:
    device_id = "sim_meter_output_voltage"
    channel_id = "sim_meter_output_voltage.primary"

    async def identify(self) -> str:
        return "OpenBench Simulated DC Voltmeter"

    async def read_meter(self, channel_id: str) -> float:
        if channel_id != self.channel_id:
            raise ValueError(f"Unknown simulated meter channel: {channel_id}")
        await asyncio.sleep(0)
        return round(12.0 + math.sin(time.monotonic() * 0.7) * 0.025, 4)
