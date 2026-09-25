const portSelect = document.getElementById("port");
const startInput = document.getElementById("start");
const endInput = document.getElementById("end");
const tidePhaseSelect = document.getElementById("tide_phase");
const maxCoefInput = document.getElementById("max_coefficient");
const coefVal = document.getElementById("coef_val");
const marginInput = document.getElementById("margin_minutes");
const marginVal = document.getElementById("margin_val");
const daylightSelect = document.getElementById("daylight");
const searchBtn = document.getElementById("search");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results-list");

maxCoefInput.addEventListener("input", () => coefVal.textContent = maxCoefInput.value);
marginInput.addEventListener("input", () => marginVal.textContent = marginInput.value);

const fmtDay = new Intl.DateTimeFormat("fr-FR", {
  weekday: "short", day: "2-digit", month: "2-digit",
});

function formatDay(iso) {
  // midi pour éviter tout décalage de jour lié au fuseau
  return fmtDay.format(new Date(iso + "T12:00:00"));
}

function show(v) {
  return v ?? "–";
}

function todayISO(offsetDays = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

async function loadPorts() {
  const res = await fetch("/api/ports");
  const ports = await res.json();
  portSelect.innerHTML = ports.map(p => `<option value="${p.id}">${p.name}</option>`).join("");
  if (ports.length === 0) {
    statusEl.textContent = "Aucun port en base. Lance d'abord precompute.py pour un port et une année (voir README).";
  }
}

function fmtHeight(v) {
  return v != null ? v.toFixed(2).replace(".", ",") : "–";
}

function fmtCoef(c) {
  return c != null ? Math.round(c) : "–";
}

// ME ≤ 50, VE ≥ 90 : repères visuels, pas des seuils officiels
function coefClass(c) {
  if (c == null) return "";
  if (c >= 90) return "ve";
  if (c <= 50) return "me";
  return "";
}

function pair(a, b) {
  if (!a && !b) return "–";
  return `${show(a)}<span class="sep">/</span>${show(b)}`;
}

function rdvCell(r) {
  const veille = r.rdv.date !== r.date ? `<span class="veille" title="RDV la veille">J-1</span> ` : "";
  return veille + r.rdv.time;
}

const TABLE_HEAD = `
  <thead>
    <tr>
      <th scope="col">Date</th>
      <th scope="col"><abbr title="Heure de rendez-vous (étale − 2 h)">RDV</abbr></th>
      <th scope="col"><abbr title="Étale de pleine mer (PM) ou de basse mer (BM)">Étale</abbr></th>
      <th scope="col" class="num"><abbr title="Hauteur d'eau à l'étale, en mètres">H (m)</abbr></th>
      <th scope="col" class="num"><abbr title="Coefficient de marée (indicatif)">Coef</abbr></th>
      <th scope="col"><abbr title="Fenêtre de plongée : étale ± marge">Fenêtre</abbr></th>
      <th scope="col"><abbr title="Lever / coucher du soleil">Soleil</abbr></th>
      <th scope="col"><abbr title="Aube / crépuscule nautique (soleil à −12°)">Naut.</abbr></th>
    </tr>
  </thead>`;

const LEGEND = `
  <p class="legend">
    <span><b>PM</b>/<b>BM</b> pleine/basse mer</span>
    <span><b>H</b> hauteur d'eau</span>
    <span><b>Soleil</b> lever/coucher</span>
    <span><b>Naut.</b> aube/crépuscule nautique</span>
    <span><b>J-1</b> RDV la veille</span>
    <span><span class="coef ve">VE</span> coef ≥ 90</span>
    <span><span class="coef me">ME</span> coef ≤ 50</span>
  </p>`;

function renderResults(data) {
  resultsEl.innerHTML = "";
  if (data.results.length === 0) {
    statusEl.textContent = "Aucun créneau ne correspond à ces critères sur la période choisie.";
    return;
  }
  statusEl.textContent = `${data.results.length} créneau(x) pour ${data.port}.`;

  // Regroupe par jour : date et heures de soleil fusionnées sur les lignes du jour
  const byDay = new Map();
  for (const r of data.results) {
    if (!byDay.has(r.date)) byDay.set(r.date, []);
    byDay.get(r.date).push(r);
  }

  const rows = [];
  for (const [day, items] of byDay) {
    const span = items.length;
    const sun = items[0].sun || {};
    items.forEach((r, i) => {
      const first = i === 0;
      rows.push(`
        <tr class="${first ? "day-start" : ""}">
          ${first ? `<th scope="row" rowspan="${span}" class="c-date">${formatDay(day)}</th>` : ""}
          <td class="c-rdv">${rdvCell(r)}</td>
          <td class="c-tide"><span class="kind ${r.kind}">${r.kind}</span>${r.time}</td>
          <td class="num">${fmtHeight(r.height_m)}</td>
          <td class="num"><span class="coef ${coefClass(r.coefficient)}">${fmtCoef(r.coefficient)}</span></td>
          <td class="c-win">${r.window.start}–${r.window.end}</td>
          ${first ? `<td rowspan="${span}" class="c-sun">${pair(sun.sunrise, sun.sunset)}</td>` : ""}
          ${first ? `<td rowspan="${span}" class="c-sun">${pair(sun.nautical_dawn, sun.nautical_dusk)}</td>` : ""}
        </tr>`);
    });
  }

  resultsEl.innerHTML = `
    <div class="table-wrap">
      <table class="windows">${TABLE_HEAD}<tbody>${rows.join("")}</tbody></table>
    </div>${LEGEND}`;
}

async function search() {
  const portId = portSelect.value;
  if (!portId) return;
  statusEl.textContent = "Recherche…";
  const params = new URLSearchParams({
    port_id: portId,
    start: startInput.value,
    end: endInput.value,
    max_coefficient: maxCoefInput.value,
    tide_phase: tidePhaseSelect.value,
    daylight: daylightSelect.value,
    margin_minutes: marginInput.value,
  });
  try {
    const res = await fetch(`/api/dive-windows?${params}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      statusEl.textContent = `Erreur : ${err.detail || res.statusText}`;
      return;
    }
    renderResults(await res.json());
  } catch (e) {
    statusEl.textContent = "Erreur réseau : le serveur est-il lancé ?";
  }
}

searchBtn.addEventListener("click", search);

startInput.value = todayISO();
endInput.value = todayISO(13);

loadPorts();