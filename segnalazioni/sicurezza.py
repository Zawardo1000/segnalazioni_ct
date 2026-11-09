"""Sicurezza dell'app web: intestazioni su TUTTE le risposte, chiave di accesso, limiti di frequenza e di dimensione.

Questi controlli valgono anche senza nginx: dentro il server le altre app arrivano alla porta direttamente (rete
fastapi_default). nginx aggiunge HTTPS, i percorsi ammessi e il limite per IP.

La pagina carica solo file propri (Leaflet è in static/vendor) e le tessere della mappa da tile.openstreetmap.org.
Referrer-Policy: le regole d'uso delle tessere di OpenStreetMap chiedono un Referer valido, quindi si manda l'origine.
"""

import hmac
import threading
import time
from collections import deque

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; manifest-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
COMMON = {
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"strict-origin-when-cross-origin",
    b"permissions-policy": b"geolocation=(self), camera=(), microphone=()",
    b"content-security-policy": CSP.encode(),
    b"x-robots-tag": b"noindex, nofollow, noarchive",
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        extra = dict(COMMON)
        # API: mai in cache; pagina e file: si ricontrollano a ogni apertura (prove frequenti, niente versioni vecchie)
        extra[b"cache-control"] = b"no-store" if scope["path"].startswith("/api/") else b"no-cache"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in extra]
                message["headers"] = headers + list(extra.items())
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _CorpoTroppoGrande(Exception):
    pass


class LimiteCorpoMiddleware:
    """Rifiuta con 413 i corpi delle richieste oltre max_bytes, dichiarati o letti, prima che arrivino all'app."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        troppo = JSONResponse({"detail": "richiesta troppo grande"}, status_code=413)
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    if int(v) > self.max_bytes:
                        return await troppo(scope, receive, send)
                except ValueError:
                    return await JSONResponse({"detail": "Content-Length non valido"}, status_code=400)(
                        scope, receive, send
                    )
        letti = 0

        async def receive_limitato() -> Message:
            nonlocal letti
            msg = await receive()
            if msg["type"] == "http.request":
                letti += len(msg.get("body", b""))
                if letti > self.max_bytes:
                    raise _CorpoTroppoGrande
            return msg

        try:
            await self.app(scope, receive_limitato, send)
        except _CorpoTroppoGrande:
            await troppo(scope, receive, send)


class LimiteAlMinuto:
    """Al massimo n eventi negli ultimi 60 secondi, per tutto il servizio (un solo processo): un tetto alle richieste
    verso il database anche se il limite per IP di nginx non basta (molti IP, o chiamate dall'interno del server)."""

    def __init__(self, n: int):
        self.n = n
        self._tempi: deque[float] = deque()
        self._lock = threading.Lock()

    def consenti(self) -> bool:
        ora = time.monotonic()
        with self._lock:
            while self._tempi and ora - self._tempi[0] > 60:
                self._tempi.popleft()
            if len(self._tempi) >= self.n:
                return False
            self._tempi.append(ora)
            return True


def richiedi_chiave(request: Request, x_chiave: str | None = Header(default=None)) -> None:
    attesa = request.app.state.settings.chiave_accesso.get_secret_value().encode()
    if not x_chiave or not hmac.compare_digest(x_chiave.encode(), attesa):
        raise HTTPException(status_code=401, detail="chiave di accesso mancante o sbagliata")
