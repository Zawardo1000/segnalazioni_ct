"""Ambiente Alembic. Il database viene da:
1. una connessione passata da Database.migrate() in config.attributes["connection"];
2. altrimenti da DATABASE_URL, come fa l'app (per `alembic revision --autogenerate`, vedi CLAUDE.md).

Alembic guarda solo lo schema public: lo stradario (schema "osm") lo gestisce l'importazione. Di PostGIS si ignora la
tabella spatial_ref_sys, che crea l'estensione.
"""

from alembic import context
from geoalchemy2 import alembic_helpers

from segnalazioni import models  # noqa: F401  registra le tabelle
from segnalazioni.config import Settings
from segnalazioni.db import Base, make_engine

config = context.config
target_metadata = Base.metadata

POSTGIS_TABLES = {"spatial_ref_sys"}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in POSTGIS_TABLES:
        return False
    return alembic_helpers.include_object(obj, name, type_, reflected, compare_to)


def _configure(**kw) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
        render_item=alembic_helpers.render_item,
        **kw,
    )


def _database_url() -> str:
    # niente check_required: per le migrazioni da riga di comando la chiave di accesso non serve
    return Settings().database_url


def run_migrations_offline() -> None:
    _configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(_database_url())
    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
