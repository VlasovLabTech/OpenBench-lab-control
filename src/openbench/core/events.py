from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from openbench.domain import Measurement


class MeasurementEventBus:
    def __init__(self, queue_size: int = 32) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[Measurement]] = set()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Measurement]]:
        queue: asyncio.Queue[Measurement] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def publish(self, measurement: Measurement) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(measurement)
            except asyncio.QueueFull:
                # A racing slow subscriber may miss one sample, but cannot grow memory.
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
