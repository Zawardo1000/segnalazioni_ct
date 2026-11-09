import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from segnalazioni import api, manutenzione
from segnalazioni.config import BASE_DIR, Settings, check_required, get_settings
from segnalazioni.db import Database
from segnalazioni.sicurezza import LimiteAlMinuto, LimiteCorpoMiddleware, SecurityHeadersMiddleware

STATIC = BASE_DIR / "segnalazioni" / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = check_required(settings or get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.database_url)
        compito = None
        try:
            # "piu-nuovo" dopo un ritorno a una versione precedente: si parte lo stesso
            app.state.schema_state = db.migrate()
            app.state.settings = settings
            app.state.db = db
            if settings.conservazione_prove_giorni > 0:
                compito = asyncio.create_task(manutenzione.ciclo(db, settings.conservazione_prove_giorni))
            yield
        finally:
            if compito is not None:
                compito.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await compito
            db.dispose()

    app = FastAPI(title="Segnalazioni CT", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.limite_abbinamenti = LimiteAlMinuto(settings.abbinamenti_al_minuto)

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

    @app.api_route("/manifest.webmanifest", methods=["GET", "HEAD"], include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    app.include_router(api.router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.add_middleware(LimiteCorpoMiddleware, max_bytes=settings.max_corpo_bytes)
    app.add_middleware(SecurityHeadersMiddleware)  # ultimo aggiunto = il più esterno: intestazioni anche sui 413
    return app
