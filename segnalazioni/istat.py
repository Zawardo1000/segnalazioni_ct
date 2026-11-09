"""Sezioni di censimento Istat 2021 (Basi territoriali) e centri abitati, per la regola dei 10.000 abitanti.

Art. 2 c. 7 CdS: dentro un centro abitato con più di 10.000 abitanti il tratto di una strada statale, regionale o
provinciale è del Comune. Conta il centro abitato, non il Comune intero (docs/PROGETTO.md §14). Istat fonde i centri
contigui (Acireale è un centro da 48 mila abitanti con dentro Capomulini, Santa Tecla, ...), ma le sezioni di censimento
hanno ognuna la sua popolazione: all'importazione si sommano per ricostruire i centri.

1. per ogni regione delle aree (AreaOSM.istat_regioni) si scarica lo zip della regione (R19_21.zip per la Sicilia,
   circa 60 MB): shapefile delle sezioni in UTM 32N (EPSG:32632) con POP21, TIPO_LOC (1 centro, 2 nucleo), LOC21_ID;
   TAB/LOC_Rxx_21.csv con i nomi delle località;
2. si caricano le sezioni dei centri e dei nuclei che toccano i riquadri delle aree (tabella sezioni);
3. centri (tabella centri): ogni centro Istat è un centro; quelli che potrebbero superare la soglia (somma di almeno
   SOGLIA_DIVISIONE abitanti) e contengono più località OSM (place=city/town/village/hamlet) si dividono tra le
   località: ogni sezione va alla località con la distanza più piccola rispetto al suo "raggio", proporzionale alla
   radice della popolazione (a densità uguale l'area cresce con gli abitanti). Così Capomulini resta un centro da
   qualche centinaio di abitanti e non si confonde con Acireale. I cartelli di centro abitato con il nome della
   località (OSM) dicono se il Comune l'ha delimitata a parte.

La regola (quale ente) sta in abbinamento.py; qui solo i dati. Lettori di shapefile e dbf con la libreria standard: le
sezioni sono poligoni semplici e i campi sono numeri.
"""

import csv
import io
import logging
import struct
import zipfile
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)

URL_REGIONE = "https://www.istat.it/storage/cartografia/basi_territoriali/2021/R{regione:02d}_21.zip"
SRID_ISTAT = 32632  # WGS84 / UTM 32N, anche per la Sicilia
TIPO_CENTRO = 1
TIPO_NUCLEO = 2
# i centri Istat da almeno questi abitanti si dividono tra le località OSM che contengono (sotto, qualunque divisione
# darebbe centri più piccoli, e la regola non cambia)
SOGLIA_DIVISIONE = 9_500
DISTANZA_LOCALITA_M = 150  # una località OSM appartiene a un centro Istat se è dentro o a meno di tanto
# due cartelli di centro abitato con lo stesso nome delimitano un tratto di centro se distano tra MIN e MAX (più vicini
# sono i due lati dello stesso varco; più lontani di solito sono ingressi diversi di una città, che coprono le sezioni)
MIN_COPPIA_CARTELLI_M = 60
MAX_COPPIA_CARTELLI_M = 2_000
# popolazione supposta delle località OSM senza population, per il loro "raggio"
POPOLAZIONE_SUPPOSTA = {"city": 100_000, "town": 8_000, "village": 1_000, "hamlet": 150}

REGIONI = {
    1: "Piemonte", 2: "Valle d'Aosta", 3: "Lombardia", 4: "Trentino-Alto Adige", 5: "Veneto",
    6: "Friuli-Venezia Giulia", 7: "Liguria", 8: "Emilia-Romagna", 9: "Toscana", 10: "Umbria", 11: "Marche",
    12: "Lazio", 13: "Abruzzo", 14: "Molise", 15: "Campania", 16: "Puglia", 17: "Basilicata", 18: "Calabria",
    19: "Sicilia", 20: "Sardegna",
}


# --- lettura dei file Istat -------------------------------------------------------------------------------------

def _esatti(f, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise ValueError("file troncato")
    return b


def leggi_dbf(f) -> Iterator[dict[str, str] | None]:
    """Record di un file dbf come {campo: testo}; None per i record cancellati (restano allineati con lo shp)."""
    h = _esatti(f, 32)
    n, lung_intestazione, lung_record = struct.unpack("<IHH", h[4:12])
    campi: list[tuple[str, int]] = []
    letti = 32
    while True:
        d = _esatti(f, 1)
        letti += 1
        if d == b"\r":
            break
        d += _esatti(f, 31)
        letti += 31
        campi.append((d[:11].split(b"\0")[0].decode("ascii"), d[16]))
    if lung_intestazione > letti:
        _esatti(f, lung_intestazione - letti)
    for _ in range(n):
        r = _esatti(f, lung_record)
        if r[:1] == b"*":
            yield None
            continue
        valori, pos = {}, 1
        for nome, lung in campi:
            valori[nome] = r[pos:pos + lung].decode("latin-1").strip()
            pos += lung
        yield valori


def leggi_shp(f) -> Iterator[bytes | None]:
    """Contenuto grezzo dei record di uno shapefile di poligoni (None per le forme vuote): si decodifica solo quello
    che serve, con anelli_wkb()."""
    h = _esatti(f, 100)
    if struct.unpack(">i", h[:4])[0] != 9994:
        raise ValueError("non è uno shapefile")
    tipo = struct.unpack("<i", h[32:36])[0]
    if tipo not in (5, 15, 25):
        raise ValueError(f"shapefile di tipo {tipo}: servono poligoni")
    while True:
        rh = f.read(8)
        if not rh:
            return
        if len(rh) != 8:
            raise ValueError("file troncato")
        parole = struct.unpack(">ii", rh)[1]
        contenuto = _esatti(f, parole * 2)
        yield None if struct.unpack_from("<i", contenuto)[0] == 0 else contenuto


def anelli_wkb(contenuto: bytes) -> bytes:
    """Gli anelli di un poligono shapefile come MULTILINESTRING in WKB (little endian): PostGIS ne ricava il poligono
    con ST_BuildArea, buchi compresi. I punti dello shapefile sono già double little endian: si copiano."""
    n_parti, n_punti = struct.unpack_from("<ii", contenuto, 36)
    parti = struct.unpack_from(f"<{n_parti}i", contenuto, 44)
    inizio_punti = 44 + 4 * n_parti
    limiti = [*parti, n_punti]
    pezzi = [struct.pack("<BII", 1, 5, n_parti)]
    for a, b in zip(limiti, limiti[1:]):
        pezzi.append(struct.pack("<BII", 1, 2, b - a))
        pezzi.append(contenuto[inizio_punti + 16 * a:inizio_punti + 16 * b])
    return b"".join(pezzi)


def _membro(z: zipfile.ZipFile, suffisso: str) -> str:
    for nome in z.namelist():
        if nome.lower().endswith(suffisso.lower()):
            return nome
    raise ValueError(f"nello zip Istat manca il file *{suffisso}")


def nomi_localita(z: zipfile.ZipFile) -> dict[int, str]:
    """LOC21_ID → nome, da TAB/LOC_Rxx_21.csv (UTF-16, separato da tabulazioni)."""
    try:
        nome = next(n for n in z.namelist() if n.upper().rsplit("/", 1)[-1].startswith("LOC_") and n.endswith(".csv"))
    except StopIteration:
        return {}
    testo = z.read(nome)
    codifica = "utf-16" if testo[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    righe = csv.DictReader(io.StringIO(testo.decode(codifica)), delimiter="\t")
    uscita = {}
    for r in righe:
        try:
            uscita[int(r["LOC21_ID"])] = (r.get("NOME") or "").strip()
        except (KeyError, TypeError, ValueError):
            continue
    return uscita


def sezioni(zip_path: Path) -> Iterator[dict]:
    """Sezioni di uno zip regionale: sez_id, pro_com, loc_id, tipo_loc, popolazione e, solo per centri e nuclei, wkb
    (anelli in EPSG:32632) e bbox. Delle altre (case sparse, località produttive) basta la popolazione."""
    with zipfile.ZipFile(zip_path) as z:
        shp, dbf = _membro(z, ".shp"), _membro(z, ".dbf")
        with z.open(shp) as fs, z.open(dbf) as fd:
            for contenuto, attr in zip(leggi_shp(fs), leggi_dbf(fd), strict=True):
                if attr is None:
                    continue
                s = {
                    "regione": int(attr["COD_REG"]),
                    "sez_id": int(attr["SEZ21_ID"]),
                    "pro_com": int(attr["PRO_COM"]),
                    "loc_id": int(attr["LOC21_ID"] or 0),
                    "tipo_loc": int(attr["TIPO_LOC"] or 0),
                    "popolazione": int(float(attr["POP21"] or 0)),
                    "wkb": None,
                    "bbox": None,
                }
                if contenuto is not None and s["tipo_loc"] in (TIPO_CENTRO, TIPO_NUCLEO):
                    s["wkb"] = anelli_wkb(contenuto)
                    s["bbox"] = struct.unpack_from("<4d", contenuto, 4)
                yield s


# --- importazione in PostGIS ------------------------------------------------------------------------------------

_LETTERE_DA, _LETTERE_A = "ÀÁÈÉÌÍÒÓÙÚÇàáèéìíòóùúç", "AAEEIIOOUUCaaeeiioouuc"
_SEGNI = "'’`-.,()/"


def _chiave(x: str) -> str:
    """Espressione SQL: nome confrontabile (senza accenti, apostrofi e punteggiatura, minuscolo, spazi singoli)."""
    da = (_LETTERE_DA + _SEGNI).replace("'", "''")
    a = _LETTERE_A + " " * len(_SEGNI)
    return f"trim(regexp_replace(lower(translate({x}, '{da}', '{a}')), '\\s+', ' ', 'g'))"


def importa(c, schema: str, zips: list[Path], riquadri: list[tuple[float, float, float, float]]) -> dict:
    """Carica le sezioni degli zip regionali e costruisce i centri, nello schema dell'importazione (osm_import).

    c: connessione SQLAlchemy in una transazione; servono le tabelle punti e confini di osm2pgsql.
    riquadri: (ovest, sud, est, nord) delle aree: si tengono le sezioni che li toccano.
    """
    s = schema
    c.exec_driver_sql(
        f"CREATE UNLOGGED TABLE {s}.sezioni_grezze (sez_id bigint, pro_com integer, loc_id bigint, tipo_loc smallint, "
        "popolazione integer, wkb bytea, x0 float8, y0 float8, x1 float8, y1 float8)"
    )
    c.exec_driver_sql(f"CREATE TABLE {s}.localita_nomi (loc_id bigint PRIMARY KEY, nome text)")
    # abitanti di ogni Comune delle regioni (tutte le sezioni, anche le case sparse e fuori dai riquadri): nessun suo
    # centro abitato ne ha di più
    c.exec_driver_sql(f"CREATE TABLE {s}.comuni_istat (pro_com integer PRIMARY KEY, popolazione integer NOT NULL)")
    grezzo = c.connection.driver_connection
    lette, popolazione = 0, 0
    regioni: set[int] = set()
    nomi: dict[int, str] = {}
    comuni: dict[int, int] = {}
    with grezzo.cursor() as cur:
        with cur.copy(f"COPY {s}.sezioni_grezze FROM STDIN") as cp:
            for z in zips:
                for sez in sezioni(z):
                    lette += 1
                    popolazione += sez["popolazione"]
                    regioni.add(sez["regione"])
                    comuni[sez["pro_com"]] = comuni.get(sez["pro_com"], 0) + sez["popolazione"]
                    if sez["wkb"] is not None:
                        cp.write_row((sez["sez_id"], sez["pro_com"], sez["loc_id"], sez["tipo_loc"],
                                      sez["popolazione"], sez["wkb"], *sez["bbox"]))
        for z in zips:
            with zipfile.ZipFile(z) as zf:
                nomi.update(nomi_localita(zf))
        with cur.copy(f"COPY {s}.localita_nomi FROM STDIN") as cp:
            for loc_id, nome in nomi.items():
                cp.write_row((loc_id, nome))
        with cur.copy(f"COPY {s}.comuni_istat FROM STDIN") as cp:
            for pro_com, abitanti in comuni.items():
                cp.write_row((pro_com, abitanti))
    if not lette:
        raise RuntimeError("negli zip Istat non ci sono sezioni")

    dentro = " OR ".join(
        f"ST_Intersects(ST_MakeEnvelope(x0, y0, x1, y1, {SRID_ISTAT}), "
        f"ST_Transform(ST_MakeEnvelope({o}, {su}, {e}, {n}, 4326), {SRID_ISTAT}))"
        for o, su, e, n in riquadri
    ) or "true"
    passi = [
        # sezioni di centri e nuclei nei riquadri; gli anelli diventano poligoni (con i buchi) con ST_BuildArea
        f"""CREATE TABLE {s}.sezioni AS
            SELECT sez_id, pro_com, loc_id, tipo_loc, popolazione, NULL::integer AS centro_id,
                   ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Transform(
                       ST_BuildArea(ST_SetSRID(ST_GeomFromWKB(wkb), {SRID_ISTAT})), 4326)), 3))
                       ::geometry(MultiPolygon, 4326) AS geom
            FROM {s}.sezioni_grezze WHERE {dentro}""",
        f"DROP TABLE {s}.sezioni_grezze",
        f"DELETE FROM {s}.sezioni WHERE geom IS NULL OR ST_IsEmpty(geom)",
        f"ALTER TABLE {s}.sezioni ADD PRIMARY KEY (sez_id)",
        f"CREATE INDEX ON {s}.sezioni USING gist (geom)",
        f"ANALYZE {s}.sezioni",
        # località Istat (centri e nuclei): sezioni unite, abitanti sommati
        f"""CREATE TEMP TABLE localita ON COMMIT DROP AS
            SELECT loc_id, min(pro_com) AS pro_com, min(tipo_loc) AS tipo_loc, sum(popolazione)::integer AS popolazione,
                   ST_Multi(ST_CollectionExtract(ST_Union(geom), 3)) AS geom
            FROM {s}.sezioni GROUP BY loc_id""",
        "CREATE INDEX ON localita USING gist (geom)",
        # località OSM dentro i centri Istat (o a meno di DISTANZA_LOCALITA_M), nel Comune del centro
        f"""CREATE TEMP TABLE localita_osm ON COMMIT DROP AS
            SELECT DISTINCT ON (l.loc_id, p.node_id) l.loc_id, p.node_id, p.nome, p.tags->>'place' AS place,
                   CASE WHEN p.tags->>'population' ~ '^[0-9]{{1,8}}$' THEN (p.tags->>'population')::integer END
                       AS popolazione_osm, p.geom, 0::float8 AS peso
            FROM localita l
            JOIN {s}.punti p ON p.tipo = 'localita' AND p.nome IS NOT NULL
                 AND p.tags->>'place' IN ('city', 'town', 'village', 'hamlet')
                 AND p.geom && ST_Expand(l.geom, 0.005)
                 AND ST_DWithin(p.geom::geography, l.geom::geography, {DISTANZA_LOCALITA_M})
            JOIN {s}.confini co ON co.admin_level = 8 AND co.tags->>'ref:ISTAT' ~ '^[0-9]{{1,6}}$'
                 AND (co.tags->>'ref:ISTAT')::integer = l.pro_com AND co.geom && p.geom AND ST_Covers(co.geom, p.geom)
            WHERE l.tipo_loc = {TIPO_CENTRO}""",
        f"""UPDATE localita_osm SET peso = sqrt(coalesce(nullif(popolazione_osm, 0), CASE place
                {" ".join(f"WHEN '{k}' THEN {v}" for k, v in POPOLAZIONE_SUPPOSTA.items())} END))""",
        f"""CREATE TEMP TABLE divise ON COMMIT DROP AS
            SELECT lo.loc_id FROM localita_osm lo JOIN localita l USING (loc_id)
            WHERE l.popolazione >= {SOGLIA_DIVISIONE} GROUP BY lo.loc_id HAVING count(*) >= 2""",
        # ogni sezione dei centri da dividere va alla località più "vicina" (distanza / radice della popolazione)
        f"""CREATE TEMP TABLE assegna ON COMMIT DROP AS
            SELECT DISTINCT ON (z.sez_id) z.sez_id, z.loc_id, z.popolazione, lo.node_id, false AS seme
            FROM {s}.sezioni z JOIN localita_osm lo USING (loc_id)
            WHERE z.loc_id IN (SELECT loc_id FROM divise)
            ORDER BY z.sez_id, ST_Distance(ST_PointOnSurface(z.geom)::geography, lo.geom::geography) / lo.peso""",
        # la sezione che contiene la località (o la più vicina) è sua: anche il borgo più piccolo ha la sua sezione
        f"""WITH vicina AS (
                SELECT DISTINCT ON (lo.node_id) lo.node_id, z.sez_id, lo.peso
                FROM localita_osm lo JOIN {s}.sezioni z USING (loc_id)
                WHERE lo.loc_id IN (SELECT loc_id FROM divise)
                ORDER BY lo.node_id, z.geom <-> lo.geom
            ), seme AS (SELECT DISTINCT ON (sez_id) sez_id, node_id FROM vicina ORDER BY sez_id, peso DESC)
            UPDATE assegna a SET node_id = seme.node_id, seme = true FROM seme WHERE a.sez_id = seme.sez_id""",
        "CREATE INDEX ON assegna (sez_id)",
        # sezioni senza abitanti (strade, verde): con la sezione abitata più vicina dello stesso centro
        f"""UPDATE assegna a SET node_id = v.node_id FROM (
                SELECT a0.sez_id, (
                    SELECT b.node_id FROM {s}.sezioni zb JOIN assegna b ON b.sez_id = zb.sez_id
                    WHERE zb.loc_id = a0.loc_id AND zb.popolazione > 0 ORDER BY zb.geom <-> z0.geom LIMIT 1
                ) AS node_id
                FROM assegna a0 JOIN {s}.sezioni z0 ON z0.sez_id = a0.sez_id
                WHERE a0.popolazione = 0 AND NOT a0.seme
            ) v WHERE a.sez_id = v.sez_id AND v.node_id IS NOT NULL""",
        f"""CREATE TABLE {s}.centri (
                id serial PRIMARY KEY,
                fonte text NOT NULL,               -- istat (sezioni) o cartelli (cartelli OSM con lo stesso nome)
                nome text,
                pro_com integer,
                loc_id bigint,                     -- località Istat
                tipo_loc smallint,                 -- 1 centro, 2 nucleo
                node_id bigint,                    -- località OSM (place)
                popolazione integer,               -- abitanti stimati del centro (sezioni Istat 2021)
                popolazione_localita integer,      -- abitanti della località Istat intera (prima della divisione)
                popolazione_osm integer,           -- population della località OSM, per confronto
                localita_istat text,               -- nome della località Istat
                diviso boolean NOT NULL DEFAULT false,
                geom geometry(MultiPolygon, 4326) NOT NULL
            )""",
        f"""INSERT INTO {s}.centri (fonte, nome, pro_com, loc_id, tipo_loc, node_id, popolazione, popolazione_localita,
                                   popolazione_osm, localita_istat, geom)
            SELECT 'istat', coalesce(n.nome, princ.nome), l.pro_com, l.loc_id, l.tipo_loc, princ.node_id, l.popolazione,
                   l.popolazione, princ.popolazione_osm, n.nome, l.geom
            FROM localita l LEFT JOIN {s}.localita_nomi n USING (loc_id)
            LEFT JOIN LATERAL (SELECT lo.node_id, lo.nome, lo.popolazione_osm FROM localita_osm lo
                               WHERE lo.loc_id = l.loc_id ORDER BY lo.peso DESC LIMIT 1) princ ON true
            WHERE l.loc_id NOT IN (SELECT loc_id FROM divise) AND NOT ST_IsEmpty(l.geom)""",
        f"""INSERT INTO {s}.centri (fonte, nome, pro_com, loc_id, tipo_loc, node_id, popolazione, popolazione_localita,
                                   popolazione_osm, localita_istat, diviso, geom)
            SELECT 'istat', lo.nome, l.pro_com, l.loc_id, {TIPO_CENTRO}, lo.node_id, sum(a.popolazione), l.popolazione,
                   lo.popolazione_osm, n.nome, true, ST_Multi(ST_CollectionExtract(ST_Union(z.geom), 3))
            FROM assegna a JOIN {s}.sezioni z USING (sez_id)
            JOIN localita_osm lo ON lo.loc_id = a.loc_id AND lo.node_id = a.node_id
            JOIN localita l ON l.loc_id = a.loc_id LEFT JOIN {s}.localita_nomi n ON n.loc_id = a.loc_id
            GROUP BY l.loc_id, l.pro_com, l.popolazione, lo.node_id, lo.nome, lo.popolazione_osm, n.nome""",
        f"""UPDATE {s}.sezioni z SET centro_id = c.id FROM {s}.centri c
            WHERE c.fonte = 'istat' AND NOT c.diviso AND c.loc_id = z.loc_id""",
        f"""UPDATE {s}.sezioni z SET centro_id = c.id FROM assegna a, {s}.centri c
            WHERE a.sez_id = z.sez_id AND c.fonte = 'istat' AND c.diviso AND c.loc_id = a.loc_id
              AND c.node_id = a.node_id""",
        # centri delimitati dai cartelli: tra due cartelli con lo stesso nome (non lo stesso varco, non troppo lontani)
        # si è dentro il centro. Area: ellisse con i due cartelli come fuochi, un po' più larga della strada.
        f"""CREATE TEMP TABLE cartelli ON COMMIT DROP AS
            SELECT node_id, nome, {_chiave('nome')} AS chiave, ST_Transform(geom, {SRID_ISTAT}) AS g
            FROM {s}.punti WHERE tipo = 'cartello' AND nome IS NOT NULL""",
        "DELETE FROM cartelli WHERE chiave = ''",
        "CREATE INDEX ON cartelli USING gist (g)",
        f"""CREATE TEMP TABLE ellissi ON COMMIT DROP AS
            SELECT chiave, nome, ST_SetSRID(ST_Translate(ST_Rotate(ST_Scale(ST_Buffer(ST_MakePoint(0, 0), 1, 12),
                       somma / 2, sqrt(power(somma / 2, 2) - power(l / 2, 2))),
                       atan2(ST_Y(gb) - ST_Y(ga), ST_X(gb) - ST_X(ga))),
                       (ST_X(ga) + ST_X(gb)) / 2, (ST_Y(ga) + ST_Y(gb)) / 2), {SRID_ISTAT}) AS g
            FROM (
                SELECT a.chiave, a.nome, a.g AS ga, b.g AS gb, ST_Distance(a.g, b.g) AS l,
                       ST_Distance(a.g, b.g) + least(0.2 * ST_Distance(a.g, b.g), 150) + 30 AS somma
                FROM cartelli a JOIN cartelli b ON a.chiave = b.chiave AND a.node_id < b.node_id
                     AND ST_DWithin(a.g, b.g, {MAX_COPPIA_CARTELLI_M}) AND NOT ST_DWithin(a.g, b.g, {MIN_COPPIA_CARTELLI_M})
            ) coppie""",
        f"""INSERT INTO {s}.centri (fonte, nome, geom)
            SELECT 'cartelli', nome, ST_Multi(ST_Transform(g, 4326)) FROM (
                SELECT min(nome) AS nome, (ST_Dump(ST_Union(g))).geom AS g FROM ellissi GROUP BY chiave
            ) u""",
        # abitanti dei centri dei cartelli: dalla località OSM con lo stesso nome (la più vicina, entro 2 km), poi dal
        # suo centro Istat (diviso o intero)
        f"""UPDATE {s}.centri c SET node_id = m.node_id, popolazione_osm = m.popolazione_osm FROM (
                SELECT DISTINCT ON (c.id) c.id, p.node_id,
                       CASE WHEN p.tags->>'population' ~ '^[0-9]{{1,8}}$' THEN (p.tags->>'population')::integer END
                           AS popolazione_osm
                FROM {s}.centri c JOIN {s}.punti p ON p.tipo = 'localita' AND p.nome IS NOT NULL
                     AND {_chiave('p.nome')} = {_chiave('c.nome')} AND p.geom && ST_Expand(c.geom, 0.03)
                     AND ST_DWithin(p.geom::geography, c.geom::geography, 2000)
                WHERE c.fonte = 'cartelli'
                ORDER BY c.id, p.geom <-> c.geom
            ) m WHERE c.id = m.id""",
        f"""UPDATE {s}.centri c SET popolazione = i.popolazione, popolazione_localita = i.popolazione_localita,
                   localita_istat = i.localita_istat, pro_com = i.pro_com, loc_id = i.loc_id, tipo_loc = i.tipo_loc,
                   diviso = i.diviso
            FROM {s}.centri i
            WHERE c.fonte = 'cartelli' AND i.fonte = 'istat' AND c.node_id IS NOT NULL AND i.node_id = c.node_id""",
        f"""UPDATE {s}.centri c SET popolazione = i.popolazione, popolazione_localita = i.popolazione_localita,
                   localita_istat = i.localita_istat, pro_com = i.pro_com, loc_id = i.loc_id, tipo_loc = i.tipo_loc
            FROM {s}.punti p, {s}.centri i
            WHERE c.fonte = 'cartelli' AND c.popolazione IS NULL AND p.node_id = c.node_id AND p.tipo = 'localita'
              AND i.fonte = 'istat' AND NOT i.diviso AND i.geom && p.geom AND ST_Covers(i.geom, p.geom)""",
        # cartelli senza una località OSM con lo stesso nome vicina (gli ingressi di una città, lontani dal suo
        # centro): dal centro Istat con lo stesso nome che toccano
        f"""UPDATE {s}.centri c SET popolazione = i.popolazione, popolazione_localita = i.popolazione_localita,
                   localita_istat = i.localita_istat, pro_com = i.pro_com, loc_id = i.loc_id, tipo_loc = i.tipo_loc,
                   node_id = i.node_id, popolazione_osm = i.popolazione_osm, diviso = i.diviso
            FROM {s}.centri i
            WHERE c.fonte = 'cartelli' AND c.popolazione IS NULL AND i.fonte = 'istat'
              AND {_chiave('i.nome')} = {_chiave('c.nome')} AND i.geom && c.geom AND ST_Intersects(i.geom, c.geom)""",
        f"CREATE INDEX ON {s}.centri USING gist (geom)",
        f"ANALYZE {s}.centri",
    ]
    for sql in passi:
        c.exec_driver_sql(sql)
    r = c.exec_driver_sql(
        f"SELECT count(*) FILTER (WHERE fonte = 'istat'), count(*) FILTER (WHERE diviso AND fonte = 'istat'), "
        f"count(*) FILTER (WHERE fonte = 'cartelli'), count(*) FILTER (WHERE fonte = 'cartelli' AND popolazione IS NOT NULL) "
        f"FROM {s}.centri"
    ).one()
    sezioni_caricate = c.exec_driver_sql(f"SELECT count(*) FROM {s}.sezioni").scalar()
    risultato = {"regioni": sorted(regioni), "sezioni_lette": lette, "abitanti_regioni": popolazione,
                 "comuni": len(comuni), "sezioni": sezioni_caricate,
                 "centri_istat": r[0], "centri_divisi": r[1], "centri_cartelli": r[2], "centri_cartelli_con_abitanti": r[3]}
    log.info("sezioni Istat e centri abitati: %s", risultato)
    return risultato
