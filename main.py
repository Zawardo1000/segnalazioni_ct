"""Avvio del backend di Segnalazioni CT (sul server: python main.py).

Un solo processo: niente --workers né --reload. uvicorn gestisce SIGTERM: chiude le richieste in corso (fino a
15 s), poi il lifespan chiude il client HTTP e libera il database, entro i 30 s concessi da Docker.
"""

import logging

import uvicorn

from segnalazioni.config import get_settings
from segnalazioni.log_setup import setup_logging


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_dir)

    from segnalazioni.app import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.web_host,
        port=settings.web_port,
        log_config=None,  # i log li configura setup_logging (file a rotazione, segreti oscurati)
        access_log=False,  # le richieste le registra già nginx
        server_header=False,
        timeout_graceful_shutdown=15,
    )
    logging.shutdown()


if __name__ == "__main__":
    main()
