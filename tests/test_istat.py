"""Sezioni Istat e centri abitati: lettura dei file, importazione con lo stradario di prova, regola dei 10.000 abitanti.

Dati inventati attorno a tests/fixtures/mini.osm: il centro Istat "Acireale" (15.400 abitanti) contiene la località OSM
Acireale (sezioni da 12.000 e 3.000 abitanti) e la frazione Capomulini (400, più la striscia della statale senza
abitanti), che i due cartelli "Capomulini" delimitano a parte.
"""

import zipfile

import pytest
from sqlalchemy import text

from segnalazioni import abbinamento, importa_osm, istat, stradario
from segnalazioni.config import AreaOSM
from segnalazioni.db import Database
from tests.conftest import impostazioni
from tests.helpers import MINI_OSM, scrivi_zip_istat

ACIREALE = 8700410001
NUCLEO = 8700420001


def _sezioni() -> list[dict]:
    base = {"regione": 19, "pro_com": 87004}
    return [
        {**base, "sez_id": 870040000001, "tipo_loc": 1, "loc_id": ACIREALE, "pop": 12000,
         "rettangolo": (15.172, 37.586, 15.179, 37.5925)},
        {**base, "sez_id": 870040000002, "tipo_loc": 1, "loc_id": ACIREALE, "pop": 3000,
         "rettangolo": (15.172, 37.582, 15.179, 37.586)},
        {**base, "sez_id": 870040000003, "tipo_loc": 1, "loc_id": ACIREALE, "pop": 400,
         "rettangolo": (15.1689, 37.578, 15.172, 37.583)},
        # la striscia della statale: nessun abitante, va con la sezione abitata accanto (Capomulini)
        {**base, "sez_id": 870040000004, "tipo_loc": 1, "loc_id": ACIREALE, "pop": 0,
         "rettangolo": (15.168, 37.574, 15.1689, 37.586)},
        {**base, "sez_id": 870040000005, "tipo_loc": 2, "loc_id": NUCLEO, "pop": 50,
         "rettangolo": (15.161, 37.571, 15.163, 37.573)},
        # case sparse: contano negli abitanti della regione, ma la forma non serve
        {**base, "sez_id": 870040000006, "tipo_loc": 4, "loc_id": 0, "pop": 100,
         "rettangolo": (15.163, 37.574, 15.166, 37.577)},
    ]


@pytest.fixture
def db(database_url):
    d = Database(database_url)
    d.migrate()  # PostGIS
    yield d
    d.dispose()


@pytest.fixture
def zip_istat(db, tmp_path):
    return scrivi_zip_istat(tmp_path / "R19_21.zip", db.engine, _sezioni(),
                            {ACIREALE: "Acireale", NUCLEO: "Nucleo di prova"})


def _settings(database_url, tmp_path):
    area = AreaOSM(nome="Prova", url="https://example.invalid/prova.osm.pbf", bbox=(15.13, 37.56, 15.19, 37.60),
                   istat_regioni=[19])
    return impostazioni(database_url, tmp_path, osm_aree=[area], osm2pgsql_cache_mb=50)


def test_lettura_dei_file_istat(zip_istat):
    sez = list(istat.sezioni(zip_istat))
    assert [s["sez_id"] for s in sez] == [s["sez_id"] for s in _sezioni()]
    assert sez[0]["popolazione"] == 12000 and sez[0]["regione"] == 19 and sez[0]["loc_id"] == ACIREALE
    assert sez[0]["wkb"][:9] == bytes([1, 5, 0, 0, 0, 1, 0, 0, 0])  # MULTILINESTRING con un anello
    assert sez[5]["wkb"] is None  # case sparse: solo la popolazione
    with zipfile.ZipFile(zip_istat) as z:
        assert istat.nomi_localita(z) == {ACIREALE: "Acireale", NUCLEO: "Nucleo di prova"}


def test_importazione_con_i_centri(db, database_url, tmp_path, zip_istat):
    r = importa_osm.esegui(_settings(database_url, tmp_path), file=MINI_OSM, istat_file=[zip_istat])
    assert r["istat"]["regioni"] == [19]
    assert r["istat"]["abitanti_regioni"] == 15550
    assert r["istat"]["sezioni"] == 5  # centri e nuclei
    assert zip_istat.exists()  # i file indicati a mano restano
    with db.engine.connect() as c:
        centri = {(f, n): (p, pl, d) for f, n, p, pl, d in c.execute(text(
            "SELECT fonte, nome, popolazione, popolazione_localita, diviso FROM osm.centri"))}
        striscia = c.scalar(text(
            "SELECT c.nome FROM osm.sezioni z JOIN osm.centri c ON c.id = z.centro_id WHERE z.sez_id = 870040000004"))
    # il centro Istat diviso tra le località OSM; la frazione ha la sua sezione e la striscia della statale
    assert centri[("istat", "Acireale")] == (15000, 15400, True)
    assert centri[("istat", "Capomulini")] == (400, 15400, True)
    assert centri[("istat", "Nucleo di prova")] == (50, 50, False)
    # tra i due cartelli "Capomulini": centro dei cartelli, con gli abitanti della frazione
    assert centri[("cartelli", "Capomulini")] == (400, 15400, True)
    assert striscia == "Capomulini"

    with db.session() as s:
        assert stradario.stato(s)["istat"]["regioni"] == [19]
        # statale tra i cartelli di Capomulini: resta ad ANAS, anche se Istat conta la frazione con Acireale
        intorno = stradario.leggi_intorno(s, 37.58, 15.1685, 100)
        ss = abbinamento.abbina(37.58, 15.1685, 5, intorno)
        # provinciale in mezzo ad Acireale (15.000 abitanti): traversa interna, del Comune
        intorno = stradario.leggi_intorno(s, 37.5893, 15.175, 100)
        dentro = abbinamento.abbina(37.5893, 15.175, 5, intorno)
        # la stessa provinciale 44 m fuori dal centro Istat: della Città metropolitana, con il dubbio del cartello
        intorno = stradario.leggi_intorno(s, 37.5893, 15.1795, 100)
        fuori = abbinamento.abbina(37.5893, 15.1795, 5, intorno)

    assert ss["ente"]["codice"] == "anas"
    assert ss["centro"]["fonte"] == "cartelli" and ss["centro"]["nome"] == "Capomulini"
    assert any("«Capomulini» (OSM), circa 400 abitanti" in m for m in ss["motivi"])
    assert any("i cartelli lo delimitano a parte" in m for m in ss["motivi"])
    assert not any("Possibile traversa" in d for d in ss["dubbi"])  # con le sezioni non servono gli indizi

    assert dentro["ente"] == {"codice": "comune", "nome": "Comune di Acireale"}
    assert any(m.startswith("Traversa interna") and "15.000 abitanti" in m for m in dentro["motivi"])
    assert dentro["centro"]["fonte"] == "istat"

    assert fuori["ente"]["nome"] == "Città metropolitana di Catania"
    assert fuori["centro"] is None and fuori["sezioni_istat"] is True
    assert any(d.startswith("A 44 m dal centro abitato «Acireale»") for d in fuori["dubbi"])


def test_senza_istat_lo_stradario_si_importa_lo_stesso(db, database_url, tmp_path):
    rotto = tmp_path / "R19_21.zip"
    rotto.write_bytes(b"non e' uno zip")
    r = importa_osm.esegui(_settings(database_url, tmp_path), file=MINI_OSM, istat_file=[rotto])
    assert r["vie"] == 4
    assert r["istat"]["regioni"] == [] and "non importate" in r["istat"]["errore"]
    with db.engine.connect() as c:
        assert c.scalar(text("SELECT to_regclass('osm.centri')")) is None
        assert c.scalar(text("SELECT to_regclass('osm.sezioni')")) is None
    with db.session() as s:
        intorno = stradario.leggi_intorno(s, 37.58, 15.1685, 100)
    risultato = abbinamento.abbina(37.58, 15.1685, 5, intorno)
    assert risultato["sezioni_istat"] is False
    assert any("Possibile traversa" in d for d in risultato["dubbi"])  # restano gli indizi di OSM
