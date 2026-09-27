// « Créneaux choisis » : créneaux choisis par la structure de l'utilisateur.
// Tout membre : s'inscrire / se désinscrire sur un créneau à venir.
// Administration de la structure : en plus, changer le type, retirer un
// créneau (qui redevient disponible dans la recherche), retirer l'inscription
// d'un membre.

const esc = Session.esc;
const $ = id => document.getElementById(id);

const gateEl = $("gate");
const picksEl = $("picks");
const bodyEl = $("picks-body");
const statusEl = $("status");
const typeFilter = $("type-filter");
const showPast = $("show-past");
const onlyMine = $("only-mine");
const summaryEl = $("summary");

let types = [];       // types actifs, ordre de l'administration
let picks = [];

const canPick = () => !!Session.user?.can.pick;

const fmtDay = new Intl.DateTimeFormat("fr-FR", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
const formatDay = iso => fmtDay.format(new Date(iso + "T12:00:00"));
const fmtHeight = v => (v != null ? v.toFixed(2).replace(".", ",") : "–");
const fmtCoef = c => (c != null ? Math.round(c) : "–");
const coefClass = c => (c == null ? "" : c >= 90 ? "ve" : c <= 50 ? "me" : "");

// Aujourd'hui en heure de Paris (les dates stockées sont locales au port)
const todayISO = () => new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Paris" }).format(new Date());

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
    : p.registered
      ? `<button type="button" class="btn-quiet btn-small" data-act="unregister">Se désinscrire</button>`
      : `<button type="button" class="btn-primary btn-small" data-act="register">S'inscrire</button>`;
  return `<div class="regs">${count}${button}</div>`;
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
  return popFor == null ? null : bodyEl.querySelector(`tr[data-id="${popFor}"] [data-act=show-regs]`);
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

function render() {
  const list = visiblePicks();
  const today = todayISO();
  renderSummary(list);
  statusEl.textContent = picks.length
    ? `${list.length} créneau(x) affiché(s) sur ${picks.length} choisi(s).`
    : "";

  if (!list.length) {
    bodyEl.innerHTML = `<tr><td colspan="10" class="empty">${picks.length
      ? "Aucun créneau avec ces filtres."
      : canPick()
        ? `Aucun créneau choisi pour l'instant. <a href="index.html">Chercher des créneaux</a>, puis choisissez un type dans la colonne « Choix ».`
        : "Aucun créneau choisi pour l'instant par votre structure."}</td></tr>`;
    return;
  }

  // Regroupement par jour, comme dans la recherche
  const byDay = new Map();
  for (const p of list) {
    if (!byDay.has(p.date)) byDay.set(p.date, []);
    byDay.get(p.date).push(p);
  }
  const rows = [];
  for (const [day, items] of byDay) {
    items.forEach((p, i) => {
      const veille = p.rdv.date !== p.date ? `<span class="veille" title="RDV la veille">J-1</span> ` : "";
      rows.push(`
        <tr data-id="${p.id}" class="${[i === 0 ? "day-start" : "", day < today ? "past" : ""].join(" ").trim()}">
          ${i === 0 ? `<th scope="row" rowspan="${items.length}" class="c-date">${formatDay(day)}</th>` : ""}
          <td class="c-rdv">${veille}${p.rdv.time}</td>
          <td class="c-port">${esc(p.port)}</td>
          <td class="c-tide"><span class="kind ${p.kind}">${p.kind}</span>${p.time}</td>
          <td class="num">${fmtHeight(p.height_m)}</td>
          <td class="num"><span class="coef ${coefClass(p.coefficient)}">${fmtCoef(p.coefficient)}</span></td>
          <td class="c-type">${typeCell(p)}</td>
          <td class="c-regs">${registrationsCell(p)}</td>
          <td class="c-by">${p.picked_by ? esc(p.picked_by) : `<span class="muted">compte supprimé</span>`}</td>
          <td class="c-actions">${canPick() ? `<button type="button" class="btn-danger btn-small" data-act="remove">Retirer</button>` : ""}</td>
        </tr>`);
    });
  }
  bodyEl.innerHTML = rows.join("");
}

function renderTypeFilter() {
  // tous les types présents dans les choix (même désactivés) + les types actifs
  const seen = new Map(types.map(t => [t.id, t]));
  for (const p of picks) if (!seen.has(p.type.id)) seen.set(p.type.id, p.type);
  const current = typeFilter.value;
  typeFilter.innerHTML = `<option value="">Tous les types</option>` +
    [...seen.values()].map(t => `<option value="${t.id}">${esc(t.label)}</option>`).join("");
  if (seen.has(Number(current))) typeFilter.value = current;
}

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
  renderTypeFilter();
  render();
}

bodyEl.addEventListener("change", async e => {
  const sel = e.target.closest("select[data-act=type]");
  if (!sel) return;
  const id = Number(sel.closest("tr").dataset.id);
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
    const id = Number(toggle.closest("tr").dataset.id);
    if (popFor === id) closePop(); else openPop(id);
    return;
  }
  const btn = e.target.closest("button[data-act=register], button[data-act=unregister], button[data-act=unregister-other]");
  if (!btn) return;
  const id = btn.closest("tr") ? Number(btn.closest("tr").dataset.id) : popFor;
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
bodyEl.addEventListener("click", onRegistrationClick);
pop.addEventListener("click", onRegistrationClick);

bodyEl.addEventListener("click", async e => {
  const btn = e.target.closest("button[data-act=remove]");
  if (!btn) return;
  const id = Number(btn.closest("tr").dataset.id);
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
