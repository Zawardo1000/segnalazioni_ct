"use strict";
// Segnalazione aperta dal link privato (/s#<codice>.<codice privato>) o da "Le mie segnalazioni" (/s#<codice>, con la
// chiave del dispositivo). Il frammento non arriva al server: il codice privato viaggia solo nell'header X-Token.

function messaggio(testo, tipo = "info") {
  sostituisci($("stato"), testo ? el("p", { class: `msg ${tipo}` }, testo) : "");
}

async function apri() {
  const m = /^#(SCT-\d{4}-\d{6,})(?:\.([A-Za-z0-9_-]{16,64}))?$/.exec(location.hash);
  if (!m) { messaggio("Link non valido.", "errore"); return; }
  const codice = m[1];
  const token = m[2] || tokenDi(codice);
  if (m[2]) aggiungiMia(codice, m[2]);  // aperta dal link: la ritrovi in "Le mie segnalazioni" di questo telefono
  const intestazioni = token ? { "X-Token": token } : { "X-Dispositivo": chiaveDispositivo() };
  messaggio("Carico…");
  let s;
  try {
    s = await leggiRisposta(await fetch(`/api/v1/s/${codice}`, { headers: intestazioni, cache: "no-store" }));
  } catch (e) {
    messaggio(e.status === 404 ? "Segnalazione non trovata (il link è completo?)." : `Non riuscito: ${e.message}`, "errore");
    return;
  }
  messaggio("");
  document.title = `Segnalazione ${s.codice}`;
  const foto = el("img", { class: "foto-segnalazione", alt: "Foto della segnalazione", hidden: true });
  const righe = [
    ["Tipo", NOMI_TIPO[s.tipo] + (s.tipo !== s.tipo_iniziale ? ` (segnalata come: ${NOMI_TIPO[s.tipo_iniziale]})` : "")],
    ["Stato", NOMI_STATO[s.stato] || s.stato],
    ["Ente", s.ente || "in attesa di assegnazione"],
    ["Dove", [s.strada, s.comune].filter(Boolean).join(", ") || "?"],
    ["Inviata il", dataOra(s.creata)],
    s.nota ? ["Nota", s.nota] : null,
  ].filter(Boolean);
  sostituisci($("segnalazione"), 
    el("div", { class: "ente" }, el("div", { class: "etichetta" }, "Segnalazione"), el("div", { class: "nome-ente" }, s.codice)),
    el("section", { class: "blocco" }, el("table", { class: "dati" }, el("tbody", {},
      righe.map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, v)))))),
    s.foto ? el("section", { class: "blocco" }, foto) : null,
    el("section", { class: "blocco" }, el("div", { id: "mappa-piccola" })),
    el("section", { class: "blocco" }, el("h2", {}, "Storico"),
      el("ul", { class: "storico" }, s.eventi.map((e) => el("li", {}, e.testo, el("span", { class: "sotto" }, dataOra(e.quando)))))),
    token ? el("section", { class: "blocco" }, moduloEmail(s.codice, token, s.email_disponibili)) : null,
  );
  $("segnalazione").hidden = false;
  const mappa = L.map("mappa-piccola", { zoomControl: false }).setView([s.lat, s.lon], 17);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(mappa);
  L.circleMarker([s.lat, s.lon], { radius: 8, color: "#fff", weight: 2, fillColor: "#0b3d6b", fillOpacity: 1 }).addTo(mappa);
  if (s.foto) {
    try {
      const r = await fetch(`/api/v1/s/${codice}/foto`, { headers: intestazioni, cache: "no-store" });
      if (!r.ok) throw new Error();
      foto.src = URL.createObjectURL(await r.blob());
      foto.hidden = false;
    } catch { foto.replaceWith(el("p", { class: "piccolo" }, "Foto non disponibile.")); }
  }
}

apri();
