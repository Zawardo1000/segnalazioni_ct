"""Pagina, API e protezioni (chiave, validazione, limiti, intestazioni)."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from segnalazioni.app import create_app
from segnalazioni.models import Prova
from tests.conftest import CHIAVE, impostazioni
from tests.helpers import caso, confini_catania, crea_stradario


def _c1(app) -> dict:
    c = caso("c1-capomulini-ss114")
    p = c["punto"]
    crea_stradario(app.app.state.db.engine, c["intorno"]["vie"], confini_catania(p["lat"], p["lon"]))
    return p


def test_health(app):
    r = app.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_pagina_file_e_intestazioni(app):
    r = app.get("/")
    assert r.status_code == 200 and "Segnalazioni CT" in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "geolocation=(self)" in r.headers["permissions-policy"]
    assert app.get("/manifest.webmanifest").headers["content-type"].startswith("application/manifest+json")
    assert app.get("/sw.js").status_code == 200
    # tutti i file che il service worker mette in cache all'installazione (se ne manca uno, non si installa)
    assert "blob:" in csp  # foto della segnalazione lette con fetch (header del codice privato)
    assert app.get("/s").status_code == 200
    for f in ("/static/app.js", "/static/comune.js", "/static/segnalazione.js", "/static/style.css",
              "/static/vendor/leaflet/leaflet.js",
              "/static/vendor/leaflet/leaflet.css", "/static/icon.svg", "/static/icon-192.png", "/static/icon-512.png"):
        assert app.get(f).status_code == 200, f
    for f in ("/", "/sw.js", "/manifest.webmanifest", "/static/app.js", "/health"):
        r = app.head(f)
        assert r.status_code == 200 and "content-security-policy" in r.headers, f
    # niente documentazione automatica
    assert app.get("/docs").status_code == 404 and app.get("/openapi.json").status_code == 404


def test_chiave_obbligatoria(app):
    corpo = {"lat": 37.5, "lon": 15.0}
    assert app.post("/api/v1/abbina", json=corpo).status_code == 401
    assert app.post("/api/v1/abbina", json=corpo, headers={"X-Chiave": "sbagliata"}).status_code == 401
    assert app.get("/api/v1/stato").status_code == 401
    r = app.post("/api/v1/prove/1/esito", json={"esito": "giusto"})
    assert r.status_code == 401
    assert r.headers["cache-control"] == "no-store"


def test_stradario_mancante(app, con_chiave):
    r = app.post("/api/v1/abbina", json={"lat": 37.5, "lon": 15.0}, headers=con_chiave)
    assert r.status_code == 503
    assert app.get("/api/v1/stato", headers=con_chiave).json()["stradario"] is None


def test_abbina_salva_la_prova_e_l_esito(app, con_chiave):
    p = _c1(app)
    r = app.post("/api/v1/abbina", json={"lat": p["lat"], "lon": p["lon"], "precisione_m": 6}, headers=con_chiave)
    assert r.status_code == 200, r.text
    dati = r.json()
    assert dati["esito"] == "deciso" and dati["ente"]["codice"] == "anas"
    assert dati["candidati"][0]["geometria"]  # per disegnarla sulla mappa
    id_prova, token = dati["id_prova"], dati["token_esito"]
    assert len(token) >= 16

    r = app.post(f"/api/v1/prove/{id_prova}/esito", headers=con_chiave,
                 json={"token": token, "esito": "sbagliato", "osm_way_scelto": 186213866, "ente_corretto": "Comune",
                       "nota": " cartello "})
    assert r.status_code == 200

    with app.app.state.db.session() as s:
        prova = s.scalars(select(Prova)).one()
    assert prova.esito_calcolo == "deciso" and prova.ente_calcolato.startswith("ANAS")
    assert prova.fonti["vie"]  # i dati letti: bastano per rifare il calcolo in un test
    assert "geometria" not in prova.risultato["candidati"][0]  # le geometrie stanno già in fonti
    assert "token_esito" not in prova.risultato  # il codice non finisce nel risultato salvato
    assert (prova.esito, prova.ente_corretto, prova.nota) == ("sbagliato", "Comune", "cartello")
    assert prova.origine == "gps"


def test_esito_solo_con_il_codice_della_prova(app, con_chiave):
    p = _c1(app)
    dati = app.post("/api/v1/abbina", json={"lat": p["lat"], "lon": p["lon"]}, headers=con_chiave).json()
    url = f"/api/v1/prove/{dati['id_prova']}/esito"
    # un altro tester ha la chiave ma non il codice di questa prova
    r = app.post(url, headers=con_chiave, json={"token": "x" * 22, "esito": "giusto"})
    assert r.status_code == 403
    assert app.post(url, headers=con_chiave, json={"esito": "giusto"}).status_code == 422
    assert app.post(url, headers=con_chiave, json={"token": dati["token_esito"], "esito": "giusto"}).status_code == 200


def test_numeri_fuori_dai_limiti_del_database(app, con_chiave):
    p = _c1(app)
    dati = app.post("/api/v1/abbina", json={"lat": p["lat"], "lon": p["lon"]}, headers=con_chiave).json()
    corpo = {"token": dati["token_esito"], "esito": "giusto", "osm_way_scelto": 10**30}
    assert app.post(f"/api/v1/prove/{dati['id_prova']}/esito", headers=con_chiave, json=corpo).status_code == 422
    corpo["osm_way_scelto"] = None
    assert app.post(f"/api/v1/prove/{10**30}/esito", headers=con_chiave, json=corpo).status_code == 422
    assert app.post("/api/v1/prove/0/esito", headers=con_chiave, json=corpo).status_code == 422


def test_origine_mappa_arriva_all_abbinamento(app, con_chiave):
    p = _c1(app)
    r = app.post("/api/v1/abbina", json={"lat": p["lat"], "lon": p["lon"], "precisione_m": 5, "origine": "mappa"},
                 headers=con_chiave)
    assert r.status_code == 200 and r.json()["principale"] == 186213866
    with app.app.state.db.session() as s:
        assert s.scalars(select(Prova)).one().origine == "mappa"


def test_fuori_area(app, con_chiave):
    _c1(app)
    r = app.post("/api/v1/abbina", json={"lat": 45.0, "lon": 9.0, "origine": "mappa"}, headers=con_chiave)
    assert r.status_code == 200
    assert r.json()["esito"] == "fuori_area"


def test_esito_di_una_prova_che_non_c_e(app, con_chiave):
    corpo = {"token": "x" * 22, "esito": "giusto"}
    assert app.post("/api/v1/prove/999/esito", json=corpo, headers=con_chiave).status_code == 404


def test_validazione(app, con_chiave):
    for corpo in ({"lat": 200, "lon": 15}, {"lat": 37, "lon": 15, "extra": 1}, {"lat": "x", "lon": 15},
                  {"lat": 37, "lon": 15, "origine": "altro"}):
        assert app.post("/api/v1/abbina", json=corpo, headers=con_chiave).status_code == 422
    r = app.post("/api/v1/abbina", content=b'{"lat": NaN, "lon": 15}',
                 headers={**con_chiave, "Content-Type": "application/json"})
    assert r.status_code == 422
    r = app.post("/api/v1/prove/1/esito", json={"esito": "forse"}, headers=con_chiave)
    assert r.status_code == 422


def test_corpo_troppo_grande(app, con_chiave):
    r = app.post("/api/v1/abbina", content=b"x" * 20_000,
                 headers={**con_chiave, "Content-Type": "application/json"})
    assert r.status_code == 413
    assert "content-security-policy" in r.headers


def test_limite_al_minuto(database_url, tmp_path):
    with TestClient(create_app(impostazioni(database_url, tmp_path, abbinamenti_al_minuto=2))) as app:
        p = _c1(app)
        corpo = {"lat": p["lat"], "lon": p["lon"]}
        stati = [app.post("/api/v1/abbina", json=corpo, headers={"X-Chiave": CHIAVE}).status_code for _ in range(3)]
    assert stati == [200, 200, 429]
