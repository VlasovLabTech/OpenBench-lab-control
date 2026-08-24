from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class DeviceModel(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(80))
    connected: Mapped[bool] = mapped_column(Boolean, default=True)
    capabilities_json: Mapped[str] = mapped_column(Text)


class ChannelModel(Base):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    capability: Mapped[str] = mapped_column(String(80))
    unit: Mapped[str] = mapped_column(String(32))
    poll_interval_s: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(32), default="ready")


class MeasurementModel(Base):
    __tablename__ = "measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    monotonic_s: Mapped[float] = mapped_column(Float)
    device_id: Mapped[str] = mapped_column(String(120), index=True)
    channel_id: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(32))
    quality: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))


class InstrumentPreferenceModel(Base):
    __tablename__ = "instrument_preferences"

    # Intentionally not a foreign key: disconnecting a device must not erase
    # the preferences that should be restored when the same stable ID returns.
    device_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    preferences_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MatrixPortModel(Base):
    __tablename__ = "matrix_ports"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(40), index=True)


class MatrixProfileModel(Base):
    __tablename__ = "matrix_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    connections: Mapped[list[MatrixConnectionModel]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class MatrixConnectionModel(Base):
    __tablename__ = "matrix_connections"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "from_port",
            "to_port",
            name="uq_profile_connection_direction",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("matrix_profiles.id", ondelete="CASCADE"), index=True
    )
    from_port: Mapped[str] = mapped_column(String(120))
    to_port: Mapped[str] = mapped_column(String(120))
    profile: Mapped[MatrixProfileModel] = relationship(back_populates="connections")


class MatrixStateModel(Base):
    __tablename__ = "matrix_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    active_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    active_profile_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    connections_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SafetyStateModel(Base):
    __tablename__ = "safety_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    state: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLogModel(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    details: Mapped[str] = mapped_column(Text)
