from __future__ import annotations

from openbench.storage.database import Database
from openbench.storage.repositories import InstrumentPreferenceRepository


class InstrumentPreferenceStore:
    """Persistent, device-ID-keyed preferences for safe UI/acquisition options."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, device_id: str) -> dict[str, object]:
        with self._database.session() as session:
            return InstrumentPreferenceRepository.get(session, device_id)

    def update(self, device_id: str, **values: object) -> dict[str, object]:
        with self._database.transaction() as session:
            preferences = InstrumentPreferenceRepository.get(session, device_id)
            preferences.update(values)
            InstrumentPreferenceRepository.put(session, device_id, preferences)
            return preferences

    def update_section(
        self,
        device_id: str,
        section: str,
        **values: object,
    ) -> dict[str, object]:
        with self._database.transaction() as session:
            preferences = InstrumentPreferenceRepository.get(session, device_id)
            stored_section = preferences.get(section)
            section_values = dict(stored_section) if isinstance(stored_section, dict) else {}
            section_values.update(values)
            preferences[section] = section_values
            InstrumentPreferenceRepository.put(session, device_id, preferences)
            return preferences
