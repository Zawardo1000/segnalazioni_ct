"""Regole dell'abbinamento sui casi verificati sul posto (tests/fixtures/casi) e su casi costruiti a mano."""

import pytest

from segnalazioni.abbinamento import abbina, distanza_m, valuta_via, Contesto
from tests.helpers import caso


def _abbina(nome: str) -> dict:
    c = caso(nome)
    p = c["punto"]
    return abbina(p["lat"], p["lon"], p["precisione_m"], c["intorno"])


def test_c1_capomulini_statale():
    r = _abbina("c1-capomulini-ss114")
    assert r["esito"] == "deciso"
    assert r["ente"]["codice"] == "anas"
    assert r["ente"]["nome"] == "ANAS – Struttura territoriale Sicilia"
    assert r["principale"] in {186213866, 180892598}  # le due carreggiate di Viale della Fiera Franca
    assert r["comune"] == "Acireale"
    # limite 30 km/h e nome urbano: il dubbio della traversa interna c'è, ma l'ente resta ANAS
    assert any("traversa interna" in d for d in r["dubbi"])
    assert r["affidabilita"] == "media"


def test_c2_capomulini_ex_statale_al_confine():
    r = _abbina("c2-capomulini-confine")
    assert r["esito"] == "deciso"
    assert r["ente"] == {"codice": "comune", "nome": "Comune di Acireale"}
    assert r["principale"] == 401896762
    assert r["comuni_vicini"] == ["Aci Castello"]
    assert r["affidabilita"] != "alta"
    testo = " ".join(r["dubbi"])
    assert "Ex SS114" in testo and "Strada di confine" in testo and "Aci Castello" in testo
    # il tratto con ref SS114, 55 m più a nord, non è un candidato
    assert 401896761 not in {c["osm_way"] for c in r["candidati"]}


def test_c3_segrate_tracciati_paralleli():
    r = _abbina("c3-segrate-rivoltana")
    assert r["esito"] == "da_scegliere"
    assert r["ente"] is None
    assert r["ente_suggerito"]["nome"] == "Città metropolitana di Milano"
    principale = next(c for c in r["candidati"] if c["osm_way"] == r["principale"])
    assert principale["ref"] == "SP15bis"
    assert r["affidabilita"] == "bassa"


def test_c3_ciclabile_della_citta_metropolitana():
    r = _abbina("c3-segrate-rivoltana")
    ciclabile = next(c for c in r["candidati"] if c["osm_way"] == 904268227)
    assert ciclabile["ente"]["nome"] == "Città metropolitana di Milano"


def test_c4_segrate_ciclabile_del_percorso_cambio():
    r = _abbina("c4-segrate-ciclabile")
    # l'utente era sulla ciclabile "Cambio Linea 6": la gestisce la Città metropolitana (operator del percorso
    # "Linea 6 Biciplan Cambio"), come la rampa e la provinciale accanto, quindi non c'è niente da scegliere
    assert r["esito"] == "deciso"
    assert r["ente"]["nome"] == "Città metropolitana di Milano"
    ciclabile = next(c for c in r["candidati"] if c["osm_way"] == 904268227)
    assert ciclabile["ente"] == {"codice": "provincia", "nome": "Città metropolitana di Milano"}
    assert any("Linea 6 Biciplan Cambio" in m for m in ciclabile["motivi"])
    # "Linea azzurra" non ha gestore: non conta
    assert {p["nome"] for p in ciclabile["percorsi"]} == {"Linea 6 Biciplan Cambio", "Linea azzurra"}


CTX_CT = Contesto({"comune": {"nome": "Catania"}, "provincia": {"nome": "Catania", "tags": {"ref": "CT"}},
                   "regione": {"nome": "Sicilia"}})


@pytest.mark.parametrize(
    "tags, codice, nome",
    [
        ({"highway": "primary", "ref": "SS 114"}, "anas", "ANAS – Struttura territoriale Sicilia"),
        ({"highway": "trunk", "ref": "SS114;E45"}, "anas", "ANAS – Struttura territoriale Sicilia"),
        ({"highway": "motorway", "ref": "A18"}, "cas", "CAS – Consorzio per le Autostrade Siciliane"),
        ({"highway": "motorway", "ref": "A19"}, "anas", "ANAS – Struttura territoriale Sicilia"),
        ({"highway": "secondary", "ref": "SP52"}, "provincia", "Città metropolitana di Catania"),
        ({"highway": "residential", "name": "Via Etnea"}, "comune", "Comune di Catania"),
        ({"highway": "secondary", "ref": "SR 7"}, "regione", "Regione Sicilia"),
    ],
)
def test_ente_dalla_sigla(tags, codice, nome):
    ente = valuta_via(tags, CTX_CT)["ente"]
    assert (ente["codice"], ente["nome"]) == (codice, nome)


@pytest.mark.parametrize(
    "provincia, sigla, atteso",
    [
        ("Siracusa", "SR", "Libero consorzio comunale di Siracusa"),
        ("Bergamo", "BG", "Provincia di Bergamo"),
        ("Milano", "MI", "Città metropolitana di Milano"),
        ("Città metropolitana di Catania", None, "Città metropolitana di Catania"),
    ],
)
def test_nome_della_provincia(provincia, sigla, atteso):
    tags = {"ISO3166-2": f"IT-{sigla}"} if sigla else {}
    ctx = Contesto({"comune": {"nome": "X"}, "provincia": {"nome": provincia, "tags": tags}})
    assert valuta_via({"highway": "secondary", "ref": "SP 3"}, ctx)["ente"]["nome"] == atteso


CTX_MI = Contesto({"comune": {"nome": "Segrate"}, "provincia": {"nome": "Milano", "tags": {"ISO3166-2": "IT-MI"}},
                   "regione": {"nome": "Lombardia"}})


@pytest.mark.parametrize(
    "gestore, atteso",
    [
        ("Città Metropolitana di Milano", ("provincia", "Città metropolitana di Milano")),  # come le SP: stesso ente
        ("Citta metropolitana di Milano", ("provincia", "Città metropolitana di Milano")),
        ("Città Metropolitana di Bergamo", ("provincia", "Città metropolitana di Bergamo")),
        ("Comune di Segrate", ("comune", "Comune di Segrate")),
        ("Comune di Cremona", ("comune", "Comune di Cremona")),
        ("ANAS S.p.A.", ("anas", "ANAS – Struttura territoriale Lombardia")),
        ("Regione Lombardia", ("regione", "Regione Lombardia")),
        ("Consorzio Pro Loco Valle di Ledro", None),
        ("Stiftung SchweizMobil", None),
        ("http://www.provincia.modena.it/", None),
    ],
)
def test_ente_da_gestore(gestore, atteso):
    from segnalazioni.abbinamento import ente_da_gestore

    ente = ente_da_gestore(gestore, CTX_MI)
    assert (None if ente is None else (ente["codice"], ente["nome"])) == atteso


def test_gestore_sul_tratto_e_sul_percorso():
    ciclabile = {"highway": "cycleway", "name": "Ciclabile"}
    # nessun gestore: Comune, con il dubbio
    v = valuta_via(ciclabile, CTX_MI)
    assert v["ente"]["nome"] == "Comune di Segrate" and v["dubbi"]
    # gestore del percorso: ente pubblico, si usa
    percorso = [{"tipo": "bicycle", "nome": "Linea 6 Biciplan Cambio", "tags": {"operator": "Città Metropolitana di Milano"}}]
    v = valuta_via(ciclabile, CTX_MI, percorso)
    assert v["ente"]["nome"] == "Città metropolitana di Milano" and not v["dubbi"]
    # gestore del percorso non pubblico: resta il Comune, con una nota
    pro_loco = [{"tipo": "bicycle", "nome": "Giro del lago", "tags": {"operator": "Pro Loco di Segrate"}}]
    v = valuta_via(ciclabile, CTX_MI, pro_loco)
    assert v["ente"]["nome"] == "Comune di Segrate"
    assert any("Pro Loco di Segrate" in m and "non è un ente" in m for m in v["motivi"])
    # gestore sul tratto: vince sul percorso, anche se non è un ente pubblico
    v = valuta_via({**ciclabile, "operator": "Parco Nord Milano"}, CTX_MI, percorso)
    assert v["ente"] == {"codice": "altro", "nome": "Parco Nord Milano"}


def test_ex_sigla_e_strada_privata():
    v = valuta_via({"highway": "service", "old_ref": "SP 5", "access": "private"}, CTX_CT)
    assert v["ente"]["codice"] == "comune"
    testo = " ".join(v["dubbi"])
    assert "Ex SP 5" in testo and "privato" in testo


def _via(osm_way, punti, **tags):
    return {"osm_way": osm_way, "tags": {"highway": "residential", **tags}, "geometria": punti}


def test_distanza():
    # 0,001 gradi di latitudine a 37,5° di latitudine sono circa 111 m
    assert distanza_m(37.5, 15.0, [[37.501, 14.99], [37.501, 15.01]]) == pytest.approx(111.0, abs=1.0)
    assert distanza_m(37.5, 15.0, [[37.5, 15.0]]) == 0


def test_principale_col_gps_e_dalla_mappa():
    # come la prova 15 di Segrate: ciclabile a 2,4 m, strada comunale a 3,3 m
    vie = [_via(1, [[37.5 + 2.4 / 111_000, 14.99], [37.5 + 2.4 / 111_000, 15.01]], highway="cycleway"),
           _via(2, [[37.5 - 3.3 / 111_000, 14.99], [37.5 - 3.3 / 111_000, 15.01]], highway="tertiary")]
    intorno = {"vie": vie, "comune": {"nome": "X"}}
    # GPS: la ciclabile conta 2 m in più (4,4 m) e vince la strada
    assert abbina(37.5, 15.0, 5, intorno)["principale"] == 2
    # punto toccato sulla mappa: distanza vera, vince la ciclabile
    assert abbina(37.5, 15.0, 5, intorno, origine="mappa")["principale"] == 1


def test_nessuna_strada_vicina():
    r = abbina(37.5, 15.0, 5, {"vie": [_via(1, [[37.502, 14.99], [37.502, 15.01]])], "comune": {"nome": "X"}})
    assert r["esito"] == "nessuna_strada"
    assert r["vicine"][0]["osm_way"] == 1


def test_strade_in_costruzione_ignorate():
    vie = [_via(1, [[37.5, 14.99], [37.5, 15.01]], highway="construction"),
           _via(2, [[37.5001, 14.99], [37.5001, 15.01]], name="Via Buona")]
    r = abbina(37.5, 15.0, 5, {"vie": vie, "comune": {"nome": "X"}})
    assert r["principale"] == 2 and r["esito"] == "deciso"


def test_gps_impreciso_affidabilita_bassa():
    vie = [_via(1, [[37.5, 14.99], [37.5, 15.01]], name="Via Unica")]
    assert abbina(37.5, 15.0, 5, {"vie": vie, "comune": {"nome": "X"}})["affidabilita"] == "alta"
    r = abbina(37.5, 15.0, 120, {"vie": vie, "comune": {"nome": "X"}})
    assert r["affidabilita"] == "bassa"
    assert any("GPS impreciso" in d for d in r["dubbi"])


def test_comune_non_determinato():
    vie = [_via(1, [[37.5, 14.99], [37.5, 15.01]])]
    r = abbina(37.5, 15.0, 5, {"vie": vie})
    assert r["ente"]["nome"] == "Comune (non determinato)"
    assert r["affidabilita"] == "bassa"


# --- traverse interne: la regola dei 10.000 abitanti (centri da osm.centri, calcolati all'importazione) ------------

def _con_centri(centri: list[dict], vicini: list[dict] | None = None, regioni: list[int] | None = None) -> Contesto:
    return Contesto({
        "comune": {"nome": "Acireale"}, "provincia": {"nome": "Catania", "tags": {"ref": "CT"}},
        "regione": {"nome": "Sicilia", "tags": {"ref:ISTAT": "19"}},
        "istat": {"regioni": [19] if regioni is None else regioni, "centri": centri, "vicini": vicini or []},
    })


def _centro(nome: str, pop: int | None, fonte: str = "istat", **kw) -> dict:
    return {"fonte": fonte, "nome": nome, "popolazione": pop, "popolazione_localita": pop, "popolazione_osm": None,
            "localita_istat": nome, "diviso": False, "tipo_loc": 1, "dal_bordo_m": 500 if fonte == "istat" else None,
            **kw}


SS = {"highway": "primary", "ref": "SS 114"}


def test_traversa_di_un_centro_grande_e_del_comune():
    v = valuta_via(SS, _con_centri([_centro("Catania", 296286)]))
    assert v["ente"] == {"codice": "comune", "nome": "Comune di Acireale"}
    assert any(m.startswith("Traversa interna") and "296.286 abitanti" in m for m in v["motivi"])
    assert v["dubbi"] == [] and v["livello"] == 2
    sp = valuta_via({"highway": "secondary", "ref": "SP 2"}, _con_centri([_centro("Catania", 296286)]))
    assert sp["ente"]["codice"] == "comune"


def test_centro_piccolo_resta_all_ente_della_strada():
    v = valuta_via(SS, _con_centri([_centro("Aci Trezza", 4441)]))
    assert v["ente"]["codice"] == "anas"
    assert any("sotto i 10.000" in m for m in v["motivi"])


@pytest.mark.parametrize("pop, codice", [(9800, "anas"), (10200, "comune")])
def test_vicino_alla_soglia_affidabilita_bassa(pop, codice):
    v = valuta_via(SS, _con_centri([_centro("Riposto", pop)]))
    assert v["ente"]["codice"] == codice  # decide la soglia vera, 10.000
    assert v["livello"] == 0 and any("vicini alla soglia" in d for d in v["dubbi"])


def test_frazione_contata_da_istat_con_il_capoluogo():
    frazione = _centro("Santa Maria delle Grazie", 1074, diviso=True, popolazione_localita=48328,
                       localita_istat="Acireale")
    v = valuta_via(SS, _con_centri([frazione]))
    assert v["ente"]["codice"] == "anas"
    assert any("Istat conta «Santa Maria delle Grazie» nel centro «Acireale» (48.328 abitanti)" in d
               for d in v["dubbi"])
    # tra i cartelli della frazione: il Comune l'ha delimitata a parte, niente dubbio
    cartelli = _centro("Capomulini", 377, fonte="cartelli", diviso=True, popolazione_localita=48328,
                       localita_istat="Acireale")
    v = valuta_via(SS, _con_centri([cartelli, frazione]))
    assert v["ente"]["codice"] == "anas" and v["dubbi"] == []
    assert any("tra i cartelli di centro abitato «Capomulini»" in m for m in v["motivi"])


def test_cartelli_senza_abitanti_usano_il_centro_istat():
    v = valuta_via(SS, _con_centri([_centro("Catania", None, fonte="cartelli"), _centro("Catania", 296286)]))
    assert v["ente"]["codice"] == "comune"
    assert any("sezioni Istat 2021 del centro «Catania»" in m for m in v["motivi"])


def test_fuori_dai_centri_e_vicino_al_limite():
    vicino = _centro("Acireale", 28794, distanza_m=40)
    v = valuta_via(SS, _con_centri([], [vicino]))
    assert v["ente"]["codice"] == "anas"
    assert any("Fuori dai centri abitati" in m for m in v["motivi"])
    assert any(d.startswith("A 40 m dal centro abitato «Acireale»") for d in v["dubbi"])
    # dentro, ma vicino al limite Istat
    v = valuta_via(SS, _con_centri([_centro("Acireale", 28794, dal_bordo_m=60)]))
    assert v["ente"]["codice"] == "comune" and any("dal limite del centro" in d for d in v["dubbi"])


def test_contano_solo_i_centri_del_comune_del_punto():
    # come a Monza vicino al confine: i cartelli di Muggiò sconfinano, ma il centro abitato lo delimita ogni Comune
    ctx = Contesto({
        "comune": {"nome": "Monza", "tags": {"ref:ISTAT": "108033"}},
        "provincia": {"nome": "Monza e della Brianza", "tags": {"ref": "MB"}},
        "regione": {"nome": "Lombardia", "tags": {"ref:ISTAT": "03"}},
        "istat": {"regioni": [3], "vicini": [],
                  "centri": [_centro("Muggiò", 23496, fonte="cartelli", pro_com=108034),
                             _centro("Monza", 121613, pro_com=108033)]},
    })
    v = valuta_via({"highway": "tertiary", "ref": "SP151"}, ctx)
    assert v["ente"] == {"codice": "comune", "nome": "Comune di Monza"}
    assert any("«Monza»" in m and "121.613" in m for m in v["motivi"])
    assert not any("Muggiò" in m for m in v["motivi"])


def test_strada_extraurbana_principale_in_un_centro_grande():
    v = valuta_via({"highway": "trunk", "ref": "SS 114"}, _con_centri([_centro("Catania", 296286)]))
    assert v["ente"]["codice"] == "anas"
    assert any("tangenziali" in d for d in v["dubbi"])


def test_senza_sezioni_istat_della_regione_restano_gli_indizi():
    lenta = {**SS, "maxspeed": "50"}
    v = valuta_via(lenta, _con_centri([_centro("Catania", 296286)], regioni=[3]))
    assert v["ente"]["codice"] == "anas" and v["forse_traversa"]
    assert any("Possibile traversa" in d for d in v["dubbi"])


def test_abbina_riporta_il_centro_e_l_affidabilita():
    intorno = {"vie": [_via(1, [[37.5, 14.99], [37.5, 15.01]], ref="SS 114")],
               "comune": {"nome": "Riposto"}, "provincia": {"nome": "Catania", "tags": {"ref": "CT"}},
               "regione": {"nome": "Sicilia", "tags": {"ref:ISTAT": "19"}},
               "istat": {"regioni": [19], "centri": [_centro("Riposto", 10030)], "vicini": []}}
    r = abbina(37.5, 15.0, 5, intorno)
    assert r["ente"]["nome"] == "Comune di Riposto"
    assert r["affidabilita"] == "bassa"
    assert r["centro"]["nome"] == "Riposto" and r["centro"]["popolazione"] == 10030 and r["sezioni_istat"]
    assert "livello" not in r["candidati"][0]
