"""Tutta la configurazione, dalle variabili d'ambiente (sul PC anche da .env).

I nomi delle variabili sono quelli dei campi in maiuscolo (DATABASE_URL, CHIAVE_ACCESSO, ...). Sul server percorsi e
porta (WEB_HOST, WEB_PORT, LOG_DIR, DATA_DIR) li fissa il compose; segreti e impostazioni stanno nel .env dell'app.
Gli altri moduli leggono da qui, mai da os.getenv.
"""

from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "segnalazioni"
BASE_DIR = Path(__file__).resolve().parent.parent  # radice del repo = /app nel container: solo lettura

GEOFABRIK = "https://download.geofabrik.de/europe/italy"


class AreaOSM(BaseModel):
    """Un'area dello stradario: estratto Geofabrik da scaricare, riquadro (ovest, sud, est, nord) da ritagliare e
    regioni (codici Istat) di cui caricare le sezioni di censimento, per la popolazione dei centri abitati."""

    nome: str
    url: str
    bbox: tuple[float, float, float, float]
    istat_regioni: list[int] = []

    @field_validator("istat_regioni")
    @classmethod
    def _regioni(cls, v: list[int]) -> list[int]:
        if any(not 1 <= r <= 20 for r in v):
            raise ValueError(f"codici Istat delle regioni da 1 a 20: {v}")
        return v

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
                bbox=(14.32, 37.02, 15.29, 37.99), istat_regioni=[19]),
        AreaOSM(nome="Lombardia", url=f"{GEOFABRIK}/nord-ovest-latest.osm.pbf",
                bbox=(8.47, 44.66, 11.45, 46.66), istat_regioni=[3]),
    ]
    # Sezioni di censimento Istat 2021, una zip per regione (circa 60 MB): popolazione dei centri abitati per le
    # traverse interne (istat.py). {regione:02d} = codice Istat della regione (19 Sicilia, 03 Lombardia).
    istat_url_regione: str = "https://www.istat.it/storage/cartografia/basi_territoriali/2021/R{regione:02d}_21.zip"
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

    # Segnalazioni: una foto per segnalazione, ricodificata dalla pagina e di nuovo dal server (foto.py), in FOTO_DIR.
    # Sul server è una cartella fuori dal backup (scelta dell'utente); senza FOTO_DIR, DATA_DIR/foto.
    foto_dir: Path | None = None
    max_foto_bytes: int = 1024 * 1024
    segnalazioni_al_minuto: int = 30
    # indirizzo pubblico della pagina, per i link privati delle segnalazioni mandati per email
    url_pubblico: str = "http://127.0.0.1:8013"

    # Email con il link privato della segnalazione (posta.py): una casella esistente via SMTP, come timbro (Gmail con
    # una password per le app). L'indirizzo di chi la chiede non si salva da nessuna parte. Senza SMTP_HOST: niente
    # invio (la pagina lo dice).
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: Literal["ssl", "starttls"] = "ssl"
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""  # mittente, es. "Segnalazioni CT <nome.cognome@gmail.com>"; vuoto = SMTP_USER
    smtp_timeout_s: float = 20
    email_per_segnalazione: int = 3  # invii al massimo per ogni segnalazione
    email_al_minuto: int = 10  # per tutto il servizio

    # Gestionale degli enti: login OpenID Connect con Keycloak (gestione.py). OIDC_ISSUER è l'indirizzo del realm
    # (https://auth.<dominio>/realms/test_app); il client è confidenziale. Chi entra deve avere nel token l'attributo
    # OIDC_ATTRIBUTO: "amministratore" oppure "ente:<codice dell'ente>" (tabella enti). Senza OIDC_ISSUER il
    # gestionale è spento.
    oidc_issuer: str = ""
    oidc_client_id: str = "segnalazioni-ct"
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_attributo: str = "segnalazioni_ct"
    # indirizzo di Keycloak dentro il server (es. http://keycloak:8080, rete backend) per le chiamate dal server
    # (configurazione, chiavi pubbliche, scambio del codice): dai container l'indirizzo pubblico non si raggiunge.
    # Vuoto = quello di OIDC_ISSUER. Il browser usa sempre l'indirizzo pubblico.
    oidc_url_interno: str = ""
    durata_sessione_ore: int = 8
    # inoltri per competenza oltre i quali la segnalazione va all'amministratore (coda da smistare)
    max_inoltri: int = 2
    # limiti dell'API, oltre a quelli di nginx: abbinamenti al minuto per tutto il servizio, corpo delle richieste
    abbinamenti_al_minuto: int = 120
    max_corpo_bytes: int = 16 * 1024

    @property
    def nome_aree(self) -> str:
        return ", ".join(a.nome for a in self.osm_aree)

    @property
    def cartella_foto(self) -> Path:
        return self.foto_dir or self.data_dir / "foto"

    @property
    def istat_regioni(self) -> list[int]:
        return sorted({r for a in self.osm_aree for r in a.istat_regioni})


def check_required(settings: Settings) -> Settings:
    if not settings.database_url.strip():
        raise RuntimeError("Variabile d'ambiente mancante: DATABASE_URL (vedi .env.example)")
    if not settings.chiave_accesso.get_secret_value().strip():
        raise RuntimeError("Variabile d'ambiente mancante: CHIAVE_ACCESSO (vedi .env.example)")
    return settings


@cache
def get_settings() -> Settings:
    return check_required(Settings())
