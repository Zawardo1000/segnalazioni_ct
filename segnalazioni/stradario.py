"""Lettura dello stradario OSM in PostGIS (schema "osm", creato da importa_osm.py) attorno a un punto.

Il database fa solo la parte spaziale con gli indici (strade entro un raggio, confini che contengono il punto, comuni
vicini); le regole stanno in abbinamento.py, che riceve l'"intorno" in forma neutra.
"""

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from segnalazioni.abbinamento import BORDO_CENTRO_M, RAGGIO_CONFINI_M, SOGLIA_BASSA, metri_per_grado

SCHEMA = "osm"
RAGGIO_CARTELLI_M = 500  # cartelli di inizio/fine centro abitato
RAGGIO_LOCALITA_M = 2000  # località (place) con il nome e spesso la popolazione


class StradarioMancante(RuntimeError):
    """Lo stradario non è ancora stato importato."""


class FuoriArea(RuntimeError):
    """Il punto non è in nessun comune dello stradario."""


def raggio_ricerca(precisione_m: float | None) -> int:
    """Raggio delle strade da leggere: copre la distanza massima di abbinamento più la precisione del GPS."""
    p = precisione_m or 10.0
    return int(min(150, max(100, 2 * p + 40)))


def presente(session: Session) -> bool:
    return session.scalar(text("SELECT to_regclass('osm.vie') IS NOT NULL AND to_regclass('osm.confini') IS NOT NULL"))


def stato(session: Session) -> dict | None:
    if not presente(session):
        return None
    if session.scalar(text("SELECT to_regclass('osm.importazione')")) is None:
        return {}
    riga = session.execute(text("SELECT * FROM osm.importazione LIMIT 1")).mappings().first()
    if riga is None:
        return {}
    return {
        "aree": riga["aree"],
        "fonti": riga["fonti"],
        "dati_del": riga["dati_del"].isoformat() if riga["dati_del"] else None,
        "importato_il": riga["importato_il"].isoformat() if riga["importato_il"] else None,
        "bbox": riga["bbox"],
        "vie": riga["vie"],
        "confini": riga["confini"],
        "istat": riga.get("istat") or {"regioni": []},  # assente negli stradari importati prima delle sezioni Istat
    }


def _linea(geojson: str) -> list[list[float]]:
    g = json.loads(geojson)
    if g.get("type") == "LineString":
        return [[c[1], c[0]] for c in g["coordinates"]]
    if g.get("type") == "MultiLineString":
        return [[c[1], c[0]] for parte in g["coordinates"] for c in parte]
    return []


def leggi_intorno(session: Session, lat: float, lon: float, raggio_m: int) -> dict:
    if not presente(session):
        raise StradarioMancante("stradario non importato (python -m segnalazioni importa-osm)")
    m_lat, m_lon = metri_per_grado(lat)
    par = {
        "lat": lat,
        "lon": lon,
        "r": raggio_m,
        "dx": raggio_m / m_lon,
        "dy": raggio_m / m_lat,
        "rc": RAGGIO_CONFINI_M,
        "dxc": RAGGIO_CONFINI_M / m_lon,
        "dyc": RAGGIO_CONFINI_M / m_lat,
    }
    punto = "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)"

    confini = session.execute(
        text(
            f"SELECT relation_id, admin_level, nome, tags FROM osm.confini "
            f"WHERE geom && {punto} AND ST_Covers(geom, {punto}) ORDER BY admin_level"
        ),
        par,
    ).mappings().all()
    per_livello = {c["admin_level"]: c for c in confini}
    comune = per_livello.get(8)
    if comune is None:
        raise FuoriArea("punto fuori dai comuni dello stradario")

    vie = session.execute(
        text(
            f"SELECT way_id, tags, ST_AsGeoJSON(geom, 7) AS geojson FROM osm.vie "
            f"WHERE geom && ST_Expand({punto}, :dx, :dy) "
            f"AND ST_DWithin(geom::geography, {punto}::geography, :r)"
        ),
        par,
    ).mappings().all()
    vicini = session.scalars(
        text(
            f"SELECT DISTINCT nome FROM osm.confini WHERE admin_level = 8 "
            f"AND geom && ST_Expand({punto}, :dxc, :dyc) AND NOT ST_Covers(geom, {punto}) "
            f"AND ST_DWithin(geom::geography, {punto}::geography, :rc)"
        ),
        par,
    ).all()

    def confine(c) -> dict | None:
        if c is None:
            return None
        return {"osm_relation": c["relation_id"], "nome": c["nome"], "tags": c["tags"] or {}}

    st = stato(session) or {}
    per_via = _percorsi(session, [v["way_id"] for v in vie])
    return {
        "vie": [
            {"osm_way": v["way_id"], "tags": v["tags"] or {}, "geometria": _linea(v["geojson"]),
             "percorsi": per_via.get(v["way_id"], [])}
            for v in vie
        ],
        "comune": confine(comune),
        "provincia": confine(per_livello.get(6)),
        "regione": confine(per_livello.get(4)),
        "comuni_vicini": [n for n in vicini if n],
        **_punti_vicini(session, lat, lon),
        "istat": _centri(session, lat, lon, (st.get("istat") or {}).get("regioni") or [],
                         ((comune or {}).get("tags") or {}).get("ref:ISTAT")),
        "fonte": {"tipo": "stradario", "dati_del": st.get("dati_del"), "importato_il": st.get("importato_il")},
    }


def _percorsi(session: Session, way_ids: list[int]) -> dict[int, list[dict]]:
    """Percorsi ciclabili (relazioni OSM) di cui fanno parte le strade (tabella osm.percorsi; assente negli stradari
    più vecchi): {way_id: [{"tipo", "nome", "tags"}]}."""
    if not way_ids or session.scalar(text("SELECT to_regclass('osm.percorsi')")) is None:
        return {}
    uscita: dict[int, list[dict]] = {}
    righe = session.execute(
        text("SELECT way_id, tipo, nome, tags FROM osm.percorsi WHERE way_id = ANY(:ids) ORDER BY relation_id"),
        {"ids": way_ids},
    ).mappings()
    for r in righe:
        uscita.setdefault(r["way_id"], []).append({"tipo": r["tipo"], "nome": r["nome"], "tags": r["tags"] or {}})
    return uscita


def _centri(session: Session, lat: float, lon: float, regioni: list[int], ref_comune: str | None = None) -> dict:
    """Centri abitati (tabella osm.centri, istat.py) che contengono il punto, prima quelli dei cartelli, i centri
    Istat grandi appena fuori e gli abitanti del Comune del punto (ref_comune: il suo ref:ISTAT in OSM).
    regioni: le regioni (codici Istat) di cui ci sono le sezioni."""
    vuoto = {"regioni": regioni, "centri": [], "vicini": [], "abitanti_comune": None}
    if not regioni or session.scalar(text("SELECT to_regclass('osm.centri')")) is None:
        return vuoto
    abitanti_comune = None
    ref = (ref_comune or "").strip()
    if ref.isdigit() and len(ref) <= 6 and session.scalar(text("SELECT to_regclass('osm.comuni_istat')")) is not None:
        abitanti_comune = session.scalar(
            text("SELECT popolazione FROM osm.comuni_istat WHERE pro_com = :p"), {"p": int(ref)}
        )
    m_lat, m_lon = metri_per_grado(lat)
    punto = "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)"
    par = {"lat": lat, "lon": lon, "r": BORDO_CENTRO_M, "dx": BORDO_CENTRO_M / m_lon, "dy": BORDO_CENTRO_M / m_lat,
           "soglia": SOGLIA_BASSA}
    campi = "fonte, nome, pro_com, popolazione, popolazione_localita, popolazione_osm, localita_istat, diviso, tipo_loc"
    dentro = session.execute(
        text(
            f"SELECT {campi}, CASE WHEN fonte = 'istat' THEN "
            f"round(ST_Distance(ST_Boundary(geom)::geography, {punto}::geography)) END AS dal_bordo_m "
            f"FROM osm.centri WHERE geom && {punto} AND ST_Covers(geom, {punto}) "
            f"ORDER BY fonte = 'cartelli' DESC, ST_Area(geom) LIMIT 4"
        ),
        par,
    ).mappings().all()
    vicini = session.execute(
        text(
            f"SELECT {campi}, round(ST_Distance(geom::geography, {punto}::geography)) AS distanza_m "
            f"FROM osm.centri WHERE fonte = 'istat' AND popolazione >= :soglia "
            f"AND geom && ST_Expand({punto}, :dx, :dy) AND NOT ST_Covers(geom, {punto}) "
            f"AND ST_DWithin(geom::geography, {punto}::geography, :r) ORDER BY distanza_m LIMIT 2"
        ),
        par,
    ).mappings().all()

    def pulito(r) -> dict:
        return {k: (int(v) if k in ("dal_bordo_m", "distanza_m") and v is not None else v) for k, v in r.items()}

    return {"regioni": regioni, "centri": [pulito(r) for r in dentro], "vicini": [pulito(r) for r in vicini],
            "abitanti_comune": abitanti_comune}


def _punti_vicini(session: Session, lat: float, lon: float) -> dict:
    """Cartelli di centro abitato e località più vicini (tabella osm.punti; assente negli stradari più vecchi)."""
    if session.scalar(text("SELECT to_regclass('osm.punti')")) is None:
        return {"cartelli": [], "localita": []}
    m_lat, m_lon = metri_per_grado(lat)
    punto = "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)"
    risultato = {}
    for tipo, chiave, raggio, quanti in (("cartello", "cartelli", RAGGIO_CARTELLI_M, 4),
                                         ("localita", "localita", RAGGIO_LOCALITA_M, 4)):
        righe = session.execute(
            text(
                f"SELECT nome, tags, ST_Y(geom) AS lat, ST_X(geom) AS lon, "
                f"ST_Distance(geom::geography, {punto}::geography) AS d FROM osm.punti "
                f"WHERE tipo = :tipo AND geom && ST_Expand({punto}, :dx, :dy) "
                f"AND ST_DWithin(geom::geography, {punto}::geography, :r) ORDER BY d LIMIT :n"
            ),
            {"lat": lat, "lon": lon, "tipo": tipo, "r": raggio, "dx": raggio / m_lon, "dy": raggio / m_lat, "n": quanti},
        ).mappings().all()
        risultato[chiave] = [
            {"nome": r["nome"], "tags": r["tags"] or {}, "lat": r["lat"], "lon": r["lon"], "distanza_m": round(r["d"])}
            for r in righe
        ]
    return risultato
