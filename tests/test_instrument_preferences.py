from __future__ import annotations

import pytest

from openbench.bootstrap import create_context
from openbench.config import Settings


@pytest.mark.asyncio
async def test_general_instrument_preferences_survive_context_restart(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'instrument-preferences.db'}"
    settings = Settings(
        database_url=database_url,
        capture_directory=str(tmp_path / "captures"),
        poll_interval_s=0.2,
        ut61e_enabled=False,
        ut61eplus_enabled=False,
        ut197_enabled=False,
        feeltech_enabled=False,
        dps150_enabled=False,
        owon_spm_enabled=False,
        micsig_enabled=False,
    )
    first = create_context(settings)
    device_id = "sim_meter_output_voltage"
    first.instrument_settings_service.update_context(device_id, "Bench supply output")
    await first.instrument_settings_service.update_poll_interval(device_id, 0.75)
    first.database.dispose()

    restarted = create_context(settings)
    restored = restarted.instrument_settings_service.get(device_id)

    assert restored.context == "Bench supply output"
    assert restored.poll_interval_s == 0.75
    restarted.database.dispose()
