"""Lettura dello stradario OSM in PostGIS (schema "osm", creato da importa_osm.py) attorno a un punto.

Il database fa solo la parte spaziale con gli indici (strade entro un raggio, confini che contengono il punto, comuni
vicini); le regole stanno in abbinamento.py, che riceve l'"intorno" in forma neutra.
"""

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from segnalazioni.abbinamento import RAGGIO_CONFINI_M, metri_per_grado

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
    riga = session.execute(
        text("SELECT aree, fonti, dati_del, importato_il, bbox, vie, confini FROM osm.importazione LIMIT 1")
    ).mappings().first()
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
