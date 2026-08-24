from __future__ import annotations

import threading

from openbench.domain import Measurement
from openbench.storage import Database
from openbench.storage.repositories import MeasurementRepository


class MeasurementService:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._latest: dict[str, Measurement] = {}
        self._lock = threading.Lock()

    def record(self, measurement: Measurement) -> None:
        with self._database.transaction() as session:
            MeasurementRepository.add(session, measurement)
        with self._lock:
            self._latest[measurement.channel_id] = measurement

    def latest(self, channel_id: str) -> Measurement | None:
        with self._lock:
            cached = self._latest.get(channel_id)
        if cached is not None:
            return cached
        with self._database.session() as session:
            measurement = MeasurementRepository.latest(session, channel_id)
        if measurement is not None:
            with self._lock:
                self._latest[channel_id] = measurement
        return measurement
