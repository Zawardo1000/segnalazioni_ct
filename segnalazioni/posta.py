"""Email con il link privato di una segnalazione, a chi l'ha fatta e solo se lo chiede.

L'indirizzo serve solo per questo invio: non si salva nel database, non si scrive nei log (log_setup.py oscura gli
indirizzi anche nei messaggi d'errore) e non resta nel server. Nella pagina resta nel telefono (localStorage), per
non doverlo riscrivere. Solo libreria standard; l'invio avviene nella richiesta, con un tempo massimo.
"""

import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from segnalazioni.config import Settings

EMAIL_RE = re.compile(r"^[^@\s<>()\[\],;:\"]+@[^@\s<>()\[\],;:\"]+\.[^@\s<>()\[\],;:\".]{2,}$")


class InvioNonRiuscito(RuntimeError):
    """Il server di posta non ha accettato il messaggio (il motivo è nel log, senza l'indirizzo)."""


def email_valida(indirizzo: str) -> bool:
    return len(indirizzo) <= 254 and bool(EMAIL_RE.match(indirizzo))


def configurata(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password.get_secret_value())


def messaggio(settings: Settings, destinatario: str, codice: str, link: str, tipo: str, ente: str | None) -> EmailMessage:
    cosa = "della carcassa" if tipo == "carcassa" else "dei rifiuti"
    corpo = (
        f"Ciao,\n\nquesto è il link per seguire la tua segnalazione {codice} ({cosa}"
        f"{f', inviata a: {ente}' if ente else ''}):\n\n{link}\n\n"
        "Con il link chiunque può vedere la segnalazione: non condividerlo se non vuoi.\n"
        "Questo indirizzo email non è stato salvato: ti abbiamo scritto solo perché l'hai chiesto.\n\n"
        "Segnalazioni CT (prototipo)\n"
    )
    mittente = settings.smtp_from or settings.smtp_user
    msg = EmailMessage()
    msg["From"] = mittente
    msg["To"] = destinatario
    msg["Subject"] = f"La tua segnalazione {codice}"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=parseaddr(mittente)[1].rpartition("@")[2] or None)
    msg.set_content(corpo)
    return msg


def invia(settings: Settings, msg: EmailMessage) -> None:
    contesto = ssl.create_default_context()
    try:
        if settings.smtp_security == "ssl":
            smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_s,
                                    context=contesto)
        else:
            smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_s)
        with smtp:
            if settings.smtp_security == "starttls":
                smtp.starttls(context=contesto)
            smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
            rifiutati = smtp.send_message(msg)
    except smtplib.SMTPRecipientsRefused as e:
        raise InvioNonRiuscito(f"destinatario rifiutato ({_codici(e.recipients)})") from None
    except (smtplib.SMTPException, OSError) as e:
        raise InvioNonRiuscito(f"{type(e).__name__}: {getattr(e, 'smtp_code', '')}".strip()) from None
    if rifiutati:
        raise InvioNonRiuscito(f"destinatario rifiutato ({_codici(rifiutati)})")


def _codici(rifiutati: dict) -> str:
    # solo i codici SMTP: gli indirizzi non vanno nei log
    return ", ".join(str(codice) for codice, _ in rifiutati.values())
