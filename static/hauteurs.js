// Recherche par hauteur d'eau : plages où l'eau est au-dessus (ou au-dessous) d'une hauteur du port.
// Réservée aux structures auxquelles les super administrateurs l'ont ouverte (et aux super administrateurs).

const $ = id => document.getElementById(id);
const esc = Session.esc;

const gateEl = $("gate");
const controlsEl = $("controls");
const resultsEl = $("results");
const rowsEl = $("rows");
const statusEl = $("status");
const portSelect = $("port");
const thresholdSelect = $("threshold");

let ports = [];          // [{id, name, thresholds: [...]}]
let slotTypes = [];      // types proposés (administrateurs de la structure)
let picks = new Map();   // "threshold_id|start_utc" → [créneaux choisis sur cette plage]
let last = null;         // dernière réponse de recherche

const fmtDay = new Intl.DateTimeFormat("fr-FR", { weekday: "short", day: "2-digit", month: "2-digit" });
const formatDay = iso => fmtDay.format(new Date(`${iso}T12:00:00`));
const toISO = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const fmtM = h => `${h.toFixed(2).replace(".", ",")} m`;
const fmtDuration = min => (min >= 60 ? `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")}` : `${min} min`);
const pickKey = (thresholdId, startUtc) => `${thresholdId}|${startUtc}`;
const canPick = () => !!Session.user?.can.pick;

// ---- Ports et hauteurs d'eau ----

function renderThresholds() {
  const port = ports.find(p => p.id === Number(portSelect.value));
  thresholdSelect.innerHTML = (port?.thresholds || []).map(t =>
    `<option value="${t.id}">${esc(t.label)} (${t.direction === "above" ? "au moins" : "au plus"} ${fmtM(t.height_m)})</option>`).join("");
}

async function loadPorts() {
  ports = await Session.api("/api/water-thresholds");
  portSelect.innerHTML = ports.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  const preferred = Session.user?.structure?.default_port_id;
  if (ports.some(p => p.id === preferred)) portSelect.value = String(preferred);
  renderThresholds();
}

portSelect.addEventListener("change", renderThresholds);

// ---- Créneaux déjà choisis par la structure ----

async function loadPicks() {
  picks = new Map();
  slotTypes = [];
  if (!Session.user?.can.view_selections) return;
  const [types, sels] = await Promise.all([
    canPick() ? Session.api("/api/slot-types") : [],
    Session.api("/api/selections"),
  ]);
  slotTypes = types;
  for (const s of sels) if (s.water) addPick(s);
}

function addPick(s) {
  const key = pickKey(s.water.threshold_id, s.water.start_utc);
  picks.set(key, [...(picks.get(key) || []), s]);
}

// ---- Résultats ----

function dayDecorations(day) {
  const classes = [], titles = [];
  let notes = "";
  if (day.weekend) classes.push("weekend");
  if (day.holiday) {
    classes.push("ferie");
    titles.push(day.holiday);
    notes += `<span class="ferie-name">${esc(day.holiday)}</span>`;
  }
  if (day.school_holiday) {
    classes.push("vacances");
    titles.push(day.school_holiday);
  }
  return { classes: classes.join(" "), notes, title: esc(titles.join(" · ")) };
}

function pickCell(r, picked) {
  const u = Session.user;
  if (!u?.can.view_selections) return "";
  const items = picked.map(p => `
    <span class="pick-item"><span title="Choisi par ${esc(p.picked_by || "compte supprimé")}">
      <span class="type-pill" style="--type-color:${esc(p.type.color)}">${esc(p.type.label)}</span>${p.note ? ` <span class="pick-note">${esc(p.note)}</span>` : ""}
    </span>${u.can.pick ? `<button type="button" class="unpick" data-unpick="${p.id}" aria-label="Retirer le choix ${esc(p.type.label)}">×</button>` : ""}</span>`).join("");
  if (r.unavailable) {
    return `${items}<span class="unavailable-tag" title="${esc(`Structure indisponible ${r.unavailable.label}`)}">Indisponible${r.unavailable.reason ? ` <span class="unavailable-reason">· ${esc(r.unavailable.reason)}</span>` : ""}</span>`;
  }
  if (!u.can.pick || !slotTypes.length) return items;
  return `${items}<select data-pick aria-label="Choisir cette plage avec un type">
      <option value="">${picked.length ? "+ Autre…" : "Choisir…"}</option>
      ${slotTypes.map(t => `<option value="${t.id}">${esc(t.label)}</option>`).join("")}
    </select>`;
}

// ---- Filtres de la ligne de titre (côté client, sur les plages déjà chargées) ----

const filtersRow = document.querySelector("table.water tr.filters");
const resetBtn = filtersRow.querySelector(".reset-filters");

function readFilters() {
  const f = {};
  for (const el of filtersRow.querySelectorAll("[data-f]")) {
    f[el.dataset.f] = el.value;
    el.classList.toggle("is-active", el.value !== "");
  }
  return f;
}

const toNum = v => (v === "" || v == null ? null : Number(v));

// Comparaison "HH:MM" en chaîne ; si min > max, la plage passe minuit (ex. 20:00 → 02:00)
function inTimeRange(t, min, max) {
  if (!min && !max) return true;
  if (min && max && min > max) return t >= min || t <= max;
  return (!min || t >= min) && (!max || t <= max);
}

function matchDay(day, mode) {
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

const isPicked = r => (picks.get(pickKey(r.threshold_id, r.start_utc)) || []).length > 0;

function applyFilters(results, f) {
  const hMin = toNum(f.hMin), hMax = toNum(f.hMax), durMin = toNum(f.durMin);
  return results.filter(r =>
    matchDay(r.day, f.day) &&
    inTimeRange(r.rdv_time, f.rdvMin, f.rdvMax) &&
    (f.tight !== "sure" || !r.tight) &&
    (durMin == null || r.minutes >= durMin) &&
    (hMin == null || r.extreme_m >= hMin) && (hMax == null || r.extreme_m <= hMax) &&
    (!f.pick || !Session.user?.can.view_selections || (f.pick === "picked") === isPicked(r))
  );
}

filtersRow.addEventListener("input", () => { if (last) render(); else readFilters(); });
resetBtn.addEventListener("click", () => {
  for (const el of filtersRow.querySelectorAll("[data-f]")) el.value = "";
  if (last) render(); else readFilters();
});

function render() {
  const data = last;
  const t = data.threshold;
  $("extreme-head").textContent = t.direction === "above" ? "Hauteur max" : "Hauteur min";
  const f = readFilters();
  const active = Object.values(f).some(v => v !== "");
  const shown = active ? applyFilters(data.results, f) : data.results;
  resetBtn.disabled = !active;
  const nPicked = data.results.filter(isPicked).length;
  const off = data.tide_sources && !(data.tide_sources.api_maree && data.tide_sources.calibration)
    ? " Horaires selon les réglages de votre structure (sans api-maree.fr ou sans correction)." : "";
  statusEl.textContent = (active ? `${shown.length} plage(s) sur ${data.results.length}` : `${data.results.length} plage(s)`)
    + ` à ${data.port} où l'eau est ${t.direction === "above" ? "au-dessus" : "au-dessous"} de ${fmtM(t.height_m)} (${t.label})`
    + (active ? " avec les filtres." : ".")
    + (nPicked ? ` ${Session.user.structure.name} en a choisi ${nPicked}.` : "") + off;
  resultsEl.classList.toggle("can-pick", !!Session.user?.can.view_selections);
  rowsEl.innerHTML = shown.length ? shown.map(r => {
    const deco = dayDecorations(r.day);
    const picked = picks.get(pickKey(r.threshold_id, r.start_utc)) || [];
    const nextDay = r.end_date !== r.date ? ` <span class="veille" title="Le lendemain">J+1</span>` : "";
    const tight = r.tight ? ` <span class="tag tight-tag" title="L'eau ne dépasse la hauteur que de ${Math.round(Math.abs(r.extreme_m - t.height_m) * 100)} cm : quelques centimètres d'erreur décalent beaucoup les heures">limite</span>` : "";
    const open = r.end_open ? ` <span class="muted" title="Fin au-delà des marées calculées">…</span>` : "";
    const day = r.daylight
      ? `${r.daylight.start}–${r.daylight.end}${r.daylight.minutes < r.minutes ? ` <span class="muted">(${fmtDuration(r.daylight.minutes)})</span>` : ""}`
      : (data.criteria.daylight === "none" ? `<span class="muted">—</span>` : "");
    return `
      <tr class="${[deco.classes, picked.length ? "is-picked" : "", r.unavailable ? "is-unavailable" : ""].join(" ").trim()}"
          data-key="${esc(pickKey(r.threshold_id, r.start_utc))}">
        <th scope="row" class="c-date"${deco.title ? ` title="${deco.title}"` : ""}>${formatDay(r.date)}${deco.notes}</th>
        <td class="c-rdv" data-label="RDV">${r.rdv_time}</td>
        <td class="c-win" data-label="Plage">${r.start} → ${r.end}${nextDay}${open}${tight}</td>
        <td class="num" data-label="Durée">${fmtDuration(r.minutes)}</td>
        <td class="num" data-label="${t.direction === "above" ? "Hauteur max" : "Hauteur min"}">${fmtM(r.extreme_m)}</td>
        <td data-label="De jour">${day}</td>
        <td class="c-pick" data-label="Choix">${pickCell(r, picked)}</td>
      </tr>`;
  }).join("")
    : data.results.length
      ? `<tr><td colspan="7" class="empty">Aucune plage ne correspond aux filtres. Élargissez-les ou effacez-les.</td></tr>`
      : `<tr><td colspan="7" class="empty">Aucune plage ne correspond sur cette période. Allongez-la, réduisez la durée minimale ou levez la contrainte de lumière.</td></tr>`;
}

async function search() {
  if (!thresholdSelect.value) {
    statusEl.textContent = "Choisissez une hauteur d'eau.";
    resultsEl.hidden = false;
    return;
  }
  const qs = new URLSearchParams({
    threshold_id: thresholdSelect.value, start: $("start").value, end: $("end").value,
    daylight: $("daylight").value, min_minutes: $("min_minutes").value,
  });
  statusEl.textContent = "Calcul…";
  resultsEl.hidden = false;
  try {
    last = await Session.api(`/api/water-windows?${qs}`);
  } catch (e) {
    statusEl.textContent = e.message;
    rowsEl.innerHTML = "";
    return;
  }
  render();
}

$("search").addEventListener("click", search);

// ---- Choix d'une plage ----

async function createPick(r, typeId, note = null) {
  const s = await Session.api("/api/selections/height", {
    method: "POST", body: { threshold_id: r.threshold_id, start_utc: r.start_utc, type_id: typeId, note },
  });
  addPick(s);
  render();
}

rowsEl.addEventListener("change", async e => {
  const sel = e.target.closest("select[data-pick]");
  if (!sel || !sel.value) return;
  const r = last.results.find(x => pickKey(x.threshold_id, x.start_utc) === sel.closest("tr").dataset.key);
  const typeId = Number(sel.value);
  const existing = picks.get(pickKey(r.threshold_id, r.start_utc)) || [];
  if (existing.length) {
    // plage déjà choisie : un intitulé aide à distinguer les créneaux (deux bateaux…)
    sel.value = "";
    Session.openForm({
      title: "Autre créneau sur cette plage",
      intro: `<p class="dialog-hint">${esc(formatDay(r.date))}, de ${esc(r.start)} à ${esc(r.end)}. Déjà choisi : `
        + `${existing.map(p => esc(p.type.label + (p.note ? ` (${p.note})` : ""))).join(", ")}.</p>`,
      fields: [{ name: "note", label: "Intitulé", required: false, value: "", hint: "Facultatif (ex. Bateau 2)." }],
      submitLabel: "Ajouter",
      onSubmit: async values => createPick(r, typeId, (values.note || "").trim().slice(0, 80) || null),
    });
    return;
  }
  sel.disabled = true;
  try {
    await createPick(r, typeId);
  } catch (err) {
    statusEl.textContent = `Choix impossible : ${err.message}`;
    sel.disabled = false;
    sel.value = "";
  }
});

rowsEl.addEventListener("click", async e => {
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
  for (const [k, list] of picks) {
    const left = list.filter(s => String(s.id) !== btn.dataset.unpick);
    if (left.length) picks.set(k, left); else picks.delete(k);
  }
  render();
});

// ---- Préférences (formulaire + filtres du tableau), propres à cette page ----

const prefsBar = $("prefs-bar");
const prefsSaveBtn = $("prefs-save");
const prefsRestoreBtn = $("prefs-restore");
const prefsStatus = $("prefs-status");
let savedPrefs = null;

const fmtStamp = new Intl.DateTimeFormat("fr-FR", { dateStyle: "short", timeStyle: "short" });
const daysBetween = (a, b) => Math.round((new Date(`${b}T12:00:00`) - new Date(`${a}T12:00:00`)) / 86400000);
const hasOption = (sel, v) => v != null && !!sel.querySelector(`option[value="${v}"]`);

function collectPrefs() {
  const span = daysBetween($("start").value, $("end").value);
  const filters = {};
  for (const el of filtersRow.querySelectorAll("[data-f]")) filters[el.dataset.f] = el.value;
  return {
    form: {
      port_id: portSelect.value ? Number(portSelect.value) : null,
      threshold_id: thresholdSelect.value ? Number(thresholdSelect.value) : null,
      // la période est gardée en durée : des dates enregistrées seraient vite périmées
      span_days: Number.isFinite(span) && span >= 0 ? Math.min(span, 366) : null,
      daylight: $("daylight").value,
      min_minutes: Number($("min_minutes").value),
    },
    filters,
  };
}

function applyPrefs(prefs) {
  const f = prefs.form || {};
  if (hasOption(portSelect, f.port_id)) {
    portSelect.value = String(f.port_id);
    renderThresholds();
  }
  if (hasOption(thresholdSelect, f.threshold_id)) thresholdSelect.value = String(f.threshold_id);
  if (f.span_days != null) {
    const now = new Date();
    $("start").value = toISO(now);
    $("end").value = toISO(new Date(now.getFullYear(), now.getMonth(), now.getDate() + f.span_days));
  }
  if (hasOption($("daylight"), f.daylight)) $("daylight").value = f.daylight;
  if (hasOption($("min_minutes"), f.min_minutes)) $("min_minutes").value = String(f.min_minutes);
  for (const el of filtersRow.querySelectorAll("[data-f]")) el.value = prefs.filters?.[el.dataset.f] ?? "";
  readFilters();
}

function showSavedStamp() {
  prefsStatus.textContent = savedPrefs?.updated_at
    ? `Enregistrées le ${fmtStamp.format(new Date(savedPrefs.updated_at))}`
    : "Aucune préférence enregistrée.";
}

// Préférences du compte : appliquées puis recherche lancée (comme la recherche par étale)
async function loadPrefs() {
  savedPrefs = null;
  prefsRestoreBtn.disabled = true;
  try {
    const prefs = await Session.api("/api/me/water-preferences");
    if (prefs.updated_at) {
      savedPrefs = prefs;
      prefsRestoreBtn.disabled = false;
      applyPrefs(prefs);
      search();
    }
    showSavedStamp();
  } catch (e) {
    prefsStatus.textContent = `Préférences non chargées : ${e.message}`;
  }
}

prefsSaveBtn.addEventListener("click", async () => {
  prefsSaveBtn.disabled = true;
  try {
    savedPrefs = await Session.api("/api/me/water-preferences", { method: "PUT", body: collectPrefs() });
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

// ---- Accès ----

const today = new Date();
$("start").value = toISO(today);
$("end").value = toISO(new Date(today.getFullYear(), today.getMonth(), today.getDate() + 13));

Session.mountAccount($("account"), [Session.LINKS.search, Session.LINKS.picks, Session.LINKS.newsletters, Session.LINKS.admin, Session.LINKS.help]);
// Note de source du pied de page : selon les réglages de la structure du compte
function tideSourceNote(user) {
  const ts = Session.tideSources(user);
  const mine = ts.api_maree && ts.calibration ? "" : " ; réglage de votre structure";
  const api = '<a href="https://api-maree.fr" rel="noopener">api-maree.fr</a>';
  if (ts.api_maree) {
    return `${api} sur le mois à venir, calcul FES ${ts.calibration ? "recalé sur cette même source" : "brut, sans correction,"} au-delà${mine}`;
  }
  return ts.calibration
    ? `calcul FES recalé sur ${api}, y compris pour le mois à venir${mine}`
    : `calcul FES brut, sans api-maree.fr ni correction${mine}`;
}

Session.onChange(async user => {
  $("tide-source-note").innerHTML = tideSourceNote(user);
  const allowed = Session.searchModes(user) !== "tides";
  controlsEl.hidden = resultsEl.hidden = !allowed;
  gateEl.hidden = allowed;
  prefsBar.hidden = !user;
  last = null;
  if (!user) {
    gateEl.innerHTML = `Connectez-vous pour chercher des plages par hauteur d'eau. <button type="button" class="btn-primary" id="gate-login">Se connecter</button>`;
    $("gate-login").addEventListener("click", () => Session.openLogin());
    return;
  }
  if (!allowed) {
    gateEl.innerHTML = `La recherche par hauteur d'eau n'est pas proposée à votre structure. <a href="index.html">Recherche par étale</a>`;
    return;
  }
  resultsEl.hidden = true;
  try {
    await Promise.all([loadPorts(), loadPicks()]);
  } catch (e) {
    statusEl.textContent = e.message;
    resultsEl.hidden = false;
    return;
  }
  if (!ports.length) {
    gateEl.hidden = false;
    controlsEl.hidden = true;
    gateEl.textContent = user.is_admin
      ? "Aucun port n'a de hauteur d'eau : ajoutez-en dans Administration → Ports → « Hauteurs d'eau »."
      : "Aucune hauteur d'eau n'est encore renseignée : demandez-la aux administrateurs de l'application.";
    return;
  }
  await loadPrefs();
});
Session.init();
