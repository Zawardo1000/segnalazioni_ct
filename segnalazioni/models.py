"""Tabelle gestite dalle migrazioni Alembic (schema public).

Lo stradario OSM sta invece nello schema "osm", che crea e sostituisce per intero l'importazione (importa_osm.py):
Alembic non lo vede.
"""

from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import BigInteger, CheckConstraint, Double, ForeignKey, Index, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

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


# --- segnalazioni ------------------------------------------------------------------------------------------------

TIPI_SEGNALAZIONE = ("carcassa", "rifiuto")
# nuova → presa_in_carico → chiusa; da_smistare: senza ente (ente non attivo nell'app, o troppi inoltri), la vede solo
# l'amministratore
STATI = ("nuova", "presa_in_carico", "chiusa", "da_smistare")
ESITI = ("rimossa", "non_trovata", "doppione")


class Ente(Base):
    """Un ente che riceve segnalazioni. codice: quello che l'abbinamento ricava dalla strada (enti.py), es. "anas",
    "provincia-ct", "comune-087004"; è anche il valore dell'attributo degli utenti del gestionale (ente:<codice>)."""

    __tablename__ = "enti"

    id: Mapped[int] = mapped_column(primary_key=True)
    codice: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    attivo: Mapped[bool] = mapped_column(nullable=False, default=True)
    creato: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Segnalazione(Base):
    __tablename__ = "segnalazioni"
    __table_args__ = (
        Index("ix_segnalazioni_punto", "punto", postgresql_using="gist"),
        Index("ix_segnalazioni_ente_stato", "ente_id", "stato"),
        Index("ix_segnalazioni_dispositivo", "dispositivo_hash"),
        CheckConstraint("tipo IN ('carcassa', 'rifiuto')", name="tipo"),
        CheckConstraint("tipo_iniziale IN ('carcassa', 'rifiuto')", name="tipo_iniziale"),
        CheckConstraint("stato IN ('nuova', 'presa_in_carico', 'chiusa', 'da_smistare')", name="stato"),
        CheckConstraint("esito IN ('rimossa', 'non_trovata', 'doppione')", name="esito"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # codice da mostrare (SCT-2026-000123): non basta per vedere la segnalazione, serve anche il codice privato
    codice: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    creata: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    aggiornata: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    # scelta di chi segnala; l'ente la può cambiare (resta quella iniziale, e il cambio va nello storico)
    tipo: Mapped[str] = mapped_column(String(10), nullable=False)
    tipo_iniziale: Mapped[str] = mapped_column(String(10), nullable=False)
    nota: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float] = mapped_column(Double, nullable=False)
    lon: Mapped[float] = mapped_column(Double, nullable=False)
    precisione_m: Mapped[float | None] = mapped_column(Double)
    origine: Mapped[str] = mapped_column(String(10), nullable=False)
    punto = mapped_column(Geography("POINT", srid=4326, spatial_index=False), nullable=False)
    comune: Mapped[str | None] = mapped_column(String(200))
    # strada su cui è la segnalazione (quella scelta, o la principale dell'abbinamento)
    osm_way: Mapped[int | None] = mapped_column(BigInteger)
    strada: Mapped[str | None] = mapped_column(String(300))
    # abbinamento calcolato dal server all'invio (senza le geometrie): perché è andata a quell'ente
    abbinamento: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ente_calcolato: Mapped[str | None] = mapped_column(String(40))  # codice dell'ente dell'abbinamento
    ente_id: Mapped[int | None] = mapped_column(ForeignKey("enti.id"))
    stato: Mapped[str] = mapped_column(String(20), nullable=False)
    esito: Mapped[str | None] = mapped_column(String(20))
    chiusa_il: Mapped[datetime | None] = mapped_column(UTCDateTime)
    inoltri: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    foto: Mapped[str | None] = mapped_column(String(64))  # nome del file in FOTO_DIR
    # impronte SHA-256 del codice privato (link della segnalazione) e della chiave del dispositivo: mai i valori
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dispositivo_hash: Mapped[str | None] = mapped_column(String(64))
    email_inviate: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # quante, mai a chi
    # analisi automatica della foto (prevista, non ancora fatta): tipo proposto, zone da oscurare, stato
    analisi: Mapped[dict | None] = mapped_column(JSONB)

    ente: Mapped[Ente | None] = relationship()


class Evento(Base):
    """Storico di una segnalazione: ogni azione, chi l'ha fatta e quando. Non si modifica e non si cancella."""

    __tablename__ = "eventi"
    __table_args__ = (Index("ix_eventi_segnalazione", "segnalazione_id", "quando"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    segnalazione_id: Mapped[int] = mapped_column(ForeignKey("segnalazioni.id"), nullable=False)
    quando: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    # creata, presa_in_carico, inoltrata, tipo_cambiato, chiusa, da_smistare, assegnata
    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    autore: Mapped[str] = mapped_column(String(200), nullable=False)  # "cittadino", "sistema" o l'utente del gestionale
    da_ente_id: Mapped[int | None] = mapped_column(ForeignKey("enti.id"))
    a_ente_id: Mapped[int | None] = mapped_column(ForeignKey("enti.id"))
    dettagli: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    visibile_cittadino: Mapped[bool] = mapped_column(nullable=False, default=True)

    da_ente: Mapped[Ente | None] = relationship(foreign_keys=[da_ente_id])
    a_ente: Mapped[Ente | None] = relationship(foreign_keys=[a_ente_id])


class Sessione(Base):
    """Sessione del gestionale dopo il login con Keycloak. Il cookie porta un codice casuale: qui solo l'impronta."""

    __tablename__ = "sessioni"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    creata: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    scade: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    sub: Mapped[str] = mapped_column(String(100), nullable=False)
    utente: Mapped[str] = mapped_column(String(200), nullable=False)
    ruolo: Mapped[str] = mapped_column(String(20), nullable=False)  # amministratore o gestore
    ente_id: Mapped[int | None] = mapped_column(ForeignKey("enti.id"))
    csrf: Mapped[str] = mapped_column(String(64), nullable=False)

    ente: Mapped[Ente | None] = relationship()


class LoginInCorso(Base):
    """Un login partito verso Keycloak e non ancora tornato: state, nonce e verificatore PKCE (validi 10 minuti)."""

    __tablename__ = "login_in_corso"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    creato: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    verificatore: Mapped[str] = mapped_column(String(128), nullable=False)
