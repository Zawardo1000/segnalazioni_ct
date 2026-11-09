"""Cancellazione delle prove vecchie (contengono le posizioni di chi prova)."""

import time
from datetime import timedelta

from fastapi.testclient import TestClient
from geoalchemy2 import WKTElement
from sqlalchemy import func, select

from segnalazioni.app import create_app
from segnalazioni.db import Database, utcnow
from segnalazioni.manutenzione import cancella_prove_vecchie
from segnalazioni.models import Prova
from tests.conftest import impostazioni


def _prova(giorni_fa: int) -> Prova:
    return Prova(creata=utcnow() - timedelta(days=giorni_fa), lat=37.5, lon=15.0, origine="gps",
                 punto=WKTElement("POINT(15.0 37.5)", srid=4326), esito_calcolo="deciso", risultato={})


def _conta(db: Database) -> int:
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(Prova))


def test_cancella_solo_le_prove_vecchie(database_url):
    db = Database(database_url)
    db.migrate()
    with db.session() as s:
        s.add_all([_prova(400), _prova(366), _prova(10)])
        s.commit()
        assert cancella_prove_vecchie(s, 0) == 0  # 0 = mai
        assert cancella_prove_vecchie(s, 365) == 2
    assert _conta(db) == 1
    db.dispose()


def test_l_app_cancella_da_sola_all_avvio(database_url, tmp_path):
    db = Database(database_url)
    db.migrate()
    with db.session() as s:
        s.add_all([_prova(400), _prova(1)])
        s.commit()
    with TestClient(create_app(impostazioni(database_url, tmp_path, conservazione_prove_giorni=365))) as app:
        assert app.get("/health").status_code == 200
        for _ in range(50):  # il primo giro parte all'avvio, in un thread
            if _conta(db) == 1:
                break
            time.sleep(0.1)
    assert _conta(db) == 1  # e lo spegnimento dell'app non si blocca sul compito periodico
    db.dispose()
