"""Abbinamento: da un punto (e dalla precisione del GPS) all'ente competente per la strada.

Funzione pura su dati già letti (dallo stradario in PostGIS, stradario.py, oppure dai casi di test): niente rete né
database, così le regole si provano sui casi verificati.

Dati in ingresso, l'"intorno" del punto:
    {
      "vie": [{"osm_way": int, "tags": {...}, "geometria": [[lat, lon], ...]}, ...],
      "comune": {"osm_relation": int | None, "nome": str, "tags": {...}} | None,
      "provincia": {"nome": str, "tags": {...}} | None,   # sigla in tags "ref" (CT) o "ISO3166-2" (IT-CT)
      "regione": {"nome": str} | None,
      "comuni_vicini": [str, ...],                         # confine entro RAGGIO_CONFINI_M dal punto
      "istat": {"regioni": [int], "centri": [...], "vicini": [...]},  # centri abitati (osm.centri), se ci sono
      "fonte": {...},                                      # da dove vengono i dati (solo informativo)
    }

Regole (docs/PROGETTO.md §2-3):
- candidati: tutte le strade entro la distanza della più vicina più la precisione del GPS (almeno SOGLIA_MIN_M),
  non solo la più vicina;
- ente dalla sigla (ref): SS → ANAS, SP → provincia o città metropolitana, A18/A20 → CAS, A19 e le altre ANAS →
  ANAS, nessuna sigla → Comune;
- traversa interna (art. 2 c. 7 CdS): dentro un centro abitato con più di 10.000 abitanti il tratto di una statale,
  regionale o provinciale è del Comune. Il centro è quello dei cartelli OSM, se il punto è tra due cartelli con lo
  stesso nome, altrimenti quello delle sezioni Istat; gli abitanti sono calcolati all'importazione (istat.py). Senza
  sezioni Istat per la regione restano gli indizi di OSM (limite di velocità, nome urbano) come dubbio;
- candidati di enti diversi → "da_scegliere": decide l'utente;
- affidabilità più bassa con ex sigle (old_ref), strade di confine, confine comunale vicino, possibile traversa
  interna, strade minori, GPS impreciso.
"""

import math
import re

DISTANZA_MAX_M = 60.0  # oltre: nessuna strada
SOGLIA_MIN_M = 8.0  # strade alternative: entro la più vicina + max(precisione, SOGLIA_MIN_M)
SOGLIA_MAX_M = 40.0
# Per scegliere la strada principale col GPS, le strade minori (servizio, ciclabili, marciapiedi...) contano come più
# lontane di questi metri. Per un punto toccato sulla mappa no: il tocco è preciso, vale la distanza vera.
PENALITA_MINORI_M = 2.0
RAGGIO_CONFINI_M = 150
PRECISIONE_MEDIA_M = 30
PRECISIONE_BASSA_M = 75

# Traverse interne (docs/PROGETTO.md §14): conta la popolazione del centro abitato, non del Comune. Tra SOGLIA_BASSA e
# SOGLIA_ALTA la stima delle sezioni Istat non basta per decidere: si segue la soglia vera, con affidabilità bassa
# (soglie decise dall'utente il 09/11/2026).
SOGLIA_ABITANTI = 10_000
SOGLIA_BASSA = 9_500
SOGLIA_ALTA = 10_500
BORDO_CENTRO_M = 150  # così vicino al limite Istat di un centro decide il cartello, che i dati possono non avere

# Tipi di highway che non sono strade su cui si segnala (stessa lista in osm/stradario.lua)
ESCLUSE = {
    "proposed", "construction", "abandoned", "disused", "razed", "platform", "bus_stop", "elevator", "raceway",
    "services", "rest_area", "emergency_bay", "escape", "busway", "via_ferrata", "corridor",
}
MINORI = {"service", "track", "path", "footway", "cycleway", "steps", "pedestrian", "bridleway"}

TIPI = {
    "motorway": "autostrada",
    "motorway_link": "rampa autostradale",
    "trunk": "strada extraurbana principale",
    "trunk_link": "rampa",
    "primary": "strada principale",
    "primary_link": "rampa",
    "secondary": "strada secondaria",
    "secondary_link": "rampa",
    "tertiary": "strada terziaria",
    "tertiary_link": "rampa",
    "unclassified": "strada locale",
    "residential": "strada residenziale",
    "living_street": "zona residenziale",
    "service": "strada di servizio",
    "track": "strada sterrata o interpoderale",
    "path": "sentiero",
    "footway": "percorso pedonale o marciapiede",
    "cycleway": "pista ciclabile",
    "pedestrian": "area pedonale",
    "steps": "scalinata",
    "bridleway": "ippovia",
    "road": "strada (tipo non indicato)",
}

# Sigle (ISO 3166-2:IT) delle città metropolitane e dei liberi consorzi siciliani
CITTA_METROPOLITANE = {"RM", "MI", "NA", "TO", "BA", "FI", "BO", "GE", "VE", "RC", "CA", "PA", "ME", "CT"}
LIBERI_CONSORZI = {"AG", "CL", "EN", "RG", "SR", "TP"}
AUTOSTRADE_CAS = {"A18", "A20"}
AUTOSTRADE_ANAS = {"A2", "A19", "A29", "A90", "A91"}

PREFISSI_URBANI = (
    "via ", "viale ", "corso ", "piazza ", "piazzale ", "largo ", "lungomare ", "vico ", "vicolo ",
    "circonvallazione ",
)
TAG_UTILI = (
    "name", "ref", "old_ref", "highway", "operator", "maxspeed", "zone:maxspeed", "oneway", "lanes", "surface",
    "access", "bicycle", "foot", "name:left", "name:right", "junction", "bridge", "tunnel", "official_name",
    "alt_name", "loc_name", "int_ref",
)
LIVELLI = ("bassa", "media", "alta")


# --- geometria --------------------------------------------------------------------------------------------------

def metri_per_grado(lat: float) -> tuple[float, float]:
    """Metri per grado di latitudine e di longitudine alla latitudine data (ellissoide WGS84)."""
    phi = math.radians(lat)
    m_lat = 111_132.954 - 559.822 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
    m_lon = 111_412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi)
    return m_lat, m_lon


def distanza_m(lat: float, lon: float, geometria: list[list[float]]) -> float:
    """Distanza minima dal punto alla polilinea [[lat, lon], ...]: proiezione locale, precisa al centimetro a qualche
    centinaio di metri."""
    if not geometria:
        return math.inf
    m_lat, m_lon = metri_per_grado(lat)
    pts = [((p[1] - lon) * m_lon, (p[0] - lat) * m_lat) for p in geometria]
    if len(pts) == 1:
        return math.hypot(*pts[0])
    best = math.inf
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        lung2 = dx * dx + dy * dy
        t = 0.0 if lung2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / lung2))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


# --- contesto amministrativo ------------------------------------------------------------------------------------

def _sigla_provincia(prov: dict | None) -> str | None:
    tags = (prov or {}).get("tags") or {}
    ref = (tags.get("ref") or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", ref):
        return ref
    m = re.fullmatch(r"IT-([A-Z]{2})", (tags.get("ISO3166-2") or "").strip().upper())
    return m.group(1) if m else None


class Contesto:
    def __init__(self, intorno: dict):
        self.comune = ((intorno.get("comune") or {}).get("nome") or "").strip() or None
        prov = intorno.get("provincia") or None
        self.provincia = ((prov or {}).get("nome") or "").strip() or None
        self.sigla = _sigla_provincia(prov)
        self.regione = ((intorno.get("regione") or {}).get("nome") or "").strip() or None
        self.comuni_vicini = sorted({n for n in intorno.get("comuni_vicini") or [] if n and n != self.comune})
        # centri abitati: solo se ci sono le sezioni Istat della regione del punto, e solo quelli del Comune del punto
        # (un Comune delimita i centri nel suo territorio: i cartelli di Muggiò non valgono a Monza)
        tags_regione = (intorno.get("regione") or {}).get("tags") or {}
        codice = _intero(str(tags_regione.get("ref:ISTAT") or ""))
        codice_comune = _intero(str((((intorno.get("comune") or {}).get("tags")) or {}).get("ref:ISTAT") or ""))
        # codici Istat (comune a 6 cifre, regione) per collegare l'ente dell'abbinamento alla tabella enti (enti.py)
        self.comune_istat = f"{codice_comune:06d}" if codice_comune is not None else None
        self.regione_istat = codice
        istat = intorno.get("istat") or {}
        self.con_istat = codice is not None and codice in (istat.get("regioni") or [])

        def del_comune(c: dict) -> bool:
            return codice_comune is None or c.get("pro_com") is None or c.get("pro_com") == codice_comune

        centri = [c for c in istat.get("centri") or [] if del_comune(c)] if self.con_istat else []
        self.centro_cartelli = next((c for c in centri if c.get("fonte") == "cartelli"), None)
        self.centro_istat = next((c for c in centri if c.get("fonte") == "istat"), None)
        self.centri_vicini = [c for c in istat.get("vicini") or [] if del_comune(c)] if self.con_istat else []
        # abitanti del Comune intero (tutte le sezioni Istat 2021): nessun suo centro abitato ne ha di più
        self.abitanti_comune = istat.get("abitanti_comune") if self.con_istat else None


def ente_anas(ctx: Contesto) -> dict:
    nome = f"ANAS – Struttura territoriale {ctx.regione}" if ctx.regione else "ANAS"
    return {"codice": "anas", "nome": nome, "contatti": "Pronto ANAS 800 841 148"}


def ente_cas() -> dict:
    return {"codice": "cas", "nome": "CAS – Consorzio per le Autostrade Siciliane"}


def ente_provincia(ctx: Contesto) -> dict:
    nome = ctx.provincia
    if not nome:
        return {"codice": "provincia", "nome": "Provincia o città metropolitana (non determinata)"}
    if nome.lower().startswith(("città metropolitana", "provincia", "libero consorzio")):
        completo = nome
    elif ctx.sigla in CITTA_METROPOLITANE:
        completo = f"Città metropolitana di {nome}"
    elif ctx.sigla in LIBERI_CONSORZI:
        completo = f"Libero consorzio comunale di {nome}"
    else:
        completo = f"Provincia di {nome}"
    return {"codice": "provincia", "nome": completo}


def ente_comune(ctx: Contesto) -> dict:
    return {"codice": "comune", "nome": f"Comune di {ctx.comune}" if ctx.comune else "Comune (non determinato)"}


def _pulisci(nome: str) -> str:
    """Spazi normalizzati e "Città Metropolitana" / "Citta metropolitana" scritti in un modo solo."""
    nome = re.sub(r"\s+", " ", nome.replace(" ", " ")).strip()
    return re.sub(r"(?i)^citt[àa]\s+metropolitana", "Città metropolitana", nome)


def ente_da_gestore(gestore: str, ctx: Contesto) -> dict | None:
    """L'ente indicato da un `operator` di OSM, se è un ente pubblico riconoscibile; altrimenti None.

    Gli enti della zona del punto tornano nella forma usata per le sigle (stesso nome, stesso gruppo): la ciclabile
    della Città metropolitana e la provinciale accanto risultano lo stesso ente.
    """
    nome = _pulisci(gestore or "")
    basso = nome.lower()
    if not nome:
        return None
    if re.search(r"\banas\b", basso):
        return ente_anas(ctx)
    if "autostrade siciliane" in basso or basso == "cas":
        return ente_cas()
    if basso.startswith(("città metropolitana", "provincia ", "libero consorzio")):
        if ctx.provincia and basso.endswith(ctx.provincia.lower()):
            return ente_provincia(ctx)
        return {"codice": "provincia", "nome": nome}
    if basso.startswith("comune di "):
        if ctx.comune and basso == f"comune di {ctx.comune.lower()}":
            return ente_comune(ctx)
        return {"codice": "comune", "nome": nome}
    if basso.startswith("regione "):
        return {"codice": "regione", "nome": nome}
    return None


# --- una strada -------------------------------------------------------------------------------------------------

def _sigle(valore: str | None) -> list[tuple[str, str]]:
    """[(sigla com'è scritta, sigla normalizzata)]: "SS 114;E45" → [("SS 114", "SS114"), ("E45", "E45")]."""
    return [(s.strip(), s.replace(" ", "").upper()) for s in re.split(r"[;,]", valore or "") if s.strip()]


def _classifica(sigle: list[tuple[str, str]]) -> tuple[str | None, str | None, str | None]:
    """(classe, sigla normalizzata, sigla scritta): classe A (autostrada), SS, SP, SR o None."""
    for orig, norm in sigle:
        m = re.match(r"^(A\d+)", norm)
        if m:
            return "A", m.group(1), orig
    for classe, regex in (("SS", r"^(SS|NSA|RA)\d"), ("SP", r"^SP\d"), ("SR", r"^SR\d")):
        for orig, norm in sigle:
            if re.match(regex, norm):
                return classe, norm, orig
    return None, None, None


def _intero(valore: str | None) -> int | None:
    m = re.match(r"^\s*(\d+)\s*$", valore or "")
    return int(m.group(1)) if m else None


def _segni_urbani(tags: dict) -> list[str]:
    segni = []
    limite = _intero(tags.get("maxspeed"))
    if limite is not None and limite <= 50:
        segni.append(f"limite {limite} km/h")
    if (tags.get("zone:maxspeed") or "").upper() == "IT:URBAN":
        segni.append("zona urbana")
    nome = tags.get("name") or ""
    if nome.lower().startswith(PREFISSI_URBANI):
        segni.append(f"nome «{nome}»")
    return segni


def valuta_via(tags: dict, ctx: Contesto, percorsi: list[dict] | None = None) -> dict:
    """Ente competente per una strada, con i motivi e i dubbi.

    percorsi: i percorsi ciclabili (relazioni OSM) di cui fa parte, [{"tipo", "nome", "tags"}].
    Nel risultato anche "livello" (affidabilità massima, 0-2) e "forse_traversa" (solo indizi OSM di traversa).
    """
    esame = {"livello": 2, "forse_traversa": False}
    hw = tags.get("highway") or ""
    tipo = TIPI.get(hw, hw or "strada")
    classe, sigla, sigla_scritta = _classifica(_sigle(tags.get("ref")))
    motivi: list[str] = []
    dubbi: list[str] = []

    if classe == "A" or hw in ("motorway", "motorway_link"):
        if sigla in AUTOSTRADE_CAS:
            ente = ente_cas()
            motivi.append(f"Autostrada {sigla}: in concessione al CAS (art. 14 c. 3 CdS)")
        elif sigla in AUTOSTRADE_ANAS:
            ente = ente_anas(ctx)
            motivi.append(f"Autostrada {sigla}: gestita da ANAS")
        else:
            gestore = tags.get("operator")
            ente = {"codice": "concessionario", "nome": gestore or "Concessionario autostradale (da verificare)"}
            motivi.append(f"Autostrada {sigla or ''}: competente il concessionario (art. 14 c. 3 CdS)".replace("  ", " "))
            if not gestore:
                dubbi.append("Concessionario non indicato in OSM")
    elif classe == "SS":
        ente = ente_anas(ctx)
        motivi.append(f"Strada statale {sigla_scritta}: ente proprietario ANAS (art. 14 CdS)")
        ente = _traversa(tags, ctx, ente, sigla_scritta, motivi, dubbi, esame)
    elif classe == "SP":
        ente = ente_provincia(ctx)
        motivi.append(f"Strada provinciale {sigla_scritta}: ente proprietario {ente['nome']} (art. 14 CdS)")
        ente = _traversa(tags, ctx, ente, sigla_scritta, motivi, dubbi, esame)
    elif classe == "SR":
        ente = {"codice": "regione", "nome": f"Regione {ctx.regione}" if ctx.regione else "Regione"}
        motivi.append(f"Strada regionale {sigla_scritta}")
        dubbi.append("Strada regionale: spesso la gestiscono province o società regionali, da verificare")
        ente = _traversa(tags, ctx, ente, sigla_scritta, motivi, dubbi, esame)
    else:
        # senza sigla: prima il gestore scritto in OSM (sul tratto, poi sul percorso ciclabile), poi il Comune
        gestore, da_percorso = _gestore(tags, percorsi or [], ctx)
        if gestore is not None:
            ente = gestore["ente"]
            motivi.append(gestore["motivo"])
        else:
            ente = ente_comune(ctx)
            if hw in MINORI:
                motivi.append(f"{tipo[:1].upper()}{tipo[1:]} senza sigla: di norma del Comune")
                dubbi.append(f"{tipo[:1].upper()}{tipo[1:]}: può essere pertinenza della strada accanto, o privata")
            else:
                motivi.append("Nessuna sigla di strada statale o provinciale: strada comunale")
        for p in da_percorso:
            motivi.append(p)
        vecchie = _sigle(tags.get("old_ref"))
        if vecchie:
            dubbi.append(
                f"Ex {vecchie[0][0]}: in OSM ha perso la sigla, probabilmente ceduta al Comune. "
                "Verifica i cartelli di inizio/fine competenza"
            )
        if tags.get("access") in ("private", "no"):
            dubbi.append("Accesso privato: probabilmente non è una strada pubblica")

    sinistra, destra = tags.get("name:left"), tags.get("name:right")
    if sinistra and destra and sinistra != destra:
        dubbi.append(f"Strada di confine: «{sinistra}» da un lato, «{destra}» dall'altro")
    gestore = tags.get("operator")
    if gestore and classe in ("SS", "SP", "SR"):
        motivi.append(f"Gestore indicato in OSM: {gestore}")
    return {"ente": ente, "motivi": motivi, "dubbi": dubbi, **esame}


def _gestore(tags: dict, percorsi: list[dict], ctx: Contesto) -> tuple[dict | None, list[str]]:
    """(gestore riconosciuto con il motivo, altre note sui percorsi) per una strada senza sigla.

    - `operator` sul tratto: è chi lo gestisce, si usa sempre (anche se non è un ente pubblico riconoscibile);
    - `operator` del percorso ciclabile: si usa solo se è un ente pubblico (Comune, Città metropolitana, Provincia,
      Regione, ANAS); pro loco, consorzi turistici e fondazioni promuovono il percorso ma non gestiscono la strada,
      quindi restano solo una nota.
    """
    note: list[str] = []
    sul_tratto = _pulisci(tags.get("operator") or "")
    if sul_tratto:
        ente = ente_da_gestore(sul_tratto, ctx) or {"codice": "altro", "nome": sul_tratto}
        return {"ente": ente, "motivo": f"Gestore indicato in OSM sul tratto: {sul_tratto}"}, note
    trovato = None
    for p in percorsi:
        gestore = _pulisci((p.get("tags") or {}).get("operator") or "")
        nome = p.get("nome") or (p.get("tags") or {}).get("name") or "senza nome"
        if not gestore:
            continue
        ente = ente_da_gestore(gestore, ctx)
        if ente is not None and trovato is None:
            trovato = {"ente": ente, "motivo": f"Fa parte del percorso ciclabile «{nome}», gestito da {gestore} (OSM)"}
        elif ente is None:
            note.append(f"Fa parte del percorso «{nome}», promosso da {gestore}: non è un ente che gestisce strade")
    return trovato, note


def _forse_traversa(tags: dict, dubbi: list[str]) -> bool:
    """Senza le sezioni Istat: indizi di OSM che il tratto sia dentro un centro abitato, come dubbio."""
    segni = _segni_urbani(tags)
    if segni:
        dubbi.append(
            "Possibile traversa interna (" + ", ".join(segni) + "): se il centro abitato attraversato, come lo "
            "delimita il Comune, ha più di 10.000 abitanti (il centro, non il Comune intero), il tratto è del Comune "
            "(art. 2 c. 7 CdS, art. 5 c. 3 del Regolamento); in OSM i tratti ceduti di solito perdono la sigla"
        )
    return bool(segni)


def _ab(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _traversa(tags: dict, ctx: Contesto, ente: dict, sigla: str | None, motivi: list[str], dubbi: list[str],
              esame: dict) -> dict:
    """Ente del tratto di una strada statale, regionale o provinciale: dentro un centro abitato con più di 10.000
    abitanti è del Comune (art. 2 c. 7 CdS). Il centro è quello dei cartelli OSM (la delimitazione del Comune, se il
    punto è tra due cartelli con lo stesso nome), altrimenti quello delle sezioni Istat; gli abitanti sono quelli
    calcolati all'importazione (osm.centri)."""
    if not ctx.con_istat:
        esame["forse_traversa"] = _forse_traversa(tags, dubbi)
        return ente
    centro = ctx.centro_cartelli or ctx.centro_istat
    if centro is None:
        motivi.append("Fuori dai centri abitati (sezioni Istat 2021 e cartelli OSM): niente traversa interna")
        for v in ctx.centri_vicini[:1]:
            dubbi.append(
                f"A {v.get('distanza_m')} m dal centro abitato «{v.get('nome')}» (circa {_ab(v['popolazione'])} "
                "abitanti secondo Istat): se il cartello di inizio centro abitato è prima di qui, il tratto è del Comune"
            )
        return ente

    nome = centro.get("nome") or "senza nome"
    if centro is ctx.centro_cartelli:
        dove = f"tra i cartelli di centro abitato «{nome}» (OSM)"
    elif centro.get("tipo_loc") == 2:
        dove = f"nel nucleo abitato «{nome}»"
    else:
        dove = f"nel centro abitato «{nome}»"
    pop, fonte = centro.get("popolazione"), "sezioni Istat 2021"
    if pop is None and ctx.centro_istat is not None and centro is not ctx.centro_istat:
        # cartelli senza una località Istat collegata: gli abitanti del centro Istat in cui cade il punto
        pop = ctx.centro_istat.get("popolazione")
        fonte = f"sezioni Istat 2021 del centro «{ctx.centro_istat.get('nome')}»"
    ab_comune = ctx.abitanti_comune
    del_comune = f"Comune di {ctx.comune}" if ctx.comune else "Comune"
    if pop is None and ab_comune is not None and ab_comune < SOGLIA_BASSA:
        # i cartelli portano spesso il nome del Comune e non della frazione (Rodano: le frazioni sono Lucino,
        # Millepini, Pobbiano): niente località collegata, ma un centro non può avere più abitanti del suo Comune
        motivi.append(
            f"Sei {dove}. Il {del_comune} ha in tutto {_ab(ab_comune)} abitanti (sezioni Istat 2021): nessun suo "
            "centro abitato arriva a 10.000, il tratto resta all'ente della strada (art. 2 c. 7 CdS)"
        )
        return ente
    if pop is None and centro.get("popolazione_osm"):
        pop, fonte = centro["popolazione_osm"], "OSM"
    if pop is None:
        in_tutto = f" (il {del_comune} ne ha in tutto {_ab(ab_comune)})" if ab_comune is not None else ""
        dubbi.append(f"Sei {dove}, ma non si sa quanti abitanti ha{in_tutto}: con più di 10.000 il tratto è del Comune")
        return ente
    abitanti = f"circa {_ab(pop)} abitanti ({fonte})"

    hw = tags.get("highway") or ""
    if hw in ("trunk", "trunk_link"):
        motivi.append(f"Sei {dove}, {abitanti}")
        if pop >= SOGLIA_BASSA:
            dubbi.append(
                "Strada extraurbana principale: varianti e tangenziali senza accessi restano dell'ente anche dentro "
                "un centro con più di 10.000 abitanti; se invece è la strada che attraversa il centro, il tratto è del "
                "Comune"
            )
        return ente

    if pop >= SOGLIA_ABITANTI:
        nuovo = ente_comune(ctx)
        motivi.append(
            f"Traversa interna: sei {dove}, {abitanti}. Sopra i 10.000 abitanti il tratto è del Comune "
            f"(art. 2 c. 7 CdS), anche se in OSM ha ancora la sigla {sigla}"
        )
    else:
        nuovo = ente
        motivi.append(f"Sei {dove}, {abitanti}: sotto i 10.000 il tratto resta all'ente della strada (art. 2 c. 7 CdS)")
    if SOGLIA_BASSA <= pop < SOGLIA_ALTA:
        esame["livello"] = 0
        dubbi.append(
            "Abitanti vicini alla soglia dei 10.000: decidono la delimitazione del centro abitato fatta dal Comune e "
            "la popolazione che ha usato"
        )
    pop_loc = centro.get("popolazione_localita")
    if pop < SOGLIA_ABITANTI and centro.get("diviso") and pop_loc and pop_loc >= SOGLIA_ABITANTI:
        loc = centro.get("localita_istat") or "?"
        if centro is ctx.centro_cartelli:
            motivi.append(f"Istat lo conta nel centro «{loc}» ({_ab(pop_loc)} abitanti), ma i cartelli lo delimitano a parte")
        else:
            dubbi.append(
                f"Istat conta «{nome}» nel centro «{loc}» ({_ab(pop_loc)} abitanti): se il Comune li ha delimitati "
                "come un solo centro abitato, il tratto è del Comune"
            )
    osm = centro.get("popolazione_osm")
    if osm and fonte != "OSM" and (osm >= SOGLIA_ABITANTI) != (pop >= SOGLIA_ABITANTI):
        dubbi.append(f"Per OSM «{nome}» ha {_ab(osm)} abitanti (a volte è la popolazione del Comune intero)")
    bordo = centro.get("dal_bordo_m")
    if centro is ctx.centro_istat and bordo is not None and bordo < BORDO_CENTRO_M:
        dubbi.append(
            f"A {bordo} m dal limite del centro secondo Istat: il limite che conta è quello dei cartelli di centro "
            "abitato"
        )
    return nuovo


# --- il punto ---------------------------------------------------------------------------------------------------

def _riassunto(d: float, via: dict) -> dict:
    tags = via.get("tags") or {}
    hw = tags.get("highway") or ""
    return {
        "osm_way": via["osm_way"],
        "nome": tags.get("name"),
        "ref": tags.get("ref"),
        "highway": hw,
        "tipo": TIPI.get(hw, hw),
        "distanza_m": round(d, 1),
    }


def _punti(intorno: dict | None, chiave: str) -> list[dict]:
    """Cartelli di centro abitato o località vicini, in breve (nome, distanza, popolazione, posizione)."""
    uscita = []
    for p in (intorno or {}).get(chiave) or []:
        tags = p.get("tags") or {}
        uscita.append({
            "nome": p.get("nome") or tags.get("name"),
            "distanza_m": p.get("distanza_m"),
            "tipo": tags.get("place"),
            "popolazione": _intero(tags.get("population")),
            "lat": p.get("lat"),
            "lon": p.get("lon"),
        })
    return uscita


def _centro(c: dict | None) -> dict | None:
    if not c:
        return None
    chiavi = ("fonte", "nome", "popolazione", "popolazione_localita", "localita_istat", "popolazione_osm", "tipo_loc")
    return {k: c.get(k) for k in chiavi}


def _base(lat: float, lon: float, precisione_m: float | None, ctx: Contesto | None, intorno: dict | None) -> dict:
    return {
        "punto": {"lat": lat, "lon": lon, "precisione_m": precisione_m},
        "comune": ctx.comune if ctx else None,
        "comune_istat": ctx.comune_istat if ctx else None,
        "regione_istat": ctx.regione_istat if ctx else None,
        "provincia": ctx.provincia if ctx else None,
        "sigla_provincia": ctx.sigla if ctx else None,
        "regione": ctx.regione if ctx else None,
        "comuni_vicini": ctx.comuni_vicini if ctx else [],
        "cartelli": _punti(intorno, "cartelli"),
        "localita": _punti(intorno, "localita"),
        # centro abitato del punto: dai cartelli OSM o dalle sezioni Istat (None fuori, o senza sezioni Istat)
        "sezioni_istat": bool(ctx and ctx.con_istat),
        "centro": _centro(ctx and (ctx.centro_cartelli or ctx.centro_istat)),
        "centro_istat": _centro(ctx and ctx.centro_cartelli and ctx.centro_istat),
        "abitanti_comune": ctx.abitanti_comune if ctx else None,
        "fonte": (intorno or {}).get("fonte"),
    }


def fuori_area(lat: float, lon: float, precisione_m: float | None, area: str) -> dict:
    return {
        **_base(lat, lon, precisione_m, None, None),
        "esito": "fuori_area",
        "ente": None,
        "ente_suggerito": None,
        "affidabilita": "bassa",
        "motivi": [f"Il punto è fuori dall'area dello stradario ({area})"],
        "dubbi": [],
        "principale": None,
        "candidati": [],
        "opzioni": [],
        "vicine": [],
    }


def abbina(lat: float, lon: float, precisione_m: float | None, intorno: dict, origine: str = "gps") -> dict:
    """origine: "gps" (posizione del telefono) o "mappa" (punto toccato sulla mappa: niente penalità alle strade
    minori nella scelta della principale)."""
    ctx = Contesto(intorno)
    penalita = 0.0 if origine == "mappa" else PENALITA_MINORI_M
    base = _base(lat, lon, precisione_m, ctx, intorno)

    misurate = []
    for via in intorno.get("vie") or []:
        hw = (via.get("tags") or {}).get("highway")
        if not hw or hw in ESCLUSE:
            continue
        misurate.append((distanza_m(lat, lon, via.get("geometria") or []), via))
    misurate.sort(key=lambda x: x[0])
    entro = [(d, v) for d, v in misurate if d <= DISTANZA_MAX_M]

    if not entro:
        return {
            **base,
            "esito": "nessuna_strada",
            "ente": None,
            "ente_suggerito": None,
            "affidabilita": "bassa",
            "motivi": [f"Nessuna strada entro {int(DISTANZA_MAX_M)} m"],
            "dubbi": [],
            "principale": None,
            "candidati": [],
            "opzioni": [],
            "vicine": [_riassunto(d, v) for d, v in misurate[:8]],
        }

    soglia = min(SOGLIA_MAX_M, max(SOGLIA_MIN_M, precisione_m or 0.0))
    limite = entro[0][0] + soglia
    candidati = []
    for d, via in entro:
        if d > limite:
            break
        tags = via.get("tags") or {}
        hw = tags.get("highway") or ""
        valutazione = valuta_via(tags, ctx, via.get("percorsi"))
        candidati.append({
            "osm_way": via["osm_way"],
            "nome": tags.get("name"),
            "ref": tags.get("ref"),
            "old_ref": tags.get("old_ref"),
            "highway": hw,
            "tipo": TIPI.get(hw, hw),
            "distanza_m": round(d, 1),
            "_punteggio": d + (penalita if hw in MINORI else 0.0),
            **valutazione,
            "percorsi": [
                {"nome": p.get("nome"), "gestore": _pulisci((p.get("tags") or {}).get("operator") or "") or None}
                for p in via.get("percorsi") or []
            ],
            "tags": {k: tags[k] for k in TAG_UTILI if k in tags},
            "geometria": via.get("geometria") or [],
        })
    candidati.sort(key=lambda c: c["_punteggio"])
    esami = {}
    for c in candidati:
        del c["_punteggio"]
        esami[c["osm_way"]] = {"livello": c.pop("livello"), "forse_traversa": c.pop("forse_traversa")}
    principale = candidati[0]
    esame = esami[principale["osm_way"]]

    gruppi: dict[tuple[str, str], dict] = {}
    for c in candidati:
        chiave = (c["ente"]["codice"], c["ente"]["nome"])
        gruppi.setdefault(chiave, {"ente": c["ente"], "vie": []})["vie"].append(c["osm_way"])
    da_scegliere = len(gruppi) > 1

    motivi = list(principale["motivi"])
    dubbi = list(principale["dubbi"])
    if esame["forse_traversa"]:
        # per la traversa conta il centro abitato delimitato dal Comune: i cartelli e le frazioni di OSM ci si
        # avvicinano più dei poligoni Istat, che fondono i centri contigui
        indizi = []
        if base["localita"]:
            loc = base["localita"][0]
            pop = f", {loc['popolazione']:,} abitanti secondo OSM".replace(",", ".") if loc["popolazione"] else ""
            indizi.append(f"località più vicina «{loc['nome']}» a {loc['distanza_m']} m{pop}")
        if base["cartelli"]:
            cart = base["cartelli"][0]
            indizi.append(f"cartello di centro abitato «{cart['nome']}» a {cart['distanza_m']} m")
        if indizi:
            dubbi.append("Per capire in quale centro abitato sei: " + "; ".join(indizi))
    livello = 2
    if da_scegliere:
        livello = 0
        dubbi.insert(0, "Entro la precisione del GPS ci sono strade di enti diversi: indica su quale sei")
    if principale["dubbi"]:
        livello = min(livello, 1)
    livello = min(livello, esame["livello"])
    if ctx.comuni_vicini:
        livello = min(livello, 1)
        dubbi.append("Vicino al confine con: " + ", ".join(ctx.comuni_vicini))
    if precisione_m is not None and precisione_m > PRECISIONE_BASSA_M:
        livello = 0
        dubbi.append(f"GPS impreciso (±{round(precisione_m)} m): aspetta qualche secondo all'aperto e riprova")
    elif precisione_m is not None and precisione_m > PRECISIONE_MEDIA_M:
        livello = min(livello, 1)
        dubbi.append(f"GPS poco preciso (±{round(precisione_m)} m)")
    if ctx.comune is None:
        livello = 0
        dubbi.append("Comune non determinato")

    ids = {c["osm_way"] for c in candidati}
    return {
        **base,
        "esito": "da_scegliere" if da_scegliere else "deciso",
        "ente": None if da_scegliere else principale["ente"],
        "ente_suggerito": principale["ente"],
        "affidabilita": LIVELLI[livello],
        "motivi": motivi,
        "dubbi": dubbi,
        "principale": principale["osm_way"],
        "candidati": candidati,
        "opzioni": list(gruppi.values()),
        "vicine": [_riassunto(d, v) for d, v in misurate if v["osm_way"] not in ids][:10],
    }
