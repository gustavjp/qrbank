from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.getenv("QRBANK_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("QRBANK_DATABASE_URL or DATABASE_URL is required for PostgreSQL migration tests")
    return url


@pytest.fixture(scope="session")
def engine(database_url: str):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")
    eng = create_engine(database_url, future=True)
    try:
        yield eng
    finally:
        eng.dispose()
