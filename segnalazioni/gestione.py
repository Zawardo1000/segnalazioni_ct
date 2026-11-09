"""Gestionale degli enti: login con Keycloak (OpenID Connect), sessioni, elenco e azioni sulle segnalazioni.

Login (flusso "authorization code" con PKCE, client confidenziale):
1. /gestione/login: state, nonce e verificatore PKCE nuovi (tabella login_in_corso, 10 minuti), redirect a Keycloak;
2. /gestione/callback: il server scambia il codice con Keycloak (segreto del client) e verifica l'ID token: firma RS256
   con le chiavi pubbliche del realm, emittente, destinatario (il client), scadenza, nonce; poi legge l'attributo
   OIDC_ATTRIBUTO ("amministratore" oppure "ente:<codice>"). Senza attributo valido, o con un ente non attivo, non si
   entra: essere un utente del realm non basta (il realm di prova può servire anche ad altre app);
3. sessione nel database (cookie __Host-, HttpOnly, Secure, SameSite=Lax, 8 ore); le richieste che cambiano qualcosa
   vogliono anche l'header X-CSRF con il codice della sessione.

Chi è gestore vede e lavora solo le segnalazioni del suo ente; l'amministratore vede tutto, anche la coda da smistare,
e assegna le segnalazioni. Ogni azione va nello storico (eventi), che vede anche chi ha segnalato.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import time
from datetime import timedelta
from typing import Literal
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from segnalazioni import foto
from segnalazioni.config import BASE_DIR, Settings
from segnalazioni.db import utcnow
from segnalazioni.models import Ente, Evento, LoginInCorso, Segnalazione, Sessione
from segnalazioni.segnala import testo_evento

log = logging.getLogger(__name__)
pagine = APIRouter(include_in_schema=False)
api = APIRouter(prefix="/api/v1/gestione")

COOKIE = "__Host-sct-sessione"
STATIC = BASE_DIR / "segnalazioni" / "static"
DURATA_LOGIN_S = 600
CACHE_OIDC_S = 3600


# --- OpenID Connect ----------------------------------------------------------------------------------------------

class Oidc:
    """Configurazione del realm (discovery) e chiavi pubbliche, in cache; chiamate dal server con un tempo massimo."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.s = settings
        self.http = httpx.Client(timeout=httpx.Timeout(10.0), transport=transport, follow_redirects=False)
        self._config: tuple[float, dict] | None = None
        self._chiavi: tuple[float, dict] | None = None

    @property
    def attivo(self) -> bool:
        return bool(self.s.oidc_issuer and self.s.oidc_client_secret.get_secret_value())

    def interno(self, url: str) -> str:
        """L'URL per le chiamate dal server: stessa strada, ma verso OIDC_URL_INTERNO se c'è."""
        if not self.s.oidc_url_interno:
            return url
        u, i = urlsplit(url), urlsplit(self.s.oidc_url_interno)
        return u._replace(scheme=i.scheme, netloc=i.netloc).geturl()

    def config(self) -> dict:
        if self._config and time.monotonic() - self._config[0] < CACHE_OIDC_S:
            return self._config[1]
        url = self.interno(self.s.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration")
        r = self.http.get(url)
        r.raise_for_status()
        c = r.json()
        if c.get("issuer") != self.s.oidc_issuer.rstrip("/"):
            raise RuntimeError("issuer della configurazione diverso da OIDC_ISSUER")
        self._config = (time.monotonic(), c)
        return c

    def chiave(self, kid: str | None) -> jwt.PyJWK:
        for giro in range(2):  # chiave sconosciuta: si rilegge una volta (Keycloak può averle ruotate)
            if giro or not self._chiavi or time.monotonic() - self._chiavi[0] >= CACHE_OIDC_S:
                r = self.http.get(self.interno(self.config()["jwks_uri"]))
                r.raise_for_status()
                self._chiavi = (time.monotonic(), r.json())
            for k in self._chiavi[1].get("keys", []):
                if k.get("kid") == kid and k.get("use", "sig") == "sig" and k.get("kty") == "RSA":
                    return jwt.PyJWK(k, algorithm="RS256")
        raise jwt.InvalidTokenError("chiave di firma sconosciuta")

    def scambia(self, codice: str, verificatore: str, redirect_uri: str) -> dict:
        r = self.http.post(
            self.interno(self.config()["token_endpoint"]),
            data={"grant_type": "authorization_code", "code": codice, "redirect_uri": redirect_uri,
                  "code_verifier": verificatore},
            auth=(self.s.oidc_client_id, self.s.oidc_client_secret.get_secret_value()),
        )
        if r.status_code != 200:
            raise RuntimeError(f"scambio del codice rifiutato da Keycloak ({r.status_code})")
        return r.json()

    def verifica(self, id_token: str, nonce: str) -> dict:
        intestazione = jwt.get_unverified_header(id_token)
        if intestazione.get("alg") != "RS256":
            raise jwt.InvalidTokenError("algoritmo non ammesso")
        dati = jwt.decode(
            id_token, self.chiave(intestazione.get("kid")), algorithms=["RS256"], audience=self.s.oidc_client_id,
            issuer=self.s.oidc_issuer.rstrip("/"), leeway=30,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
        )
        if not hmac.compare_digest(str(dati.get("nonce")), nonce):
            raise jwt.InvalidTokenError("nonce diverso")
        if dati.get("azp") not in (None, self.s.oidc_client_id):
            raise jwt.InvalidTokenError("token emesso per un altro client")
        return dati


def _impronta(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def _redirect_uri(settings: Settings) -> str:
    return f"{settings.url_pubblico.rstrip('/')}/gestione/callback"


def ruolo_da_attributo(valore, s: Session) -> tuple[str, Ente | None] | None:
    """("amministratore", None) o ("gestore", ente) dall'attributo dell'utente; None se non dà accesso."""
    if isinstance(valore, list):
        valore = valore[0] if len(valore) == 1 else None
    if not isinstance(valore, str):
        return None
    valore = valore.strip()
    if valore == "amministratore":
        return "amministratore", None
    if valore.startswith("ente:"):
        ente = s.scalar(select(Ente).where(Ente.codice == valore[5:], Ente.attivo.is_(True)))
        return ("gestore", ente) if ente else None
    return None


def _pagina_errore(testo: str, stato: int) -> HTMLResponse:
    from html import escape

    return HTMLResponse(
        f"<!doctype html><html lang=it><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>Gestionale</title><link rel=stylesheet href=/static/style.css><main><div class='msg errore'>"
        f"{escape(testo)}</div><p><a href=/gestione/login>Accedi di nuovo</a></p></main></html>",
        status_code=stato,
    )


@pagine.api_route("/gestione", methods=["GET", "HEAD"])
def pagina_gestione() -> FileResponse:
    return FileResponse(STATIC / "gestione.html", media_type="text/html; charset=utf-8")


@pagine.get("/gestione/login")
def login(request: Request) -> Response:
    oidc: Oidc = request.app.state.oidc
    if not oidc.attivo:
        return _pagina_errore("Gestionale non configurato.", 503)
    if not request.app.state.limite_login.consenti():
        return _pagina_errore("Troppi accessi in questo momento: riprova tra un minuto.", 429)
    try:
        autorizza = oidc.config()["authorization_endpoint"]
    except Exception:
        log.exception("configurazione di Keycloak non disponibile")
        return _pagina_errore("Servizio di accesso non raggiungibile: riprova più tardi.", 503)
    state, nonce, verificatore = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    sfida = base64.urlsafe_b64encode(hashlib.sha256(verificatore.encode()).digest()).rstrip(b"=").decode()
    with request.app.state.db.session() as s:
        s.execute(delete(LoginInCorso).where(LoginInCorso.creato < utcnow() - timedelta(seconds=DURATA_LOGIN_S)))
        s.execute(delete(Sessione).where(Sessione.scade < utcnow()))
        s.add(LoginInCorso(state=state, nonce=nonce, verificatore=verificatore))
        s.commit()
    settings = request.app.state.settings
    query = urlencode({"response_type": "code", "client_id": settings.oidc_client_id,
                       "redirect_uri": _redirect_uri(settings), "scope": "openid", "state": state, "nonce": nonce,
                       "code_challenge": sfida, "code_challenge_method": "S256"})
    return RedirectResponse(f"{autorizza}?{query}", status_code=302)


@pagine.get("/gestione/callback")
def callback(request: Request, code: str | None = Query(default=None, max_length=4096),
             state: str | None = Query(default=None, max_length=128),
             error: str | None = Query(default=None, max_length=200)) -> Response:
    app = request.app
    settings: Settings = app.state.settings
    oidc: Oidc = app.state.oidc
    if not oidc.attivo:
        return _pagina_errore("Gestionale non configurato.", 503)
    if error or not code or not state:
        return _pagina_errore("Accesso non riuscito.", 400)
    with app.state.db.session() as s:
        in_corso = s.get(LoginInCorso, state)
        if in_corso is not None:
            s.delete(in_corso)  # un codice di stato vale una volta sola
            s.commit()
        if in_corso is None or (utcnow() - in_corso.creato).total_seconds() > DURATA_LOGIN_S:
            return _pagina_errore("Accesso scaduto o non valido: riprova.", 400)
        try:
            token = oidc.scambia(code, in_corso.verificatore, _redirect_uri(settings))
            dati = oidc.verifica(token.get("id_token") or "", in_corso.nonce)
        except (jwt.InvalidTokenError, RuntimeError, httpx.HTTPError, ValueError) as e:
            log.warning("login non riuscito: %s", e)
            return _pagina_errore("Accesso non riuscito.", 401)
        utente = str(dati.get("preferred_username") or dati["sub"])[:200]
        ruolo = ruolo_da_attributo(dati.get(settings.oidc_attributo), s)
        if ruolo is None:
            log.warning("login rifiutato per %s: attributo %s mancante o non valido", utente, settings.oidc_attributo)
            return _pagina_errore("Il tuo utente non è abilitato al gestionale delle segnalazioni.", 403)
        sid = secrets.token_urlsafe(32)
        s.add(Sessione(id_hash=_impronta(sid), scade=utcnow() + timedelta(hours=settings.durata_sessione_ore),
                       sub=str(dati["sub"])[:100], utente=utente, ruolo=ruolo[0],
                       ente_id=ruolo[1].id if ruolo[1] else None, csrf=secrets.token_urlsafe(32)))
        s.commit()
    log.info("login di %s (%s%s)", utente, ruolo[0], f", {ruolo[1].codice}" if ruolo[1] else "")
    r = RedirectResponse("/gestione", status_code=303)
    r.set_cookie(COOKIE, sid, max_age=settings.durata_sessione_ore * 3600, path="/", secure=True, httponly=True,
                 samesite="lax")
    return r


@pagine.get("/gestione/uscito")
def uscito() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=it><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<title>Gestionale</title><link rel=stylesheet href=/static/style.css><main><p class=msg>Sei uscito.</p>"
        "<p><a href=/gestione/login>Accedi di nuovo</a></p></main></html>"
    )


# --- sessione e permessi -----------------------------------------------------------------------------------------

def sessione(request: Request, sid: str | None = Cookie(default=None, alias=COOKIE)) -> Sessione:
    if not sid or len(sid) > 100:
        raise HTTPException(status_code=401, detail="accesso richiesto")
    with request.app.state.db.session() as s:
        sess = s.get(Sessione, _impronta(sid))
        if sess is None or sess.scade < utcnow():
            raise HTTPException(status_code=401, detail="accesso richiesto")
        ente = sess.ente  # caricato ora: dopo la chiusura della sessione del database l'oggetto resta leggibile
        if sess.ente_id is not None and (ente is None or not ente.attivo):
            raise HTTPException(status_code=403, detail="ente non attivo")
        return sess


def modifica(sess: Sessione = Depends(sessione), x_csrf: str | None = Header(default=None)) -> Sessione:
    """Per le richieste che cambiano qualcosa: anche il codice CSRF della sessione (la pagina lo legge da /io)."""
    if not x_csrf or not hmac.compare_digest(x_csrf, sess.csrf):
        raise HTTPException(status_code=403, detail="codice CSRF mancante o sbagliato")
    return sess


def _visibile(sess: Sessione, seg: Segnalazione) -> bool:
    return sess.ruolo == "amministratore" or (seg.ente_id is not None and seg.ente_id == sess.ente_id)


def _carica(s: Session, sess: Sessione, id_seg: int) -> Segnalazione:
    seg = s.get(Segnalazione, id_seg)
    if seg is None or not _visibile(sess, seg):
        raise HTTPException(status_code=404, detail="segnalazione non trovata")
    return seg


def _autore(sess: Sessione) -> str:
    return f"{sess.utente} ({sess.ente.nome})" if sess.ente else f"{sess.utente} (amministratore)"


def _evento(s: Session, seg: Segnalazione, sess: Sessione, tipo: str, a_ente_id: int | None = None, **dettagli) -> None:
    s.add(Evento(segnalazione_id=seg.id, tipo=tipo, autore=_autore(sess), da_ente_id=seg.ente_id, a_ente_id=a_ente_id,
                 dettagli=dettagli, visibile_cittadino=True))
    seg.aggiornata = utcnow()


def _aperta(seg: Segnalazione) -> None:
    if seg.stato == "chiusa":
        raise HTTPException(status_code=409, detail="segnalazione già chiusa")


def _riga(seg: Segnalazione) -> dict:
    return {"id": seg.id, "codice": seg.codice, "creata": seg.creata.isoformat(), "aggiornata": seg.aggiornata.isoformat(),
            "tipo": seg.tipo, "stato": seg.stato, "esito": seg.esito, "comune": seg.comune, "strada": seg.strada,
            "lat": seg.lat, "lon": seg.lon, "ente": seg.ente.nome if seg.ente else None, "inoltri": seg.inoltri}


# --- API del gestionale ------------------------------------------------------------------------------------------

@api.get("/io")
def io(sess: Sessione = Depends(sessione)) -> dict:
    return {"utente": sess.utente, "ruolo": sess.ruolo, "csrf": sess.csrf,
            "ente": {"codice": sess.ente.codice, "nome": sess.ente.nome} if sess.ente else None}


@api.post("/esci")
def esci(request: Request, sess: Sessione = Depends(modifica)) -> Response:
    settings: Settings = request.app.state.settings
    with request.app.state.db.session() as s:
        s.execute(delete(Sessione).where(Sessione.id_hash == sess.id_hash))
        s.commit()
    uscita = None
    try:
        fine = request.app.state.oidc.config().get("end_session_endpoint")
        if fine:
            uscita = fine + "?" + urlencode({"client_id": settings.oidc_client_id,
                                             "post_logout_redirect_uri": f"{settings.url_pubblico.rstrip('/')}/gestione/uscito"})
    except Exception:
        log.warning("configurazione di Keycloak non disponibile per l'uscita")
    r = JSONResponse({"redirect": uscita or "/gestione/uscito"})
    r.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return r


@api.get("/enti")
def lista_enti(request: Request, sess: Sessione = Depends(sessione)) -> dict:
    with request.app.state.db.session() as s:
        righe = s.scalars(select(Ente).where(Ente.attivo.is_(True)).order_by(Ente.nome)).all()
        return {"enti": [{"codice": e.codice, "nome": e.nome} for e in righe]}


@api.get("/segnalazioni")
def lista(request: Request, sess: Sessione = Depends(sessione),
          stato: Literal["nuova", "presa_in_carico", "chiusa", "da_smistare", "aperte"] | None = None,
          tipo: Literal["carcassa", "rifiuto"] | None = None) -> dict:
    with request.app.state.db.session() as s:
        q = select(Segnalazione).order_by(Segnalazione.id.desc()).limit(500)
        if sess.ruolo != "amministratore":
            q = q.where(Segnalazione.ente_id == sess.ente_id)
        if stato == "aperte":
            q = q.where(Segnalazione.stato.in_(("nuova", "presa_in_carico", "da_smistare")))
        elif stato:
            q = q.where(Segnalazione.stato == stato)
        if tipo:
            q = q.where(Segnalazione.tipo == tipo)
        return {"segnalazioni": [_riga(x) for x in s.scalars(q)]}


IdSeg = Path(ge=1, le=2**63 - 1)


@api.get("/segnalazioni/{id_seg}")
def dettaglio(request: Request, id_seg: int = IdSeg, sess: Sessione = Depends(sessione)) -> dict:
    with request.app.state.db.session() as s:
        seg = _carica(s, sess, id_seg)
        eventi = s.scalars(select(Evento).where(Evento.segnalazione_id == seg.id).order_by(Evento.quando, Evento.id))
        a = seg.abbinamento or {}
        return {
            **_riga(seg), "tipo_iniziale": seg.tipo_iniziale, "nota": seg.nota, "precisione_m": seg.precisione_m,
            "origine": seg.origine, "osm_way": seg.osm_way, "foto": seg.foto is not None,
            "chiusa_il": seg.chiusa_il.isoformat() if seg.chiusa_il else None,
            "ente_calcolato": seg.ente_calcolato,
            "abbinamento": {k: a.get(k) for k in ("esito", "affidabilita", "motivi", "dubbi", "centro")},
            "eventi": [{"quando": e.quando.isoformat(), "tipo": e.tipo, "autore": e.autore, "testo": testo_evento(e)}
                       for e in eventi],
        }


@api.get("/segnalazioni/{id_seg}/foto")
def foto_gestione(request: Request, id_seg: int = IdSeg, sess: Sessione = Depends(sessione)) -> FileResponse:
    with request.app.state.db.session() as s:
        seg = _carica(s, sess, id_seg)
        p = foto.percorso(request.app.state.settings.cartella_foto, seg.foto)
    if p is None:
        raise HTTPException(status_code=404, detail="foto non disponibile")
    return FileResponse(p, media_type="image/jpeg")


@api.post("/segnalazioni/{id_seg}/prendi")
def prendi(request: Request, id_seg: int = IdSeg, sess: Sessione = Depends(modifica)) -> dict:
    with request.app.state.db.session() as s:
        seg = _carica(s, sess, id_seg)
        if seg.stato != "nuova":
            raise HTTPException(status_code=409, detail="si prende in carico solo una segnalazione nuova")
        seg.stato = "presa_in_carico"
        _evento(s, seg, sess, "presa_in_carico")
        s.commit()
        return _riga(seg)


class Chiusura(BaseModel):
    model_config = ConfigDict(extra="forbid")
    esito: Literal["rimossa", "non_trovata", "doppione"]
    nota: str | None = Field(default=None, max_length=1000)


@api.post("/segnalazioni/{id_seg}/chiudi")
def chiudi(body: Chiusura, request: Request, id_seg: int = IdSeg, sess: Sessione = Depends(modifica)) -> dict:
    with request.app.state.db.session() as s:
        seg = _carica(s, sess, id_seg)
        _aperta(seg)
        if seg.stato == "da_smistare":
            raise HTTPException(status_code=409, detail="prima va assegnata a un ente")
        seg.stato, seg.esito, seg.chiusa_il = "chiusa", body.esito, utcnow()
        _evento(s, seg, sess, "chiusa", esito=body.esito, nota=(body.nota or "").strip() or None)
        s.commit()
        return _riga(seg)


class Inoltro(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ente: str = Field(max_length=40)
    motivazione: str = Field(min_length=5, max_length=1000)


@api.post("/segnalazioni/{id_seg}/inoltra")
def inoltra(body: Inoltro, request: Request, id_seg: int = IdSeg, sess: Sessione = Depends(modifica)) -> dict:
    """Manda la segnalazione per competenza a un altro ente. Dopo MAX_INOLTRI va all'amministratore."""
    settings: Settings = request.app.state.settings
    with request.app.state.db.session() as s:
        seg = _carica(s, sess, id_seg)
        _aperta(seg)
        dest = s.scalar(select(Ente).where(Ente.codice == body.ente, Ente.attivo.is_(True)))
        if dest is None:
            raise HTTPException(status_code=422, detail="ente di destinazione sconosciuto")
        if dest.id == seg.ente_id:
            raise HTTPException(status_code=422, detail="la segnalazione è già di questo ente")
        _evento(s, seg, sess, "inoltrata", a_ente_id=dest.id, motivazione=body.motivazione.strip())
        seg.inoltri += 1
        if seg.inoltri > settings.max_inoltri:
            seg.ente_id, seg.stato = None, "da_smistare"
            s.add(Evento(segnalazione_id=seg.id, tipo="da_smistare", autore="sistema",
                         dettagli={"motivo": f"più di {settings.max_inoltri} inoltri", "proposto": dest.codice},
                         visibile_cittadino=True))
        else:
            seg.ente_id, seg.stato = dest.id, "nuova"
        s.commit()
        return {"id": seg.id, "stato": seg.stato, "visibile": _visibile(sess, seg)}


class CambioTipo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tipo: Literal["carcassa", "rifiuto"]


@api.post("/segnalazioni/{id_seg}/tipo")
def cambia_tipo(body: CambioTipo, request: Request, id_seg: int = IdSeg, sess: Sessione = Depends(modifica)) -> dict:
    with request.app.state.db.session() as s:
        seg = _carica(s, sess, id_seg)
        _aperta(seg)
        if body.tipo != seg.tipo:
            _evento(s, seg, sess, "tipo_cambiato", da=seg.tipo, a=body.tipo)
            seg.tipo = body.tipo
            s.commit()
        return _riga(seg)


class Assegnazione(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ente: str = Field(max_length=40)


@api.post("/segnalazioni/{id_seg}/assegna")
def assegna(body: Assegnazione, request: Request, id_seg: int = IdSeg, sess: Sessione = Depends(modifica)) -> dict:
    if sess.ruolo != "amministratore":
        raise HTTPException(status_code=403, detail="solo l'amministratore assegna le segnalazioni")
    with request.app.state.db.session() as s:
        seg = _carica(s, sess, id_seg)
        _aperta(seg)
        dest = s.scalar(select(Ente).where(Ente.codice == body.ente, Ente.attivo.is_(True)))
        if dest is None:
            raise HTTPException(status_code=422, detail="ente sconosciuto")
        _evento(s, seg, sess, "assegnata", a_ente_id=dest.id)
        seg.ente_id, seg.stato, seg.inoltri = dest.id, "nuova", 0
        s.commit()
        return _riga(seg)
