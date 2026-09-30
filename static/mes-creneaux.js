// « Créneaux choisis » : créneaux choisis par la structure de l'utilisateur.
// Tout membre : s'inscrire / se désinscrire sur un créneau à venir, dans les
// délais fixés par la structure (le serveur fait foi).
// Administration de la structure : en plus, changer le type, retirer un
// créneau (qui redevient disponible dans la recherche), retirer l'inscription
// d'un membre.
//
// Deux affichages, mémorisés dans le navigateur :
//  - Calendrier : grille du mois + fiches des créneaux du jour sélectionné ;
//  - Liste : tableau sur grand écran, fiches groupées par jour sur mobile.
// Chaque créneau affiché porte data-id : un seul affichage est rendu à la fois.

const esc = Session.esc;
const $ = id => document.getElementById(id);

const gateEl = $("gate");
const picksEl = $("picks");
const statusEl = $("status");
const typeFilter = $("type-filter");
const showPast = $("show-past");
const onlyMine = $("only-mine");
const summaryEl = $("summary");
const emptyEl = $("picks-empty");
const exportBtn = $("export-xlsx");

const calEl = $("cal-view");
const calTitle = $("cal-title");
const calCount = $("cal-count");
const calGrid = $("cal-grid");
const dayPanel = $("day-panel");

const listEl = $("list-view");
const tableWrap = $("table-wrap");
const bodyEl = $("picks-body");
const listCardsEl = $("list-cards");

const narrow = window.matchMedia("(max-width: 700px)");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

let types = [];       // types actifs, ordre de l'administration
let picks = [];       // triés par date puis heure d'étale

const canPick = () => !!Session.user?.can.pick;

// ---- Dates (chaînes ISO AAAA-MM-JJ, midi local pour éviter les pièges de l'heure d'été) ----

const asDate = iso => new Date(iso + "T12:00:00");
const toISO = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const addDays = (iso, n) => { const d = asDate(iso); d.setDate(d.getDate() + n); return toISO(d); };
const nextDay = iso => addDays(iso, 1);
const monthOf = iso => iso.slice(0, 7) + "-01";
const addMonths = (monthIso, n) => { const d = asDate(monthIso); d.setMonth(d.getMonth() + n); return toISO(d); };
const cap = s => s.charAt(0).toUpperCase() + s.slice(1);

const fmtDay = new Intl.DateTimeFormat("fr-FR", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
const fmtLong = new Intl.DateTimeFormat("fr-FR", { weekday: "long", day: "numeric", month: "long" });
const fmtLongYear = new Intl.DateTimeFormat("fr-FR", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
const fmtMonth = new Intl.DateTimeFormat("fr-FR", { month: "long", year: "numeric" });
const formatDay = iso => fmtDay.format(asDate(iso));
const formatLong = iso => fmtLong.format(asDate(iso));

const fmtHeight = v => (v != null ? v.toFixed(2).replace(".", ",") : "–");
const fmtCoef = c => (c != null ? Math.round(c) : "–");
const coefClass = c => (c == null ? "" : c >= 90 ? "ve" : c <= 50 ? "me" : "");
const plural = (n, one, many) => `${n} ${n > 1 ? many : one}`;

// Aujourd'hui en heure de Paris (les dates stockées sont locales au port)
const todayISO = () => new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Paris" }).format(new Date());

// ---- Affichage choisi (préférence locale au navigateur) ----

const VIEW_KEY = "maree.creneaux.affichage";
let view = (() => {
  try { return localStorage.getItem(VIEW_KEY) === "list" ? "list" : "cal"; } catch { return "cal"; }
})();
let month = null;        // premier jour du mois affiché dans le calendrier
let selectedDay = null;  // jour dont les créneaux sont détaillés sous le calendrier

// ---- Morceaux communs aux trois rendus ----

function typeCell(p) {
  if (!canPick()) return `<span class="type-pill" style="--type-color:${esc(p.type.color)}">${esc(p.type.label)}</span>`;
  // un type désactivé reste affiché sur les choix existants, sans pouvoir être rechoisi
  const options = types.map(t =>
    `<option value="${t.id}"${t.id === p.type.id ? " selected" : ""}>${esc(t.label)}</option>`);
  if (!types.some(t => t.id === p.type.id)) {
    options.unshift(`<option value="${p.type.id}" selected disabled>${esc(p.type.label)} (désactivé)</option>`);
  }
  return `<select data-act="type" style="--type-color:${esc(p.type.color)}" aria-label="Type du créneau">${options.join("")}</select>`;
}

function registrationsCell(p) {
  const n = p.registrations.length;
  const open = popFor === p.id;
  const count = `<button type="button" class="reg-count${p.registered ? " reg-me" : ""}" data-act="show-regs"
    aria-haspopup="true" aria-expanded="${open}" aria-controls="regs-pop"
    title="Voir les inscrits">${n}<span class="visually-hidden"> inscrit(s)</span></button>`;
  const button = p.past
    ? ""
    : !p.registered
      ? p.can_register
        ? `<button type="button" class="btn-primary btn-small" data-act="register"
             title="Inscription possible jusqu'au ${formatDay(p.register_until)} inclus">S'inscrire</button>`
        : `<span class="reg-locked" title="Inscriptions closes depuis le ${formatDay(nextDay(p.register_until))} : contactez un administrateur de la structure">🔒 Inscriptions closes</span>`
      : p.can_unregister
        ? `<button type="button" class="btn-quiet btn-small" data-act="unregister"
             title="Possible jusqu'au ${formatDay(p.unregister_until)} inclus">Se désinscrire</button>`
        : `<span class="reg-locked" title="Désinscription close depuis le ${formatDay(nextDay(p.unregister_until))} : contactez un administrateur de la structure">🔒 Désinscription close</span>`;
  return `<div class="regs">${count}${button}</div>`;
}

const veilleMark = p => (p.rdv.date !== p.date ? `<span class="veille" title="RDV la veille">J-1</span> ` : "");
const pickedBy = p => (p.picked_by ? esc(p.picked_by) : `<span class="muted">compte supprimé</span>`);
const removeButton = () => (canPick() ? `<button type="button" class="btn-danger btn-small" data-act="remove">Retirer</button>` : "");

// Fiche d'un créneau (calendrier et liste sur mobile) : l'heure de RDV d'abord,
// c'est elle qui compte pour venir plonger.
function slotCard(p) {
  return `
    <li class="slot-card${p.past ? " past" : ""}${p.registered ? " mine" : ""}" data-id="${p.id}" style="--type-color:${esc(p.type.color)}">
      <div class="slot-head">
        <p class="slot-rdv"><abbr title="Heure de rendez-vous (étale − 2 h)">RDV</abbr> ${veilleMark(p)}<strong>${p.rdv.time}</strong></p>
        <p class="slot-port">${esc(p.port)}</p>
      </div>
      <p class="slot-tide">
        <span><span class="kind ${p.kind}" title="Étale de ${p.kind === "PM" ? "pleine" : "basse"} mer">${p.kind}</span>${p.time}</span>
        <span title="Hauteur d'eau à l'étale">${fmtHeight(p.height_m)} m</span>
        <span title="Coefficient de marée (indicatif)">coef <span class="coef ${coefClass(p.coefficient)}">${fmtCoef(p.coefficient)}</span></span>
      </p>
      <div class="slot-row c-type">${typeCell(p)}</div>
      <div class="slot-row">${registrationsCell(p)}</div>
      <div class="slot-foot">
        <span class="slot-by">Choisi par ${pickedBy(p)}</span>
        ${removeButton()}
      </div>
    </li>`;
}

function groupByDay(list) {
  const byDay = new Map();
  for (const p of list) {
    if (!byDay.has(p.date)) byDay.set(p.date, []);
    byDay.get(p.date).push(p);
  }
  return byDay;
}

// ---- Info-bulle des inscrits (au clic sur le nombre) ----

const pop = document.createElement("div");
pop.id = "regs-pop";
pop.className = "regs-pop";
pop.setAttribute("role", "dialog");
pop.setAttribute("aria-label", "Inscrits");
pop.hidden = true;
document.body.append(pop);
let popFor = null;  // id du créneau affiché

function popAnchor() {
  return popFor == null ? null : picksEl.querySelector(`[data-id="${popFor}"] [data-act=show-regs]`);
}

function renderPop() {
  const p = picks.find(x => x.id === popFor);
  const anchor = popAnchor();
  if (!p || !anchor) return closePop();
  const me = Session.user.id;
  const items = p.registrations.map(r => {
    const mine = r.user_id === me;
    const remove = canPick() && !mine
      ? `<button type="button" class="chip-remove" data-act="unregister-other" data-user="${r.user_id}"
           title="Retirer l'inscription" aria-label="Retirer l'inscription de ${esc(r.display_name)}">×</button>`
      : "";
    return `<li${mine ? ` class="me"` : ""} title="${esc(r.username)}">${esc(r.display_name)}${mine ? " (vous)" : ""}${remove}</li>`;
  }).join("");
  pop.innerHTML = `<p class="regs-pop-title">${p.registrations.length} inscrit(s)</p>` +
    (items ? `<ul>${items}</ul>` : `<p class="muted">Personne pour l'instant.</p>`);
  pop.hidden = false;
  // sous le bouton, sans déborder de la fenêtre
  const a = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  const left = Math.max(8, Math.min(a.left, window.innerWidth - w - 8));
  const top = a.bottom + 6 + h > window.innerHeight ? a.top - h - 6 : a.bottom + 6;
  pop.style.left = `${left}px`;
  pop.style.top = `${Math.max(8, top)}px`;
}

function openPop(id) {
  popFor = id;
  render();      // met à jour aria-expanded
  renderPop();
}

function closePop() {
  if (popFor == null) return;
  const anchor = popAnchor();
  popFor = null;
  pop.hidden = true;
  anchor?.setAttribute("aria-expanded", "false");
}

document.addEventListener("click", e => {
  if (popFor != null && !pop.contains(e.target) && !e.target.closest("[data-act=show-regs]")) closePop();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && popFor != null) {
    const anchor = popAnchor();
    closePop();
    anchor?.focus();
  }
});
window.addEventListener("resize", () => closePop());
document.addEventListener("scroll", () => closePop(), true);  // capture : aussi le défilement du tableau

// ---- Filtres et résumé ----

function visiblePicks() {
  const today = todayISO();
  return picks.filter(p =>
    (showPast.checked || p.date >= today) &&
    (!onlyMine.checked || p.registered) &&
    (!typeFilter.value || String(p.type.id) === typeFilter.value));
}

function renderSummary(list) {
  const counts = new Map();
  for (const p of list) {
    const c = counts.get(p.type.id) || { type: p.type, n: 0 };
    c.n++;
    counts.set(p.type.id, c);
  }
  summaryEl.innerHTML = [...counts.values()].map(c =>
    `<li><span class="type-pill" style="--type-color:${esc(c.type.color)}">${esc(c.type.label)}<b>${c.n}</b></span></li>`).join("");
}

// ---- Calendrier ----

// Jour à détailler en arrivant sur un mois : le prochain créneau du mois,
// sinon le premier, sinon aujourd'hui s'il en fait partie, sinon le 1er.
function defaultDay(m, list) {
  const today = todayISO();
  const inMonth = list.filter(p => monthOf(p.date) === m);
  const next = inMonth.find(p => p.date >= today) || inMonth[0];
  if (next) return next.date;
  return monthOf(today) === m ? today : m;
}

function renderCalendar(list) {
  const today = todayISO();
  const byDay = groupByDay(list);
  const first = asDate(month);
  const lead = (first.getDay() + 6) % 7;  // lundi en premier
  const daysInMonth = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  const start = addDays(month, -lead);
  const nInMonth = list.filter(p => monthOf(p.date) === month).length;

  calTitle.textContent = cap(fmtMonth.format(first));
  calCount.textContent = nInMonth ? plural(nInMonth, "créneau", "créneaux") : "aucun créneau";

  const cells = [];
  for (let i = 0; i < Math.ceil((lead + daysInMonth) / 7) * 7; i++) {
    const day = addDays(start, i);
    const items = byDay.get(day) || [];
    const mine = items.filter(p => p.registered).length;
    const cls = ["cal-cell",
      monthOf(day) !== month && "out",
      i % 7 >= 5 && "weekend",
      day < today && "is-past",
      items.length && "has"].filter(Boolean).join(" ");
    const label = cap(fmtLongYear.format(asDate(day))) +
      (items.length ? `, ${plural(items.length, "créneau", "créneaux")}` : "") +
      (mine ? `, inscrit sur ${mine}` : "");
    const chips = items.slice(0, 3).map(p =>
      `<span class="cal-chip${p.registered ? " mine" : ""}" style="--type-color:${esc(p.type.color)}"><b>${p.rdv.time}</b> ${esc(p.port)}</span>`).join("") +
      (items.length > 3 ? `<span class="cal-more">+${items.length - 3}</span>` : "");
    cells.push(`<button type="button" class="${cls}" data-day="${day}"
      aria-pressed="${day === selectedDay}" tabindex="${day === selectedDay ? 0 : -1}"
      ${day === today ? `aria-current="date"` : ""} aria-label="${esc(label)}">
      <span class="cal-num" aria-hidden="true">${Number(day.slice(8))}</span>
      <span class="cal-slots" aria-hidden="true">${chips}</span>
    </button>`);
  }
  calGrid.innerHTML = cells.join("");

  const items = byDay.get(selectedDay) || [];
  let html = `<h3 class="day-title">${cap(formatLong(selectedDay))}</h3>`;
  if (items.length) {
    html += `<ul class="slot-cards">${items.map(slotCard).join("")}</ul>`;
  } else {
    const next = list.find(p => p.date > selectedDay);
    html += `<p class="day-empty">Aucun créneau ce jour.${next
      ? ` <button type="button" class="btn-quiet btn-small" data-goto="${next.date}">Créneau suivant : ${formatLong(next.date)}</button>`
      : ""}</p>`;
  }
  dayPanel.innerHTML = html;
}

function selectDay(day, { focus = false, reveal = false } = {}) {
  closePop();
  selectedDay = day;
  month = monthOf(day);
  render();
  if (focus) calGrid.querySelector(`[data-day="${day}"]`)?.focus();
  // sur mobile, les fiches du jour sont sous la grille : on les amène à l'écran
  if (reveal && narrow.matches) {
    dayPanel.scrollIntoView({ block: "nearest", behavior: reducedMotion.matches ? "auto" : "smooth" });
  }
}

function goMonth(m) {
  month = m;
  selectedDay = defaultDay(m, visiblePicks());
  closePop();
  render();
}

// ---- Liste ----

function renderTable(list) {
  const today = todayISO();
  if (!list.length) {
    bodyEl.innerHTML = `<tr><td colspan="10" class="empty">Aucun créneau avec ces filtres.</td></tr>`;
    return;
  }
  const rows = [];
  for (const [day, items] of groupByDay(list)) {
    items.forEach((p, i) => {
      rows.push(`
        <tr data-id="${p.id}" class="${[i === 0 ? "day-start" : "", day < today ? "past" : ""].join(" ").trim()}">
          ${i === 0 ? `<th scope="row" rowspan="${items.length}" class="c-date">${formatDay(day)}</th>` : ""}
          <td class="c-rdv">${veilleMark(p)}${p.rdv.time}</td>
          <td class="c-port">${esc(p.port)}</td>
          <td class="c-tide"><span class="kind ${p.kind}">${p.kind}</span>${p.time}</td>
          <td class="num">${fmtHeight(p.height_m)}</td>
          <td class="num"><span class="coef ${coefClass(p.coefficient)}">${fmtCoef(p.coefficient)}</span></td>
          <td class="c-type">${typeCell(p)}</td>
          <td class="c-regs">${registrationsCell(p)}</td>
          <td class="c-by">${pickedBy(p)}</td>
          <td class="c-actions">${removeButton()}</td>
        </tr>`);
    });
  }
  bodyEl.innerHTML = rows.join("");
}

function renderListCards(list) {
  listCardsEl.innerHTML = list.length
    ? [...groupByDay(list)].map(([day, items]) => `
        <section class="day-group">
          <h3 class="day-title">${cap(formatLong(day))}</h3>
          <ul class="slot-cards">${items.map(slotCard).join("")}</ul>
        </section>`).join("")
    : `<p class="day-empty">Aucun créneau avec ces filtres.</p>`;
}

// ---- Rendu d'ensemble ----

function render() {
  const list = visiblePicks();
  renderSummary(list);
  statusEl.textContent = picks.length
    ? `${list.length} créneau(x) affiché(s) sur ${picks.length} choisi(s).`
    : "";
  for (const b of picksEl.querySelectorAll("[data-view]")) b.setAttribute("aria-pressed", String(b.dataset.view === view));

  const none = !picks.length;
  exportBtn.disabled = !list.length;
  emptyEl.hidden = !none;
  if (none) {
    emptyEl.innerHTML = canPick()
      ? `Aucun créneau choisi pour l'instant. <a href="index.html">Chercher des créneaux</a>, puis choisissez un type dans la colonne « Choix ».`
      : "Aucun créneau choisi pour l'instant par votre structure.";
  }

  const showCal = !none && view === "cal";
  const showList = !none && view === "list";
  const cards = showList && narrow.matches;
  calEl.hidden = !showCal;
  listEl.hidden = !showList;
  tableWrap.hidden = cards;
  listCardsEl.hidden = !cards;

  // un seul affichage dans le DOM : data-id reste unique
  calGrid.innerHTML = dayPanel.innerHTML = bodyEl.innerHTML = listCardsEl.innerHTML = "";
  if (showCal) {
    if (!month) { month = monthOf(todayISO()); selectedDay = defaultDay(month, list); }
    renderCalendar(list);
  } else if (cards) {
    renderListCards(list);
  } else if (showList) {
    renderTable(list);
  }
}

// passage mobile ↔ grand écran : la liste change de forme
narrow.addEventListener("change", () => { closePop(); render(); });

function renderTypeFilter() {
  // tous les types présents dans les choix (même désactivés) + les types actifs
  const seen = new Map(types.map(t => [t.id, t]));
  for (const p of picks) if (!seen.has(p.type.id)) seen.set(p.type.id, p.type);
  const current = typeFilter.value;
  typeFilter.innerHTML = `<option value="">Tous les types</option>` +
    [...seen.values()].map(t => `<option value="${t.id}">${esc(t.label)}</option>`).join("");
  if (seen.has(Number(current))) typeFilter.value = current;
}

const byWhen = (a, b) => (a.date + a.time).localeCompare(b.date + b.time);

async function load() {
  closePop();
  statusEl.textContent = "Chargement…";
  try {
    [types, picks] = await Promise.all([
      canPick() ? Session.api("/api/slot-types") : [],
      Session.api("/api/selections"),
    ]);
  } catch (e) {
    statusEl.textContent = e.message;
    return;
  }
  picks.sort(byWhen);
  loadedDay = todayISO();
  renderTypeFilter();
  render();
}

// Les droits d'inscription dépendent de la date : un onglet resté ouvert (ou
// rouvert le lendemain) se remet à jour quand on y revient, pour ne pas
// proposer « S'inscrire » sur un créneau dont les inscriptions sont closes.
let loadedDay = null;
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && loadedDay && loadedDay !== todayISO() && Session.user?.can.view_selections) load();
});

// ---- Actions sur un créneau (tableau ou fiche : l'élément porteur a data-id) ----

const holderId = el => Number(el.closest("[data-id]").dataset.id);

picksEl.addEventListener("change", async e => {
  const sel = e.target.closest("select[data-act=type]");
  if (!sel) return;
  const id = holderId(sel);
  sel.disabled = true;
  try {
    const updated = await Session.api(`/api/selections/${id}`, { method: "PATCH", body: { type_id: Number(sel.value) } });
    picks = picks.map(p => (p.id === id ? updated : p));
    renderTypeFilter();
    render();
  } catch (err) {
    statusEl.textContent = `Changement impossible : ${err.message}`;
    sel.disabled = false;
  }
});

// Inscription / désinscription (soi-même), retrait d'un inscrit (administration,
// depuis l'info-bulle), ouverture de l'info-bulle
async function onRegistrationClick(e) {
  const toggle = e.target.closest("button[data-act=show-regs]");
  if (toggle) {
    const id = holderId(toggle);
    if (popFor === id) closePop(); else openPop(id);
    return;
  }
  const btn = e.target.closest("button[data-act=register], button[data-act=unregister], button[data-act=unregister-other]");
  if (!btn) return;
  const id = btn.closest("[data-id]") ? holderId(btn) : popFor;
  const act = btn.dataset.act;
  const p = picks.find(x => x.id === id);
  if (act === "unregister-other") {
    const r = p?.registrations.find(x => String(x.user_id) === btn.dataset.user);
    if (r && !confirm(`Retirer l'inscription de ${r.display_name} ?`)) return;
  }
  const url = act === "unregister-other"
    ? `/api/selections/${id}/registrations/${btn.dataset.user}`
    : `/api/selections/${id}/registration`;
  btn.disabled = true;
  try {
    const updated = await Session.api(url, { method: act === "register" ? "POST" : "DELETE" });
    picks = picks.map(x => (x.id === id ? updated : x));
    render();
    if (popFor === id) renderPop();
  } catch (err) {
    statusEl.textContent = `${act === "register" ? "Inscription" : "Désinscription"} impossible : ${err.message}`;
    // 404 : créneau retiré entre-temps ; 409 : passé depuis le chargement
    if (err.status === 404 || err.status === 409) load();
    else btn.disabled = false;
  }
}
picksEl.addEventListener("click", onRegistrationClick);
pop.addEventListener("click", onRegistrationClick);

picksEl.addEventListener("click", async e => {
  const btn = e.target.closest("button[data-act=remove]");
  if (!btn) return;
  const id = holderId(btn);
  const n = picks.find(p => p.id === id)?.registrations.length || 0;
  if (n && !confirm(`${n} membre(s) inscrit(s) sur ce créneau : le retirer annule leur inscription. Continuer ?`)) return;
  btn.disabled = true;
  try {
    await Session.api(`/api/selections/${id}`, { method: "DELETE" });
  } catch (err) {
    if (err.status !== 404) {
      statusEl.textContent = `Retrait impossible : ${err.message}`;
      btn.disabled = false;
      return;
    }
  }
  picks = picks.filter(p => p.id !== id);
  renderTypeFilter();
  render();
});

// ---- Export Excel des créneaux affichés (filtres compris, quel que soit l'affichage) ----

const fmtWeekday = new Intl.DateTimeFormat("fr-FR", { weekday: "long" });

const EXPORT_COLUMNS = [
  { header: "Date", type: "date", width: 11, value: p => p.date },
  { header: "Jour", width: 10, value: p => fmtWeekday.format(asDate(p.date)) },
  { header: "Date RDV", type: "date", width: 11, value: p => p.rdv.date },
  { header: "Heure RDV", type: "time", width: 10, value: p => p.rdv.time },
  { header: "Port", width: 18, value: p => p.port },
  { header: "Étale", width: 7, value: p => p.kind },
  { header: "Heure étale", type: "time", width: 11, value: p => p.time },
  { header: "Hauteur (m)", type: "decimal", width: 11, value: p => p.height_m },
  { header: "Coefficient", type: "int", width: 11, value: p => (p.coefficient != null ? Math.round(p.coefficient) : null) },
  { header: "Type", width: 16, value: p => p.type.label },
  { header: "Nb inscrits", type: "int", width: 11, value: p => p.registrations.length },
  { header: "Inscrits", width: 40, value: p => p.registrations.map(r => r.display_name).join(", ") },
  { header: "Inscrit (moi)", width: 12, value: p => (p.registered ? "oui" : "") },
  { header: "Choisi par", width: 18, value: p => p.picked_by || "compte supprimé" },
];

exportBtn.addEventListener("click", () => {
  const list = visiblePicks();
  if (!list.length) return;
  const structure = Session.user?.structure?.name || "structure";
  XlsxExport.download(
    `creneaux-choisis-${XlsxExport.slug(structure)}-${list[0].date}-au-${list[list.length - 1].date}.xlsx`,
    "Créneaux choisis", EXPORT_COLUMNS, list);
});

// ---- Navigation : affichage, mois, jour ----

picksEl.addEventListener("click", e => {
  const viewBtn = e.target.closest("button[data-view]");
  if (viewBtn) {
    view = viewBtn.dataset.view;
    try { localStorage.setItem(VIEW_KEY, view); } catch { /* navigation privée : tant pis */ }
    closePop();
    render();
    return;
  }
  const nav = e.target.closest("button[data-cal]");
  if (nav) {
    const act = nav.dataset.cal;
    if (act === "today") selectDay(todayISO());
    else goMonth(addMonths(month, act === "prev" ? -1 : 1));
    return;
  }
  const cell = e.target.closest(".cal-cell");
  if (cell) return selectDay(cell.dataset.day, { focus: true, reveal: true });
  const go = e.target.closest("button[data-goto]");
  if (go) selectDay(go.dataset.goto);
});

// Clavier dans la grille : flèches (jour / semaine), Début / Fin (semaine),
// Page préc. / suiv. (mois)
calGrid.addEventListener("keydown", e => {
  if (!e.target.closest(".cal-cell")) return;
  const dow = (asDate(selectedDay).getDay() + 6) % 7;
  const moves = {
    ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7,
    Home: -dow, End: 6 - dow,
  };
  let day;
  if (e.key in moves) day = addDays(selectedDay, moves[e.key]);
  else if (e.key === "PageUp" || e.key === "PageDown") {
    const d = asDate(selectedDay);
    const target = addMonths(monthOf(selectedDay), e.key === "PageUp" ? -1 : 1);
    const last = new Date(asDate(target).getFullYear(), asDate(target).getMonth() + 1, 0).getDate();
    day = target.slice(0, 8) + String(Math.min(d.getDate(), last)).padStart(2, "0");
  } else return;
  e.preventDefault();
  selectDay(day, { focus: true });
});

typeFilter.addEventListener("change", () => { closePop(); render(); });
showPast.addEventListener("change", () => { closePop(); render(); });
onlyMine.addEventListener("change", () => { closePop(); render(); });

Session.mountAccount($("account"), [Session.LINKS.search, Session.LINKS.admin]);
Session.onChange(user => {
  const member = !!user?.can.view_selections;
  picksEl.hidden = !member;
  gateEl.hidden = member;
  $("structure-name").textContent = member ? `· ${user.structure.name}` : "";
  document.body.classList.toggle("read-only", member && !user.can.pick);
  picks = [];
  month = selectedDay = null;
  if (!user) {
    gateEl.innerHTML = `Connectez-vous pour voir les créneaux choisis par votre structure. <button type="button" class="btn-primary" id="gate-login">Se connecter</button>`;
    $("gate-login").addEventListener("click", () => Session.openLogin());
    return;
  }
  if (!member) {
    gateEl.textContent = `Le compte « ${user.display_name} » n'est rattaché à aucune structure : il n'a pas de créneaux choisis.`;
    return;
  }
  $("picks-hint").textContent = user.can.pick
    ? "Liste commune à la structure. Les heures sont celles calculées au moment du choix. Retirer un créneau le rend de nouveau disponible dans la recherche."
    : "Liste commune à la structure : inscrivez-vous sur les créneaux qui vous intéressent. Seuls ses administrateurs choisissent les créneaux. Les heures sont celles calculées au moment du choix.";
  load();
});
Session.init();
