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
const exportBtn = document.getElementById("export-xlsx");

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

// RDV : réservé aux comptes connectés (absent des résultats d'un visiteur)
const hasRdv = data => data?.rdv_offset_minutes != null;

function rdvCell(r) {
  if (!r.rdv) return "";
  const veille = r.rdv.date !== r.date ? `<span class="veille" title="RDV la veille">J-1</span> ` : "";
  return veille + r.rdv.time;
}

// Délai étale → RDV, fixé par la structure du compte (2 h sinon) ; RDV au pas de 5 min
function rdvTitle(offset) {
  const h = Math.floor(offset / 60), m = offset % 60;
  const delay = m ? `${h} h ${String(m).padStart(2, "0")}` : `${h} h`;
  return `Heure de rendez-vous (étale − ${delay}, arrondie aux 5 min inférieures)`;
}

const TABLE_HEAD = `
  <thead>
    <tr>
      <th scope="col">Date</th>
      <th scope="col" class="c-rdv"><abbr class="rdv-abbr" title="${rdvTitle(120)}">RDV</abbr></th>
      <th scope="col"><abbr title="Étale de pleine mer (PM) ou de basse mer (BM)">Étale</abbr></th>
      <th scope="col" class="num"><abbr title="Hauteur d'eau à l'étale, en mètres">H (m)</abbr></th>
      <th scope="col" class="num"><abbr title="Coefficient de marée (indicatif)">Coef</abbr></th>
      <th scope="col"><abbr title="Fenêtre de plongée : étale ± marge">Fenêtre</abbr></th>
      <th scope="col"><abbr title="Lever / coucher du soleil">Soleil</abbr></th>
      <th scope="col"><abbr title="Aube / crépuscule nautique (soleil à −12°)">Naut.</abbr></th>
      <th scope="col" class="c-pick-head">Choix</th>
    </tr>
    <tr class="filters">
      <th data-label="Jour">
        <select data-f="day" aria-label="Filtrer par type de jour">
          <option value="">Tous</option>
          <option value="off">Week-end ou férié</option>
          <option value="weekend">Week-end</option>
          <option value="ferie">Férié</option>
          <option value="vacances">Vacances</option>
          <option value="semaine">En semaine</option>
        </select>
      </th>
      <th class="c-rdv" data-label="RDV">
        <div class="range stack">
          <input type="time" data-f="rdvMin" aria-label="RDV à partir de" title="RDV à partir de">
          <input type="time" data-f="rdvMax" aria-label="RDV jusqu'à" title="RDV jusqu'à">
        </div>
      </th>
      <th data-label="Étale">
        <select data-f="kind" aria-label="Filtrer par étale">
          <option value="">Tous</option>
          <option value="PM">PM</option>
          <option value="BM">BM</option>
        </select>
      </th>
      <th class="num" data-label="Hauteur (m)">
        <div class="range">
          <input type="number" data-f="hMin" step="0.1" placeholder="min" aria-label="Hauteur minimale (m)">
          <input type="number" data-f="hMax" step="0.1" placeholder="max" aria-label="Hauteur maximale (m)">
        </div>
      </th>
      <th class="num" data-label="Coefficient">
        <div class="range">
          <input type="number" data-f="coefMin" min="20" max="120" step="1" placeholder="min" aria-label="Coefficient minimal">
          <input type="number" data-f="coefMax" min="20" max="120" step="1" placeholder="max" aria-label="Coefficient maximal">
        </div>
      </th>
      <th colspan="3">
        <button type="button" class="reset-filters" disabled>Effacer les filtres</button>
      </th>
      <th class="c-pick-head" data-label="Choix">
        <select data-f="pick" aria-label="Filtrer par choix">
          <option value="">Tous</option>
          <option value="free">Non choisis</option>
          <option value="picked">Choisis</option>
        </select>
      </th>
    </tr>
  </thead>`;

const LEGEND = `
  <p class="legend">
    <span><b>PM</b>/<b>BM</b> pleine/basse mer</span>
    <span><b>H</b> hauteur d'eau</span>
    <span><b>Soleil</b> lever/coucher</span>
    <span><b>Naut.</b> aube/crépuscule nautique</span>
    <span class="c-rdv"><b>J-1</b> RDV la veille</span>
    <span><span class="coef ve">VE</span> coef ≥ 90</span>
    <span><span class="coef me">ME</span> coef ≤ 50</span>
    <span><span class="swatch swatch-weekend"></span>samedi/dimanche</span>
    <span><span class="swatch swatch-ferie"></span>jour férié</span>
    <span><span class="swatch swatch-vacances"></span>vacances scolaires</span>
    <span><span class="swatch swatch-picked"></span>créneau déjà choisi</span>
  </p>`;

// ---- Filtres de la ligne de titre (côté client, sur les résultats déjà chargés) ----

let lastData = null;
let lastShown = [];   // créneaux affichés (filtres compris) : ce que l'export reprend
let tableEl = null;
// Filtres de colonnes à appliquer dès que le tableau existe (préférences chargées avant la 1re recherche)
let pendingFilters = {};

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

function matchPick(r, mode) {
  if (!mode || !Session.user?.can.view_selections) return true;
  return mode === "picked" ? isPicked(r) : !isPicked(r);
}

function applyFilters(results, f) {
  const rdv = hasRdv(lastData);  // filtre RDV caché et ignoré sans RDV
  const hMin = toNum(f.hMin), hMax = toNum(f.hMax);
  const cMin = toNum(f.coefMin), cMax = toNum(f.coefMax);
  return results.filter(r =>
    matchPick(r, f.pick) &&
    matchDay(r.day, f.day) &&
    (!f.kind || r.kind === f.kind) &&
    (!rdv || inTimeRange(r.rdv.time, f.rdvMin, f.rdvMax)) &&
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
      const picked = pickedFor(r);
      rows.push(`
        <tr class="${[first ? "day-start" : "", deco.classes, picked ? "is-picked" : ""].join(" ").trim()}" data-key="${escapeHtml(slotKey(r))}" data-day="${escapeHtml(formatDay(day))}">
          ${first ? `<th scope="row" rowspan="${span}" class="c-date"${deco.title ? ` title="${deco.title}"` : ""}>${formatDay(day)}${deco.notes}</th>` : ""}
          <td class="c-rdv" data-label="RDV">${rdvCell(r)}</td>
          <td class="c-tide" data-label="Étale"><span class="kind ${r.kind}">${r.kind}</span>${r.time}</td>
          <td class="num" data-label="Hauteur">${fmtHeight(r.height_m)}</td>
          <td class="num" data-label="Coef"><span class="coef ${coefClass(r.coefficient)}">${fmtCoef(r.coefficient)}</span></td>
          <td class="c-win" data-label="Fenêtre">${r.window.start}–${r.window.end}</td>
          ${first ? `<td rowspan="${span}" class="c-sun" data-label="Soleil">${pair(sun.sunrise, sun.sunset)}</td>` : ""}
          ${first ? `<td rowspan="${span}" class="c-sun" data-label="Nautique">${pair(sun.nautical_dawn, sun.nautical_dusk)}</td>` : ""}
          <td class="c-pick" data-label="Choix">${pickCell(picked)}</td>
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
  resultsEl.closest(".results").classList.toggle("show-rdv", hasRdv(lastData));
  if (hasRdv(lastData)) tableEl.querySelector(".rdv-abbr").title = rdvTitle(lastData.rdv_offset_minutes);
  const shown = active ? applyFilters(all, f) : all;
  lastShown = shown;
  exportBtn.hidden = false;
  exportBtn.disabled = !shown.length;

  tableEl.tHead.querySelector(".reset-filters").disabled = !active;
  const nPicked = Session.user?.can.view_selections ? all.filter(isPicked).length : 0;
  statusEl.textContent = (active
    ? `${shown.length} créneau(x) sur ${all.length} pour ${lastData.port} avec les filtres.`
    : `${all.length} créneau(x) pour ${lastData.port}.`)
    + (nPicked ? ` ${Session.user.structure.name} en a choisi ${nPicked}.` : "")
    + schoolHolidaysWarning(lastData);

  tableEl.tBodies[0].innerHTML = shown.length
    ? buildRows(shown)
    : `<tr><td colspan="9" class="empty">Aucun créneau ne correspond aux filtres. Élargis-les ou efface-les.</td></tr>`;
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
  setFilterInputs(pendingFilters);
  thead.addEventListener("input", renderRows);
  tableEl.tBodies[0].addEventListener("change", onPickChange);
  tableEl.tBodies[0].addEventListener("click", onUnpickClick);
  thead.querySelector(".reset-filters").addEventListener("click", () => {
    for (const el of thead.querySelectorAll("[data-f]")) el.value = "";
    renderRows();
  });
}

function setFilterInputs(filters) {
  if (!tableEl) return;
  for (const el of tableEl.tHead.querySelectorAll("[data-f]")) {
    el.value = filters[el.dataset.f] ?? "";
  }
}

function renderResults(data) {
  lastData = data;
  if (data.results.length === 0) {
    lastShown = [];
    exportBtn.hidden = true;
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

// ---- Export Excel des créneaux affichés ----

const fmtWeekday = new Intl.DateTimeFormat("fr-FR", { weekday: "long" });

function exportColumns() {
  const cols = [
    { header: "Date", type: "date", width: 11, value: r => r.date },
    { header: "Jour", width: 10, value: r => fmtWeekday.format(new Date(r.date + "T12:00:00")) },
    { header: "Port", width: 18, value: () => lastData.port },
    ...(hasRdv(lastData) ? [
      { header: "Date RDV", type: "date", width: 11, value: r => r.rdv.date },
      { header: "Heure RDV", type: "time", width: 10, value: r => r.rdv.time },
    ] : []),
    { header: "Étale", width: 7, value: r => r.kind },
    { header: "Heure étale", type: "time", width: 11, value: r => r.time },
    { header: "Hauteur (m)", type: "decimal", width: 11, value: r => r.height_m },
    { header: "Coefficient", type: "int", width: 11, value: r => (r.coefficient != null ? Math.round(r.coefficient) : null) },
    { header: "Fenêtre début", type: "time", width: 13, value: r => r.window.start },
    { header: "Fenêtre fin", type: "time", width: 11, value: r => r.window.end },
    { header: "Lever soleil", type: "time", width: 11, value: r => r.sun?.sunrise },
    { header: "Coucher soleil", type: "time", width: 13, value: r => r.sun?.sunset },
    { header: "Aube nautique", type: "time", width: 13, value: r => r.sun?.nautical_dawn },
    { header: "Crépuscule nautique", type: "time", width: 18, value: r => r.sun?.nautical_dusk },
    { header: "Week-end", width: 10, value: r => (r.day?.weekend ? "oui" : "") },
    { header: "Jour férié", width: 16, value: r => r.day?.holiday || "" },
    { header: "Vacances scolaires", width: 22, value: r => r.day?.school_holiday || "" },
  ];
  if (Session.user?.can.view_selections) {
    cols.push(
      { header: "Choix", width: 16, value: r => pickedFor(r)?.type.label || "" },
      { header: "Choisi par", width: 18, value: r => pickedFor(r)?.picked_by || "" },
    );
  }
  return cols;
}

exportBtn.addEventListener("click", () => {
  if (!lastData || !lastShown.length) return;
  const first = lastShown[0].date, last = lastShown[lastShown.length - 1].date;
  XlsxExport.download(`creneaux-${XlsxExport.slug(lastData.port)}-${first}-au-${last}.xlsx`,
    `Créneaux ${lastData.port}`, exportColumns(), lastShown);
});

startInput.value = todayISO();
endInput.value = todayISO(13);

// ---- Créneaux choisis (par structure, typés via la liste de la structure) ----
// Administration de la structure : choisit et retire. Visualisation : voit seulement.

let slotTypes = [];                 // types actifs de la structure, dans l'ordre de l'administration
let picks = new Map();              // "port_id|ts_utc" → créneau choisi

const slotKey = r => `${r.port_id}|${r.ts_utc}`;
const pickedFor = r => picks.get(slotKey(r)) || null;
const isPicked = r => picks.has(slotKey(r));

function typePill(t) {
  return `<span class="type-pill" style="--type-color:${escapeHtml(t.color)}">${escapeHtml(t.label)}</span>`;
}

function pickCell(picked) {
  const u = Session.user;
  if (!u?.can.view_selections) return "";
  if (picked) {
    const by = picked.picked_by ? ` title="Choisi par ${escapeHtml(picked.picked_by)}"` : "";
    return `<span${by}>${typePill(picked.type)}</span>` + (u.can.pick
      ? `<button type="button" class="unpick" data-unpick="${picked.id}" title="Retirer ce choix" aria-label="Retirer ce choix">×</button>`
      : "");
  }
  if (!u.can.pick) return "";
  if (!slotTypes.length) {
    return `<span class="muted" title="Créez d'abord des types de créneaux dans l'administration">aucun type</span>`;
  }
  return `<select data-pick aria-label="Choisir ce créneau avec un type">
      <option value="">Choisir…</option>
      ${slotTypes.map(t => `<option value="${t.id}">${escapeHtml(t.label)}</option>`).join("")}
    </select>`;
}

async function loadPicks(user) {
  slotTypes = [];
  picks = new Map();
  if (user?.can.view_selections) {
    try {
      const [types, sels] = await Promise.all([
        user.can.pick ? Session.api("/api/slot-types") : [],
        Session.api("/api/selections"),
      ]);
      slotTypes = types;
      picks = new Map(sels.map(s => [slotKey(s), s]));
    } catch (e) {
      statusEl.textContent = `Créneaux choisis non chargés : ${e.message}`;
    }
  }
  if (tableEl) renderRows();
}

async function onPickChange(e) {
  const sel = e.target.closest("select[data-pick]");
  if (!sel || !sel.value) return;
  const r = lastData.results.find(x => slotKey(x) === sel.closest("tr").dataset.key);
  if (!r) return;
  sel.disabled = true;
  try {
    const s = await Session.api("/api/selections", {
      method: "POST",
      body: { port_id: r.port_id, ts_utc: r.ts_utc, type_id: Number(sel.value) },
    });
    picks.set(slotKey(s), s);
  } catch (err) {
    statusEl.textContent = `Choix impossible : ${err.message}`;
    // déjà choisi (autre onglet) : on resynchronise pour griser la ligne
    if (err.status === 409) return loadPicks(Session.user);
    sel.disabled = false;
    sel.value = "";
    return;
  }
  renderRows();
}

async function onUnpickClick(e) {
  const btn = e.target.closest("button[data-unpick]");
  if (!btn) return;
  btn.disabled = true;
  try {
    await Session.api(`/api/selections/${btn.dataset.unpick}`, { method: "DELETE" });
  } catch (err) {
    if (err.status !== 404) {
      statusEl.textContent = `Retrait impossible : ${err.message}`;
      btn.disabled = false;
      return;
    }
  }
  for (const [k, s] of picks) if (String(s.id) === btn.dataset.unpick) picks.delete(k);
  renderRows();
}

// ---- Préférences utilisateur (formulaire + filtres de colonnes) ----

const prefsBar = document.getElementById("prefs-bar");
const prefsSaveBtn = document.getElementById("prefs-save");
const prefsRestoreBtn = document.getElementById("prefs-restore");
const prefsStatus = document.getElementById("prefs-status");
let savedPrefs = null;

const fmtStamp = new Intl.DateTimeFormat("fr-FR", { dateStyle: "short", timeStyle: "short" });

function daysBetween(a, b) {
  return Math.round((new Date(b + "T12:00:00") - new Date(a + "T12:00:00")) / 86400000);
}

function currentFilters() {
  if (!tableEl) return { ...pendingFilters };
  const f = {};
  for (const el of tableEl.tHead.querySelectorAll("[data-f]")) f[el.dataset.f] = el.value;
  return f;
}

function collectPrefs() {
  const span = daysBetween(startInput.value, endInput.value);
  return {
    form: {
      port_id: portSelect.value ? Number(portSelect.value) : null,
      span_days: Number.isFinite(span) && span >= 0 ? Math.min(span, 366) : null,
      tide_phase: tidePhaseSelect.value,
      max_coefficient: Number(maxCoefInput.value),
      margin_minutes: Number(marginInput.value),
      daylight: daylightSelect.value,
    },
    filters: currentFilters(),
  };
}

function applyPrefs(prefs) {
  const f = prefs.form || {};
  if (f.port_id != null && portSelect.querySelector(`option[value="${f.port_id}"]`)) {
    portSelect.value = String(f.port_id);
  }
  if (f.span_days != null) {
    startInput.value = todayISO();
    endInput.value = todayISO(f.span_days);
  }
  if (f.tide_phase) tidePhaseSelect.value = f.tide_phase;
  if (f.daylight) daylightSelect.value = f.daylight;
  if (f.max_coefficient != null) {
    maxCoefInput.value = f.max_coefficient;
    coefVal.textContent = maxCoefInput.value;
  }
  if (f.margin_minutes != null) {
    marginInput.value = f.margin_minutes;
    marginVal.textContent = marginInput.value;
  }
  pendingFilters = { ...(prefs.filters || {}) };
  setFilterInputs(pendingFilters);
  if (tableEl) renderRows();
}

function showSavedStamp() {
  prefsStatus.textContent = savedPrefs?.updated_at
    ? `Enregistrées le ${fmtStamp.format(new Date(savedPrefs.updated_at))}`
    : "Aucune préférence enregistrée.";
}

async function onSessionChange(user) {
  resultsEl.closest(".results").classList.toggle("can-pick", !!user?.can.view_selections);
  // connexion / déconnexion : relance la recherche affichée pour ajouter ou retirer le RDV
  let refresh = !!lastData && hasRdv(lastData) !== !!user;
  await loadPicks(user);
  // port par défaut de la structure ; les préférences du membre, appliquées ensuite, priment
  const defaultPort = user?.structure?.default_port_id;
  if (defaultPort != null && portSelect.querySelector(`option[value="${defaultPort}"]`)) {
    portSelect.value = String(defaultPort);
  }
  prefsBar.hidden = !user;
  savedPrefs = null;
  prefsRestoreBtn.disabled = true;
  if (!user) {
    if (refresh) search();
    return;
  }
  try {
    const prefs = await Session.api("/api/me/preferences");
    if (prefs.updated_at) {
      savedPrefs = prefs;
      prefsRestoreBtn.disabled = false;
      applyPrefs(prefs);
      search();
      refresh = false;
    }
    showSavedStamp();
  } catch (e) {
    prefsStatus.textContent = `Préférences non chargées : ${e.message}`;
  }
  if (refresh) search();
}

prefsSaveBtn.addEventListener("click", async () => {
  prefsSaveBtn.disabled = true;
  try {
    savedPrefs = await Session.api("/api/me/preferences", { method: "PUT", body: collectPrefs() });
    prefsRestoreBtn.disabled = false;
    showSavedStamp();
  } catch (e) {
    prefsStatus.textContent = `Échec de l'enregistrement : ${e.message}`;
  } finally {
    prefsSaveBtn.disabled = false;
  }
});

prefsRestoreBtn.addEventListener("click", () => {
  if (!savedPrefs) return;
  applyPrefs(savedPrefs);
  search();
});

(async () => {
  // les ports d'abord : la préférence port_id doit trouver son option
  try {
    await loadPorts();
  } catch (e) {
    statusEl.textContent = "Erreur réseau : le serveur est-il lancé ?";
  }
  Session.mountAccount(document.getElementById("account"), [Session.LINKS.picks, Session.LINKS.admin]);
  // connexion depuis la recherche : les membres d'une structure vont directement à leurs créneaux choisis
  Session.redirectAfterLogin = u => (u.can.view_selections ? "mes-creneaux.html" : null);
  Session.onChange(onSessionChange);
  await Session.init();
})();