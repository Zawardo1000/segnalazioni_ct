"""Segnalazioni dei cittadini: invio con la foto, link privato, chiave del dispositivo, email, coda da smistare."""

import io
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, text

from segnalazioni import enti, foto, posta
from segnalazioni.app import create_app
from segnalazioni.models import Ente, Evento, Segnalazione
from tests.conftest import CHIAVE, impostazioni
from tests.helpers import caso, confini_catania, crea_stradario

DISPOSITIVO = "d" * 43


def jpeg(larghezza=1200, altezza=900, exif_gps=True, orientamento=None) -> bytes:
    im = Image.new("RGB", (larghezza, altezza), (120, 160, 90))
    exif = Image.Exif()
    if exif_gps:
        exif[0x010F] = "Telefono di prova"  # Make
        exif[0x8825] = {1: "N", 2: (37.0, 34.0, 52.0)}  # GPSInfo
    if orientamento:
        exif[0x0112] = orientamento
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


@pytest.fixture
def app_seg(database_url, tmp_path):
    settings = impostazioni(database_url, tmp_path, foto_dir=tmp_path / "foto", url_pubblico="https://esempio.it",
                            smtp_host="smtp.esempio.it", smtp_user="u@esempio.it", smtp_password="x")
    with TestClient(create_app(settings)) as client:
        c1 = caso("c1-capomulini-ss114")
        p = c1["punto"]
        crea_stradario(client.app.state.db.engine, c1["intorno"]["vie"], confini_catania(p["lat"], p["lon"]))
        with client.app.state.db.session() as s:
            enti.crea_enti_prova(s)
        client.punto = p
        yield client


def _invia(client, dati: dict | None = None, foto_bytes: bytes | None = None, chiave: str | None = CHIAVE):
    p = client.punto
    corpo = {"lat": p["lat"], "lon": p["lon"], "precisione_m": 5, "tipo": "carcassa", "dispositivo": DISPOSITIVO,
             **(dati or {})}
    return client.post(
        "/api/v1/segnalazioni",
        data={"dati": json.dumps(corpo)},
        files={"foto": ("foto.jpg", foto_bytes if foto_bytes is not None else jpeg(), "image/jpeg")},
        headers={"X-Chiave": chiave} if chiave else {},
    )


def test_invio_e_lettura_con_il_link_privato(app_seg, tmp_path):
    r = _invia(app_seg, {"nota": "  lato mare  "})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["codice"].startswith("SCT-") and d["ente"] == "ANAS – Struttura territoriale Sicilia"
    assert d["link"] == f"https://esempio.it/s#{d['codice']}.{d['token']}"
    # la foto salvata è un JPEG senza metadati (niente posizione né modello del telefono)
    salvate = list((tmp_path / "foto").glob("*.jpg"))
    assert len(salvate) == 1
    with Image.open(salvate[0]) as im:
        assert im.format == "JPEG" and not im.getexif()

    vista = app_seg.get(f"/api/v1/s/{d['codice']}", headers={"X-Token": d["token"]})
    assert vista.status_code == 200
    v = vista.json()
    assert (v["tipo"], v["stato"], v["ente"], v["nota"]) == ("carcassa", "nuova", d["ente"], "lato mare")
    assert v["eventi"][0]["testo"] == "Inviata a ANAS – Struttura territoriale Sicilia"
    assert v["email_disponibili"] == 3
    f = app_seg.get(f"/api/v1/s/{d['codice']}/foto", headers={"X-Token": d["token"]})
    assert f.status_code == 200 and f.headers["content-type"] == "image/jpeg" and f.content[:3] == b"\xff\xd8\xff"
    # con la chiave del dispositivo che l'ha inviata, senza il link
    assert app_seg.get(f"/api/v1/s/{d['codice']}", headers={"X-Dispositivo": DISPOSITIVO}).status_code == 200
    # codice privato sbagliato, altro dispositivo o niente: come se non esistesse
    for h in ({"X-Token": "x" * 22}, {"X-Dispositivo": "e" * 43}, {}):
        assert app_seg.get(f"/api/v1/s/{d['codice']}", headers=h).status_code == 404
    assert app_seg.get("/api/v1/s/SCT-2026-999999", headers={"X-Token": d["token"]}).status_code == 404

    with app_seg.app.state.db.session() as s:
        seg = s.scalar(select(Segnalazione))
        # nel database solo le impronte, mai i codici
        assert seg.token_hash != d["token"] and len(seg.token_hash) == 64
        assert seg.dispositivo_hash != DISPOSITIVO
        assert seg.ente_calcolato == "anas" and seg.osm_way in {186213866, 180892598}
        assert "geometria" not in json.dumps(seg.abbinamento)

    mie = app_seg.post("/api/v1/mie", json={"dispositivo": DISPOSITIVO}).json()["segnalazioni"]
    assert [m["codice"] for m in mie] == [d["codice"]]
    assert app_seg.post("/api/v1/mie", json={"dispositivo": "e" * 43}).json()["segnalazioni"] == []


def test_invio_protetto_e_validato(app_seg):
    assert _invia(app_seg, chiave=None).status_code == 401
    assert _invia(app_seg, chiave="sbagliata").status_code == 401
    assert _invia(app_seg, foto_bytes=b"GIF89a...").status_code == 422
    assert _invia(app_seg, {"tipo": "altro"}).status_code == 422
    assert _invia(app_seg, {"osm_way": 1}).status_code == 422  # strada non vicina al punto
    assert _invia(app_seg, {"lat": 45.0, "lon": 9.0}).status_code == 422  # fuori dall'area
    troppo = b"\xff\xd8\xff" + b"0" * (1024 * 1024 + 70 * 1024)
    assert _invia(app_seg, foto_bytes=troppo).status_code == 413


def test_ente_non_attivo_va_da_smistare(app_seg):
    with app_seg.app.state.db.session() as s:
        s.scalar(select(Ente).where(Ente.codice == "anas")).attivo = False
        s.commit()
    d = _invia(app_seg, {"tipo": "rifiuto"}).json()
    assert d["ente"] is None and d["stato"] == "da_smistare"
    assert d["ente_calcolato"] == "ANAS – Struttura territoriale Sicilia"
    v = app_seg.get(f"/api/v1/s/{d['codice']}", headers={"X-Token": d["token"]}).json()
    assert v["stato"] == "da_smistare" and v["ente"] is None
    assert [e["tipo"] for e in v["eventi"]] == ["creata", "da_smistare"]


def test_email_con_il_link_senza_salvare_l_indirizzo(app_seg, monkeypatch, caplog):
    inviati = []
    monkeypatch.setattr(posta, "invia", lambda settings, msg: inviati.append(msg))
    d = _invia(app_seg).json()
    url = f"/api/v1/s/{d['codice']}/email"
    h = {"X-Token": d["token"]}
    caplog.set_level(logging.INFO)
    assert app_seg.post(url, json={"email": "non-valida"}, headers=h).status_code == 422
    assert app_seg.post(url, json={"email": "mario@esempio.it"}).status_code == 403  # serve il link
    assert app_seg.post(url, json={"email": "mario@esempio.it"}, headers={"X-Dispositivo": DISPOSITIVO}).status_code == 403
    for rimaste in (2, 1, 0):
        r = app_seg.post(url, json={"email": "mario@esempio.it"}, headers=h)
        assert r.status_code == 200 and r.json()["email_disponibili"] == rimaste
    assert app_seg.post(url, json={"email": "mario@esempio.it"}, headers=h).status_code == 429
    assert len(inviati) == 3
    msg = inviati[0]
    assert msg["To"] == "mario@esempio.it" and d["link"] in msg.get_content()
    # l'indirizzo non resta da nessuna parte: né nel database né nei log
    with app_seg.app.state.db.engine.connect() as c:
        for tabella in ("segnalazioni", "eventi"):
            righe = c.execute(text(f"SELECT row_to_json(t)::text FROM {tabella} t")).scalars().all()
            assert not any("mario@" in r for r in righe), tabella
    assert "mario@" not in caplog.text


def test_email_non_configurata(database_url, tmp_path):
    with TestClient(create_app(impostazioni(database_url, tmp_path, foto_dir=tmp_path / "foto"))) as client:
        c1 = caso("c1-capomulini-ss114")
        p = c1["punto"]
        crea_stradario(client.app.state.db.engine, c1["intorno"]["vie"], confini_catania(p["lat"], p["lon"]))
        client.punto = p
        d = _invia(client).json()
        h = {"X-Token": d["token"]}
        assert client.get(f"/api/v1/s/{d['codice']}", headers=h).json()["email_disponibili"] == 0
        assert client.post(f"/api/v1/s/{d['codice']}/email", json={"email": "a@b.it"}, headers=h).status_code == 503
        # senza enti attivi: tutto da smistare
        assert d["stato"] == "da_smistare"
        with client.app.state.db.session() as s:
            assert [e.tipo for e in s.scalars(select(Evento).order_by(Evento.id))] == ["creata", "da_smistare"]


def test_prepara_foto():
    pulita = foto.prepara(jpeg(4000, 3000, orientamento=6), 1024 * 1024)
    with Image.open(io.BytesIO(pulita)) as im:
        assert not im.getexif()
        assert im.size == (1536, 2048)  # raddrizzata (orientamento 6: ruotata di 90°) e rimpicciolita
    with pytest.raises(foto.FotoNonValida):
        foto.prepara(b"\x89PNG\r\n", 1024 * 1024)
    with pytest.raises(foto.FotoNonValida):
        foto.prepara(b"\xff\xd8\xff\xe0 rotto", 1024 * 1024)
    assert foto.percorso(Path("."), "../../etc/passwd") is None
