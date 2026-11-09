"use strict";
// Funzioni comuni alle pagine (abbinamento e segnalazione, segnalazione aperta dal link, gestionale).
// I testi che arrivano dal server (nomi, note, dati OSM) sono dati non fidati: sempre textContent, mai innerHTML.

function leggi(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } }
function scrivi(k, v) { try { localStorage.setItem(k, v); } catch { /* memoria del browser non disponibile */ } }
function togli(k) { try { localStorage.removeItem(k); } catch { /* idem */ } }
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

// replaceChildren che salta i blocchi vuoti (null diventerebbe il testo "null")
function sostituisci(nodo, ...figli) {
  nodo.replaceChildren(...figli.flat().filter((f) => f !== null && f !== undefined && f !== false));
}

function dataOra(iso) {
  return iso ? new Date(iso).toLocaleString("it-IT", { dateStyle: "short", timeStyle: "short" }) : "?";
}

const NOMI_TIPO = { carcassa: "Carcassa di animale", rifiuto: "Rifiuti abbandonati" };
const NOMI_STATO = { nuova: "Inviata", presa_in_carico: "Presa in carico", chiusa: "Chiusa", da_smistare: "In attesa di assegnazione" };

// Chiave del dispositivo: un codice casuale che resta su questo telefono e lega le sue segnalazioni ("Le mie
// segnalazioni"). Al server arriva solo per riconoscerle: lì se ne salva l'impronta.
function chiaveDispositivo() {
  let k = leggi("dispositivo");
  if (!/^[A-Za-z0-9_-]{32,64}$/.test(k)) {
    const b = crypto.getRandomValues(new Uint8Array(32));
    k = btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    scrivi("dispositivo", k);
  }
  return k;
}

// Segnalazioni fatte da qui, con il loro codice privato: [{codice, token, creata}]
function mieLocali() {
  try { const v = JSON.parse(leggi("mie") || "[]"); return Array.isArray(v) ? v : []; } catch { return []; }
}
function aggiungiMia(codice, token) {
  const lista = mieLocali().filter((m) => m.codice !== codice);
  lista.unshift({ codice, token, creata: new Date().toISOString() });
  scrivi("mie", JSON.stringify(lista.slice(0, 200)));
}
function tokenDi(codice) { const m = mieLocali().find((x) => x.codice === codice); return m ? m.token : ""; }

async function leggiRisposta(r) {
  let dati = null;
  try { dati = await r.json(); } catch { /* risposta non JSON */ }
  if (!r.ok) {
    const msg = (dati && typeof dati.detail === "string" && dati.detail) || `errore ${r.status}`;
    const e = new Error(msg);
    e.status = r.status;
    throw e;
  }
  return dati;
}

// Copia negli appunti, con un messaggio nel bottone
async function copia(testo, bottone) {
  try {
    await navigator.clipboard.writeText(testo);
    const prima = bottone.textContent;
    bottone.textContent = "Copiato";
    setTimeout(() => { bottone.textContent = prima; }, 1500);
  } catch { window.prompt("Copia il link:", testo); }
}

// Modulo per farsi mandare il link per email: l'indirizzo resta solo su questo telefono (localStorage), il server lo
// usa per l'invio e non lo salva.
function moduloEmail(codice, token, disponibili) {
  const campo = el("input", { type: "email", inputmode: "email", autocomplete: "email", maxlength: 254,
    placeholder: "la.tua@email.it", value: leggi("email") });
  const esito = el("p", { class: "piccolo", "aria-live": "polite" });
  const bottone = el("button", { type: "submit" }, "Mandami il link");
  const form = el("form", { class: "email-link" },
    el("label", {}, "Vuoi il link anche per email?"),
    el("div", { class: "riga-bottoni" }, campo, bottone),
    el("p", { class: "piccolo" }, "L'indirizzo serve solo per questo invio: non lo salviamo da nessuna parte. Resta "
      + "solo su questo telefono, per non doverlo riscrivere la prossima volta."),
    esito);
  if (!disponibili) {
    bottone.disabled = true;
    campo.disabled = true;
    esito.textContent = disponibili === 0 ? "Invio per email non disponibile per questa segnalazione." : "";
  }
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const email = campo.value.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$/.test(email) || email.length > 254) {
      esito.textContent = "Indirizzo email non valido.";
      return;
    }
    bottone.disabled = true;
    esito.textContent = "Invio…";
    try {
      const r = await fetch(`/api/v1/s/${encodeURIComponent(codice)}/email`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-Token": token },
        body: JSON.stringify({ email }), cache: "no-store",
      });
      const d = await leggiRisposta(r);
      scrivi("email", email);
      esito.textContent = `Inviato. ${d.email_disponibili > 0 ? `Puoi mandarlo ancora ${d.email_disponibili} volte.` : ""}`;
      bottone.disabled = d.email_disponibili <= 0;
    } catch (e) {
      esito.textContent = `Non inviato: ${e.message}`;
      bottone.disabled = false;
    }
  });
  return form;
}
