from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from openbench.api import create_app
from openbench.bootstrap import ApplicationContext, create_context
from openbench.config import Settings


@pytest.fixture
def database_url(tmp_path: pytest.TempPathFactory) -> str:
    return f"sqlite:///{tmp_path / 'openbench-test.db'}"


@pytest.fixture
def context(
    database_url: str,
    tmp_path: pytest.TempPathFactory,
) -> Iterator[ApplicationContext]:
    value = create_context(
        Settings(
            database_url=database_url,
            capture_directory=str(tmp_path / "captures"),
            poll_interval_s=0.02,
            ut61e_enabled=False,
            ut61eplus_enabled=False,
            ut197_enabled=False,
            feeltech_enabled=False,
            dps150_enabled=False,
            owon_spm_enabled=False,
            micsig_enabled=False,
        )
    )
    yield value
    value.database.dispose()


@pytest.fixture
def client(
    database_url: str,
    tmp_path: pytest.TempPathFactory,
) -> Iterator[TestClient]:
    application = create_app(
        Settings(
            database_url=database_url,
            capture_directory=str(tmp_path / "captures"),
            poll_interval_s=0.02,
            ut61e_enabled=False,
            ut61eplus_enabled=False,
            ut197_enabled=False,
            feeltech_enabled=False,
            dps150_enabled=False,
            owon_spm_enabled=False,
            micsig_enabled=False,
        )
    )
    with TestClient(application) as test_client:
        yield test_client
