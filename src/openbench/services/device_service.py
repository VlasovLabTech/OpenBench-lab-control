from __future__ import annotations

from openbench.domain import Channel, Device
from openbench.storage import Database
from openbench.storage.repositories import DeviceRepository


class DeviceService:
    def __init__(self, database: Database) -> None:
        self._database = database

    def register(self, device: Device, channels: tuple[Channel, ...]) -> None:
        with self._database.transaction() as session:
            DeviceRepository.upsert(session, device, channels)

    def list_devices(self) -> tuple[Device, ...]:
        with self._database.session() as session:
            return DeviceRepository.list_devices(session)

    def list_channels(self) -> tuple[Channel, ...]:
        with self._database.session() as session:
            return DeviceRepository.list_channels(session)
