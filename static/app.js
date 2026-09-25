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

const fmtDate = new Intl.DateTimeFormat("fr-FR", {
  weekday: "long", day: "numeric", month: "long", year: "numeric",
});

function formatDate(iso) {
  // midi pour éviter tout décalage de jour lié au fuseau
  return fmtDate.format(new Date(iso + "T12:00:00"));
}

const fmtDay = new Intl.DateTimeFormat("fr-FR", {
  weekday: "short", day: "2-digit", month: "2-digit",
});

function formatDay(iso) {
  // midi pour éviter tout décalage de jour lié au fuseau
  return fmtDay.format(new Date(iso + "T12:00:00"));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Nature du jour (week-end, férié, vacances) : classes de ligne + libellés
function dayDecorations(day) {
  if (!day) return { classes: "", notes: "", title: "" };
  const classes = [];
  const titles = [];
  let notes = "";
  if (day.weekend) classes.push("weekend");
  if (day.holiday) {
    classes.push("ferie");
    titles.push(day.holiday);
    notes += `<span class="ferie-name">${escapeHtml(day.holiday)}</span>`;
  }
  if (day.school_holiday) {
    classes.push("vacances");
    titles.push(day.school_holiday);
  }
  return { classes: classes.join(" "), notes, title: escapeHtml(titles.join(" · ")) };
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
    <tr class="filters">
      <th>
        <select data-f="day" aria-label="Filtrer par type de jour">
          <option value="">Tous</option>
          <option value="off">Week-end ou férié</option>
          <option value="weekend">Week-end</option>
          <option value="ferie">Férié</option>
          <option value="vacances">Vacances</option>
          <option value="semaine">En semaine</option>
        </select>
      </th>
      <th>
        <div class="range stack">
          <input type="time" data-f="rdvMin" aria-label="RDV à partir de" title="RDV à partir de">
          <input type="time" data-f="rdvMax" aria-label="RDV jusqu'à" title="RDV jusqu'à">
        </div>
      </th>
      <th>
        <select data-f="kind" aria-label="Filtrer par étale">
          <option value="">Tous</option>
          <option value="PM">PM</option>
          <option value="BM">BM</option>
        </select>
      </th>
      <th class="num">
        <div class="range">
          <input type="number" data-f="hMin" step="0.1" placeholder="min" aria-label="Hauteur minimale (m)">
          <input type="number" data-f="hMax" step="0.1" placeholder="max" aria-label="Hauteur maximale (m)">
        </div>
      </th>
      <th class="num">
        <div class="range">
          <input type="number" data-f="coefMin" min="20" max="120" step="1" placeholder="min" aria-label="Coefficient minimal">
          <input type="number" data-f="coefMax" min="20" max="120" step="1" placeholder="max" aria-label="Coefficient maximal">
        </div>
      </th>
      <th colspan="3">
        <button type="button" class="reset-filters" disabled>Effacer les filtres</button>
      </th>
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
    <span><span class="swatch swatch-weekend"></span>samedi/dimanche</span>
    <span><span class="swatch swatch-ferie"></span>jour férié</span>
    <span><span class="swatch swatch-vacances"></span>vacances scolaires</span>
  </p>`;

// ---- Filtres de la ligne de titre (côté client, sur les résultats déjà chargés) ----

let lastData = null;
let tableEl = null;

function readFilters() {
  const f = {};
  for (const el of tableEl.tHead.querySelectorAll("[data-f]")) {
    f[el.dataset.f] = el.value;
    el.classList.toggle("is-active", el.value !== "");
  }
  return f;
}

const toNum = v => (v === "" || v == null ? null : Number(v));

function inRange(v, min, max) {
  if (min == null && max == null) return true;
  if (v == null) return false;
  return (min == null || v >= min) && (max == null || v <= max);
}

// Comparaison "HH:MM" en chaîne ; si min > max, la plage passe minuit (ex. 20:00 → 02:00)
function inTimeRange(t, min, max) {
  if (!min && !max) return true;
  if (min && max && min > max) return t >= min || t <= max;
  return (!min || t >= min) && (!max || t <= max);
}

function matchDay(day, mode) {
  if (!mode) return true;
  const d = day || {};
  switch (mode) {
    case "weekend":  return !!d.weekend;
    case "ferie":    return !!d.holiday;
    case "off":      return !!(d.weekend || d.holiday);
    case "vacances": return !!d.school_holiday;
    case "semaine":  return !d.weekend && !d.holiday;
    default:         return true;
  }
}

function applyFilters(results, f) {
  const hMin = toNum(f.hMin), hMax = toNum(f.hMax);
  const cMin = toNum(f.coefMin), cMax = toNum(f.coefMax);
  return results.filter(r =>
    matchDay(r.day, f.day) &&
    (!f.kind || r.kind === f.kind) &&
    inTimeRange(r.rdv.time, f.rdvMin, f.rdvMax) &&
    inRange(r.height_m, hMin, hMax) &&
    // on filtre sur la valeur arrondie, celle qui est affichée
    inRange(r.coefficient != null ? Math.round(r.coefficient) : null, cMin, cMax)
  );
}

function buildRows(results) {
  // Regroupe par jour : date et heures de soleil fusionnées sur les lignes du jour
  const byDay = new Map();
  for (const r of results) {
    if (!byDay.has(r.date)) byDay.set(r.date, []);
    byDay.get(r.date).push(r);
  }

  const rows = [];
  for (const [day, items] of byDay) {
    const span = items.length;
    const sun = items[0].sun || {};
    const deco = dayDecorations(items[0].day);
    items.forEach((r, i) => {
      const first = i === 0;
      rows.push(`
        <tr class="${[first ? "day-start" : "", deco.classes].join(" ").trim()}">
          ${first ? `<th scope="row" rowspan="${span}" class="c-date"${deco.title ? ` title="${deco.title}"` : ""}>${formatDay(day)}${deco.notes}</th>` : ""}
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
  return rows.join("");
}

// Vacances absentes de la base ou ne couvrant pas la période : on le dit
function schoolHolidaysWarning(data) {
  const sh = data.school_holidays;
  if (!sh || sh.covered) return "";
  return sh.periods
    ? ` Vacances scolaires connues seulement en partie pour cette période (académie de ${sh.academy}).`
    : ` Vacances scolaires non chargées : lance « python -m app.calendar_fr ».`;
}

function renderRows() {
  if (!lastData || !tableEl) return;
  const f = readFilters();
  const active = Object.values(f).some(v => v !== "");
  const all = lastData.results;
  const shown = active ? applyFilters(all, f) : all;

  tableEl.tHead.querySelector(".reset-filters").disabled = !active;
  statusEl.textContent = (active
    ? `${shown.length} créneau(x) sur ${all.length} pour ${lastData.port} avec les filtres.`
    : `${all.length} créneau(x) pour ${lastData.port}.`) + schoolHolidaysWarning(lastData);

  tableEl.tBodies[0].innerHTML = shown.length
    ? buildRows(shown)
    : `<tr><td colspan="8" class="empty">Aucun créneau ne correspond aux filtres. Élargis-les ou efface-les.</td></tr>`;
}

// Le tableau est construit une seule fois : les filtres restent en place d'une recherche à l'autre
function ensureTable() {
  if (tableEl) return;
  resultsEl.innerHTML = `
    <div class="table-wrap">
      <table class="windows">${TABLE_HEAD}<tbody></tbody></table>
    </div>${LEGEND}`;
  tableEl = resultsEl.querySelector("table.windows");
  const thead = tableEl.tHead;
  thead.addEventListener("input", renderRows);
  thead.querySelector(".reset-filters").addEventListener("click", () => {
    for (const el of thead.querySelectorAll("[data-f]")) el.value = "";
    renderRows();
  });
}

function renderResults(data) {
  lastData = data;
  if (data.results.length === 0) {
    resultsEl.hidden = true;
    statusEl.textContent = "Aucun créneau ne correspond à ces critères sur la période choisie.";
    return;
  }
  ensureTable();
  resultsEl.hidden = false;
  renderRows();
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
  let res;
  try {
    res = await fetch(`/api/dive-windows?${params}`);
  } catch (e) {
    statusEl.textContent = "Erreur réseau : le serveur est-il lancé ?";
    console.error(e);
    return;
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    statusEl.textContent = `Erreur ${res.status} : ${err.detail || res.statusText}`;
    return;
  }
  try {
    renderResults(await res.json());
  } catch (e) {
    statusEl.textContent = `Erreur d'affichage : ${e.message}`;
    console.error(e);
  }
}

searchBtn.addEventListener("click", search);

startInput.value = todayISO();
endInput.value = todayISO(13);

loadPorts();