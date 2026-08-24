from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from openbench.bootstrap import ApplicationContext, create_context
from openbench.config import Settings
from openbench.domain import MatrixConnection
from openbench.services.matrix_service import (
    INITIAL_PROFILE_ID,
    MatrixValidationError,
)
from openbench.storage.models import MatrixConnectionModel, MatrixProfileModel


def test_self_connection_forbidden(context: ApplicationContext) -> None:
    with pytest.raises(MatrixValidationError, match="itself"):
        context.matrix_service.create_profile(
            "Invalid self route",
            (MatrixConnection("dut.vout", "dut.vout"),),
        )


def test_duplicate_connection_forbidden_regardless_of_direction(
    context: ApplicationContext,
) -> None:
    with pytest.raises(MatrixValidationError, match="duplicate"):
        context.matrix_service.create_profile(
            "Invalid duplicate",
            (
                MatrixConnection("dut.vout", "meter.voltage_hi"),
                MatrixConnection("meter.voltage_hi", "dut.vout"),
            ),
        )


def test_source_to_source_forbidden(context: ApplicationContext) -> None:
    with pytest.raises(MatrixValidationError, match="source-to-source"):
        context.matrix_service.create_profile(
            "Invalid sources",
            (MatrixConnection("source.main", "source.aux"),),
        )


def test_unknown_port_forbidden(context: ApplicationContext) -> None:
    with pytest.raises(MatrixValidationError, match="unknown port"):
        context.matrix_service.create_profile(
            "Invalid unknown",
            (MatrixConnection("dut.vout", "missing.port"),),
        )


def test_profile_apply_is_atomic_on_validation_failure(
    context: ApplicationContext,
) -> None:
    context.matrix_service.apply_profile(INITIAL_PROFILE_ID)
    before = context.matrix_service.active()
    invalid_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    with context.database.transaction() as session:
        model = MatrixProfileModel(
            id=invalid_id,
            name="Persisted invalid profile",
            version=1,
            created_at=now,
            updated_at=now,
            connections=[
                MatrixConnectionModel(
                    from_port="dut.vout",
                    to_port="missing.port",
                )
            ],
        )
        session.add(model)

    with pytest.raises(MatrixValidationError):
        context.matrix_service.apply_profile(invalid_id)

    after = context.matrix_service.active()
    assert after.profile_id == before.profile_id
    assert after.active_connections == before.active_connections


def test_open_all_clears_active_state(context: ApplicationContext) -> None:
    context.matrix_service.apply_profile(INITIAL_PROFILE_ID)
    result = context.matrix_service.open_all()
    assert result.active_connections == ()
    assert context.matrix_service.active().profile_id is None


def test_profile_persists_across_context_restart(
    context: ApplicationContext,
    database_url: str,
) -> None:
    created = context.matrix_service.create_profile(
        "Persistent profile",
        (MatrixConnection("dut.vout", "scope.ch1"),),
    )
    context.database.dispose()

    restored = create_context(Settings(database_url=database_url, poll_interval_s=0.02))
    try:
        assert restored.matrix_service.get_profile(created.id).name == "Persistent profile"
    finally:
        restored.database.dispose()


def test_active_state_persists_across_context_restart(
    context: ApplicationContext,
    database_url: str,
) -> None:
    context.matrix_service.apply_profile(INITIAL_PROFILE_ID)
    context.database.dispose()

    restored = create_context(Settings(database_url=database_url, poll_interval_s=0.02))
    try:
        active = restored.matrix_service.active()
        assert active.profile_id == INITIAL_PROFILE_ID
        assert len(active.active_connections) == 2
    finally:
        restored.database.dispose()
