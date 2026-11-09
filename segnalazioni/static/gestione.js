"use strict";
// Gestionale degli enti: elenco e mappa delle segnalazioni, dettaglio con foto e storico, azioni (presa in carico,
// chiusura, inoltro per competenza, cambio di tipo; l'amministratore assegna le segnalazioni da smistare).
// La sessione è un cookie HttpOnly; le richieste che cambiano qualcosa portano anche il codice CSRF (X-CSRF).

const NOMI_ESITO = { rimossa: "Rimossa", non_trovata: "Non trovata sul posto", doppione: "Doppione" };
const COLORI_STATO = { nuova: "#c62828", presa_in_carico: "#ef6c00", da_smistare: "#6a1b9a", chiusa: "#2e7d32" };
const g = { io: null, enti: [], mappa: null, livello: null, scelta: null, segnalazioni: [] };

function messaggio(testo, tipo = "info") {
  sostituisci($("stato"), testo ? el("p", { class: `msg ${tipo}` }, testo) : "");
}

async function api(percorso, corpo) {
  const opzioni = { cache: "no-store", credentials: "same-origin", headers: {} };
  if (corpo !== undefined) {
    opzioni.method = "POST";
    opzioni.headers["Content-Type"] = "application/json";
    opzioni.headers["X-CSRF"] = g.io ? g.io.csrf : "";
    opzioni.body = JSON.stringify(corpo);
  }
  const r = await fetch(`/api/v1/gestione${percorso}`, opzioni);
  if (r.status === 401) { location.href = "/gestione/login"; throw new Error("accesso"); }
  return leggiRisposta(r);
}

async function avvia() {
  try {
    g.io = await api("/io");
  } catch (e) {
    if (e.message !== "accesso") messaggio(`Non riuscito: ${e.message}`, "errore");
    return;
  }
  $("chi").textContent = `${g.io.utente} · ${g.io.ente ? g.io.ente.nome : "amministratore"}`;
  $("esci").hidden = false;
  $("filtri").hidden = false;
  if (g.io.ruolo === "amministratore") $("f-stato").value = "da_smistare";
  g.enti = (await api("/enti")).enti;
  g.mappa = L.map("mappa-gestione").setView([37.53, 15.07], 10);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(g.mappa);
  g.livello = L.layerGroup().addTo(g.mappa);
  await carica();
}

async function carica() {
  const parametri = new URLSearchParams();
  if ($("f-stato").value) parametri.set("stato", $("f-stato").value);
  if ($("f-tipo").value) parametri.set("tipo", $("f-tipo").value);
  messaggio("Carico…");
  try {
    g.segnalazioni = (await api(`/segnalazioni?${parametri}`)).segnalazioni;
  } catch (e) { messaggio(`Non riuscito: ${e.message}`, "errore"); return; }
  messaggio(g.segnalazioni.length ? "" : "Nessuna segnalazione con questi filtri.");
  disegna();
}

function disegna() {
  g.livello.clearLayers();
  const punti = [];
  for (const s of g.segnalazioni) {
    L.circleMarker([s.lat, s.lon], { radius: 8, color: "#fff", weight: 2, fillColor: COLORI_STATO[s.stato] || "#616161",
      fillOpacity: 1 }).bindTooltip(el("span", {}, `${s.codice} · ${NOMI_TIPO[s.tipo]}`))
      .on("click", () => apri(s.id)).addTo(g.livello);
    punti.push([s.lat, s.lon]);
  }
  if (punti.length) g.mappa.fitBounds(punti, { maxZoom: 16, padding: [30, 30] });
  $("elenco").hidden = !g.segnalazioni.length;
  sostituisci($("elenco"), el("h2", {}, `Segnalazioni (${g.segnalazioni.length})`), g.segnalazioni.map((s) =>
    el("button", { type: "button", class: `riga-seg${s.id === g.scelta ? " scelta" : ""}`, onclick: () => apri(s.id) },
      el("strong", {}, s.codice), " ", el("span", { class: "etichetta-stato" }, NOMI_STATO[s.stato] || s.stato),
      el("span", { class: "sotto" }, `${NOMI_TIPO[s.tipo]} · ${dataOra(s.creata)} · `
        + `${[s.strada, s.comune].filter(Boolean).join(", ")}${g.io.ruolo === "amministratore" ? ` · ${s.ente || "da smistare"}` : ""}`))));
}

function selectEnti(escludi) {
  return el("select", {}, el("option", { value: "" }, "Scegli l'ente…"),
    g.enti.filter((e) => e.nome !== escludi).map((e) => el("option", { value: e.codice }, e.nome)));
}

async function azione(percorso, corpo, esito) {
  try {
    await api(percorso, corpo);
    await carica();
    if (g.scelta && g.segnalazioni.some((s) => s.id === g.scelta)) await apri(g.scelta);
    else { $("dettaglio").hidden = true; g.scelta = null; }
  } catch (e) {
    if (esito) esito.textContent = `Non riuscito: ${e.message}`;
    else messaggio(`Non riuscito: ${e.message}`, "errore");
  }
}

async function apri(id) {
  g.scelta = id;
  disegna();
  let s;
  try { s = await api(`/segnalazioni/${Number(id)}`); } catch (e) { messaggio(`Non riuscito: ${e.message}`, "errore"); return; }
  const aperta = s.stato !== "chiusa";
  const esito = el("p", { class: "piccolo", "aria-live": "polite" });
  const azioni = [];
  if (aperta && s.stato === "nuova") {
    azioni.push(el("button", { type: "button", onclick: () => azione(`/segnalazioni/${s.id}/prendi`, {}, esito) },
      "Prendi in carico"));
  }
  if (aperta && s.stato !== "da_smistare") {
    const sceltaEsito = el("select", {}, Object.entries(NOMI_ESITO).map(([v, t]) => el("option", { value: v }, t)));
    const nota = el("input", { type: "text", maxlength: 1000, placeholder: "Nota (facoltativa)" });
    azioni.push(el("div", { class: "azione" }, el("strong", {}, "Chiudi"), sceltaEsito, nota,
      el("button", { type: "button", onclick: () => azione(`/segnalazioni/${s.id}/chiudi`,
        { esito: sceltaEsito.value, nota: nota.value || null }, esito) }, "Chiudi la segnalazione")));
  }
  if (aperta) {
    const dest = selectEnti(s.ente);
    const motivazione = el("input", { type: "text", maxlength: 1000, placeholder: "Motivazione (obbligatoria)" });
    const amministratore = g.io.ruolo === "amministratore";
    azioni.push(el("div", { class: "azione" }, el("strong", {}, amministratore ? "Assegna o inoltra" : "Inoltra per competenza"),
      dest, amministratore ? null : motivazione,
      el("button", { type: "button", onclick: () => {
        if (!dest.value) { esito.textContent = "Scegli l'ente."; return; }
        if (amministratore) azione(`/segnalazioni/${s.id}/assegna`, { ente: dest.value }, esito);
        else azione(`/segnalazioni/${s.id}/inoltra`, { ente: dest.value, motivazione: motivazione.value }, esito);
      } }, amministratore ? "Assegna" : "Inoltra")));
    const altro = s.tipo === "carcassa" ? "rifiuto" : "carcassa";
    azioni.push(el("button", { type: "button", onclick: () => azione(`/segnalazioni/${s.id}/tipo`, { tipo: altro }, esito) },
      `Cambia tipo in: ${NOMI_TIPO[altro]}`));
  }
  const foto = el("img", { class: "foto-segnalazione", alt: "Foto della segnalazione", hidden: true });
  const a = s.abbinamento || {};
  const righe = [
    ["Tipo", NOMI_TIPO[s.tipo] + (s.tipo !== s.tipo_iniziale ? ` (segnalata come: ${NOMI_TIPO[s.tipo_iniziale]})` : "")],
    ["Stato", (NOMI_STATO[s.stato] || s.stato) + (s.esito ? `: ${NOMI_ESITO[s.esito]}` : "")],
    ["Ente", s.ente || "da smistare"],
    ["Dove", [s.strada, s.comune].filter(Boolean).join(", ") || "?"],
    ["Posizione", `${s.lat.toFixed(6)}, ${s.lon.toFixed(6)} (${s.origine}${s.precisione_m ? `, ±${Math.round(s.precisione_m)} m` : ""})`],
    ["Inviata il", dataOra(s.creata)],
    s.nota ? ["Nota", s.nota] : null,
    ["Abbinamento", `${a.esito || "?"}, affidabilità ${a.affidabilita || "?"}${s.ente_calcolato ? ` (ente calcolato: ${s.ente_calcolato})` : ""}`],
  ].filter(Boolean);
  sostituisci($("dettaglio"), 
    el("h2", {}, s.codice),
    el("table", { class: "dati" }, el("tbody", {}, righe.map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, v))))),
    s.foto ? foto : null,
    a.motivi && a.motivi.length ? el("details", {}, el("summary", {}, "Perché questo ente"),
      el("ul", { class: "motivi" }, [...a.motivi, ...(a.dubbi || [])].map((m) => el("li", {}, m)))) : null,
    el("p", {}, el("a", { href: `https://www.openstreetmap.org/?mlat=${s.lat}&mlon=${s.lon}#map=18/${s.lat}/${s.lon}`,
      target: "_blank", rel: "noopener" }, "Apri su OpenStreetMap")),
    azioni.length ? el("div", { class: "azioni" }, azioni) : null,
    esito,
    el("h2", {}, "Storico"),
    el("ul", { class: "storico" }, s.eventi.map((e) => el("li", {}, e.testo, el("span", { class: "sotto" },
      `${dataOra(e.quando)} · ${e.autore}`)))),
  );
  $("dettaglio").hidden = false;
  if (s.foto) {
    try {
      const r = await fetch(`/api/v1/gestione/segnalazioni/${s.id}/foto`, { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) throw new Error();
      foto.src = URL.createObjectURL(await r.blob());
      foto.hidden = false;
    } catch { foto.replaceWith(el("p", { class: "piccolo" }, "Foto non disponibile.")); }
  }
}

$("aggiorna").addEventListener("click", carica);
$("f-stato").addEventListener("change", carica);
$("f-tipo").addEventListener("change", carica);
$("esci").addEventListener("click", async () => {
  try { const d = await api("/esci", {}); location.href = d.redirect; } catch { location.href = "/gestione/uscito"; }
});
avvia();
