# Postgres per il PC e la CI, costruito come il Postgres condiviso del server: postgres:18 ufficiale + PostGIS
# dal repository PGDG (gia' configurato nell'immagine). Vale su x86 (PC) e ARM64 (CI, server).
FROM postgres:18
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-18-postgis-3 \
 && rm -rf /var/lib/apt/lists/*
