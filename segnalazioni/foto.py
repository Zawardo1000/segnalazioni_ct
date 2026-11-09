"""Foto delle segnalazioni: controllo, ricodifica e salvataggio in FOTO_DIR.

La pagina ricodifica già la foto (JPEG, 1600 px sul lato lungo, sotto MAX_FOTO_BYTES: spariscono anche i dati EXIF con
la posizione e il modello del telefono). Il server non si fida: apre il file con Pillow (solo JPEG, con un tetto ai
pixel contro le "bombe" di decompressione), lo raddrizza secondo l'orientamento EXIF e lo salva di nuovo senza
metadati. Il nome del file è casuale e non dice niente della segnalazione.
"""

import io
import os
import secrets
import warnings
from pathlib import Path

from PIL import Image, ImageOps

LATO_MAX = 2048
MAX_PIXEL = 40_000_000  # una foto da 48 Mpx del telefono la rimpicciolisce già la pagina
QUALITA = (82, 72, 62, 52)
NOME = "^[0-9a-f]{32}\\.jpg$"


class FotoNonValida(ValueError):
    """La foto non è un JPEG leggibile, o è troppo grande anche dopo la ricodifica."""


def prepara(dati: bytes, max_bytes: int) -> bytes:
    """JPEG pulito (senza metadati, orientato, al massimo LATO_MAX px e max_bytes) dalla foto ricevuta."""
    if not dati.startswith(b"\xff\xd8\xff"):
        raise FotoNonValida("serve una foto JPEG")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(dati)) as im:
                if im.format != "JPEG":
                    raise FotoNonValida("serve una foto JPEG")
                if im.width * im.height > MAX_PIXEL:
                    raise FotoNonValida("foto troppo grande")
                im.load()
                pulita = ImageOps.exif_transpose(im).convert("RGB")
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning, SyntaxError) as e:
        raise FotoNonValida("foto non leggibile") from e
    pulita.thumbnail((LATO_MAX, LATO_MAX))
    for q in QUALITA:
        buf = io.BytesIO()
        pulita.save(buf, "JPEG", quality=q, optimize=True)  # senza exif=: niente metadati
        if buf.tell() <= max_bytes:
            return buf.getvalue()
    raise FotoNonValida("foto troppo grande")


def salva(cartella: Path, dati: bytes) -> str:
    """Scrive la foto con un nome casuale (prima in un file temporaneo, poi lo rinomina); restituisce il nome."""
    cartella.mkdir(parents=True, exist_ok=True)
    nome = f"{secrets.token_hex(16)}.jpg"
    temporaneo = cartella / f".{nome}.tmp"
    fd = os.open(temporaneo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(dati)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporaneo, cartella / nome)
    except BaseException:
        temporaneo.unlink(missing_ok=True)
        raise
    return nome


def percorso(cartella: Path, nome: str | None) -> Path | None:
    """Il file della foto, se esiste (le foto non sono nel backup: dopo un ripristino possono mancare)."""
    import re

    if not nome or not re.fullmatch(NOME, nome):
        return None
    p = cartella / nome
    return p if p.is_file() else None
