"""Lettura dello stradario in PostGIS: strade nel raggio, confini che contengono il punto, comuni vicini."""

import pytest

from segnalazioni import stradario
from segnalazioni.db import Database
from tests.helpers import confini_catania, crea_stradario


@pytest.fixture
def db(database_url):
    d = Database(database_url)
    d.migrate()  # PostGIS
    yield d
    d.dispose()


LAT, LON = 37.58, 15.168


def _vie():
    return [
        {"osm_way": 1, "tags": {"highway": "primary", "ref": "SS 114"}, "geometria": [[37.575, 15.1681], [37.585, 15.1681]]},
        {"osm_way": 2, "tags": {"highway": "residential", "name": "Via Lontana"}, "geometria": [[37.59, 15.168], [37.59, 15.17]]},
    ]


def test_mancante(db):
    with db.session() as s:
        assert not stradario.presente(s)
        assert stradario.stato(s) is None
        with pytest.raises(stradario.StradarioMancante):
            stradario.leggi_intorno(s, LAT, LON, 100)


def test_intorno(db):
    crea_stradario(db.engine, _vie(), confini_catania(LAT, LON))
    with db.session() as s:
        i = stradario.leggi_intorno(s, LAT, LON, 100)
        st = stradario.stato(s)
    assert [v["osm_way"] for v in i["vie"]] == [1]  # la via a 1 km resta fuori dal raggio
    assert i["vie"][0]["geometria"][0] == [37.575, 15.1681]
    assert i["comune"]["nome"] == "Acireale" and i["comune"]["osm_relation"] == 39290
    assert i["provincia"]["tags"]["ref"] == "CT" and i["regione"]["nome"] == "Sicilia"
    assert i["comuni_vicini"] == []
    assert i["fonte"]["tipo"] == "stradario" and i["fonte"]["dati_del"].startswith("2026-11-08")
    assert st["vie"] == 2


def test_comune_vicino_e_fuori_area(db):
    confini = confini_catania(LAT, LON)
    # Aci Castello: quadrato subito a sud, con il confine a circa 100 m dal punto
    confini.append({"relation_id": 39276, "admin_level": 8, "nome": "Aci Castello", "tags": {},
                    "bbox": (LON - 0.01, LAT - 0.02, LON + 0.01, LAT - 0.0009)})
    crea_stradario(db.engine, _vie(), confini)
    with db.session() as s:
        assert stradario.leggi_intorno(s, LAT, LON, 100)["comuni_vicini"] == ["Aci Castello"]
        with pytest.raises(stradario.FuoriArea):
            stradario.leggi_intorno(s, 45.0, 9.0, 100)


def test_raggio_ricerca():
    assert stradario.raggio_ricerca(None) == 100
    assert stradario.raggio_ricerca(40) == 120
    assert stradario.raggio_ricerca(5000) == 150
