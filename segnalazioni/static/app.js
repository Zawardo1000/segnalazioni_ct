"use strict";
// Pagina principale: posizione GPS (o punto scelto sulla mappa) → ente competente per la strada, e segnalazione di una
// carcassa o di rifiuti con una foto. Solo Leaflet come libreria (più comune.js). I testi che arrivano da OSM (nomi,
// sigle, tag) sono dati non fidati: passano sempre da textContent, mai da innerHTML.

const COLORI = {
  anas: "#c62828", cas: "#6a1b9a", provincia: "#1565c0", comune: "#2e7d32", regione: "#ef6c00",
  concessionario: "#6a1b9a",
};
const COLORE_ALTRO = "#616161";
const TITOLI = { nessuna_strada: "Nessuna strada vicina", fuori_area: "Fuori dall'area coperta" };
const ENTI_CORRETTI = ["ANAS", "Città metropolitana o provincia", "Comune", "CAS", "Privato", "Altro"];
const POSIZIONE_FRESCA_MS = 15000;
// foto: lato lungo e peso massimi dopo la ricodifica nel browser (il server accetta al massimo 1 MB)
const FOTO_LATO = 1600;
const FOTO_MAX_BYTES = 1000 * 1024;

const stato = {
  chiave: leggi("chiave"), posizione: null, risultato: null, scelta: null, sceltaFatta: false, mappa: null,
  livello: null, inCorso: false, foto: null, tipo: "", nota: "", inviata: null,
};

function messaggio(testo, tipo = "info") {
  sostituisci($("stato"), testo ? el("p", { class: `msg ${tipo}` }, testo) : "");
}

function colore(ente) { return (ente && COLORI[ente.codice]) || COLORE_ALTRO; }
function data(iso) { return iso ? new Date(iso).toLocaleDateString("it-IT") : "?"; }

// --- chiave di accesso ------------------------------------------------------------------------------------------

function chiediChiave(motivo) {
  $("form-chiave").hidden = false;
  if (motivo) messaggio(motivo, "errore");
  $("chiave").focus();
}

$("form-chiave").addEventListener("submit", (ev) => {
  ev.preventDefault();
  stato.chiave = $("chiave").value.trim();
  scrivi("chiave", stato.chiave);
  $("form-chiave").hidden = true;
  messaggio("Chiave salvata su questo telefono.");
  caricaStato();
});

async function chiama(percorso, corpo) {
  const r = await fetch(percorso, {
    method: corpo === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-Chiave": stato.chiave },
    body: corpo === undefined ? undefined : JSON.stringify(corpo),
    cache: "no-store",
  });
  if (r.status === 401) {
    chiediChiave("Chiave di accesso mancante o sbagliata.");
    throw new Error("chiave");
  }
  let dati = null;
  try { dati = await r.json(); } catch { /* risposta non JSON */ }
  if (!r.ok) throw new Error((dati && typeof dati.detail === "string" && dati.detail) || `errore ${r.status}`);
  return dati;
}

// --- mappa e GPS ------------------------------------------------------------------------------------------------

function iniziaMappa() {
  const m = L.map("mappa").setView([37.53, 15.07], 11);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(m);
  stato.livello = L.layerGroup().addTo(m);
  // pressione lunga sul telefono, tasto destro sul PC: prova quel punto
  m.on("contextmenu", (e) => abbina(e.latlng.lat, e.latlng.lng, 5, "mappa"));
  stato.mappa = m;
}

function erroreGps(err) {
  if (err.code === 1) return "GPS: permesso negato";
  if (err.code === 2) return "GPS: posizione non disponibile";
  return "GPS: in attesa…";
}

function aggiornaGps() {
  const p = stato.posizione;
  const g = $("gps");
  g.textContent = `GPS ±${Math.round(p.precisione)} m`;
  g.className = "gps " + (p.precisione <= 15 ? "bene" : p.precisione <= 40 ? "medio" : "male");
}

function avviaGps() {
  if (!("geolocation" in navigator)) {
    $("gps").textContent = "GPS non disponibile";
    return;
  }
  navigator.geolocation.watchPosition(
    (p) => {
      stato.posizione = { lat: p.coords.latitude, lon: p.coords.longitude, precisione: p.coords.accuracy, quando: Date.now() };
      aggiornaGps();
    },
    (err) => { $("gps").textContent = erroreGps(err); $("gps").className = "gps male"; },
    { enableHighAccuracy: true, maximumAge: 0, timeout: 30000 },
  );
}

function doveSono() {
  const p = stato.posizione;
  if (p && Date.now() - p.quando < POSIZIONE_FRESCA_MS) return abbina(p.lat, p.lon, p.precisione, "gps");
  if (!("geolocation" in navigator)) return messaggio("Questo browser non dà la posizione.", "errore");
  messaggio("Cerco la posizione…");
  navigator.geolocation.getCurrentPosition(
    (pos) => abbina(pos.coords.latitude, pos.coords.longitude, pos.coords.accuracy, "gps"),
    (err) => messaggio(erroreGps(err), "errore"),
    { enableHighAccuracy: true, maximumAge: 0, timeout: 20000 },
  );
}

// --- abbinamento ------------------------------------------------------------------------------------------------

async function abbina(lat, lon, precisione, origine) {
  if (!stato.chiave) return chiediChiave("Serve la chiave di accesso.");
  if (stato.inCorso) return;
  stato.inCorso = true;
  $("dove").disabled = true;
  messaggio(origine === "mappa" ? "Provo il punto scelto sulla mappa…" : "Cerco la strada…");
  try {
    const r = await chiama("/api/v1/abbina", { lat, lon, precisione_m: precisione, origine });
    r.origine = origine;  // serve all'invio della segnalazione
    stato.risultato = r;
    stato.scelta = r.principale;
    stato.sceltaFatta = false;
    stato.inviata = null;
    messaggio("");
    mostra(r, true);
  } catch (e) {
    if (e.message !== "chiave") {
      messaggio(navigator.onLine ? `Non riuscito: ${e.message}` : "Senza rete: riprova quando c'è campo.", "errore");
    }
  } finally {
    stato.inCorso = false;
    $("dove").disabled = false;
  }
}

function disegna(r, centra) {
  const g = stato.livello;
  g.clearLayers();
  const p = r.punto;
  if (p.precisione_m) {
    L.circle([p.lat, p.lon], { radius: p.precisione_m, color: "#0b3d6b", weight: 1, fillOpacity: 0.08 }).addTo(g);
  }
  for (const c of r.candidati) {
    if (!c.geometria || c.geometria.length < 2) continue;
    const scelta = c.osm_way === stato.scelta;
    L.polyline(c.geometria, { color: colore(c.ente), weight: scelta ? 9 : 5, opacity: scelta ? 0.9 : 0.55 })
      .addTo(g)
      .on("click", () => { stato.scelta = c.osm_way; stato.sceltaFatta = true; mostra(r, false); });
  }
  for (const c of r.cartelli || []) {
    if (c.lat == null || c.lon == null) continue;
    // cartello di centro abitato: quadratino bianco e rosso; il nome (dato OSM) come nodo di testo, mai HTML
    L.circleMarker([c.lat, c.lon], { radius: 6, color: "#b3261e", weight: 3, fillColor: "#fff", fillOpacity: 1 })
      .bindTooltip(el("span", {}, `Centro abitato: ${c.nome || "senza nome"}`))
      .addTo(g);
  }
  L.circleMarker([p.lat, p.lon], { radius: 7, color: "#fff", weight: 2, fillColor: "#0b3d6b", fillOpacity: 1 }).addTo(g);
  if (centra) stato.mappa.setView([p.lat, p.lon], Math.max(stato.mappa.getZoom(), 17));
}

function tabella(righe) {
  return el("table", { class: "dati" }, el("tbody", {}, righe.map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, v)))));
}

function elenco(titolo, voci, classe = "") {
  if (!voci || !voci.length) return null;
  return el("section", { class: `blocco ${classe}` }, el("h2", {}, titolo), el("ul", { class: "motivi" }, voci.map((v) => el("li", {}, v))));
}

function boxEnte(r) {
  const ente = r.ente || r.ente_suggerito;
  let titolo;
  if (r.esito === "deciso") titolo = ente.nome;
  else if (r.esito === "da_scegliere") titolo = `Da scegliere: forse ${ente.nome}`;
  else titolo = TITOLI[r.esito] || r.esito;
  const box = el(
    "div", { class: "ente" },
    el("div", { class: "etichetta" }, "Ente competente"),
    el("div", { class: "nome-ente" }, titolo),
    ente && ente.contatti ? el("div", { class: "contatti" }, ente.contatti) : null,
    el("span", { class: `affidabilita ${r.affidabilita}` }, `affidabilità ${r.affidabilita}`),
  );
  box.style.borderLeftColor = colore(ente);  // CSSOM: ammesso dalla Content-Security-Policy
  return box;
}

function dettagliVia(c) {
  const righe = [
    ["Strada", c.nome || "senza nome"],
    ["Sigla", c.ref || "nessuna"],
    c.old_ref ? ["Ex sigla", c.old_ref] : null,
    ["Tipo", c.tipo || c.highway],
    ["Distanza", `${c.distanza_m} m`],
    c.tags.operator ? ["Gestore (OSM)", c.tags.operator] : null,
    c.percorsi && c.percorsi.length ? ["Percorso ciclabile", c.percorsi.map((p) =>
      `${p.nome || "senza nome"}${p.gestore ? ` (gestore: ${p.gestore})` : ""}`).join("; ")] : null,
    c.tags.maxspeed ? ["Limite", `${c.tags.maxspeed} km/h`] : null,
    c.tags["name:left"] ? ["Lato sinistro", c.tags["name:left"]] : null,
    c.tags["name:right"] ? ["Lato destro", c.tags["name:right"]] : null,
    ["Ente di questa strada", c.ente.nome],
  ].filter(Boolean);
  return el(
    "section", { class: "blocco" },
    el("h2", {}, "Strada"),
    tabella(righe),
    el("p", { class: "piccolo" },
      el("a", { href: `https://www.openstreetmap.org/way/${Number(c.osm_way)}`, target: "_blank", rel: "noopener" },
        `Apri su OpenStreetMap (way ${c.osm_way})`)),
  );
}

function abitanti(n) { return n == null ? "" : `circa ${n.toLocaleString("it-IT")} ab.`; }

// centro abitato del punto: dai cartelli OSM o dalle sezioni Istat (regola dei 10.000 abitanti)
function centroAbitato(r) {
  const c = r.centro;
  if (!c) return r.sezioni_istat ? "fuori dai centri abitati" : null;
  const fonte = c.fonte === "cartelli" ? "tra i cartelli OSM" : c.tipo_loc === 2 ? "nucleo, sezioni Istat" : "sezioni Istat";
  const parti = [`${c.nome || "senza nome"} (${fonte})`];
  if (c.popolazione != null) parti.push(abitanti(c.popolazione));
  if (c.localita_istat && c.popolazione_localita != null && c.popolazione_localita !== c.popolazione) {
    parti.push(`per Istat parte di «${c.localita_istat}», ${abitanti(c.popolazione_localita)}`);
  }
  return parti.join(", ");
}

function luogo(r) {
  const p = r.punto;
  const centro = centroAbitato(r);
  const righe = [
    ["Comune", r.comune || "non determinato"],
    centro ? ["Centro abitato", centro] : null,
    ["Provincia", r.provincia ? `${r.provincia}${r.sigla_provincia ? ` (${r.sigla_provincia})` : ""}` : "?"],
    ["Regione", r.regione || "?"],
    r.comuni_vicini && r.comuni_vicini.length ? ["Comuni vicini", r.comuni_vicini.join(", ")] : null,
    r.localita && r.localita.length ? ["Località vicine", r.localita.map((l) =>
      `${l.nome || "?"} (${l.popolazione ? `${l.popolazione.toLocaleString("it-IT")} ab., ` : ""}${l.distanza_m} m)`).join(", ")] : null,
    r.cartelli && r.cartelli.length ? ["Cartelli di centro abitato", r.cartelli.map((c) =>
      `${c.nome || "senza nome"} (${c.distanza_m} m)`).join(", ")] : null,
    ["Punto", `${p.lat.toFixed(6)}, ${p.lon.toFixed(6)}${p.precisione_m ? ` (±${Math.round(p.precisione_m)} m)` : ""}`],
    r.fonte && r.fonte.dati_del ? ["Dati OSM del", data(r.fonte.dati_del)] : null,
  ].filter(Boolean);
  return el("section", { class: "blocco" }, el("h2", {}, "Dove"), tabella(righe));
}

function sceltaCandidati(r) {
  const bottoni = r.candidati.map((c) => {
    const campione = el("span", { class: "campione" });
    campione.style.background = colore(c.ente);
    return el(
      "button", { type: "button", class: `candidato${c.osm_way === stato.scelta ? " scelto" : ""}`,
        onclick: () => { stato.scelta = c.osm_way; stato.sceltaFatta = true; mostra(r, false); } },
      campione,
      el("span", { class: "testo" }, `${c.nome || c.ref || c.tipo}${c.ref && c.nome ? ` (${c.ref})` : ""}`,
        el("span", { class: "sotto" }, `${c.tipo} · ${c.distanza_m} m · ${c.ente.nome}`)),
    );
  });
  const titolo = r.esito === "da_scegliere" ? "Su quale sei?" : "Strade vicine (tocca per vedere)";
  return el("section", { class: "blocco" }, el("h2", {}, titolo), bottoni);
}

function vicine(r) {
  if (!r.vicine || !r.vicine.length || r.candidati.length) return null;
  return elenco("Strade più vicine", r.vicine.map((v) => `${v.nome || v.ref || v.tipo}: ${v.distanza_m} m`));
}

function boxEsito(r) {
  const select = el("select", { "aria-label": "Ente giusto" },
    el("option", { value: "" }, "Ente giusto…"), ENTI_CORRETTI.map((v) => el("option", { value: v }, v)));
  const nota = el("textarea", { rows: 2, maxlength: 2000, placeholder: "Nota (es. cartello «fine competenza ANAS» qui vicino)" });
  const salvato = el("p", { class: "piccolo" });
  const invia = async (esito) => {
    try {
      await chiama(`/api/v1/prove/${Number(r.id_prova)}/esito`, {
        token: r.token_esito, esito, osm_way_scelto: stato.scelta, ente_corretto: select.value || null,
        nota: nota.value || null,
      });
      salvato.textContent = `Salvato: ${esito} (prova n. ${r.id_prova}).`;
    } catch (e) {
      if (e.message !== "chiave") salvato.textContent = `Non salvato: ${e.message}`;
    }
  };
  const sbagliato = el("div", { class: "sbagliato", hidden: true }, select, nota,
    el("button", { type: "button", onclick: () => invia("sbagliato") }, "Invia"));
  return el(
    "section", { class: "blocco" },
    el("h2", {}, "È giusto?"),
    el("div", { class: "riga-bottoni" },
      el("button", { type: "button", class: "giusto", onclick: () => invia("giusto") }, "Giusto"),
      el("button", { type: "button", class: "sbagliato-btn", onclick: () => { sbagliato.hidden = false; } }, "Sbagliato")),
    sbagliato,
    salvato,
  );
}

function mostra(r, centra) {
  disegna(r, centra);
  const scelto = r.candidati.find((c) => c.osm_way === stato.scelta) || r.candidati[0];
  sostituisci($("risultato"), 
    boxEnte(r),
    boxSegnala(r),
    elenco("Perché", r.motivi),
    elenco("Dubbi", r.dubbi, "dubbi"),
    r.candidati.length > 1 ? sceltaCandidati(r) : null,
    scelto ? dettagliVia(scelto) : null,
    vicine(r),
    luogo(r),
    r.id_prova ? boxEsito(r) : null,
  );
  $("risultato").hidden = false;
  if (r.versione) $("versione").textContent = `versione ${r.versione}`;
}

async function caricaStato() {
  if (!stato.chiave) return;
  try {
    const s = await chiama("/api/v1/stato");
    $("versione").textContent = `versione ${s.versione}`;
    $("stradario").textContent = s.stradario && s.stradario.dati_del
      ? ` · stradario del ${data(s.stradario.dati_del)}` : " · stradario non importato";
  } catch { /* si riprova al prossimo abbinamento */ }
}

// --- avvio ------------------------------------------------------------------------------------------------------

// --- segnalazione -----------------------------------------------------------------------------------------------

// Ricodifica la foto nel browser: JPEG, al massimo FOTO_LATO px e FOTO_MAX_BYTES. Così al server arriva sempre un JPEG
// (anche dagli iPhone, che scattano in HEIC) e spariscono i dati EXIF (posizione, modello del telefono).
async function preparaFoto(file) {
  let sorgente;
  try {
    sorgente = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    sorgente = await new Promise((ok, ko) => {
      const img = new Image();
      img.onload = () => ok(img);
      img.onerror = () => ko(new Error("formato"));
      img.src = URL.createObjectURL(file);
    });
  }
  const w = sorgente.width, h = sorgente.height;
  if (!w || !h) throw new Error("formato");
  let scala = Math.min(1, FOTO_LATO / Math.max(w, h));
  for (let giro = 0; giro < 4; giro++) {
    const c = document.createElement("canvas");
    c.width = Math.round(w * scala);
    c.height = Math.round(h * scala);
    c.getContext("2d").drawImage(sorgente, 0, 0, c.width, c.height);
    for (const q of [0.8, 0.7, 0.6]) {
      const blob = await new Promise((ok) => c.toBlob(ok, "image/jpeg", q));
      if (blob && blob.size <= FOTO_MAX_BYTES) return blob;
    }
    scala *= 0.75;
  }
  throw new Error("troppo grande");
}

function boxSegnala(r) {
  if (r.esito === "fuori_area") return null;
  if (stato.inviata) return boxInviata(stato.inviata);
  const anteprima = el("img", { class: "anteprima", alt: "Foto da inviare", hidden: !stato.foto });
  if (stato.foto) anteprima.src = URL.createObjectURL(stato.foto);
  const notaFoto = el("p", { class: "piccolo", "aria-live": "polite" });
  const inputFoto = el("input", { type: "file", accept: "image/*", capture: "environment", class: "nascosto",
    id: "foto" });
  inputFoto.addEventListener("change", async () => {
    const file = inputFoto.files && inputFoto.files[0];
    if (!file) return;
    notaFoto.textContent = "Preparo la foto…";
    try {
      stato.foto = await preparaFoto(file);
      anteprima.src = URL.createObjectURL(stato.foto);
      anteprima.hidden = false;
      notaFoto.textContent = `Foto pronta (${Math.round(stato.foto.size / 1024)} KB).`;
    } catch (e) {
      stato.foto = null;
      notaFoto.textContent = e.message === "formato"
        ? "Questo telefono ha dato la foto in un formato che il browser non legge (HEIF?): imposta la fotocamera "
          + "in JPEG («più compatibile») e riprova."
        : "Foto non utilizzabile: riprova.";
    }
  });
  const tipi = Object.entries(NOMI_TIPO).map(([v, testo]) => el("label", { class: "scelta-tipo" },
    el("input", { type: "radio", name: "tipo", value: v, checked: stato.tipo === v,
      onchange: () => { stato.tipo = v; } }), testo));
  const nota = el("textarea", { rows: 2, maxlength: 500, placeholder: "Nota facoltativa (es. lato della strada, "
    + "dimensioni)" });
  nota.value = stato.nota;
  nota.addEventListener("input", () => { stato.nota = nota.value; });
  const esito = el("p", { class: "piccolo", "aria-live": "polite" });
  const invia = el("button", { type: "button", class: "primario" }, "Invia segnalazione");
  invia.addEventListener("click", () => inviaSegnalazione(r, invia, esito));
  return el("section", { class: "blocco segnala" },
    el("h2", {}, "Segnala qui"),
    el("div", { class: "tipi" }, tipi),
    el("label", { for: "foto", class: "bottone-foto" }, stato.foto ? "Rifai la foto" : "Scatta la foto"),
    inputFoto, anteprima, notaFoto,
    nota,
    r.esito === "da_scegliere"
      ? el("p", { class: "piccolo" }, "Qui vicino ci sono strade di enti diversi: tocca qui sotto quella su cui sei.")
      : null,
    invia, esito);
}

async function inviaSegnalazione(r, bottone, esito) {
  if (!stato.tipo) { esito.textContent = "Scegli se è una carcassa o sono rifiuti."; return; }
  if (!stato.foto) { esito.textContent = "Serve una foto."; return; }
  if (r.esito === "da_scegliere" && !stato.sceltaFatta) {
    esito.textContent = "Tocca la strada su cui sei (qui sotto o sulla mappa).";
    return;
  }
  const p = r.punto;
  const dati = {
    lat: p.lat, lon: p.lon, precisione_m: p.precisione_m, origine: r.origine || "gps", tipo: stato.tipo,
    nota: stato.nota.trim() || null, osm_way: r.candidati.length ? stato.scelta : null,
    dispositivo: chiaveDispositivo(),
  };
  const corpo = new FormData();
  corpo.append("dati", JSON.stringify(dati));
  corpo.append("foto", stato.foto, "foto.jpg");
  bottone.disabled = true;
  esito.textContent = "Invio…";
  try {
    const risposta = await fetch("/api/v1/segnalazioni", {
      method: "POST", headers: { "X-Chiave": stato.chiave }, body: corpo, cache: "no-store",
    });
    if (risposta.status === 401) { chiediChiave("Chiave di accesso mancante o sbagliata."); throw new Error("chiave"); }
    const d = await leggiRisposta(risposta);
    aggiungiMia(d.codice, d.token);
    stato.inviata = d;
    stato.foto = null;
    stato.nota = "";
    mostra(r, false);
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (e) {
    bottone.disabled = false;
    if (e.message !== "chiave") {
      esito.textContent = navigator.onLine ? `Non inviata: ${e.message}` : "Senza rete: riprova quando c'è campo.";
    }
  }
}

function boxInviata(d) {
  const condividi = navigator.share
    ? el("button", { type: "button", onclick: () => navigator.share({ title: `Segnalazione ${d.codice}`, url: d.link })
      .catch(() => {}) }, "Condividi")
    : null;
  const bottoneCopia = el("button", { type: "button" }, "Copia il link");
  bottoneCopia.addEventListener("click", () => copia(d.link, bottoneCopia));
  return el("section", { class: "blocco inviata" },
    el("h2", {}, "Segnalazione inviata"),
    el("p", { class: "nome-ente" }, d.codice),
    el("p", {}, d.ente ? `Inviata a: ${d.ente}.` : `L'ente competente (${d.ente_calcolato || "non determinato"}) non `
      + "usa ancora l'app: la smistiamo noi."),
    el("p", {}, "Con questo link la segui (chi ha il link la vede: tienilo per te):"),
    el("p", {}, el("a", { href: d.link }, d.link)),
    el("div", { class: "riga-bottoni" }, bottoneCopia, condividi),
    moduloEmail(d.codice, d.token, d.email_disponibili),
    el("p", {}, el("button", { type: "button", onclick: () => { stato.inviata = null; mostra(stato.risultato, false); } },
      "Un'altra segnalazione qui")));
}

// --- le mie segnalazioni ----------------------------------------------------------------------------------------

async function mostraMie() {
  const box = $("mie");
  box.hidden = false;
  sostituisci(box, el("h2", {}, "Le mie segnalazioni"), el("p", { class: "piccolo" }, "Carico…"));
  let lista = [];
  try {
    const r = await fetch("/api/v1/mie", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dispositivo: chiaveDispositivo() }), cache: "no-store",
    });
    lista = (await leggiRisposta(r)).segnalazioni;
  } catch (e) {
    sostituisci(box, el("h2", {}, "Le mie segnalazioni"), el("p", { class: "msg errore" }, `Non riuscito: ${e.message}`));
    return;
  }
  const recupero = `${location.origin}/#dispositivo=${chiaveDispositivo()}`;
  const bottoneRecupero = el("button", { type: "button" }, "Copia il link di recupero");
  bottoneRecupero.addEventListener("click", () => copia(recupero, bottoneRecupero));
  sostituisci(box, 
    el("h2", {}, "Le mie segnalazioni"),
    lista.length ? el("ul", { class: "mie" }, lista.map((s) => {
      const token = tokenDi(s.codice);
      return el("li", {},
        el("a", { href: `/s#${s.codice}${token ? `.${token}` : ""}` }, s.codice),
        ` · ${NOMI_TIPO[s.tipo] || s.tipo} · ${NOMI_STATO[s.stato] || s.stato}`,
        el("span", { class: "sotto" }, `${dataOra(s.creata)} · ${[s.strada, s.comune].filter(Boolean).join(", ")}`
          + `${s.ente ? ` · ${s.ente}` : ""}`));
    })) : el("p", {}, "Nessuna segnalazione da questo telefono."),
    el("p", { class: "piccolo" }, "Le riconosciamo da un codice che resta su questo telefono. Per ritrovarle su un "
      + "altro telefono (o se cancelli i dati del browser) apri il link di recupero: non darlo ad altri."),
    bottoneRecupero,
  );
}

// Link da condividere con la chiave nel frammento (…/#chiave=…): il frammento non arriva al server né nei log;
// la chiave si salva sul telefono e sparisce dall'indirizzo.
// anche il link di recupero delle segnalazioni (…/#dispositivo=…) porta la chiave del dispositivo su questo telefono.
(function chiaviDalLink() {
  let trovata = false;
  const m = /(?:^#|&)chiave=([^&]+)/.exec(location.hash);
  if (m) {
    try { stato.chiave = decodeURIComponent(m[1]); scrivi("chiave", stato.chiave); trovata = true; } catch { /* no */ }
  }
  const d = /(?:^#|&)dispositivo=([A-Za-z0-9_-]{32,64})(?:&|$)/.exec(location.hash);
  if (d) { scrivi("dispositivo", d[1]); trovata = true; }
  if (trovata) history.replaceState(null, "", location.pathname + location.search);
  if (d) setTimeout(mostraMie, 0);
})();

iniziaMappa();
avviaGps();
$("dove").addEventListener("click", doveSono);
$("apri-mie").addEventListener("click", mostraMie);
if (!stato.chiave) chiediChiave();
else caricaStato();
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* senza service worker funziona lo stesso, solo online */ });
}
