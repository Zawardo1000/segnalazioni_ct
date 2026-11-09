"""Comandi di amministrazione: python -m segnalazioni <comando>.

Sul server: docker compose exec segnalazioni-ct python -m segnalazioni <comando> (nella cartella dello stack).
  migrate                     porta lo schema all'ultima migrazione (l'app lo fa anche all'avvio)
  importa-osm                 scarica, ritaglia e importa lo stradario OSM (importa_osm.py)
  stato                       stato dello stradario (aree, data dei dati, numero di strade e confini)
  abbina LAT LON              abbinamento di un punto, in JSON, senza salvarlo
  prove                       ultime prove fatte con la pagina (--sbagliate: solo quelle segnate sbagliate)
  prova N                     una prova per intero; con --caso, come caso di test (tests/fixtures/casi/)
  pulisci-prove               cancella subito le prove più vecchie di CONSERVAZIONE_PROVE_GIORNI (l'app lo fa ogni 6 ore)
"""

import argparse
import json
import logging
from pathlib import Path

from sqlalchemy import select

from segnalazioni.config import Settings
from segnalazioni.log_setup import setup_logging


def _settings() -> Settings:
    s = Settings()  # la chiave di accesso serve solo all'app web
    if not s.database_url.strip():
        raise SystemExit("Variabile d'ambiente mancante: DATABASE_URL (vedi .env.example)")
    return s


def _stampa(dati) -> None:
    print(json.dumps(dati, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m segnalazioni", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("migrate", help="porta lo schema all'ultima migrazione")
    p = sub.add_parser("importa-osm", help="scarica, ritaglia e importa lo stradario OSM")
    p.add_argument("--file", type=Path, help="estratto OSM già scaricato (.osm.pbf o .osm) invece di Geofabrik")
    p.add_argument("--senza-ritaglio", action="store_true", help="importa il file intero, senza ritagliarlo")
    p.add_argument("--conserva-file", action="store_true", help="non cancellare gli estratti scaricati")
    sub.add_parser("stato", help="stato dello stradario")
    p = sub.add_parser("abbina", help="abbinamento di un punto, in JSON (non viene salvato)")
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("--precisione", type=float, default=None, help="precisione del GPS in metri")
    p.add_argument("--mappa", action="store_true", help="come un punto toccato sulla mappa (distanza vera)")
    p = sub.add_parser("prove", help="ultime prove fatte con la pagina")
    p.add_argument("--sbagliate", action="store_true", help="solo quelle segnate sbagliate")
    p.add_argument("-n", type=int, default=30, help="quante (predefinito 30)")
    p = sub.add_parser("prova", help="una prova per intero")
    p.add_argument("id", type=int)
    p.add_argument("--caso", action="store_true", help="come caso di test: punto, dati letti e risposta dell'utente")
    sub.add_parser("pulisci-prove", help="cancella le prove più vecchie di CONSERVAZIONE_PROVE_GIORNI")
    args = parser.parse_args(argv)

    settings = _settings()
    setup_logging(settings.log_dir)
    log = logging.getLogger("segnalazioni.cli")

    from segnalazioni.db import Database

    db = Database(settings.database_url)
    try:
        if args.comando == "migrate":
            print(db.migrate())
            return 0

        if args.comando == "importa-osm":
            from segnalazioni import importa_osm

            db.migrate()  # PostGIS c'è solo dopo la prima migrazione
            try:
                r = importa_osm.esegui(settings, file=args.file, ritaglio=not args.senza_ritaglio,
                                       conserva_file=args.conserva_file)
            except Exception as e:
                log.error("importazione non riuscita: %s", e)
                return 1
            _stampa(r)
            return 0

        from segnalazioni import abbinamento, stradario
        from segnalazioni.models import Prova

        if args.comando == "pulisci-prove":
            from segnalazioni.manutenzione import cancella_prove_vecchie

            db.migrate()
            with db.session() as s:
                n = cancella_prove_vecchie(s, settings.conservazione_prove_giorni)
            print(f"cancellate {n} prove più vecchie di {settings.conservazione_prove_giorni} giorni")
            return 0

        with db.session() as s:
            if args.comando == "stato":
                _stampa(stradario.stato(s))
                return 0

            if args.comando == "abbina":
                try:
                    intorno = stradario.leggi_intorno(s, args.lat, args.lon, stradario.raggio_ricerca(args.precisione))
                    r = abbinamento.abbina(args.lat, args.lon, args.precisione, intorno,
                                           origine="mappa" if args.mappa else "gps")
                except stradario.FuoriArea:
                    r = abbinamento.fuori_area(args.lat, args.lon, args.precisione, settings.nome_aree)
                for c in r["candidati"]:
                    c.pop("geometria", None)
                _stampa(r)
                return 0

            if args.comando == "prove":
                q = select(Prova).order_by(Prova.id.desc()).limit(args.n)
                if args.sbagliate:
                    q = q.where(Prova.esito == "sbagliato")
                for p in s.scalars(q):
                    print(
                        f"{p.id:>6}  {p.creata:%d/%m %H:%M}  {p.lat:.6f},{p.lon:.6f} ±{p.precisione_m or 0:.0f}m "
                        f"{p.origine:<5} {p.esito_calcolo:<13} {p.ente_calcolato or '-'}"
                        f"{'  [' + p.esito + ']' if p.esito else ''}"
                        f"{'  -> ' + p.ente_corretto if p.ente_corretto else ''}{'  «' + p.nota + '»' if p.nota else ''}"
                    )
                return 0

            if args.comando == "prova":
                p = s.get(Prova, args.id)
                if p is None:
                    print(f"prova {args.id} non trovata")
                    return 1
                risposta = {"esito": p.esito, "osm_way_scelto": p.osm_way_scelto, "ente_corretto": p.ente_corretto,
                            "nota": p.nota, "il": p.esito_il}
                if args.caso:
                    _stampa({"nome": f"prova-{p.id}", "punto": {"lat": p.lat, "lon": p.lon,
                             "precisione_m": p.precisione_m}, "intorno": p.fonti, "risposta_utente": risposta})
                else:
                    _stampa({"id": p.id, "creata": p.creata, "origine": p.origine, "risultato": p.risultato,
                             "risposta_utente": risposta})
                return 0
    finally:
        db.dispose()
    return 2
