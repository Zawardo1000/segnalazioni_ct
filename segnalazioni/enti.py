"""Enti che ricevono le segnalazioni: dal risultato dell'abbinamento al codice dell'ente (tabella enti).

Codici: "anas", "cas", "provincia-<sigla>" (città metropolitana, provincia o libero consorzio, es. provincia-ct),
"comune-<codice Istat a 6 cifre>" (es. comune-087004), "regione-<codice Istat>". Lo stesso codice sta nell'attributo
degli utenti del gestionale (ente:<codice>, gestione.py). Un ente che non è nella tabella, o non è attivo, non riceve
niente: la segnalazione va nella coda "da smistare" dell'amministratore.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from segnalazioni.models import Ente

# Enti finti per le prove (python -m segnalazioni crea-enti-prova): quelli attorno ai casi verificati
ENTI_PROVA = [
    ("anas", "ANAS – Struttura territoriale Sicilia", "anas"),
    ("provincia-ct", "Città metropolitana di Catania", "citta_metropolitana"),
    ("comune-087002", "Comune di Aci Castello", "comune"),
    ("comune-087004", "Comune di Acireale", "comune"),
    ("comune-087015", "Comune di Catania", "comune"),
]


def codice_ente(risultato: dict) -> str | None:
    """Codice dell'ente scelto dall'abbinamento (ente deciso, o suggerito se c'è da scegliere), None se non si sa."""
    ente = risultato.get("ente") or risultato.get("ente_suggerito") or {}
    tipo = ente.get("codice")
    if tipo == "anas":
        return "anas"
    if tipo == "cas":
        return "cas"
    if tipo == "provincia":
        sigla = risultato.get("sigla_provincia")
        return f"provincia-{sigla.lower()}" if sigla else None
    if tipo == "comune":
        istat = risultato.get("comune_istat")
        return f"comune-{istat}" if istat else None
    if tipo == "regione":
        istat = risultato.get("regione_istat")
        return f"regione-{istat}" if istat else None
    return None  # concessionari, gestori indicati in OSM: li smista l'amministratore


def attivo(session: Session, codice: str | None) -> Ente | None:
    if not codice:
        return None
    return session.scalar(select(Ente).where(Ente.codice == codice, Ente.attivo.is_(True)))


def crea_enti_prova(session: Session) -> list[str]:
    """Crea (o riattiva) gli enti finti; restituisce i codici creati."""
    creati = []
    for codice, nome, tipo in ENTI_PROVA:
        ente = session.scalar(select(Ente).where(Ente.codice == codice))
        if ente is None:
            session.add(Ente(codice=codice, nome=nome, tipo=tipo, attivo=True))
            creati.append(codice)
        else:
            ente.nome, ente.tipo, ente.attivo = nome, tipo, True
    session.commit()
    return creati
