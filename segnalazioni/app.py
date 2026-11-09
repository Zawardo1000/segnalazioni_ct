from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from segnalazioni import api, gestione, segnala
from segnalazioni.config import BASE_DIR, Settings, check_required, get_settings
from segnalazioni.db import Database
from segnalazioni.sicurezza import LimiteAlMinuto, LimiteCorpoMiddleware, SecurityHeadersMiddleware

STATIC = BASE_DIR / "segnalazioni" / "static"


def create_app(settings: Settings | None = None, oidc_transport=None) -> FastAPI:
    """oidc_transport: trasporto httpx per le chiamate a Keycloak (nei test, un Keycloak finto)."""
    settings = check_required(settings or get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.database_url)
        try:
            # "piu-nuovo" dopo un ritorno a una versione precedente: si parte lo stesso
            app.state.schema_state = db.migrate()
            app.state.settings = settings
            app.state.db = db
            yield
        finally:
            db.dispose()

    app = FastAPI(title="Segnalazioni CT", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.limite_abbinamenti = LimiteAlMinuto(settings.abbinamenti_al_minuto)
    app.state.limite_segnalazioni = LimiteAlMinuto(settings.segnalazioni_al_minuto)
    app.state.limite_email = LimiteAlMinuto(settings.email_al_minuto)
    app.state.limite_login = LimiteAlMinuto(60)
    app.state.oidc = gestione.Oidc(settings, oidc_transport)

    @app.exception_handler(RequestValidationError)
    async def dati_non_validi(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Solo i nomi dei campi, mai i valori ricevuti: un NaN farebbe fallire la risposta (JSON non valido) e
        # rimandare indietro l'input non serve a nessuno.
        campi = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) or "corpo" for e in exc.errors()})
        return JSONResponse({"detail": "dati non validi", "campi": campi}, status_code=422)

    # GET e HEAD (nginx ammette entrambi; i file statici li accettano già)
    @app.api_route("/health", methods=["GET", "HEAD"])
    def health() -> dict:
        return {"status": "ok"}

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def pagina() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")

    # dalla radice: il service worker controlla le pagine sotto il suo percorso
    @app.api_route("/sw.js", methods=["GET", "HEAD"], include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(STATIC / "sw.js", media_type="text/javascript; charset=utf-8")

    # pagina della segnalazione aperta dal link privato (/s#<codice>.<codice privato>: il frammento non arriva qui)
    @app.api_route("/s", methods=["GET", "HEAD"], include_in_schema=False)
    def pagina_segnalazione() -> FileResponse:
        return FileResponse(STATIC / "segnalazione.html", media_type="text/html; charset=utf-8")

    @app.api_route("/manifest.webmanifest", methods=["GET", "HEAD"], include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    app.include_router(api.router)
    app.include_router(segnala.invio)
    app.include_router(segnala.privato)
    app.include_router(gestione.pagine)
    app.include_router(gestione.api)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    # la foto: il suo limite più i dati del modulo e le intestazioni delle parti
    foto_max = settings.max_foto_bytes + 64 * 1024
    app.add_middleware(LimiteCorpoMiddleware, max_bytes=settings.max_corpo_bytes,
                       eccezioni={("POST", "/api/v1/segnalazioni"): foto_max})
    app.add_middleware(SecurityHeadersMiddleware)  # ultimo aggiunto = il più esterno: intestazioni anche sui 413
    return app
