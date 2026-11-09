"""Versione del codice in esecuzione: sul server gitdeploy scrive il commit installato in `.gitdeploy-commit`, nella
radice del codice (/app). Sul PC il file non c'è."""

import functools
import re

from segnalazioni.config import BASE_DIR

COMMIT_FILE = BASE_DIR / ".gitdeploy-commit"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


@functools.cache
def versione() -> str:
    try:
        value = COMMIT_FILE.read_text(encoding="ascii").strip()
    except OSError:
        return "sviluppo"
    return value[:7] if _COMMIT_RE.match(value) else "sconosciuta"
