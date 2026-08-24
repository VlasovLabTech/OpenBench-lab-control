from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from openbench.domain import (
    Channel,
    Device,
    MatrixConnection,
    MatrixPort,
    MatrixProfile,
    Measurement,
)
from openbench.storage.models import (
    ChannelModel,
    DeviceModel,
    InstrumentPreferenceModel,
    MatrixPortModel,
    MatrixProfileModel,
    MeasurementModel,
)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class DeviceRepository:
    @staticmethod
    def upsert(session: Session, device: Device, channels: tuple[Channel, ...]) -> None:
        model = session.get(DeviceModel, device.id)
        if model is None:
            model = DeviceModel(id=device.id)
            session.add(model)
        model.name = device.name
        model.kind = device.kind
        model.connected = device.connected
        model.capabilities_json = json.dumps(device.capabilities)
        for channel in channels:
            channel_model = session.get(ChannelModel, channel.id)
            if channel_model is None:
                channel_model = ChannelModel(id=channel.id, device_id=device.id)
                session.add(channel_model)
            channel_model.name = channel.name
            channel_model.capability = channel.capability
            channel_model.unit = channel.unit
            channel_model.poll_interval_s = channel.poll_interval_s
            channel_model.state = channel.state

    @staticmethod
    def list_devices(session: Session) -> tuple[Device, ...]:
        models = session.scalars(select(DeviceModel).order_by(DeviceModel.id)).all()
        return tuple(
            Device(
                id=model.id,
                name=model.name,
                kind=model.kind,
                connected=model.connected,
                capabilities=tuple(json.loads(model.capabilities_json)),
            )
            for model in models
        )

    @staticmethod
    def list_channels(session: Session) -> tuple[Channel, ...]:
        models = session.scalars(select(ChannelModel).order_by(ChannelModel.id)).all()
        return tuple(
            Channel(
                id=model.id,
                device_id=model.device_id,
                name=model.name,
                capability=model.capability,
                unit=model.unit,
                poll_interval_s=model.poll_interval_s,
                state=model.state,
            )
            for model in models
        )


class MeasurementRepository:
    @staticmethod
    def add(session: Session, measurement: Measurement) -> None:
        session.add(
            MeasurementModel(
                timestamp_utc=measurement.timestamp_utc,
                monotonic_s=measurement.monotonic_s,
                device_id=measurement.device_id,
                channel_id=measurement.channel_id,
                value=measurement.value,
                unit=measurement.unit,
                quality=measurement.quality,
                status=measurement.status,
            )
        )

    @staticmethod
    def latest(session: Session, channel_id: str) -> Measurement | None:
        statement = (
            select(MeasurementModel)
            .where(MeasurementModel.channel_id == channel_id)
            .order_by(MeasurementModel.id.desc())
            .limit(1)
        )
        model = session.scalar(statement)
        if model is None:
            return None
        timestamp = _aware(model.timestamp_utc)
        assert hasattr(timestamp, "tzinfo")
        return Measurement(
            timestamp_utc=timestamp,
            monotonic_s=model.monotonic_s,
            device_id=model.device_id,
            channel_id=model.channel_id,
            value=model.value,
            unit=model.unit,
            quality=model.quality,
            status=model.status,
        )


class InstrumentPreferenceRepository:
    @staticmethod
    def get(session: Session, device_id: str) -> dict[str, object]:
        model = session.get(InstrumentPreferenceModel, device_id)
        if model is None:
            return {}
        try:
            value = json.loads(model.preferences_json)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def put(
        session: Session,
        device_id: str,
        preferences: dict[str, object],
    ) -> None:
        model = session.get(InstrumentPreferenceModel, device_id)
        if model is None:
            model = InstrumentPreferenceModel(device_id=device_id)
            session.add(model)
        model.preferences_json = json.dumps(
            preferences,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        model.updated_at = datetime.now(UTC)


class MatrixRepository:
    @staticmethod
    def list_ports(session: Session) -> tuple[MatrixPort, ...]:
        models = session.scalars(select(MatrixPortModel).order_by(MatrixPortModel.id)).all()
        return tuple(MatrixPort(id=m.id, name=m.name, type=m.type) for m in models)

    @staticmethod
    def list_profiles(session: Session) -> tuple[MatrixProfile, ...]:
        statement = (
            select(MatrixProfileModel)
            .options(selectinload(MatrixProfileModel.connections))
            .order_by(MatrixProfileModel.name)
        )
        return tuple(MatrixRepository.to_domain(model) for model in session.scalars(statement))

    @staticmethod
    def get_profile(session: Session, profile_id: str) -> MatrixProfileModel | None:
        statement = (
            select(MatrixProfileModel)
            .options(selectinload(MatrixProfileModel.connections))
            .where(MatrixProfileModel.id == profile_id)
        )
        return session.scalar(statement)

    @staticmethod
    def get_profile_by_name(session: Session, name: str) -> MatrixProfileModel | None:
        statement = (
            select(MatrixProfileModel)
            .options(selectinload(MatrixProfileModel.connections))
            .where(MatrixProfileModel.name == name)
        )
        return session.scalar(statement)

    @staticmethod
    def to_domain(model: MatrixProfileModel) -> MatrixProfile:
        created = _aware(model.created_at)
        updated = _aware(model.updated_at)
        assert hasattr(created, "tzinfo")
        assert hasattr(updated, "tzinfo")
        return MatrixProfile(
            id=model.id,
            name=model.name,
            version=model.version,
            connections=tuple(
                MatrixConnection(from_port=item.from_port, to_port=item.to_port)
                for item in sorted(model.connections, key=lambda connection: connection.id)
            ),
            created_at=created,
            updated_at=updated,
        )
