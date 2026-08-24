from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from openbench.storage.models import Base


class Database:
    def __init__(self, url: str) -> None:
        engine_kwargs: dict[str, object] = {}
        if url.startswith("sqlite"):
            engine_kwargs["connect_args"] = {
                "check_same_thread": False,
                "timeout": 30.0,
            }
            if url.startswith("sqlite:///") and url not in {"sqlite://", "sqlite:///:memory:"}:
                database_path = Path(url.removeprefix("sqlite:///"))
                database_path.parent.mkdir(parents=True, exist_ok=True)
        if url in {"sqlite://", "sqlite:///:memory:"}:
            engine_kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **engine_kwargs)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite)
        self._session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )
        self._transaction_lock = RLock()

    @staticmethod
    def _configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        # OpenBench writes from request handlers and background polling
        # threads. SQLite only permits one writer, so serialize these short
        # in-process transactions instead of racing for its database lock.
        with self._transaction_lock:
            with self._session_factory.begin() as session:
                yield session

    def dispose(self) -> None:
        self.engine.dispose()
