"""API della pagina di prova dell'abbinamento (/api/v1, tutte con la chiave di accesso nell'header X-Chiave)."""

import hmac
import logging
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from geoalchemy2 import WKTElement
from pydantic import BaseModel, ConfigDict, Field

from segnalazioni import abbinamento, stradario
from segnalazioni.db import utcnow
from segnalazioni.models import Prova
from segnalazioni.sicurezza import richiedi_chiave
from segnalazioni.versione import versione

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", dependencies=[Depends(richiedi_chiave)])


class RichiestaAbbina(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    precisione_m: float | None = Field(default=None, ge=0, le=100_000, allow_inf_nan=False)
    origine: Literal["gps", "mappa"] = "gps"


BIGINT_MAX = 2**63 - 1  # oltre, Postgres rifiuta il valore (errore 500 invece di 422)


class RichiestaEsito(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=16, max_length=64)  # il codice ricevuto con il risultato dell'abbinamento
    esito: Literal["giusto", "sbagliato"]
    osm_way_scelto: int | None = Field(default=None, ge=1, le=BIGINT_MAX)
    ente_corretto: str | None = Field(default=None, max_length=200)
    nota: str | None = Field(default=None, max_length=2000)


@router.post("/abbina")
def abbina(body: RichiestaAbbina, request: Request) -> dict:
    """Calcola l'abbinamento del punto e lo salva come prova (con i dati letti, se SALVA_FONTI)."""
    db = request.app.state.db
    settings = request.app.state.settings
    if not request.app.state.limite_abbinamenti.consenti():
        raise HTTPException(status_code=429, detail="troppe richieste: riprova tra un minuto")
    with db.session() as s:
        try:
            intorno = stradario.leggi_intorno(s, body.lat, body.lon, stradario.raggio_ricerca(body.precisione_m))
        except stradario.StradarioMancante:
            raise HTTPException(status_code=503, detail="stradario non ancora importato") from None
        except stradario.FuoriArea:
            intorno = None
        if intorno is None:
            risultato = abbinamento.fuori_area(body.lat, body.lon, body.precisione_m, settings.nome_aree)
        else:
            risultato = abbinamento.abbina(body.lat, body.lon, body.precisione_m, intorno, origine=body.origine)
        ente = risultato.get("ente") or risultato.get("ente_suggerito")
        token = secrets.token_urlsafe(16)
        prova = Prova(
            token_esito=token,
            lat=body.lat,
            lon=body.lon,
            precisione_m=body.precisione_m,
            origine=body.origine,
            punto=WKTElement(f"POINT({body.lon} {body.lat})", srid=4326),
            esito_calcolo=risultato["esito"],
            ente_calcolato=ente["nome"] if ente else None,
            risultato={k: v for k, v in risultato.items() if k != "candidati"}
            | {"candidati": [{k: v for k, v in c.items() if k != "geometria"} for c in risultato["candidati"]]},
            fonti=intorno if (intorno is not None and settings.salva_fonti) else None,
        )
        s.add(prova)
        s.commit()
        risultato["id_prova"] = prova.id
        risultato["token_esito"] = token
    risultato["versione"] = versione()
    log.info("prova %s: %s, %s", risultato["id_prova"], risultato["esito"], ente["nome"] if ente else "-")
    return risultato


@router.post("/prove/{id_prova}/esito")
def esito(body: RichiestaEsito, request: Request, id_prova: int = Path(ge=1, le=BIGINT_MAX)) -> dict:
    """Risposta dell'utente sulla prova: giusto o sbagliato, con la strada scelta e l'ente corretto.

    Solo chi ha fatto la prova (ha il suo codice) può salvarne l'esito.
    """
    with request.app.state.db.session() as s:
        prova = s.get(Prova, id_prova)
        if prova is None:
            raise HTTPException(status_code=404, detail="prova non trovata")
        if not prova.token_esito or not hmac.compare_digest(prova.token_esito.encode(), body.token.encode()):
            raise HTTPException(status_code=403, detail="codice della prova sbagliato")
        prova.esito = body.esito
        prova.osm_way_scelto = body.osm_way_scelto
        prova.ente_corretto = (body.ente_corretto or "").strip() or None
        prova.nota = (body.nota or "").strip() or None
        prova.esito_il = utcnow()
        s.commit()
    return {"ok": True}


@router.get("/stato")
def stato(request: Request) -> dict:
    with request.app.state.db.session() as s:
        return {"versione": versione(), "stradario": stradario.stato(s), "aree": request.app.state.settings.nome_aree}
