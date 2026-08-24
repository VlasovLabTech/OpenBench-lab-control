from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openbench.core.capabilities import MatrixCapability
from openbench.core.safety import EMERGENCY_STOP, SAFE
from openbench.domain import (
    MatrixApplyResult,
    MatrixConnection,
    MatrixPort,
    MatrixProfile,
    SafetyState,
)
from openbench.storage import Database
from openbench.storage.models import (
    AuditLogModel,
    MatrixConnectionModel,
    MatrixPortModel,
    MatrixProfileModel,
    MatrixStateModel,
    SafetyStateModel,
)
from openbench.storage.repositories import MatrixRepository

INITIAL_PROFILE_ID = "00000000-0000-4000-8000-000000000001"


class MatrixValidationError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class MatrixNotFoundError(LookupError):
    pass


class MatrixConflictError(RuntimeError):
    pass


class SafetyInterlockError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _serialize_connections(connections: Sequence[MatrixConnection]) -> str:
    return json.dumps(
        [{"from_port": item.from_port, "to_port": item.to_port} for item in connections]
    )


def _deserialize_connections(value: str) -> tuple[MatrixConnection, ...]:
    data = json.loads(value)
    return tuple(
        MatrixConnection(from_port=item["from_port"], to_port=item["to_port"]) for item in data
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class MatrixService:
    def __init__(self, database: Database, driver: MatrixCapability) -> None:
        self._database = database
        self._driver = driver
        self._lock = threading.RLock()

    def initialize(self) -> None:
        now = _now()
        with self._database.transaction() as session:
            for port in self._driver.list_ports():
                if session.get(MatrixPortModel, port.id) is None:
                    session.add(MatrixPortModel(id=port.id, name=port.name, type=port.type))
            state = session.get(MatrixStateModel, 1)
            if state is None:
                state = MatrixStateModel(
                    id=1,
                    active_profile_id=None,
                    active_profile_name=None,
                    connections_json="[]",
                    updated_at=now,
                )
                session.add(state)
            safety = session.get(SafetyStateModel, 1)
            if safety is None:
                session.add(SafetyStateModel(id=1, state=SAFE, reason=None, updated_at=now))
            if session.get(MatrixProfileModel, INITIAL_PROFILE_ID) is None:
                initial = MatrixProfileModel(
                    id=INITIAL_PROFILE_ID,
                    name="Basic output voltage measurement",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                initial.connections = [
                    MatrixConnectionModel(
                        from_port="dut.vout",
                        to_port="meter.voltage_hi",
                    ),
                    MatrixConnectionModel(
                        from_port="dut.gnd",
                        to_port="meter.voltage_lo",
                    ),
                ]
                session.add(initial)
        self._restore_driver_state()

    def _restore_driver_state(self) -> None:
        with self._database.session() as session:
            state = session.get(MatrixStateModel, 1)
            connections = (
                _deserialize_connections(state.connections_json) if state is not None else ()
            )
        self._driver.open_all()
        if connections:
            self._driver.apply_connections(connections)

    def list_ports(self) -> tuple[MatrixPort, ...]:
        with self._database.session() as session:
            return MatrixRepository.list_ports(session)

    def list_profiles(self) -> tuple[MatrixProfile, ...]:
        with self._database.session() as session:
            return MatrixRepository.list_profiles(session)

    def get_profile(self, profile_id_or_name: str) -> MatrixProfile:
        with self._database.session() as session:
            model = MatrixRepository.get_profile(session, profile_id_or_name)
            if model is None:
                model = MatrixRepository.get_profile_by_name(session, profile_id_or_name)
            if model is None:
                raise MatrixNotFoundError(f"Unknown matrix profile: {profile_id_or_name}")
            return MatrixRepository.to_domain(model)

    def create_profile(
        self,
        name: str,
        connections: Sequence[MatrixConnection],
    ) -> MatrixProfile:
        clean_name = name.strip()
        if not clean_name:
            raise MatrixValidationError(("Profile name must not be empty",))
        errors = self.validate_connections(connections)
        if errors:
            raise MatrixValidationError(errors)
        now = _now()
        model = MatrixProfileModel(
            id=str(uuid.uuid4()),
            name=clean_name,
            version=1,
            created_at=now,
            updated_at=now,
            connections=[
                MatrixConnectionModel(from_port=item.from_port, to_port=item.to_port)
                for item in connections
            ],
        )
        try:
            with self._database.transaction() as session:
                session.add(model)
        except IntegrityError as exc:
            raise MatrixConflictError(f"Profile name already exists: {clean_name}") from exc
        return self.get_profile(model.id)

    def update_profile(
        self,
        profile_id: str,
        name: str,
        connections: Sequence[MatrixConnection],
    ) -> MatrixProfile:
        clean_name = name.strip()
        if not clean_name:
            raise MatrixValidationError(("Profile name must not be empty",))
        errors = self.validate_connections(connections)
        if errors:
            raise MatrixValidationError(errors)
        try:
            with self._database.transaction() as session:
                model = MatrixRepository.get_profile(session, profile_id)
                if model is None:
                    raise MatrixNotFoundError(f"Unknown matrix profile: {profile_id}")
                model.name = clean_name
                model.version += 1
                model.updated_at = _now()
                model.connections.clear()
                model.connections.extend(
                    MatrixConnectionModel(from_port=item.from_port, to_port=item.to_port)
                    for item in connections
                )
        except IntegrityError as exc:
            raise MatrixConflictError(f"Profile name already exists: {clean_name}") from exc
        return self.get_profile(profile_id)

    def delete_profile(self, profile_id: str) -> None:
        with self._database.transaction() as session:
            state = self._state_model(session)
            if state.active_profile_id == profile_id:
                raise MatrixConflictError("Cannot delete the active matrix profile")
            model = session.get(MatrixProfileModel, profile_id)
            if model is None:
                raise MatrixNotFoundError(f"Unknown matrix profile: {profile_id}")
            session.delete(model)

    def validate_connections(
        self,
        connections: Sequence[MatrixConnection],
    ) -> tuple[str, ...]:
        ports = {port.id: port for port in self.list_ports()}
        errors: list[str] = []
        seen: set[tuple[str, str]] = set()
        for index, connection in enumerate(connections, start=1):
            prefix = f"Connection {index}"
            if connection.from_port == connection.to_port:
                errors.append(f"{prefix}: a port cannot be connected to itself")
            unknown = [
                port_id
                for port_id in (connection.from_port, connection.to_port)
                if port_id not in ports
            ]
            if unknown:
                errors.append(f"{prefix}: unknown port(s): {', '.join(unknown)}")
            normalized = connection.normalized
            if normalized in seen:
                errors.append(f"{prefix}: duplicate connection")
            seen.add(normalized)
            if not unknown:
                first = ports[connection.from_port]
                second = ports[connection.to_port]
                if first.type == "source" and second.type == "source":
                    errors.append(f"{prefix}: source-to-source connection is forbidden")
        return tuple(errors)

    def validate_profile(self, profile_id: str) -> tuple[str, ...]:
        profile = self.get_profile(profile_id)
        return self.validate_connections(profile.connections)

    def apply_profile(self, profile_id_or_name: str) -> MatrixApplyResult:
        previous: tuple[MatrixConnection, ...] = ()
        driver_changed = False
        with self._lock:
            try:
                with self._database.transaction() as session:
                    model = MatrixRepository.get_profile(session, profile_id_or_name)
                    if model is None:
                        model = MatrixRepository.get_profile_by_name(session, profile_id_or_name)
                    if model is None:
                        raise MatrixNotFoundError(f"Unknown matrix profile: {profile_id_or_name}")
                    profile = MatrixRepository.to_domain(model)
                    errors = self._validate_connections_in_session(
                        session,
                        profile.connections,
                    )
                    if errors:
                        raise MatrixValidationError(errors)
                    safety = self._safety_model(session)
                    if safety.state != SAFE:
                        raise SafetyInterlockError(
                            "Matrix apply is blocked while emergency stop is active"
                        )
                    state = self._state_model(session)
                    previous = _deserialize_connections(state.connections_json)

                    # The validated target is applied break-before-make.
                    self._driver.open_all()
                    driver_changed = True
                    self._driver.apply_connections(profile.connections)

                    state.active_profile_id = profile.id
                    state.active_profile_name = profile.name
                    state.connections_json = _serialize_connections(profile.connections)
                    state.updated_at = _now()
                    self._audit(
                        session,
                        "matrix_profile_applied",
                        {"profile_id": profile.id, "profile_name": profile.name},
                    )
            except Exception:
                if driver_changed:
                    self._driver.open_all()
                    if previous:
                        self._driver.apply_connections(previous)
                raise
        return MatrixApplyResult(
            success=True,
            profile_id=profile.id,
            profile_name=profile.name,
            active_connections=profile.connections,
            message="Profile validated and applied using break-before-make",
        )

    def open_all(self, reason: str = "manual") -> MatrixApplyResult:
        with self._lock:
            self._driver.open_all()
            with self._database.transaction() as session:
                state = self._state_model(session)
                state.active_profile_id = None
                state.active_profile_name = None
                state.connections_json = "[]"
                state.updated_at = _now()
                self._audit(session, "matrix_open_all", {"reason": reason})
        return MatrixApplyResult(
            success=True,
            profile_id=None,
            profile_name=None,
            active_connections=(),
            message="All matrix routes are open",
        )

    def active(self) -> MatrixApplyResult:
        with self._database.session() as session:
            state = self._state_model(session)
            return MatrixApplyResult(
                success=True,
                profile_id=state.active_profile_id,
                profile_name=state.active_profile_name,
                active_connections=_deserialize_connections(state.connections_json),
                message=(
                    "Matrix profile active"
                    if state.active_profile_id is not None
                    else "All matrix routes are open"
                ),
            )

    def emergency_stop(self, reason: str = "operator request") -> MatrixApplyResult:
        with self._lock:
            self._driver.open_all()
            with self._database.transaction() as session:
                state = self._state_model(session)
                state.active_profile_id = None
                state.active_profile_name = None
                state.connections_json = "[]"
                state.updated_at = _now()
                safety = self._safety_model(session)
                safety.state = EMERGENCY_STOP
                safety.reason = reason
                safety.updated_at = _now()
                self._audit(session, "emergency_stop", {"reason": reason})
        return MatrixApplyResult(
            success=True,
            profile_id=None,
            profile_name=None,
            active_connections=(),
            message="Emergency stop active; all matrix routes are open",
        )

    def safety_state(self) -> SafetyState:
        with self._database.session() as session:
            model = self._safety_model(session)
            return SafetyState(
                state=model.state,
                reason=model.reason,
                updated_at=_aware(model.updated_at),
            )

    def reset_simulated_safety(self) -> SafetyState:
        with self._database.transaction() as session:
            model = self._safety_model(session)
            model.state = SAFE
            model.reason = None
            model.updated_at = _now()
            self._audit(
                session,
                "simulation_safety_reset",
                {"non_production": True},
            )
        return self.safety_state()

    def _validate_connections_in_session(
        self,
        session: Session,
        connections: Sequence[MatrixConnection],
    ) -> tuple[str, ...]:
        models = session.scalars(select(MatrixPortModel)).all()
        ports = {
            model.id: MatrixPort(id=model.id, name=model.name, type=model.type) for model in models
        }
        errors: list[str] = []
        seen: set[tuple[str, str]] = set()
        for index, connection in enumerate(connections, start=1):
            prefix = f"Connection {index}"
            if connection.from_port == connection.to_port:
                errors.append(f"{prefix}: a port cannot be connected to itself")
            unknown = [
                item for item in (connection.from_port, connection.to_port) if item not in ports
            ]
            if unknown:
                errors.append(f"{prefix}: unknown port(s): {', '.join(unknown)}")
            if connection.normalized in seen:
                errors.append(f"{prefix}: duplicate connection")
            seen.add(connection.normalized)
            if not unknown:
                if (
                    ports[connection.from_port].type == "source"
                    and ports[connection.to_port].type == "source"
                ):
                    errors.append(f"{prefix}: source-to-source connection is forbidden")
        return tuple(errors)

    @staticmethod
    def _state_model(session: Session) -> MatrixStateModel:
        model = session.get(MatrixStateModel, 1)
        if model is None:
            raise RuntimeError("Matrix service has not been initialized")
        return model

    @staticmethod
    def _safety_model(session: Session) -> SafetyStateModel:
        model = session.get(SafetyStateModel, 1)
        if model is None:
            raise RuntimeError("Safety service has not been initialized")
        return model

    @staticmethod
    def _audit(session: Session, action: str, details: dict[str, object]) -> None:
        session.add(
            AuditLogModel(
                timestamp_utc=_now(),
                action=action,
                details=json.dumps(details, ensure_ascii=False),
            )
        )
