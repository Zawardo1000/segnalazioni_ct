"""Log: console più file a rotazione in LOG_DIR (2 MB × 3), segreti e chiavi oscurati in ogni riga."""

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from segnalazioni.config import APP_NAME


class RedactingFormatter(logging.Formatter):
    """Oscura chiavi e password in ogni riga, compresi i traceback."""

    _PATTERNS = [
        (re.compile(r"(?i)(x-chiave:\s*)\S+"), r"\1<oscurato>"),
        (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1<oscurato>"),
        (re.compile(r"(?i)\b(api_?key|token|secret|password|chiave)=[^&\s'\"]+"), r"\1=<oscurato>"),
        # password negli URL dei database (postgresql+psycopg://utente:password@host)
        (re.compile(r"(://[^:/\s@]+:)[^@\s]+@"), r"\1<oscurato>@"),
    ]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for pattern, replacement in self._PATTERNS:
            text = pattern.sub(replacement, text)
        return text


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, RedactingFormatter) for h in root.handlers):  # già configurato
        return
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    formatter = RedactingFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / f"{APP_NAME}.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    except OSError as e:
        root.warning("Log su file disattivato (%s): %s", log_dir, e)
        return
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
