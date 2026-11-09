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


def utm32(engine: Engine, lon: float, lat: float) -> tuple[float, float]:
    """Coordinate EPSG:32632 (quelle delle sezioni Istat) di un punto, calcolate da PostGIS."""
    with engine.connect() as c:
        r = c.execute(
            text("SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 32632) g"),
            {"lon": lon, "lat": lat},
        ).one()
    return r[0], r[1]


def scrivi_zip_istat(percorso: Path, engine: Engine, sezioni: list[dict], localita: dict[int, str]) -> Path:
    """Zip come quelli Istat delle Basi territoriali 2021 (SHP/Rxx_21_WGS84.shp e .dbf, TAB/LOC_Rxx_21.csv in UTF-16).

    sezioni: [{"regione", "pro_com", "sez_id", "tipo_loc", "loc_id", "pop", "rettangolo": (ovest, sud, est, nord)}]
    con il rettangolo in gradi; localita: {LOC21_ID: nome}.
    """
    import io
    import struct
    import zipfile

    campi = [("COD_REG", 10), ("PRO_COM", 10), ("SEZ21_ID", 13), ("TIPO_LOC", 10), ("LOC21_ID", 13), ("POP21", 13)]
    record_shp, record_dbf = [], []
    for i, s in enumerate(sezioni, start=1):
        o, su, e, n = s["rettangolo"]
        angoli = [utm32(engine, lon, lat) for lon, lat in ((o, su), (o, n), (e, n), (e, su), (o, su))]  # orario
        xs, ys = [p[0] for p in angoli], [p[1] for p in angoli]
        contenuto = struct.pack("<i4dii", 5, min(xs), min(ys), max(xs), max(ys), 1, len(angoli)) + struct.pack("<i", 0)
        contenuto += b"".join(struct.pack("<2d", x, y) for x, y in angoli)
        record_shp.append(struct.pack(">ii", i, len(contenuto) // 2) + contenuto)
        valori = (s["regione"], s["pro_com"], s["sez_id"], s["tipo_loc"], s["loc_id"], s["pop"])
        record_dbf.append(b" " + b"".join(str(v).rjust(lung).encode("ascii") for v, (_, lung) in zip(valori, campi)))
    corpo = b"".join(record_shp)
    shp = struct.pack(">i5ii", 9994, 0, 0, 0, 0, 0, (100 + len(corpo)) // 2) + struct.pack("<ii", 1000, 5)
    shp += struct.pack("<8d", 0, 0, 0, 0, 0, 0, 0, 0) + corpo
    lung_record = 1 + sum(lung for _, lung in campi)
    dbf = struct.pack("<B3BIHH20x", 3, 126, 11, 9, len(sezioni), 32 + 32 * len(campi) + 1, lung_record)
    for nome, lung in campi:
        dbf += struct.pack("<11sc4xBB14x", nome.encode("ascii"), b"N", lung, 0)
    dbf += b"\r" + b"".join(record_dbf) + b"\x1a"
    regione = sezioni[0]["regione"]
    tab = "COD_REG\tPRO_COM\tLOC21_ID\tTIPO_LOC\tNOME\n" + "".join(
        f"{regione}\t{loc_id // 100000}\t{loc_id}\t1\t{nome}\n" for loc_id, nome in localita.items()
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"SHP/R{regione:02d}_21_WGS84.shp", shp)
        z.writestr(f"SHP/R{regione:02d}_21_WGS84.dbf", dbf)
        z.writestr(f"TAB/LOC_R{regione:02d}_21.csv", tab.encode("utf-16"))
    percorso.write_bytes(buf.getvalue())
    return percorso
