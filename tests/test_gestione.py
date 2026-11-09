"""Gestionale: login OpenID Connect con un Keycloak finto (chiavi RSA generate qui), sessioni e azioni degli enti."""

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from segnalazioni import enti
from segnalazioni.app import create_app
from segnalazioni.models import Sessione
from tests.conftest import CHIAVE, impostazioni
from tests.helpers import caso, confini_catania, crea_stradario
from tests.test_segnalazioni import DISPOSITIVO, jpeg

ISSUER = "https://auth.esempio.it/realms/test_app"


class FintoKeycloak:
    def __init__(self):
        self.chiave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.attributo: str | None = "ente:anas"
        self.utente = "gestore.anas"
        self.nonce = self.sfida = None
        self.alterazioni: dict = {}
        self.chiave_firma = None  # per firmare con un'altra chiave
        self.host: set[str] = set()

    def id_token(self) -> str:
        ora = int(time.time())
        dati = {"iss": ISSUER, "aud": "segnalazioni-ct", "azp": "segnalazioni-ct", "sub": "u-1", "iat": ora,
                "exp": ora + 300, "nonce": self.nonce, "preferred_username": self.utente}
        if self.attributo is not None:
            dati["segnalazioni_ct"] = self.attributo
        dati.update(self.alterazioni)
        return jwt.encode(dati, self.chiave_firma or self.chiave, algorithm="RS256", headers={"kid": "k1"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.host.add(request.url.netloc.decode())
        percorso = request.url.path
        if percorso.endswith("/.well-known/openid-configuration"):
            base = ISSUER + "/protocol/openid-connect"
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": f"{base}/auth",
                                             "token_endpoint": f"{base}/token", "jwks_uri": f"{base}/certs",
                                             "end_session_endpoint": f"{base}/logout"})
        if percorso.endswith("/certs"):
            k = jwt.algorithms.RSAAlgorithm.to_jwk(self.chiave.public_key(), as_dict=True)
            return httpx.Response(200, json={"keys": [{**k, "kid": "k1", "use": "sig", "alg": "RS256"}]})
        if percorso.endswith("/token"):
            if request.headers.get("authorization") != "Basic " + base64.b64encode(b"segnalazioni-ct:segreto").decode():
                return httpx.Response(401, json={"error": "invalid_client"})
            form = parse_qs(request.content.decode())
            verificatore = form["code_verifier"][0]
            sfida = base64.urlsafe_b64encode(hashlib.sha256(verificatore.encode()).digest()).rstrip(b"=").decode()
            if sfida != self.sfida or form["code"] != ["codice-buono"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.id_token(), "access_token": "a", "token_type": "Bearer"})
        return httpx.Response(404)


@pytest.fixture
def kc():
    return FintoKeycloak()


def _client(database_url, tmp_path, kc, **kw):
    settings = impostazioni(database_url, tmp_path, foto_dir=tmp_path / "foto", url_pubblico="https://esempio.it",
                            oidc_issuer=ISSUER, oidc_client_secret="segreto", **kw)
    return TestClient(create_app(settings, oidc_transport=httpx.MockTransport(kc)), base_url="https://testserver")


@pytest.fixture
def gest(database_url, tmp_path, kc):
    with _client(database_url, tmp_path, kc) as client:
        c1 = caso("c1-capomulini-ss114")
        p = c1["punto"]
        crea_stradario(client.app.state.db.engine, c1["intorno"]["vie"], confini_catania(p["lat"], p["lon"]))
        with client.app.state.db.session() as s:
            enti.crea_enti_prova(s)
        dati = {"lat": p["lat"], "lon": p["lon"], "precisione_m": 5, "tipo": "carcassa", "dispositivo": DISPOSITIVO}
        r = client.post("/api/v1/segnalazioni", data={"dati": json.dumps(dati)},
                        files={"foto": ("f.jpg", jpeg(), "image/jpeg")}, headers={"X-Chiave": CHIAVE})
        assert r.status_code == 201, r.text
        client.segnalazione = r.json()
        yield client


def entra(client, kc) -> httpx.Response:
    r = client.get("/gestione/login", follow_redirects=False)
    assert r.status_code == 302
    url = urlsplit(r.headers["location"])
    q = parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == ISSUER + "/protocol/openid-connect/auth"
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["openid"]
    assert q["redirect_uri"] == ["https://esempio.it/gestione/callback"] and q["client_id"] == ["segnalazioni-ct"]
    kc.nonce, kc.sfida = q["nonce"][0], q["code_challenge"][0]
    client.ultimo_state = q["state"][0]
    return client.get("/gestione/callback", params={"code": "codice-buono", "state": q["state"][0]},
                      follow_redirects=False)


def _io(client) -> dict:
    r = client.get("/api/v1/gestione/io")
    assert r.status_code == 200, r.text
    return r.json()


def test_gestore_lavora_le_segnalazioni_del_suo_ente(gest, kc):
    assert gest.get("/api/v1/gestione/io").status_code == 401
    r = entra(gest, kc)
    assert r.status_code == 303 and r.headers["location"] == "/gestione"
    cookie = r.headers["set-cookie"]
    assert "__Host-sct-sessione=" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "Path=/" in cookie
    assert "samesite=lax" in cookie.lower()
    io = _io(gest)
    assert io["ruolo"] == "gestore" and io["ente"]["codice"] == "anas" and io["utente"] == "gestore.anas"
    csrf = {"X-CSRF": io["csrf"]}

    elenco = gest.get("/api/v1/gestione/segnalazioni", params={"stato": "aperte"}).json()["segnalazioni"]
    assert [s["codice"] for s in elenco] == [gest.segnalazione["codice"]]
    sid = elenco[0]["id"]
    det = gest.get(f"/api/v1/gestione/segnalazioni/{sid}").json()
    assert det["ente_calcolato"] == "anas" and det["abbinamento"]["esito"] == "deciso"
    assert gest.get(f"/api/v1/gestione/segnalazioni/{sid}/foto").headers["content-type"] == "image/jpeg"

    # senza il codice CSRF niente modifiche
    assert gest.post(f"/api/v1/gestione/segnalazioni/{sid}/prendi").status_code == 403
    assert gest.post(f"/api/v1/gestione/segnalazioni/{sid}/prendi", headers={"X-CSRF": "x"}).status_code == 403
    assert gest.post(f"/api/v1/gestione/segnalazioni/{sid}/prendi", headers=csrf).json()["stato"] == "presa_in_carico"
    assert gest.post(f"/api/v1/gestione/segnalazioni/{sid}/prendi", headers=csrf).status_code == 409
    assert gest.post(f"/api/v1/gestione/segnalazioni/{sid}/tipo", json={"tipo": "rifiuto"},
                     headers=csrf).json()["tipo"] == "rifiuto"
    # l'assegnazione è dell'amministratore
    assert gest.post(f"/api/v1/gestione/segnalazioni/{sid}/assegna", json={"ente": "comune-087004"},
                     headers=csrf).status_code == 403
    assert gest.post(f"/api/v1/gestione/segnalazioni/{sid}/inoltra", json={"ente": "comune-087004", "motivazione": "x"},
                     headers=csrf).status_code == 422  # motivazione troppo corta
    r = gest.post(f"/api/v1/gestione/segnalazioni/{sid}/inoltra",
                  json={"ente": "comune-087004", "motivazione": "traversa ceduta al Comune"}, headers=csrf)
    assert r.json() == {"id": sid, "stato": "nuova", "visibile": False}
    # ora è del Comune: ANAS non la vede più
    assert gest.get(f"/api/v1/gestione/segnalazioni/{sid}").status_code == 404
    assert gest.get("/api/v1/gestione/segnalazioni").json()["segnalazioni"] == []

    # chi ha segnalato vede lo storico, con i nomi degli enti e non degli utenti
    v = gest.get(f"/api/v1/s/{gest.segnalazione['codice']}", headers={"X-Token": gest.segnalazione["token"]}).json()
    assert [e["testo"] for e in v["eventi"]] == [
        "Inviata a ANAS – Struttura territoriale Sicilia",
        "Presa in carico da ANAS – Struttura territoriale Sicilia",
        "Tipo cambiato da carcassa a rifiuto (ANAS – Struttura territoriale Sicilia)",
        "Inoltrata per competenza da ANAS – Struttura territoriale Sicilia a Comune di Acireale: traversa ceduta al "
        "Comune",
    ]
    assert v["tipo"] == "rifiuto" and v["tipo_iniziale"] == "carcassa" and v["ente"] == "Comune di Acireale"
    assert "gestore.anas" not in json.dumps(v)

    # uscita: sessione cancellata, poi il logout di Keycloak
    r = gest.post("/api/v1/gestione/esci", headers=csrf)
    destinazione = urlsplit(r.json()["redirect"])
    assert destinazione.path.endswith("/protocol/openid-connect/logout")
    assert parse_qs(destinazione.query)["post_logout_redirect_uri"] == ["https://esempio.it/gestione/uscito"]
    assert gest.get("/api/v1/gestione/io").status_code == 401


def test_amministratore_smista_e_chiude(gest, kc):
    kc.attributo, kc.utente = "amministratore", "admin"
    entra(gest, kc)
    io = _io(gest)
    assert io["ruolo"] == "amministratore" and io["ente"] is None
    csrf = {"X-CSRF": io["csrf"]}
    sid = gest.get("/api/v1/gestione/segnalazioni").json()["segnalazioni"][0]["id"]
    # oltre MAX_INOLTRI (2) la segnalazione torna da smistare
    for ente in ("comune-087004", "comune-087015"):
        assert gest.post(f"/api/v1/gestione/segnalazioni/{sid}/inoltra", json={"ente": ente, "motivazione": "prova inoltro"},
                         headers=csrf).json()["stato"] == "nuova"
    r = gest.post(f"/api/v1/gestione/segnalazioni/{sid}/inoltra", json={"ente": "comune-087002",
                  "motivazione": "terzo inoltro"}, headers=csrf)
    assert r.json()["stato"] == "da_smistare"
    da_smistare = gest.get("/api/v1/gestione/segnalazioni", params={"stato": "da_smistare"}).json()["segnalazioni"]
    assert [s["id"] for s in da_smistare] == [sid]
    assert gest.post(f"/api/v1/gestione/segnalazioni/{sid}/chiudi", json={"esito": "rimossa"},
                     headers=csrf).status_code == 409  # prima va assegnata
    assert gest.post(f"/api/v1/gestione/segnalazioni/{sid}/assegna", json={"ente": "provincia-ct"},
                     headers=csrf).json()["ente"] == "Città metropolitana di Catania"
    r = gest.post(f"/api/v1/gestione/segnalazioni/{sid}/chiudi", json={"esito": "rimossa", "nota": "rimossa da SCMC"},
                  headers=csrf)
    assert r.json()["stato"] == "chiusa" and r.json()["esito"] == "rimossa"
    assert gest.post(f"/api/v1/gestione/segnalazioni/{sid}/tipo", json={"tipo": "rifiuto"},
                     headers=csrf).status_code == 409
    eventi = gest.get(f"/api/v1/gestione/segnalazioni/{sid}").json()["eventi"]
    assert eventi[-1]["testo"] == "Chiusa da Città metropolitana di Catania: rimossa – rimossa da SCMC"
    assert eventi[-1]["autore"] == "admin (amministratore)"


@pytest.mark.parametrize("attributo", [None, "ente:inesistente", "gestore", ["ente:anas", "amministratore"]])
def test_senza_attributo_valido_non_si_entra(gest, kc, attributo):
    kc.attributo = attributo
    r = entra(gest, kc)
    assert r.status_code == 403 and "non è abilitato" in r.text
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize("alterazione", [
    {"aud": "altra-app"}, {"iss": "https://altro.it/realms/test_app"}, {"exp": 1}, {"nonce": "altro"},
    {"azp": "altra-app"},
])
def test_token_non_valido(gest, kc, alterazione):
    kc.alterazioni = alterazione
    r = entra(gest, kc)
    assert r.status_code == 401 and "set-cookie" not in r.headers


def test_firma_di_un_altra_chiave_e_state_riusato(gest, kc):
    kc.chiave_firma = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert entra(gest, kc).status_code == 401
    kc.chiave_firma = None
    assert entra(gest, kc).status_code == 303
    # lo stesso state non vale due volte
    r = gest.get("/gestione/callback", params={"code": "codice-buono", "state": gest.ultimo_state},
                 follow_redirects=False)
    assert r.status_code == 400
    assert gest.get("/gestione/callback", params={"error": "access_denied"}, follow_redirects=False).status_code == 400


def test_sessione_scaduta_e_url_interno(database_url, tmp_path, kc):
    with _client(database_url, tmp_path, kc, oidc_url_interno="http://keycloak:8080") as client:
        with client.app.state.db.session() as s:
            enti.crea_enti_prova(s)
        assert entra(client, kc).status_code == 303
        assert kc.host == {"keycloak:8080"}  # dal server solo l'indirizzo interno; il browser va su quello pubblico
        assert client.get("/api/v1/gestione/io").status_code == 200
        with client.app.state.db.session() as s:
            for sess in s.scalars(select(Sessione)):
                sess.scade = sess.creata
            s.commit()
        assert client.get("/api/v1/gestione/io").status_code == 401


def test_gestionale_non_configurato(app):
    assert app.get("/gestione/login", follow_redirects=False).status_code == 503
    assert app.get("/gestione").status_code == 200  # la pagina c'è, ma senza accesso non mostra niente
    assert app.get("/api/v1/gestione/segnalazioni").status_code == 401
