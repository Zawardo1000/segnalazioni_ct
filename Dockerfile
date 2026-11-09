# Immagine del backend, la stessa sul PC, nella CI e sul server: il deploy continuo la ricostruisce da qui quando
# cambiano questo file, .dockerignore o requirements*. Contiene Python, i pacchetti di sistema e l'ambiente
# Python (venv) dell'app: il codice NON e' nell'immagine, compose lo monta in sola lettura su /app.
# Qui si decide solo cosa c'e' nell'immagine: i vincoli di sicurezza (sola lettura, utente 10001, niente
# capability, limiti di CPU e RAM) li impone il compose del server, qualunque cosa dica questo file.
#   APT_PACKAGES  pacchetti Debian aggiuntivi: osm2pgsql e osmium-tool per importare lo stradario OSM in PostGIS
#                 (segnalazioni/importa_osm.py)
#   PREPARE       comando eseguito dopo l'installazione (es. scaricare modelli che altrimenti l'app scaricherebbe
#                 al primo avvio: il container gira in sola lettura)
# Dipendenze: prima requirements.lock (versioni bloccate e verificate), poi requirements.txt: una libreria
# aggiunta in requirements.txt ma non ancora nel lock entra comunque (ultima versione), quelle gia'
# bloccate restano com'erano; pip check ferma la build se le versioni sono incompatibili.
# Python 3.15 come bot_wa: dal 09/11/2026 pydantic-core 2.50.4 (FastAPI) ha il pacchetto cp315 per Linux aarch64
# (timbro e' rimasto a 3.14 per questo). Prima di aggiungere una libreria con parti compilate controlla su PyPI che
# esista per cp315 manylinux aarch64: nell'immagine non c'e' il compilatore.
# Gira come utente non privilegiato "app" (uid/gid 10001), lo stesso che impone il server.
FROM python:3.15-slim

ARG APT_PACKAGES="osm2pgsql osmium-tool"
ARG PREPARE=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ${APT_PACKAGES} \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin app

COPY requirements.txt requirements.loc[k] /tmp/req/
RUN python -m venv /opt/venv \
 && if [ -f /tmp/req/requirements.lock ]; then pip install -r /tmp/req/requirements.lock; fi \
 && pip install -r /tmp/req/requirements.txt \
 && pip check \
 && rm -rf /tmp/req \
 && if [ -n "$PREPARE" ]; then sh -c "$PREPARE"; fi

WORKDIR /app
USER app
