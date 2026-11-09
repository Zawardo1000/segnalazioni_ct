"""Manutenzione periodica fatta dall'app: cancellazione delle prove vecchie.

Le prove contengono le posizioni di chi ha usato la pagina di prova: si tengono per CONSERVAZIONE_PROVE_GIORNI
(predefinito 365) e poi si cancellano. Il compito gira all'avvio e poi ogni 6 ore, nel processo dell'app (un solo
processo, nessun pianificatore esterno); a mano: python -m segnalazioni pulisci-prove.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from segnalazioni.db import Database, utcnow
from segnalazioni.models import Prova

log = logging.getLogger(__name__)

INTERVALLO_S = 6 * 3600


def cancella_prove_vecchie(session: Session, giorni: int) -> int:
    """Cancella le prove create più di `giorni` giorni fa e restituisce quante; con 0 non cancella niente."""
    if giorni <= 0:
        return 0
    limite = utcnow() - timedelta(days=giorni)
    n = session.execute(delete(Prova).where(Prova.creata < limite)).rowcount or 0
    session.commit()
    return n


async def ciclo(db: Database, giorni: int) -> None:
    """Gira finché l'app è attiva; gli errori si registrano e si riprova al giro dopo."""
    while True:
        try:
            def lavoro() -> int:
                with db.session() as s:
                    return cancella_prove_vecchie(s, giorni)

            n = await asyncio.to_thread(lavoro)
            if n:
                log.info("cancellate %d prove più vecchie di %d giorni", n, giorni)
        except Exception:
            log.exception("cancellazione delle prove vecchie non riuscita")
        await asyncio.sleep(INTERVALLO_S)
