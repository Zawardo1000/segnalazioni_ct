"""I test usano un Postgres con PostGIS vero (come il server): TEST_DATABASE_URL, impostata da compose.yaml.

Ogni test parte da un database vuoto (schemi public, osm e osm_import ricreati). Il database di prova si crea da solo
la prima volta. Si lanciano con: docker compose run --rm test
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from segnalazioni.app import create_app
from segnalazioni.config import Settings

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
CHIAVE = "chiave-di-prova"


@pytest.fixture(scope="session")
def _test_database() -> str:
    if not TEST_DATABASE_URL:
        raise RuntimeError("TEST_DATABASE_URL mancante: i test usano Postgres, lanciali con docker compose run --rm test")
    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        if not c.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}):
            c.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    return TEST_DATABASE_URL


@pytest.fixture
def database_url(_test_database) -> str:
    """URL di un database vuoto, senza tabelle, migrazioni né stradario."""
    engine = create_engine(_test_database, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS osm CASCADE"))
        c.execute(text("DROP SCHEMA IF EXISTS osm_import CASCADE"))
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return _test_database


def impostazioni(database_url: str, tmp_path, **kw) -> Settings:
    return Settings(database_url=database_url, chiave_accesso=CHIAVE, data_dir=tmp_path / "data",
                    log_dir=tmp_path / "logs", _env_file=None, **kw)


@pytest.fixture
def app(database_url, tmp_path):
    with TestClient(create_app(impostazioni(database_url, tmp_path))) as client:
        yield client


@pytest.fixture
def con_chiave() -> dict:
    return {"X-Chiave": CHIAVE}
