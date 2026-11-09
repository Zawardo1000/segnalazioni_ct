"""Importazione vera con osmium e osm2pgsql (pacchetti dell'immagine) su un piccolo file OSM, senza rete."""

import pytest
from sqlalchemy import text

from segnalazioni import abbinamento, importa_osm, stradario
from segnalazioni.config import AreaOSM
from segnalazioni.db import Database
from tests.conftest import impostazioni
from tests.helpers import MINI_OSM


@pytest.fixture
def db(database_url):
    d = Database(database_url)
    d.migrate()  # PostGIS
    yield d
    d.dispose()


def _settings(database_url, tmp_path):
    area = AreaOSM(nome="Prova", url="https://example.invalid/prova.osm.pbf", bbox=(15.13, 37.56, 15.19, 37.60))
    return impostazioni(database_url, tmp_path, osm_aree=[area], osm2pgsql_cache_mb=50)


def test_importa_ritaglia_e_si_legge(db, database_url, tmp_path):
    r = importa_osm.esegui(_settings(database_url, tmp_path), file=MINI_OSM)
    assert (r["vie"], r["comuni"]) == (3, 2)  # la strada in costruzione resta fuori
    with db.session() as s:
        st = stradario.stato(s)
        intorno = stradario.leggi_intorno(s, 37.58, 15.1686, 100)
        middle = s.scalar(text("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'planet_osm%'"))
    assert st["aree"] == "Prova" and st["bbox"][0]["nome"] == "Prova"
    assert middle == 0  # --drop: niente tabelle intermedie
    assert intorno["comune"]["nome"] == "Acireale"
    assert intorno["provincia"]["tags"]["ref"] == "CT"
    assert intorno["regione"]["nome"] == "Sicilia"
    assert intorno["cartelli"][0]["nome"] == "Capomulini" and intorno["cartelli"][0]["distanza_m"] < 70
    assert intorno["localita"][0]["tags"]["population"] == "1500"
    risultato = abbinamento.abbina(37.58, 15.1686, 5, intorno)
    assert risultato["principale"] == 200
    assert risultato["ente"]["nome"] == "ANAS – Struttura territoriale Sicilia"
    # limite 50: possibile traversa, e l'app indica la frazione e il cartello più vicini
    assert risultato["localita"][0]["popolazione"] == 1500
    assert any("«Capomulini»" in d and "1.500 abitanti" in d for d in risultato["dubbi"])
    # ciclabile del percorso con gestore: la Città metropolitana, non il Comune
    with db.session() as s:
        vicino = stradario.leggi_intorno(s, 37.587, 15.1761, 100)
    ciclabile = next(v for v in vicino["vie"] if v["osm_way"] == 203)
    assert ciclabile["percorsi"][0]["nome"] == "Percorso di prova"
    r2 = abbinamento.abbina(37.587, 15.1761, 5, vicino)
    assert r2["ente"]["nome"] == "Città metropolitana di Catania"
    # ritagli e file intermedi cancellati, il file indicato a mano resta
    assert not list((tmp_path / "data" / "osm").glob("*.pbf"))
    assert MINI_OSM.exists()


def test_nuova_importazione_sostituisce_la_vecchia(db, database_url, tmp_path):
    s = _settings(database_url, tmp_path)
    importa_osm.esegui(s, file=MINI_OSM, ritaglio=False)
    importa_osm.esegui(s, file=MINI_OSM, ritaglio=False)
    with db.engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM osm.vie")) == 3
        assert c.scalar(text("SELECT count(*) FROM osm.importazione")) == 1
        assert c.scalar(text("SELECT to_regnamespace('osm_import')")) is None


def test_importazione_vuota_lascia_lo_stradario_di_prima(db, database_url, tmp_path):
    s = _settings(database_url, tmp_path)
    importa_osm.esegui(s, file=MINI_OSM, ritaglio=False)
    vuoto = tmp_path / "vuoto.osm"
    vuoto.write_text('<?xml version="1.0"?><osm version="0.6"></osm>', encoding="utf-8")
    with pytest.raises(RuntimeError, match="importazione vuota"):
        importa_osm.esegui(s, file=vuoto, ritaglio=False)
    with db.engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM osm.vie")) == 3
