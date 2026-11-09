"""Segnalazioni dei cittadini: invio (foto, posizione, tipo) e consultazione con il link privato.

- POST /api/v1/segnalazioni (chiave di accesso, multipart: "dati" in JSON e "foto"): il server rifà l'abbinamento
  (non si fida dell'ente calcolato dalla pagina), salva la foto ricodificata e la segnalazione, e risponde con il
  codice e il link privato. La segnalazione va all'ente della strada se è attivo nell'app, altrimenti nella coda "da
  smistare" dell'amministratore.
- /api/v1/s/{codice}: lettura, foto e invio del link per email, con il codice privato (header X-Token, dal link) o con
  la chiave del dispositivo che l'ha inviata (X-Dispositivo). Senza la chiave di accesso: il link deve funzionare
  anche su un altro telefono. Codice sbagliato e segnalazione inesistente danno la stessa risposta (404).
- POST /api/v1/mie: le segnalazioni inviate da questo dispositivo (chiave del dispositivo).

Nel database ci sono solo le impronte SHA-256 del codice privato e della chiave del dispositivo; l'email di chi chiede
il link non si salva (posta.py).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Path, Request, UploadFile
from fastapi.responses import FileResponse
from geoalchemy2 import WKTElement
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from segnalazioni import abbinamento, enti, foto, posta, stradario
from segnalazioni.models import Ente, Evento, Segnalazione
from segnalazioni.sicurezza import richiedi_chiave

log = logging.getLogger(__name__)
invio = APIRouter(prefix="/api/v1", dependencies=[Depends(richiedi_chiave)])
privato = APIRouter(prefix="/api/v1")

CODICE_RE = r"^SCT-\d{4}-\d{6,}$"
CHIAVE_DISPOSITIVO_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
NOMI_ESITO = {"rimossa": "rimossa", "non_trovata": "non trovata sul posto", "doppione": "già segnalata (doppione)"}


def impronta(valore: str) -> str:
    return hashlib.sha256(valore.encode()).hexdigest()


class DatiSegnalazione(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    precisione_m: float | None = Field(default=None, ge=0, le=100_000, allow_inf_nan=False)
    origine: Literal["gps", "mappa"] = "gps"
    tipo: Literal["carcassa", "rifiuto"]
    nota: str | None = Field(default=None, max_length=500)
    osm_way: int | None = Field(default=None, ge=1, le=2**63 - 1)  # strada scelta tra i candidati
    dispositivo: str | None = Field(default=None, pattern=CHIAVE_DISPOSITIVO_RE.pattern)


def _senza_geometrie(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "candidati"} | {
        "candidati": [{k: v for k, v in c.items() if k != "geometria"} for c in r["candidati"]]
    }


def _scegli(risultato: dict, osm_way: int | None) -> tuple[dict | None, dict | None]:
    """(strada, ente) della segnalazione: la strada scelta se è tra i candidati, altrimenti la principale; senza
    strade, il Comune del punto (rifiuti fuori strada: rifiuti urbani)."""
    candidati = risultato.get("candidati") or []
    if osm_way is not None:
        scelta = next((c for c in candidati if c["osm_way"] == osm_way), None)
        if scelta is None:
            raise HTTPException(status_code=422, detail="la strada scelta non è tra quelle vicine al punto")
        return scelta, scelta["ente"]
    if risultato["esito"] == "da_scegliere":
        raise HTTPException(status_code=422, detail="ci sono strade di enti diversi: indica su quale sei")
    if risultato["esito"] == "nessuna_strada":
        return None, {"codice": "comune", "nome": f"Comune di {risultato['comune']}"} if risultato.get("comune") else None
    principale = next((c for c in candidati if c["osm_way"] == risultato.get("principale")), None)
    return principale, risultato.get("ente")


def _nome_strada(strada: dict | None) -> str | None:
    if not strada:
        return None
    nome, ref = strada.get("nome"), strada.get("ref")
    if nome and ref:
        return f"{nome} ({ref})"
    return nome or ref or strada.get("tipo")


def _nuovo_codice(s: Session) -> tuple[int, str]:
    numero = s.scalar(text("SELECT nextval(pg_get_serial_sequence('segnalazioni', 'id'))"))
    return numero, f"SCT-{datetime.now(UTC):%Y}-{numero:06d}"


@invio.post("/segnalazioni", status_code=201)
async def nuova(request: Request, dati: str = Form(max_length=4000), foto_file: UploadFile = File(alias="foto")) -> dict:
    app = request.app
    settings = app.state.settings
    try:
        d = DatiSegnalazione.model_validate(json.loads(dati))
    except (ValueError, ValidationError):
        raise HTTPException(status_code=422, detail="dati della segnalazione non validi") from None
    if not app.state.limite_segnalazioni.consenti():
        raise HTTPException(status_code=429, detail="troppe segnalazioni in questo momento: riprova tra un minuto")
    grezza = await foto_file.read(settings.max_foto_bytes + 1)
    if len(grezza) > settings.max_foto_bytes:
        raise HTTPException(status_code=413, detail="foto troppo grande (al massimo 1 MB)")
    try:
        pulita = await asyncio.to_thread(foto.prepara, grezza, settings.max_foto_bytes)
    except foto.FotoNonValida as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return await asyncio.to_thread(_salva, app, d, pulita)


def _salva(app, d: DatiSegnalazione, pulita: bytes) -> dict:
    settings = app.state.settings
    with app.state.db.session() as s:
        try:
            intorno = stradario.leggi_intorno(s, d.lat, d.lon, stradario.raggio_ricerca(d.precisione_m))
        except stradario.StradarioMancante:
            raise HTTPException(status_code=503, detail="stradario non ancora importato") from None
        except stradario.FuoriArea:
            raise HTTPException(status_code=422, detail=f"punto fuori dall'area coperta ({settings.nome_aree})") from None
        risultato = abbinamento.abbina(d.lat, d.lon, d.precisione_m, intorno, origine=d.origine)
        strada, ente_strada = _scegli(risultato, d.osm_way)
        codice_ente = enti.codice_ente({**risultato, "ente": ente_strada}) if ente_strada else None
        ente = enti.attivo(s, codice_ente)

        nome_file = foto.salva(settings.cartella_foto, pulita)
        try:
            numero, codice = _nuovo_codice(s)
            token = secrets.token_urlsafe(16)
            seg = Segnalazione(
                id=numero, codice=codice, tipo=d.tipo, tipo_iniziale=d.tipo, nota=(d.nota or "").strip() or None,
                lat=d.lat, lon=d.lon, precisione_m=d.precisione_m, origine=d.origine,
                punto=WKTElement(f"POINT({d.lon} {d.lat})", srid=4326), comune=risultato.get("comune"),
                osm_way=strada["osm_way"] if strada else None, strada=_nome_strada(strada),
                abbinamento=_senza_geometrie(risultato), ente_calcolato=codice_ente,
                ente_id=ente.id if ente else None, stato="nuova" if ente else "da_smistare", inoltri=0,
                foto=nome_file, token_hash=impronta(token),
                dispositivo_hash=impronta(d.dispositivo) if d.dispositivo else None, email_inviate=0,
            )
            s.add(seg)
            s.flush()
            s.add(Evento(segnalazione_id=seg.id, tipo="creata", autore="cittadino", a_ente_id=seg.ente_id,
                         dettagli={"tipo": d.tipo, "ente_calcolato": codice_ente,
                                   "ente_calcolato_nome": (ente_strada or {}).get("nome")},
                         visibile_cittadino=True))
            if ente is None:
                s.add(Evento(segnalazione_id=seg.id, tipo="da_smistare", autore="sistema",
                             dettagli={"motivo": "ente non attivo nell'app" if codice_ente else "ente non determinato"},
                             visibile_cittadino=True))
            s.commit()
        except BaseException:
            (settings.cartella_foto / nome_file).unlink(missing_ok=True)
            raise
        log.info("segnalazione %s (%s): %s", codice, d.tipo, ente.codice if ente else f"da smistare ({codice_ente})")
        return {
            "codice": codice,
            "token": token,
            "link": f"{settings.url_pubblico.rstrip('/')}/s#{codice}.{token}",
            "stato": seg.stato,
            "ente": ente.nome if ente else None,
            "ente_calcolato": (ente_strada or {}).get("nome"),
            "email_disponibili": settings.email_per_segnalazione if posta.configurata(settings) else 0,
        }


# --- consultazione con il link privato o dal dispositivo ---------------------------------------------------------

def _autorizzata(s: Session, codice: str, x_token: str | None, x_dispositivo: str | None) -> Segnalazione:
    seg = s.scalar(select(Segnalazione).where(Segnalazione.codice == codice))
    ok = False
    if seg is not None:
        if x_token and TOKEN_RE.match(x_token):
            ok = hmac.compare_digest(seg.token_hash, impronta(x_token))
        if not ok and x_dispositivo and seg.dispositivo_hash and CHIAVE_DISPOSITIVO_RE.match(x_dispositivo):
            ok = hmac.compare_digest(seg.dispositivo_hash, impronta(x_dispositivo))
    if not ok:
        raise HTTPException(status_code=404, detail="segnalazione non trovata")
    return seg


def _nome(e: Ente | None) -> str:
    return e.nome if e else "?"


def testo_evento(ev: Evento) -> str:
    d = ev.dettagli or {}
    if ev.tipo == "creata":
        return f"Inviata a {_nome(ev.a_ente)}" if ev.a_ente else "Inviata"
    if ev.tipo == "da_smistare":
        return "In attesa di assegnazione all'ente competente"
    if ev.tipo == "assegnata":
        return f"Assegnata a {_nome(ev.a_ente)}"
    if ev.tipo == "presa_in_carico":
        return f"Presa in carico da {_nome(ev.da_ente)}"
    if ev.tipo == "inoltrata":
        motivo = f": {d['motivazione']}" if d.get("motivazione") else ""
        return f"Inoltrata per competenza da {_nome(ev.da_ente)} a {_nome(ev.a_ente)}{motivo}"
    if ev.tipo == "tipo_cambiato":
        return f"Tipo cambiato da {d.get('da')} a {d.get('a')} ({_nome(ev.da_ente)})"
    if ev.tipo == "chiusa":
        nota = f" – {d['nota']}" if d.get("nota") else ""
        return f"Chiusa da {_nome(ev.da_ente)}: {NOMI_ESITO.get(d.get('esito'), d.get('esito'))}{nota}"
    return ev.tipo


def vista_cittadino(s: Session, seg: Segnalazione) -> dict:
    eventi = s.scalars(
        select(Evento).where(Evento.segnalazione_id == seg.id, Evento.visibile_cittadino.is_(True))
        .order_by(Evento.quando, Evento.id)
    ).all()
    return {
        "codice": seg.codice,
        "creata": seg.creata.isoformat(),
        "tipo": seg.tipo,
        "tipo_iniziale": seg.tipo_iniziale,
        "stato": seg.stato,
        "esito": seg.esito,
        "ente": seg.ente.nome if seg.ente else None,
        "comune": seg.comune,
        "strada": seg.strada,
        "lat": seg.lat,
        "lon": seg.lon,
        "nota": seg.nota,
        "foto": seg.foto is not None,
        "eventi": [{"quando": e.quando.isoformat(), "tipo": e.tipo, "testo": testo_evento(e)} for e in eventi],
    }


Codice = Path(pattern=CODICE_RE, max_length=30)


@privato.get("/s/{codice}")
def leggi(request: Request, codice: str = Codice, x_token: str | None = Header(default=None),
          x_dispositivo: str | None = Header(default=None)) -> dict:
    settings = request.app.state.settings
    with request.app.state.db.session() as s:
        seg = _autorizzata(s, codice, x_token, x_dispositivo)
        vista = vista_cittadino(s, seg)
        vista["email_disponibili"] = (
            max(0, settings.email_per_segnalazione - seg.email_inviate) if posta.configurata(settings) else 0
        )
        return vista


@privato.get("/s/{codice}/foto")
def foto_cittadino(request: Request, codice: str = Codice, x_token: str | None = Header(default=None),
                   x_dispositivo: str | None = Header(default=None)) -> FileResponse:
    with request.app.state.db.session() as s:
        seg = _autorizzata(s, codice, x_token, x_dispositivo)
        p = foto.percorso(request.app.state.settings.cartella_foto, seg.foto)
    if p is None:
        raise HTTPException(status_code=404, detail="foto non disponibile")
    return FileResponse(p, media_type="image/jpeg")


class RichiestaEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)


@privato.post("/s/{codice}/email")
def manda_link(body: RichiestaEmail, request: Request, codice: str = Codice, x_token: str | None = Header(default=None),
               x_dispositivo: str | None = Header(default=None)) -> dict:
    """Manda il link privato all'indirizzo indicato. L'indirizzo non si salva: si conta solo quante email sono partite
    per la segnalazione (al massimo EMAIL_PER_SEGNALAZIONE)."""
    app = request.app
    settings = app.state.settings
    indirizzo = body.email.strip()
    if not posta.email_valida(indirizzo):
        raise HTTPException(status_code=422, detail="indirizzo email non valido")
    if not posta.configurata(settings):
        raise HTTPException(status_code=503, detail="invio delle email non disponibile")
    if not x_token:
        raise HTTPException(status_code=403, detail="serve il link privato della segnalazione")
    with app.state.db.session() as s:
        seg = _autorizzata(s, codice, x_token, None)  # il link si manda solo a chi ha il link
        if seg.email_inviate >= settings.email_per_segnalazione:
            raise HTTPException(status_code=429, detail="per questa segnalazione sono già partite tutte le email")
        if not app.state.limite_email.consenti():
            raise HTTPException(status_code=429, detail="troppe email in questo momento: riprova tra un minuto")
        seg.email_inviate += 1  # si conta prima: un invio non riuscito vale comunque come tentativo
        s.commit()
        link = f"{settings.url_pubblico.rstrip('/')}/s#{seg.codice}.{x_token}"
        msg = posta.messaggio(settings, indirizzo, seg.codice, link, seg.tipo, seg.ente.nome if seg.ente else None)
        rimaste = settings.email_per_segnalazione - seg.email_inviate
    try:
        posta.invia(settings, msg)
    except posta.InvioNonRiuscito as e:
        log.warning("email del link di %s non inviata: %s", codice, e)
        raise HTTPException(status_code=502, detail="invio non riuscito: controlla l'indirizzo e riprova") from None
    log.info("email del link di %s inviata", codice)
    return {"ok": True, "email_disponibili": rimaste}


class RichiestaMie(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dispositivo: str = Field(pattern=CHIAVE_DISPOSITIVO_RE.pattern)


@privato.post("/mie")
def mie(body: RichiestaMie, request: Request) -> dict:
    with request.app.state.db.session() as s:
        righe = s.scalars(
            select(Segnalazione).where(Segnalazione.dispositivo_hash == impronta(body.dispositivo))
            .order_by(Segnalazione.id.desc()).limit(200)
        ).all()
        return {"segnalazioni": [
            {"codice": r.codice, "creata": r.creata.isoformat(), "tipo": r.tipo, "stato": r.stato, "esito": r.esito,
             "ente": r.ente.nome if r.ente else None, "comune": r.comune, "strada": r.strada}
            for r in righe
        ]}
