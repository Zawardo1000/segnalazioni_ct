"use strict";
// Pagina di prova dell'abbinamento: posizione GPS (o punto scelto sulla mappa) → ente competente per la strada.
// Solo Leaflet come libreria. I testi che arrivano da OSM (nomi, sigle, tag) sono dati non fidati: passano sempre da
// textContent, mai da innerHTML.

const COLORI = {
  anas: "#c62828", cas: "#6a1b9a", provincia: "#1565c0", comune: "#2e7d32", regione: "#ef6c00",
  concessionario: "#6a1b9a",
};
const COLORE_ALTRO = "#616161";
const TITOLI = { nessuna_strada: "Nessuna strada vicina", fuori_area: "Fuori dall'area coperta" };
const ENTI_CORRETTI = ["ANAS", "Città metropolitana o provincia", "Comune", "CAS", "Privato", "Altro"];
const POSIZIONE_FRESCA_MS = 15000;

const stato = {
  chiave: leggi("chiave"), posizione: null, risultato: null, scelta: null, mappa: null, livello: null, inCorso: false,
};

function leggi(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } }
function scrivi(k, v) { try { localStorage.setItem(k, v); } catch { /* memoria del browser non disponibile */ } }
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...figli) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : String(v));
  }
  for (const f of figli.flat()) {
    if (f === null || f === undefined || f === false) continue;
    e.append(f instanceof Node ? f : document.createTextNode(String(f)));
  }
  return e;
}

function messaggio(testo, tipo = "info") {
  $("stato").replaceChildren(testo ? el("p", { class: `msg ${tipo}` }, testo) : "");
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
    stato.risultato = r;
    stato.scelta = r.principale;
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
      .on("click", () => { stato.scelta = c.osm_way; mostra(r, false); });
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

function luogo(r) {
  const p = r.punto;
  const righe = [
    ["Comune", r.comune || "non determinato"],
    ["Provincia", r.provincia ? `${r.provincia}${r.sigla_provincia ? ` (${r.sigla_provincia})` : ""}` : "?"],
    ["Regione", r.regione || "?"],
    r.comuni_vicini && r.comuni_vicini.length ? ["Comuni vicini", r.comuni_vicini.join(", ")] : null,
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
        onclick: () => { stato.scelta = c.osm_way; mostra(r, false); } },
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
        esito, osm_way_scelto: stato.scelta, ente_corretto: select.value || null, nota: nota.value || null,
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
  $("risultato").replaceChildren(
    boxEnte(r),
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

// Link da condividere con la chiave nel frammento (…/#chiave=…): il frammento non arriva al server né nei log;
// la chiave si salva sul telefono e sparisce dall'indirizzo.
(function chiaveDalLink() {
  const m = /(?:^#|&)chiave=([^&]+)/.exec(location.hash);
  if (!m) return;
  try { stato.chiave = decodeURIComponent(m[1]); } catch { return; }
  scrivi("chiave", stato.chiave);
  history.replaceState(null, "", location.pathname + location.search);
})();

iniziaMappa();
avviaGps();
$("dove").addEventListener("click", doveSono);
if (!stato.chiave) chiediChiave();
else caricaStato();
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* senza service worker funziona lo stesso, solo online */ });
}
