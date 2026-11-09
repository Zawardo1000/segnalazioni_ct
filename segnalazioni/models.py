"""Tabelle gestite dalle migrazioni Alembic (schema public).

Lo stradario OSM sta invece nello schema "osm", che crea e sostituisce per intero l'importazione (importa_osm.py):
Alembic non lo vede.
"""

from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import BigInteger, CheckConstraint, Double, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from segnalazioni.db import Base, UTCDateTime, utcnow


class Prova(Base):
    """Una prova di abbinamento fatta con la pagina di prova: punto, risultato calcolato, esito dato dall'utente."""

    __tablename__ = "prove"
    __table_args__ = (
        Index("ix_prove_punto", "punto", postgresql_using="gist"),
        Index("ix_prove_creata", "creata"),
        CheckConstraint("esito IN ('giusto', 'sbagliato')", name="esito"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    creata: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    lat: Mapped[float] = mapped_column(Double, nullable=False)
    lon: Mapped[float] = mapped_column(Double, nullable=False)
    precisione_m: Mapped[float | None] = mapped_column(Double)
    # "gps" (posizione del telefono) o "mappa" (punto scelto a mano)
    origine: Mapped[str] = mapped_column(String(10), nullable=False)
    punto = mapped_column(Geography("POINT", srid=4326, spatial_index=False), nullable=False)
    # esito del calcolo: deciso, da_scegliere, nessuna_strada, fuori_area
    esito_calcolo: Mapped[str] = mapped_column(String(20), nullable=False)
    ente_calcolato: Mapped[str | None] = mapped_column(String(200))
    risultato: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # dati letti dallo stradario per questa prova (SALVA_FONTI): bastano per rifare il calcolo in un test
    fonti: Mapped[dict | None] = mapped_column(JSONB)
    # codice casuale dato a chi ha fatto la prova: serve per salvarne l'esito (gli id sono progressivi e la chiave di
    # accesso è condivisa: senza, un tester potrebbe sovrascrivere le risposte degli altri)
    token_esito: Mapped[str | None] = mapped_column(String(32))
    # risposta dell'utente
    esito: Mapped[str | None] = mapped_column(String(10))
    osm_way_scelto: Mapped[int | None] = mapped_column(BigInteger)
    ente_corretto: Mapped[str | None] = mapped_column(String(200))
    nota: Mapped[str | None] = mapped_column(Text)
    esito_il: Mapped[datetime | None] = mapped_column(UTCDateTime)
