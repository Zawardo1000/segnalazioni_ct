"""Importazione dello stradario OSM in PostGIS, nello schema "osm" (fuori dalle migrazioni Alembic).

1. scarica gli estratti Geofabrik delle aree (OSM_AREE) in DATA_DIR/osm/, ognuno una volta sola, controllandone
   l'impronta MD5; un file di meno di 20 ore si riusa (Geofabrik aggiorna gli estratti una volta al giorno);
2. ritaglia ogni area sul suo riquadro con osmium (strategia "smart": i confini comunali restano completi anche dove
   escono dal riquadro) e unisce i ritagli;
3. importa con osm2pgsql (stile osm/stradario.lua, modalità --slim --drop: i dati intermedi stanno nel database,
   non in RAM, e alla fine si cancellano) nello schema osm_import;
4. controlla che ci siano strade e comuni, poi sostituisce lo schema osm in una sola transazione: l'app non vede mai
   uno stradario a metà, e un'importazione fallita lascia quello di prima;
5. cancella i file (gli estratti pesano centinaia di MB e si riscaricano: non devono finire nel backup), salvo
   --conserva-file. Se l'importazione fallisce restano, per riprovare senza riscaricarli.

osmium e osm2pgsql sono pacchetti di sistema (APT_PACKAGES del Dockerfile). La password del database passa a
osm2pgsql nelle variabili d'ambiente di libpq, mai negli argomenti (che finiscono nei log e in ps).
Comando: python -m segnalazioni importa-osm [--file estratto.osm.pbf] [--senza-ritaglio] [--conserva-file]
"""

import hashlib
import json
import logging
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url

from segnalazioni.config import BASE_DIR, AreaOSM, Settings
from segnalazioni.db import Database

log = logging.getLogger(__name__)

STILE = BASE_DIR / "segnalazioni" / "osm" / "stradario.lua"
SCHEMA = "osm"
SCHEMA_IMPORT = "osm_import"
RIUSA_SE_PIU_RECENTE_DI_S = 20 * 3600
TIMEOUT_COMANDI_S = 4 * 3600
TENTATIVI_DOWNLOAD = 6


def _esegui(cmd: list[str], env: dict[str, str] | None = None) -> str:
    log.info("eseguo: %s", " ".join(cmd))
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=TIMEOUT_COMANDI_S, check=False)
    uscita = (r.stdout + r.stderr).strip()
    for riga in uscita.splitlines()[-15:]:
        log.info("  %s", riga)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} terminato con codice {r.returncode}: {uscita[-500:]}")
    return r.stdout


def scarica(settings: Settings, url: str, cartella: Path) -> Path:
    dest = cartella / url.rsplit("/", 1)[-1]
    if dest.exists() and time.time() - dest.stat().st_mtime < RIUSA_SE_PIU_RECENTE_DI_S:
        log.info("estratto già scaricato da meno di 20 ore: %s", dest)
        return dest
    parziale = dest.with_name(dest.name + ".part")
    timeout = httpx.Timeout(60.0, read=120.0)
    with httpx.Client(headers={"User-Agent": settings.user_agent}, timeout=timeout, follow_redirects=True) as c:
        r = c.get(url + ".md5")
        r.raise_for_status()
        atteso = r.text.split()[0].strip().lower()
        log.info("scarico %s", url)
        _scarica_riprendendo(c, url, parziale)
    h = hashlib.md5(usedforsecurity=False)
    with parziale.open("rb") as f:
        while blocco := f.read(1 << 20):
            h.update(blocco)
    if h.hexdigest() != atteso:
        parziale.unlink(missing_ok=True)
        raise RuntimeError(f"impronta MD5 diversa da quella di Geofabrik per {url}")
    parziale.replace(dest)
    log.info("scaricati %.1f MB, MD5 verificato", dest.stat().st_size / 1e6)
    return dest


def _scarica_riprendendo(c: httpx.Client, url: str, parziale: Path) -> None:
    """Scarica in `parziale`; se il trasferimento si blocca o cade, riprende da dove era arrivato (Range)."""
    for tentativo in range(1, TENTATIVI_DOWNLOAD + 1):
        gia = parziale.stat().st_size if parziale.exists() else 0
        try:
            with c.stream("GET", url, headers={"Range": f"bytes={gia}-"} if gia else {}) as risposta:
                if gia and risposta.status_code == 416:  # il file parziale è già completo
                    return
                risposta.raise_for_status()
                if gia and risposta.status_code != 206:  # il server ha ignorato Range: si ricomincia
                    gia = 0
                with parziale.open("ab" if gia else "wb") as f:
                    for blocco in risposta.iter_bytes(1 << 20):
                        f.write(blocco)
            return
        except httpx.TransportError as e:
            if tentativo == TENTATIVI_DOWNLOAD:
                raise RuntimeError(f"download di {url} non riuscito dopo {tentativo} tentativi: {e}") from e
            arrivati = parziale.stat().st_size if parziale.exists() else 0
            log.warning("download interrotto a %.1f MB (%s): riprendo (tentativo %d di %d)",
                        arrivati / 1e6, e, tentativo + 1, TENTATIVI_DOWNLOAD)
            time.sleep(min(30, 5 * tentativo))


def data_dei_dati(file: Path) -> datetime | None:
    """Data dei dati OSM dall'intestazione del file (gli estratti Geofabrik la scrivono)."""
    try:
        valore = _esegui(["osmium", "fileinfo", "-g", "header.option.osmosis_replication_timestamp", str(file)])
    except RuntimeError:
        return None
    try:
        return datetime.fromisoformat(valore.strip().replace("Z", "+00:00")).astimezone(UTC) if valore.strip() else None
    except ValueError:
        return None


def ritaglia(sorgente: Path, area: AreaOSM, dest: Path) -> Path:
    ovest, sud, est, nord = area.bbox
    _esegui([
        "osmium", "extract", "--bbox", f"{ovest},{sud},{est},{nord}", "--strategy", "smart",
        "-S", "types=multipolygon,boundary", "--overwrite", "-o", str(dest), str(sorgente),
    ])
    return dest


def unisci(files: list[Path], dest: Path) -> Path:
    if len(files) == 1:
        return files[0]
    _esegui(["osmium", "merge", "--overwrite", "-o", str(dest), *map(str, files)])
    return dest


def importa(settings: Settings, file: Path, metadati: dict) -> dict:
    url = make_url(settings.database_url)
    db = Database(settings.database_url)
    try:
        with db.engine.begin() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA_IMPORT} CASCADE"))
            c.execute(text(f"CREATE SCHEMA {SCHEMA_IMPORT}"))

        env = dict(os.environ)
        env["SCHEMA_OSM"] = SCHEMA_IMPORT
        if url.host:
            env["PGHOST"] = url.host
        if url.port:
            env["PGPORT"] = str(url.port)
        if url.username:
            env["PGUSER"] = url.username
        if url.password:
            env["PGPASSWORD"] = url.password
        inizio = time.monotonic()
        _esegui(
            [
                "osm2pgsql", "--create", "--slim", "--drop", f"--middle-schema={SCHEMA_IMPORT}", "--output=flex",
                f"--style={STILE}", f"--database={url.database}", f"--cache={settings.osm2pgsql_cache_mb}",
                "--number-processes=1", "--log-progress=false", str(file),
            ],
            env=env,
        )
        durata = time.monotonic() - inizio

        with db.engine.begin() as c:
            vie = c.scalar(text(f"SELECT count(*) FROM {SCHEMA_IMPORT}.vie"))
            confini = c.scalar(text(f"SELECT count(*) FROM {SCHEMA_IMPORT}.confini"))
            comuni = c.scalar(text(f"SELECT count(*) FROM {SCHEMA_IMPORT}.confini WHERE admin_level = 8"))
            if not vie or not comuni:
                raise RuntimeError(f"importazione vuota ({vie} strade, {comuni} comuni): resta lo stradario di prima")
            c.execute(text(
                f"CREATE TABLE {SCHEMA_IMPORT}.importazione (aree text NOT NULL, fonti text NOT NULL, "
                "dati_del timestamptz, importato_il timestamptz NOT NULL, bbox jsonb, vie integer NOT NULL, "
                "confini integer NOT NULL)"
            ))
            c.execute(
                text(
                    f"INSERT INTO {SCHEMA_IMPORT}.importazione VALUES "
                    "(:aree, :fonti, :dati_del, :importato_il, CAST(:bbox AS jsonb), :vie, :confini)"
                ),
                {**metadati, "bbox": json.dumps(metadati["bbox"]), "importato_il": datetime.now(UTC),
                 "vie": vie, "confini": confini},
            )
            c.execute(text(f"ANALYZE {SCHEMA_IMPORT}.vie"))
            c.execute(text(f"ANALYZE {SCHEMA_IMPORT}.confini"))

        with db.engine.begin() as c:
            c.execute(text("SET LOCAL lock_timeout = '30s'"))
            c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
            c.execute(text(f"ALTER SCHEMA {SCHEMA_IMPORT} RENAME TO {SCHEMA}"))
        risultato = {"aree": metadati["aree"], "vie": vie, "confini": confini, "comuni": comuni,
                     "dati_del": metadati["dati_del"], "secondi_osm2pgsql": round(durata)}
        log.info("stradario importato: %s", risultato)
        return risultato
    finally:
        db.dispose()


def esegui(settings: Settings, file: Path | None = None, ritaglio: bool = True, conserva_file: bool = False) -> dict:
    cartella = settings.data_dir / "osm"
    cartella.mkdir(parents=True, exist_ok=True)
    aree = settings.osm_aree

    # sorgenti: il file indicato, oppure gli estratti Geofabrik (ognuno scaricato una volta sola)
    if file is not None:
        sorgenti = {a.url: file for a in aree}
        fonti = [file.name]
    else:
        sorgenti = {}
        for a in aree:
            if a.url not in sorgenti:
                sorgenti[a.url] = scarica(settings, a.url, cartella)
        fonti = list(sorgenti)
    date = [d for d in (data_dei_dati(f) for f in set(sorgenti.values())) if d]

    if ritaglio:
        ritagli = [ritaglia(sorgenti[a.url], a, cartella / f"ritaglio-{i}.osm.pbf") for i, a in enumerate(aree)]
        da_importare = unisci(ritagli, cartella / "stradario.osm.pbf")
        temporanei = [*ritagli, da_importare]
        bbox = [{"nome": a.nome, "bbox": list(a.bbox)} for a in aree]
        nomi = settings.nome_aree
    else:
        da_importare = file if file is not None else next(iter(sorgenti.values()))
        temporanei, bbox, nomi = [], None, da_importare.name

    metadati = {"aree": nomi, "fonti": " ".join(fonti), "dati_del": min(date) if date else None, "bbox": bbox}
    try:
        risultato = importa(settings, da_importare, metadati)
    finally:
        for f in set(temporanei):
            f.unlink(missing_ok=True)
    if file is None and not conserva_file:
        for f in set(sorgenti.values()):
            f.unlink(missing_ok=True)
    return risultato
