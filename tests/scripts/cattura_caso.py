"""Cattura un caso di test dai servizi pubblici di OSM (Overpass e Nominatim), nella stessa forma dello stradario.

Serve per i punti fuori dallo stradario importato, o per fissare un caso verificato. I test poi girano senza rete su
questi file (tests/fixtures/casi/). Solo libreria standard, gira con qualsiasi Python:

    python tests/scripts/cattura_caso.py NOME LAT LON [--precisione M] [--descrizione "..."]

Per un punto già provato con la pagina basta invece: python -m segnalazioni prova N --caso
Dati © OpenStreetMap contributors (ODbL).
"""

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

UA = "segnalazioni-ct/0.1 (+https://github.com/Zawardo1000/segnalazioni_ct)"
OVERPASS = ["https://overpass.openstreetmap.fr/api/interpreter", "https://overpass-api.de/api/interpreter"]
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
CARTELLA = Path(__file__).resolve().parent.parent / "fixtures" / "casi"
RAGGIO_VIE_M = 150
RAGGIO_CONFINI_M = 150  # come abbinamento.RAGGIO_CONFINI_M


def _json(url: str, dati: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=dati, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def overpass(lat: float, lon: float) -> tuple[dict, str]:
    q = (
        f"[out:json][timeout:50];"
        f'way(around:{RAGGIO_VIE_M},{lat},{lon})["highway"];out tags geom;'
        f'rel(around:{RAGGIO_CONFINI_M},{lat},{lon})["boundary"="administrative"]["admin_level"="8"];out tags;'
    )
    for url in OVERPASS:
        try:
            return _json(url, urllib.parse.urlencode({"data": q}).encode()), url
        except Exception as e:  # server occupato o irraggiungibile: si prova il successivo
            print(f"{url}: {e}")
    raise SystemExit("nessun server Overpass risponde")


def nominatim(lat: float, lon: float) -> dict:
    par = {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "addressdetails": 1, "extratags": 1,
           "accept-language": "it"}
    return _json(f"{NOMINATIM}?{urllib.parse.urlencode(par)}")


def intorno(lat: float, lon: float) -> dict:
    ov, url = overpass(lat, lon)
    time.sleep(1.1)  # regole d'uso di Nominatim: al massimo una richiesta al secondo
    nom = nominatim(lat, lon)
    addr = nom.get("address") or {}
    comune = {
        "osm_relation": nom.get("osm_id") if nom.get("osm_type") == "relation" else None,
        "nome": nom.get("name"),
        "tags": nom.get("extratags") or {},
    }
    vie = [
        {"osm_way": e["id"], "tags": e.get("tags") or {}, "geometria": [[g["lat"], g["lon"]] for g in e.get("geometry", [])]}
        for e in ov["elements"]
        if e["type"] == "way"
    ]
    vicini = sorted({(e.get("tags") or {}).get("name") for e in ov["elements"] if e["type"] == "relation"} - {None})
    return {
        "vie": sorted(vie, key=lambda v: v["osm_way"]),
        "comune": comune,
        "provincia": {"nome": addr["county"], "tags": {"ISO3166-2": addr.get("ISO3166-2-lvl6")}} if addr.get("county") else None,
        "regione": {"nome": addr["state"]} if addr.get("state") else None,
        "comuni_vicini": [n for n in vicini if n != comune["nome"]],
        "fonte": {"tipo": "overpass+nominatim", "overpass": url, "letto_il": datetime.now(UTC).isoformat(timespec="seconds")},
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("nome")
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("--precisione", type=float, default=5.0)
    p.add_argument("--descrizione", default="")
    a = p.parse_args()
    caso = {
        "nome": a.nome,
        "descrizione": a.descrizione,
        "punto": {"lat": a.lat, "lon": a.lon, "precisione_m": a.precisione},
        "intorno": intorno(a.lat, a.lon),
    }
    CARTELLA.mkdir(parents=True, exist_ok=True)
    dest = CARTELLA / f"{a.nome}.json"
    dest.write_text(json.dumps(caso, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{dest}: {len(caso['intorno']['vie'])} strade, comune {caso['intorno']['comune']['nome']}, "
          f"vicini {caso['intorno']['comuni_vicini']}")


if __name__ == "__main__":
    main()
