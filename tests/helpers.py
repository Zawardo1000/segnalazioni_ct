import json
from pathlib import Path

from sqlalchemy import Engine, text

CASI = Path(__file__).parent / "fixtures" / "casi"
MINI_OSM = Path(__file__).parent / "fixtures" / "mini.osm"


def caso(nome: str) -> dict:
    return json.loads((CASI / f"{nome}.json").read_text(encoding="utf-8"))


def crea_stradario(engine: Engine, vie: list[dict], confini: list[dict]) -> None:
    """Schema osm come lo crea osm2pgsql con osm/stradario.lua, riempito a mano.

    vie: [{"osm_way", "tags", "geometria": [[lat, lon], ...]}] (la forma dei casi di test)
    confini: [{"relation_id", "admin_level", "nome", "tags", "bbox": (ovest, sud, est, nord)}]
    """
    with engine.begin() as c:
        c.execute(text("CREATE SCHEMA osm"))
        c.execute(text(
            "CREATE TABLE osm.vie (way_id bigint NOT NULL, highway text NOT NULL, tags jsonb, "
            "geom geometry(LineString, 4326) NOT NULL)"
        ))
        c.execute(text("CREATE INDEX ON osm.vie USING gist (geom)"))
        c.execute(text(
            "CREATE TABLE osm.confini (relation_id bigint NOT NULL, admin_level int2 NOT NULL, nome text, tags jsonb, "
            "geom geometry(MultiPolygon, 4326) NOT NULL)"
        ))
        c.execute(text(
            "CREATE TABLE osm.importazione (aree text NOT NULL, fonti text NOT NULL, dati_del timestamptz, "
            "importato_il timestamptz NOT NULL, bbox jsonb, vie integer NOT NULL, confini integer NOT NULL)"
        ))
        for v in vie:
            if len(v["geometria"]) < 2:
                continue
            wkt = "LINESTRING(" + ", ".join(f"{lon} {lat}" for lat, lon in v["geometria"]) + ")"
            c.execute(
                text("INSERT INTO osm.vie VALUES (:id, :hw, CAST(:tags AS jsonb), ST_GeomFromText(:wkt, 4326))"),
                {"id": v["osm_way"], "hw": v["tags"].get("highway", ""), "tags": json.dumps(v["tags"]), "wkt": wkt},
            )
        for k in confini:
            ovest, sud, est, nord = k["bbox"]
            c.execute(
                text(
                    "INSERT INTO osm.confini VALUES (:id, :livello, :nome, CAST(:tags AS jsonb), "
                    "ST_Multi(ST_MakeEnvelope(:o, :s, :e, :n, 4326)))"
                ),
                {"id": k["relation_id"], "livello": k["admin_level"], "nome": k["nome"],
                 "tags": json.dumps(k.get("tags") or {}), "o": ovest, "s": sud, "e": est, "n": nord},
            )
        c.execute(text(
            "INSERT INTO osm.importazione VALUES ('prova', 'test', '2026-11-08T21:00:00Z', now(), NULL, "
            "(SELECT count(*) FROM osm.vie), (SELECT count(*) FROM osm.confini))"
        ))


def confini_catania(lat: float, lon: float, comune: str = "Acireale") -> list[dict]:
    """Comune (un quadrato di circa 1 km attorno al punto), Città metropolitana di Catania e Sicilia."""
    d = 0.005
    return [
        {"relation_id": 39290, "admin_level": 8, "nome": comune, "tags": {"name": comune},
         "bbox": (lon - d, lat - d, lon + d, lat + d)},
        {"relation_id": 39181, "admin_level": 6, "nome": "Catania", "tags": {"name": "Catania", "ref": "CT"},
         "bbox": (lon - 1, lat - 1, lon + 1, lat + 1)},
        {"relation_id": 39152, "admin_level": 4, "nome": "Sicilia", "tags": {"name": "Sicilia"},
         "bbox": (lon - 2, lat - 2, lon + 2, lat + 2)},
    ]
