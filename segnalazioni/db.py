import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, DateTime, Engine, MetaData, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

# Chiave del lock "advisory" di Postgres per le migrazioni (numero qualsiasi, purché fisso)
MIGRATE_LOCK = 0x7365676E616C01

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """timestamptz di Postgres: rifiuta le date senza fuso e le rilegge sempre in UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime senza fuso orario")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        return None if value is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    # vincoli con nome: le migrazioni li ritrovano per modificarli
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def make_engine(url: str) -> Engine:
    # Postgres è condiviso con altri servizi: poche connessioni, verificate prima dell'uso (riavvii del DB)
    return create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=3600)


class Database:
    def __init__(self, url: str):
        self.url = url
        self.engine = make_engine(url)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def _alembic(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        return cfg, ScriptDirectory.from_config(cfg)

    def schema_state(self, conn: Connection | None = None) -> str:
        """"aggiornato", "indietro" (mancano migrazioni) o "piu-nuovo" (schema di una versione più recente del codice:
        succede dopo un ritorno automatico alla versione precedente)."""
        from alembic.runtime.migration import MigrationContext

        _, script = self._alembic()
        known = {r.revision for r in script.walk_revisions()}
        if conn is None:
            with self.engine.connect() as c:
                return self.schema_state(c)
        current = set(MigrationContext.configure(conn).get_current_heads())
        if current - known:
            return "piu-nuovo"
        return "aggiornato" if current == set(script.get_heads()) else "indietro"

    def migrate(self) -> str:
        """Porta lo schema all'ultima migrazione Alembic (lo schema si crea solo così) e ne restituisce lo stato.

        Un lock advisory serializza le migrazioni. Se lo schema è più nuovo del codice non tocca niente e lo segnala:
        le migrazioni aggiungono e non tolgono, quindi il codice vecchio gira anche sullo schema nuovo.
        """
        from alembic import command

        cfg, _ = self._alembic()
        with self.engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MIGRATE_LOCK})
            conn.execute(text("SET LOCAL lock_timeout = '15s'"))  # mai in coda per sempre dietro un'altra sessione
            state = self.schema_state(conn)
            if state == "indietro":
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, "head")
                state = "aggiornato"
        if state == "piu-nuovo":
            log.warning("schema del database più nuovo del codice: nessuna migrazione applicata")
        return state

    def session(self) -> Session:
        return self.sessions()

    def dispose(self) -> None:
        self.engine.dispose()
