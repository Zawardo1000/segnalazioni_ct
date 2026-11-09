"""Tutta la configurazione, dalle variabili d'ambiente (sul PC anche da .env).

I nomi delle variabili sono quelli dei campi in maiuscolo (DATABASE_URL, CHIAVE_ACCESSO, ...). Sul server percorsi e
porta (WEB_HOST, WEB_PORT, LOG_DIR, DATA_DIR) li fissa il compose; segreti e impostazioni stanno nel .env dell'app.
Gli altri moduli leggono da qui, mai da os.getenv.
"""

from functools import cache
from pathlib import Path

from pydantic import BaseModel, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "segnalazioni"
BASE_DIR = Path(__file__).resolve().parent.parent  # radice del repo = /app nel container: solo lettura

GEOFABRIK = "https://download.geofabrik.de/europe/italy"


class AreaOSM(BaseModel):
    """Un'area dello stradario: estratto Geofabrik da scaricare e riquadro (ovest, sud, est, nord) da ritagliare."""

    nome: str
    url: str
    bbox: tuple[float, float, float, float]

    @field_validator("bbox")
    @classmethod
    def _bbox(cls, v: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        ovest, sud, est, nord = v
        if not (-180 <= ovest < est <= 180 and -90 <= sud < nord <= 90):
            raise ValueError(f"bbox non valido (ovest, sud, est, nord): {v}")
        return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # obbligatoria (vedi .env.example): postgresql+psycopg://utente:password@host:5432/db, con PostGIS
    database_url: str = ""
    # obbligatoria: chi la conosce usa la pagina di prova (header X-Chiave)
    chiave_accesso: SecretStr = SecretStr("")
    web_host: str = "127.0.0.1"  # sul server 0.0.0.0, solo dentro il container
    web_port: int = 8013
    log_dir: Path = BASE_DIR / "logs"
    data_dir: Path = BASE_DIR / "local-data"  # estratti OSM scaricati per l'importazione

    # Stradario: estratti OSM di Geofabrik, ritagliati sui riquadri delle aree, uniti e importati in PostGIS
    # (importa_osm.py). Geofabrik non ha estratti di una sola regione: la Sicilia sta in "isole" (con la
    # Sardegna), la Lombardia in "nord-ovest" (con Piemonte, Liguria e Valle d'Aosta). Riquadri con un paio di km di
    # margine: dentro ci sono anche pezzi delle province e regioni vicine, e va bene così.
    osm_aree: list[AreaOSM] = [
        AreaOSM(nome="Città metropolitana di Catania", url=f"{GEOFABRIK}/isole-latest.osm.pbf",
                bbox=(14.32, 37.02, 15.29, 37.99)),
        AreaOSM(nome="Lombardia", url=f"{GEOFABRIK}/nord-ovest-latest.osm.pbf",
                bbox=(8.47, 44.66, 11.45, 46.66)),
    ]
    # memoria per osm2pgsql (modalità --slim: i dati intermedi stanno nel database, non in RAM)
    osm2pgsql_cache_mb: int = 300

    # Solo per catturare casi di test da posizioni fuori dallo stradario (tests/scripts/cattura_caso.py)
    overpass_urls: list[str] = [
        "https://overpass.openstreetmap.fr/api/interpreter",
        "https://overpass-api.de/api/interpreter",
    ]
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    # identifica l'app verso Geofabrik, Overpass e Nominatim (lo chiedono le loro regole d'uso)
    user_agent: str = "segnalazioni-ct/0.1 (+https://github.com/Zawardo1000/segnalazioni_ct)"

    # conserva, per ogni prova, i dati letti dallo stradario: da lì si ricavano i casi di test
    salva_fonti: bool = True
    # le prove contengono le posizioni di chi prova: si cancellano dopo questi giorni (0 = mai)
    conservazione_prove_giorni: int = 365
    # limiti dell'API, oltre a quelli di nginx: abbinamenti al minuto per tutto il servizio, corpo delle richieste
    abbinamenti_al_minuto: int = 120
    max_corpo_bytes: int = 16 * 1024

    @property
    def nome_aree(self) -> str:
        return ", ".join(a.nome for a in self.osm_aree)


def check_required(settings: Settings) -> Settings:
    if not settings.database_url.strip():
        raise RuntimeError("Variabile d'ambiente mancante: DATABASE_URL (vedi .env.example)")
    if not settings.chiave_accesso.get_secret_value().strip():
        raise RuntimeError("Variabile d'ambiente mancante: CHIAVE_ACCESSO (vedi .env.example)")
    return settings


@cache
def get_settings() -> Settings:
    return check_required(Settings())
