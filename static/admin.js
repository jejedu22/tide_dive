// Administration.
// - Super administrateur : structures, ports, données et tâches (précalcul, FES,
//   vacances), et types / comptes de toutes les structures.
// - Administrateur de structure : types de créneaux et comptes de SA structure.
// Toutes les vérifications de droits sont faites côté serveur (/api/admin/*) ;
// ici on ne fait que masquer ce qui n'est pas accessible.
// Les tâches longues sont exécutées par le worker ; cette page ne fait que
// les mettre en file et suivre leur avancement.

const esc = Session.esc;
const $ = id => document.getElementById(id);

const gateEl = $("gate");
const adminEl = $("admin");
const flashEl = $("flash");

const fmtStamp = new Intl.DateTimeFormat("fr-FR", { dateStyle: "short", timeStyle: "short" });
const fmtDate = new Intl.DateTimeFormat("fr-FR", { dateStyle: "long" });
const stamp = iso => (iso ? fmtStamp.format(new Date(iso)) : "–");
const fmtNum = (v, d) => (v == null ? "–" : v.toFixed(d).replace(".", ","));

function fmtBytes(n) {
  if (!n) return "0 o";
  const units = ["o", "Ko", "Mo", "Go", "To"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  return `${(n / 1024 ** i).toFixed(i >= 3 ? 1 : 0).replace(".", ",")} ${units[i]}`;
}

function fmtDuration(s) {
  if (s == null) return "–";
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`;
}

function flash(html) {
  flashEl.innerHTML = html;
}

const isSuper = () => !!Session.user?.can.super_admin;

// Petit dialogue de formulaire : body = HTML des champs ; onSubmit(form) lève en cas d'erreur
function openDialog({ title, body, submitLabel = "Enregistrer", onSubmit }) {
  const d = document.createElement("dialog");
  d.className = "account-dialog";
  d.innerHTML = `
    <form method="dialog">
      <h2>${esc(title)}</h2>
      ${body}
      <p class="dialog-error" role="alert"></p>
      <div class="dialog-actions">
        <button type="button" class="btn-quiet" value="cancel">Annuler</button>
        <button type="submit" class="btn-primary">${esc(submitLabel)}</button>
      </div>
    </form>`;
  document.body.append(d);
  const form = d.querySelector("form");
  d.addEventListener("close", () => d.remove());
  d.querySelector("[value=cancel]").addEventListener("click", () => d.close());
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const btn = form.querySelector("[type=submit]");
    btn.disabled = true;
    try {
      await onSubmit(form);
      d.close();
    } catch (err) {
      d.querySelector(".dialog-error").textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  });
  d.showModal();
  return form;
}

// ---------------------------------------------------------------------------
// Onglets
// ---------------------------------------------------------------------------

const ALL_TABS = ["dashboard", "activite", "structures", "ports", "donnees", "communication", "exploitation", "fiche", "types", "sites", "utilisateurs", "mailjet", "journal"];
const SUPER_TABS = ["dashboard", "structures", "ports", "donnees", "communication", "exploitation"];
let TABS = ALL_TABS;       // onglets accessibles au compte connecté
let activeTab = null;

function showTab(name) {
  if (!TABS.includes(name)) name = TABS[0];
  activeTab = name;
  for (const btn of document.querySelectorAll("[role=tab]")) {
    btn.setAttribute("aria-selected", String(btn.dataset.tab === name));
  }
  for (const t of ALL_TABS) $(`tab-${t}`).hidden = t !== name;
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "dashboard") loadDashboard();
  if (name === "activite") loadHome();
  if (name === "fiche") loadFiche();
  if (name === "journal") loadJournal();
  if (name === "communication") { loadAnnouncements(); loadBroadcastRecipients(); }
  if (name === "exploitation") { loadMaintenance(); loadSchedule(); loadBackups(); loadMailLog(); loadQuality(); }
  if (name === "structures") { loadStructures(); loadRequests(); }
  if (name === "donnees") { loadStatus(); loadJobs(); }
  if (name === "ports" && portsMap) Carte.refresh(portsMap.map);
  if (name === "types") loadTypes();
  if (name === "sites") loadSites();
  if (name === "utilisateurs") loadUsers();
  if (name === "mailjet") loadMailjet();
}

document.querySelector(".tabs").addEventListener("click", e => {
  const btn = e.target.closest("[role=tab]");
  if (btn) showTab(btn.dataset.tab);
});
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
document.addEventListener("click", e => {
  const btn = e.target.closest("[data-goto-tab]");
  if (btn) showTab(btn.dataset.gotoTab);
});
flashEl.addEventListener("click", e => {
  const link = e.target.closest("[data-goto]");
  if (link) { e.preventDefault(); showTab(link.dataset.goto); }
});

const seeJobs = `<a href="#donnees" data-goto="donnees">Suivre dans « Données et tâches »</a>`;

// ---------------------------------------------------------------------------
// État : worker, modèle FES, vacances
// ---------------------------------------------------------------------------

let status = null;

async function loadStatus() {
  const before = status?.fes_model;
  try {
    status = await Session.api("/api/admin/status");
  } catch (e) {
    return;
  }
  renderWorkerBanner();
  renderSources();
  // les années des ports sont signalées par rapport au modèle actuel
  if (status.fes_model !== before) renderPorts();
}

// Santé : données manquantes ou incohérentes, tâches en échec, sauvegarde, disque (app/health.py)
// Santé (contrôles des données) : chargée à part, elle sert au bandeau et au tableau de bord. Un seul appel en
// cours à la fois : le tableau de bord réutilise celui du bandeau.
let lastHealth = null;
let healthPromise = null;
function fetchHealth() {
  healthPromise ??= Session.api("/api/admin/health").finally(() => { healthPromise = null; });
  return healthPromise;
}

async function loadHealth() {
  const box = $("health-banner");
  let health;
  try {
    health = lastHealth = await fetchHealth();
  } catch (e) {
    return;
  }
  renderDashHealth();
  const items = health.problems;
  box.hidden = !items.length;
  if (!items.length) return;
  const li = p => `<li class="${p.level === "error" ? "health-error" : "health-warning"}">${esc(p.message)}</li>`;
  box.innerHTML = `<strong>${health.errors ? `${health.errors} problème(s) à traiter` : "À surveiller"}</strong>`
    + `<ul class="plain">${items.map(li).join("")}</ul>`;
  box.classList.toggle("banner-warning", !health.errors);
}

function renderWorkerBanner() {
  const w = status.worker;
  const banner = $("worker-banner");
  if (!w.alive) {
    banner.hidden = false;
    banner.innerHTML = `Le worker ne tourne pas${w.heartbeat_at ? ` (dernier signe de vie : ${stamp(w.heartbeat_at)})` : ""} : les tâches resteront en attente. Démarrez-le avec <code>docker compose up -d worker</code>.`;
  } else if (!w.info.aviso_configured) {
    banner.hidden = false;
    banner.innerHTML = `Identifiants AVISO+ absents pour le worker : le téléchargement du modèle FES échouera. Renseignez <code>AVISO_USERNAME</code> et <code>AVISO_PASSWORD</code> dans <code>.env</code>.`;
  } else {
    banner.hidden = true;
  }
}

function renderSources() {
  const m = status.models;
  let fes;
  if (!m.exists) {
    fes = `<p>Dossier <code>${esc(m.directory)}</code> introuvable côté API.</p>`;
  } else if (!m.files) {
    fes = `<p><strong>Aucun modèle téléchargé.</strong> Lancez le téléchargement avant tout calcul.</p>`;
  } else {
    fes = `
      <p>${fmtBytes(m.total_bytes)} dans <code>${esc(m.directory)}</code></p>
      <ul class="plain">${m.entries.map(e => `
        <li><strong>${esc(e.name)}</strong> : ${e.files} fichier(s), ${fmtBytes(e.bytes)}${e.modified_at ? `, mis à jour le ${stamp(e.modified_at)}` : ""}</li>`).join("")}
      </ul>`;
  }
  $("fes-summary").innerHTML = fes;

  // Modèle des calculs : liste avec l'état de téléchargement de chaque modèle
  const avail = f => (f.available === true ? "téléchargé" : f.available === false ? "non téléchargé" : "état inconnu");
  const calc = $("calc-model");
  const keep = calc.dataset.dirty === "1" ? calc.value : status.fes_model;
  calc.innerHTML = status.fes_models.map(f =>
    `<option value="${esc(f.model)}">${esc(f.model)} (${avail(f)})</option>`).join("");
  calc.value = keep;
  renderModelNote();

  // Téléchargement : par défaut le modèle des calculs, puis le dernier choisi
  const sel = $("fes-model");
  if (!sel.options.length) {
    sel.innerHTML = status.fes_models.map(f => `<option${f.model === status.fes_model ? " selected" : ""}>${esc(f.model)}</option>`).join("");
  }

  const h = status.school_holidays;
  $("holidays-summary").innerHTML = h.periods
    ? `<p>${h.periods} périodes pour l'académie de ${esc(h.academy)}, jusqu'au ${fmtDate.format(new Date(h.last_end + "T12:00:00"))}.</p>`
    : `<p><strong>Aucune période chargée</strong> pour l'académie de ${esc(h.academy)}.</p>`;
}

// ---------------------------------------------------------------------------
// Tâches
// ---------------------------------------------------------------------------

const STATUS_LABELS = {
  queued: "En attente",
  running: "En cours",
  succeeded: "Terminée",
  failed: "Échec",
  cancelled: "Annulée",
};

let jobs = [];
let openJobId = null;
const jobsBody = $("jobs-body");

function statusTag(j) {
  const label = j.status === "running" && j.cancel_requested ? "Annulation…" : STATUS_LABELS[j.status];
  return `<span class="tag job-${j.status}">${label}</span>`;
}

function renderJobs() {
  jobsBody.innerHTML = jobs.length ? jobs.map(j => `
    <tr data-id="${j.id}" class="${j.id === openJobId ? "is-open" : ""}">
      <td class="num" data-label="N°">${j.id}</td>
      <th scope="row">${esc(j.label)}</th>
      <td data-label="Statut">${statusTag(j)}</td>
      <td data-label="Lancée par">${esc(j.created_by)}</td>
      <td data-label="Créée">${stamp(j.created_at)}</td>
      <td class="num" data-label="Durée">${fmtDuration(j.duration_s)}</td>
      <td class="actions">
        <button type="button" class="btn-quiet" data-act="log">Journal</button>
        ${["queued", "running"].includes(j.status) && !j.cancel_requested
          ? `<button type="button" class="btn-danger" data-act="cancel">Annuler</button>` : ""}
      </td>
    </tr>`).join("")
    : `<tr><td colspan="7" class="empty">Aucune tâche pour l'instant.</td></tr>`;

  const active = jobs.filter(j => ["queued", "running"].includes(j.status)).length;
  const badge = $("active-count");
  badge.hidden = !active;
  badge.textContent = active;
}

let previousStatuses = new Map();

async function loadJobs() {
  try {
    jobs = await Session.api("/api/admin/jobs");
  } catch (e) {
    return;
  }
  // Un précalcul vient de se terminer : les années disponibles ont changé
  let refreshPorts = false, refreshStatus = false;
  for (const j of jobs) {
    const before = previousStatuses.get(j.id);
    if (before && before !== j.status && !["queued", "running"].includes(j.status)) {
      // précalcul : années disponibles ; atlas de courants : courant des sites et zones téléchargées
      if (j.kind === "precompute" || j.kind === "currents_atlas") refreshPorts = true;
      else refreshStatus = true;
    }
  }
  previousStatuses = new Map(jobs.map(j => [j.id, j.status]));
  renderJobs();
  if (refreshPorts) loadPorts();
  if (refreshStatus) loadStatus();
  if (openJobId) loadJobLog(openJobId, { quiet: true });
}

async function loadJobLog(id, { quiet = false } = {}) {
  const pre = $("job-log-text");
  try {
    const j = await Session.api(`/api/admin/jobs/${id}`);
    $("job-log-title").textContent = `#${j.id} · ${j.label} · ${STATUS_LABELS[j.status]}`;
    // colle en bas si l'utilisateur y était déjà (suivi en direct)
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.textContent = j.log || (j.status === "queued" ? "En attente du worker…" : "(journal vide)");
    if (atBottom || !quiet) pre.scrollTop = pre.scrollHeight;
    $("job-log").hidden = false;
  } catch (e) {
    if (!quiet) pre.textContent = e.message;
  }
}

jobsBody.addEventListener("click", async e => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const id = Number(btn.closest("tr").dataset.id);
  if (btn.dataset.act === "log") {
    openJobId = id;
    renderJobs();
    loadJobLog(id);
  } else if (btn.dataset.act === "cancel") {
    const j = jobs.find(x => x.id === id);
    if (!confirm(`Annuler « ${j.label} » ?`)) return;
    try {
      await Session.api(`/api/admin/jobs/${id}/cancel`, { method: "POST" });
      loadJobs();
    } catch (err) {
      flash(esc(err.message));
    }
  }
});

$("job-log-close").addEventListener("click", () => {
  openJobId = null;
  $("job-log").hidden = true;
  renderJobs();
});

async function enqueue(kind, params) {
  try {
    const j = await Session.api("/api/admin/jobs", { method: "POST", body: { kind, params } });
    flash(`« ${esc(j.label)} » ajoutée à la file. ${activeTab === "donnees" ? "" : seeJobs}`);
    loadJobs();
    return j;
  } catch (e) {
    flash(esc(e.message));
    return null;
  }
}

function renderModelNote() {
  const calc = $("calc-model");
  const f = status.fes_models.find(x => x.model === calc.value);
  const dirty = calc.value !== status.fes_model;
  calc.dataset.dirty = dirty ? "1" : "";
  $("calc-model-save").disabled = !dirty;
  const parts = [];
  if (f?.available === false) {
    parts.push(`<strong>${esc(f.model)} n'est pas téléchargé</strong> (${f.missing}/${f.expected} fichiers manquants) : les calculs échoueront tant qu'il ne l'est pas.`);
  }
  parts.push(dirty
    ? "Non enregistré."
    : "S'applique aux prochains calculs, y compris le calcul annuel automatique. Les années déjà calculées gardent leur modèle (visible dans l'onglet Ports) : relancez-les pour en changer.");
  $("calc-model-note").innerHTML = parts.join(" ");
}

$("calc-model").addEventListener("change", renderModelNote);

$("model-form").addEventListener("submit", async e => {
  e.preventDefault();
  const model = $("calc-model").value;
  try {
    const r = await Session.api("/api/admin/settings/tide-model", { method: "PUT", body: { model } });
    status.fes_model = r.fes_model;
    $("calc-model").dataset.dirty = "";
    flash(`Les prochains calculs utiliseront ${esc(r.fes_model)}.`);
    renderSources();
    renderPorts();
  } catch (err) {
    flash(esc(err.message));
  }
});

$("fes-form").addEventListener("submit", e => {
  e.preventDefault();
  enqueue("fetch_models", { model: $("fes-model").value });
});
$("holidays-sync").addEventListener("click", () => enqueue("school_holidays", {}));

// Rafraîchissement : rapide tant qu'une tâche est active, lent sinon
let pollTimer = null;
function schedulePoll() {
  clearTimeout(pollTimer);
  const active = jobs.some(j => ["queued", "running"].includes(j.status));
  pollTimer = setTimeout(async () => {
    if (document.visibilityState === "visible" && Session.user?.is_admin) {
      await loadJobs();
      if (activeTab === "donnees" || !status?.worker.alive) await loadStatus();
    }
    schedulePoll();
  }, active ? 2000 : 10000);
}

// ---------------------------------------------------------------------------
// Structures (super administrateur ; un administrateur de structure ne voit que la sienne)
// ---------------------------------------------------------------------------

let structures = [];
const structuresBody = $("structures-body");
const structureForm = $("structure-form");

async function loadStructures() {
  try {
    structures = await Session.api("/api/admin/structures");
    if (isSuper()) Session.setStructures(structures);  // sélecteur de l'en-tête
  } catch (e) {
    flash(esc(e.message));
    return;
  }
  renderStructures();
  renderStructureSelects();
}

function renderStructures() {
  structuresBody.innerHTML = structures.length ? structures.map(st => {
    const members = st.managers + st.viewers;
    return `
      <tr data-id="${st.id}">
        <th scope="row">${esc(st.name)}${st.archived_at ? ` <span class="tag tag-warn" title="Archivée le ${stamp(st.archived_at)}">archivée</span>` : ""}
          ${featureTags(st)}</th>
        <td class="num" data-label="Administration">${st.managers || `<span class="tag job-failed" title="Personne ne peut choisir de créneaux ni gérer cette structure">aucun</span>`}</td>
        <td class="num" data-label="Visualisation">${st.viewers}</td>
        <td class="num" data-label="Types">${st.types}</td>
        <td class="num" data-label="Créneaux choisis">${st.selections}</td>
        <td data-label="Recherche">
          <select data-act="search-modes" aria-label="Recherche proposée à ${esc(st.name)}">
            ${Object.entries(SEARCH_MODES).map(([v, label]) =>
              `<option value="${v}"${(st.search_modes || "tides") === v ? " selected" : ""}>${label}</option>`).join("")}
          </select>
        </td>
        <td class="actions">
          <button type="button" class="btn-quiet" data-act="members">Membres</button>
          <button type="button" class="btn-quiet" data-act="types">Types</button>
          <button type="button" class="btn-quiet" data-act="features" title="Fonctions proposées à la structure">Fonctions…</button>
          <button type="button" class="btn-quiet" data-act="transfer" title="Rattacher des membres à une autre structure">Transférer…</button>
          <button type="button" class="btn-quiet" data-act="rename">Renommer</button>
          <button type="button" class="btn-quiet" data-act="${st.archived_at ? "unarchive" : "archive"}">${st.archived_at ? "Réactiver" : "Archiver"}</button>
          <button type="button" class="btn-danger" data-act="delete" ${members ? `disabled title="Encore ${members} membre(s)"` : ""}>Supprimer</button>
        </td>
      </tr>`;
  }).join("")
    : `<tr><td colspan="7" class="empty">Aucune structure. Créez-en une, puis ajoutez-lui des comptes dans « Utilisateurs ».</td></tr>`;
}

// ---------------------------------------------------------------------------
// Demandes de création de structure (formulaire public)
// ---------------------------------------------------------------------------

let requests = [];
const REQUEST_STATUS = { new: "nouvelle", done: "traitée", rejected: "sans suite" };

async function loadRequests() {
  if (!isSuper()) return;
  try {
    requests = await Session.api("/api/admin/structure-requests");
  } catch (e) {
    $("requests-list").innerHTML = `<p class="empty">${esc(e.message)}</p>`;
    return;
  }
  renderRequests();
}

function requestCard(r) {
  const when = new Date(r.created_at).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
  const status = r.status === "done" && r.structure
    ? `structure « ${esc(r.structure.name)} » créée`
    : REQUEST_STATUS[r.status];
  const acts = [];
  if (r.status === "new") {
    acts.push(`<button type="button" class="btn-primary btn-small" data-act="create">Créer la structure</button>`);
    acts.push(`<button type="button" class="btn-quiet btn-small" data-act="reject">Classer sans suite</button>`);
  } else {
    acts.push(`<button type="button" class="btn-quiet btn-small" data-act="reopen">Remettre en attente</button>`);
  }
  if (r.structure) acts.push(`<button type="button" class="btn-secondary btn-small" data-act="account">Préparer le compte</button>`);
  acts.push(`<button type="button" class="btn-danger btn-small" data-act="delete">Supprimer</button>`);
  return `
    <article class="request-card${r.status === "new" ? " is-new" : ""}" data-id="${r.id}">
      <div class="request-head">
        <strong>${esc(r.structure_name)}</strong>${r.city ? ` <span class="muted">· ${esc(r.city)}</span>` : ""}
        <span class="tag${r.status === "new" ? " tag-pending" : ""}">${status}</span>
      </div>
      <p class="request-contact">${esc(r.contact_name)} · <a href="mailto:${esc(r.email)}">${esc(r.email)}</a>${r.phone ? ` · <a href="tel:${esc(r.phone.replace(/\s/g, ""))}">${esc(r.phone)}</a>` : ""}</p>
      ${r.message ? `<p class="request-message">${esc(r.message)}</p>` : ""}
      <p class="request-meta muted">Reçue le ${when}${r.handled_by ? ` · traitée par ${esc(r.handled_by)}` : ""}</p>
      <div class="request-acts">${acts.join("")}</div>
    </article>`;
}

function renderRequests() {
  const pending = requests.filter(r => r.status === "new").length;
  const count = $("requests-count");
  count.hidden = !pending;
  count.textContent = `${pending} à traiter`;
  $("requests-list").innerHTML = requests.length
    ? requests.map(requestCard).join("")
    : `<p class="empty">Aucune demande pour le moment.</p>`;
}

// Formulaire « Ajouter un utilisateur » prérempli avec le contact, administrateur de la structure créée
function prepareAccount(r) {
  showTab("utilisateurs");
  const [first, ...rest] = r.contact_name.split(" ");
  createForm.first_name.value = rest.length ? first : "";
  createForm.last_name.value = rest.length ? rest.join(" ") : first;
  createForm.email.value = r.email;
  createForm.phone.value = r.phone || "";
  if ([...createForm.structure_id.options].some(o => o.value === String(r.structure.id))) {
    createForm.structure_id.value = String(r.structure.id);
  }
  // compte de structure, jamais super administrateur ; les écouteurs du formulaire suivent
  createForm.is_admin.checked = false;
  syncCreateRole();
  createForm.role.value = "manager";
  for (const name of ["first_name", "last_name", "email"]) {
    createForm[name].dispatchEvent(new Event("input", { bubbles: true }));
  }
  createForm.scrollIntoView({ block: "start" });
  createForm.first_name.focus();
  flash(`Compte de ${esc(r.contact_name)} prérempli : vérifiez le prénom et le nom, puis validez.`);
}

$("requests-list").addEventListener("click", async e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const r = requests.find(x => x.id === Number(btn.closest("[data-id]").dataset.id));
  const update = saved => { requests = requests.map(x => (x.id === saved.id ? saved : x)); renderRequests(); };
  try {
    switch (btn.dataset.act) {
      case "create":
        openDialog({
          title: "Créer la structure demandée",
          submitLabel: "Créer",
          body: `<label>Nom de la structure <input name="name" required maxlength="80" value="${esc(r.structure_name)}"></label>
                 <p class="dialog-hint">La demande sera classée comme traitée. Vous pourrez ensuite préparer le compte de ${esc(r.contact_name)}.</p>`,
          onSubmit: async form => {
            update(await Session.api(`/api/admin/structure-requests/${r.id}/create-structure`,
              { method: "POST", body: { name: form.name.value.trim() } }));
            loadStructures();
            flash(`Structure créée. <button type="button" class="btn-secondary btn-small" id="flash-account">Préparer le compte de ${esc(r.contact_name)}</button>`);
            $("flash-account")?.addEventListener("click", () => prepareAccount(requests.find(x => x.id === r.id)));
          },
        });
        break;
      case "account":
        prepareAccount(r);
        break;
      case "reject":
      case "reopen":
        update(await Session.api(`/api/admin/structure-requests/${r.id}`,
          { method: "PATCH", body: { status: btn.dataset.act === "reject" ? "rejected" : "new" } }));
        break;
      case "delete":
        if (!confirm(`Supprimer la demande de « ${r.structure_name} » ? Les coordonnées du contact seront effacées.`)) return;
        await Session.api(`/api/admin/structure-requests/${r.id}`, { method: "DELETE" });
        requests = requests.filter(x => x.id !== r.id);
        renderRequests();
        break;
    }
  } catch (err) {
    flash(esc(err.message));
  }
});

// ---------------------------------------------------------------------------
// Tableau de bord (super administrateur)
// ---------------------------------------------------------------------------

const fmtAgo = iso => {
  if (!iso) return "jamais";
  const days = Math.floor((Date.now() - new Date(iso)) / 86400000);
  return days <= 0 ? "aujourd'hui" : days === 1 ? "hier" : `il y a ${days} jours`;
};

const journalLine = r => `
  <li><span class="journal-at">${esc(fmtStamp.format(new Date(r.at)))}</span>
  <span><strong>${esc(r.actor || "?")}</strong> · ${esc(r.action)}${r.target ? ` : <em>${esc(r.target)}</em>` : ""}${r.structure ? ` <span class="tag">${esc(r.structure)}</span>` : ""}</span></li>`;

// Santé dans le tableau de bord (indicateur et liste), dès qu'elle est chargée
function renderDashHealth() {
  const h = lastHealth;
  const kpiEl = $("dash-health-kpi");
  if (h && kpiEl) kpiEl.innerHTML = `<strong>${h.ok ? "✓" : h.errors}</strong><span>${h.ok ? "santé" : "erreurs de santé"}</span>`;
  $("dash-health").innerHTML = !h ? `<li class="muted">Contrôle en cours…</li>` : h.problems.length ? h.problems.map(p => `
    <li><span><span class="tag ${p.level === "error" ? "tag-error" : "tag-warn"}">${p.level === "error" ? "erreur" : "avertissement"}</span> ${esc(p.message)}</span></li>`).join("")
    : `<li class="muted">Aucun problème détecté.</li>`;
}

async function loadDashboard() {
  let d;
  try {
    d = await Session.api("/api/admin/dashboard");
  } catch (e) {
    $("dash-kpis").innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    return;
  }
  const kpi = (value, label, title = "") => `<div class="kpi"${title ? ` title="${esc(title)}"` : ""}><strong>${value}</strong><span>${esc(label)}</span></div>`;
  const withIssues = d.structures.filter(s => s.issues.length).length;
  $("dash-kpis").innerHTML = [
    kpi(d.structures.length, "structures", withIssues ? `${withIssues} avec un point d'attention` : ""),
    kpi(d.users.total, "comptes"),
    kpi(d.users.active_30d, "actifs sur 30 jours", "connectés au moins une fois depuis 30 jours"),
    kpi(d.users.pending_invites, "invitations en attente"),
    kpi(d.selections.upcoming, "créneaux à venir"),
    kpi(d.selections.registrations_30d, "inscriptions (30 j)"),
    `<div class="kpi" id="dash-health-kpi"><strong>…</strong><span>santé</span></div>`,
  ].join("");
  renderDashHealth();
  if (!lastHealth) loadHealth();
  $("dash-structures").innerHTML = d.structures.length ? d.structures.map(s => `
    <tr${s.issues.length ? ' class="dash-warn"' : ""}>
      <th scope="row">${esc(s.name)}${s.caci_check ? ' <span class="tag" title="Vérification du CACI à l\'inscription">CACI</span>' : ""}</th>
      <td class="num" data-label="Membres">${s.members}</td><td class="num" data-label="Admin.">${s.managers}</td>
      <td class="num" data-label="Créneaux à venir">${s.upcoming}</td><td class="num" data-label="Inscriptions (30 j)">${s.registrations_30d}</td>
      <td class="num" data-label="Sites">${s.sites}</td>
      <td data-label="Dernière connexion admin.">${esc(fmtAgo(s.last_admin_login))}</td>
      <td class="dash-issues" data-label="Points d'attention">${s.issues.length ? s.issues.map(i => `<span class="tag tag-warn">${esc(i)}</span>`).join(" ") : '<span class="muted">—</span>'}</td>
    </tr>`).join("") : `<tr><td colspan="8" class="empty">Aucune structure.</td></tr>`;
  const year = new Date().getFullYear();
  $("dash-ports").innerHTML = d.ports.length ? d.ports.map(p => `
    <li${p.has_current_year ? "" : ' class="dash-warn"'}><strong>${esc(p.name)}</strong>
    ${p.years.length ? p.years.map(y => `<span class="tag${y === year ? "" : " tag-quiet"}">${y}</span>`).join(" ") : '<span class="muted">aucune année calculée</span>'}
    ${p.has_current_year ? "" : `<span class="tag tag-warn">${year} manquante</span>`}</li>`).join("")
    : `<li class="muted">Aucun port.</li>`;

  $("dash-recent").innerHTML = d.recent.length ? d.recent.map(journalLine).join("") : `<li class="muted">Aucune action enregistrée.</li>`;
}

// ---------------------------------------------------------------------------
// Activité de la structure : à faire, créneaux à surveiller, statistiques
// ---------------------------------------------------------------------------

let homeScope = null;    // structure affichée (choix du super administrateur)
let homeStats = null;    // dernières statistiques chargées (export Excel)
const homeQS = (extra = {}) => {
  const p = new URLSearchParams(extra);
  if (isSuper() && homeScope) p.set("structure_id", homeScope);
  const q = p.toString();
  return q ? `?${q}` : "";
};

$("home-structure").addEventListener("change", e => {
  homeScope = Number(e.target.value) || null;
  loadHome();
});
$("stats-months").addEventListener("change", () => loadStats());

const fmtDayShort = new Intl.DateTimeFormat("fr-FR", { weekday: "short", day: "numeric", month: "short" });
const dayLabel = iso => fmtDayShort.format(new Date(`${iso}T12:00:00`));
const pct = r => (r == null ? "—" : `${Math.round(r * 100)} %`);
const picksLink = `<a href="/mes-creneaux.html">Créneaux choisis</a>`;

function homeSlot(s, extra = "") {
  const places = s.max_registrations == null ? `${s.registrations} inscrit(s)` : `${Math.min(s.registrations, s.max_registrations)}/${s.max_registrations}`;
  return `<li><span class="type-pill" style="--type-color:${esc(s.color)}">${esc(s.type)}</span>
    <strong>${esc(dayLabel(s.date))}</strong> ${esc(s.rdv_time)} · ${esc(s.place)} <span class="muted">${esc(places)}</span>${extra}</li>`;
}

async function loadHome() {
  if (isSuper() && !homeScope) {
    $("home-kpis").innerHTML = `<p class="muted">Choisissez une structure.</p>`;
    return;
  }
  let d;
  try {
    d = await Session.api(`/api/admin/structure-dashboard${homeQS()}`);
  } catch (e) {
    $("home-kpis").innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    return;
  }
  const c = d.counts;
  const kpi = (value, label, title = "") => `<div class="kpi"${title ? ` title="${esc(title)}"` : ""}><strong>${value}</strong><span>${esc(label)}</span></div>`;
  $("home-kpis").innerHTML = [
    kpi(c.members, "membres", `${c.managers} en administration`),
    kpi(c.active_30d, "actifs sur 30 jours", "connectés au moins une fois depuis 30 jours"),
    kpi(c.upcoming, "créneaux à venir"),
    kpi(c.registrations_30d, "inscriptions (30 j)"),
    kpi(c.sites, "sites de plongée"),
  ].join("");
  const todo = [];
  const settings = { "aucun type de créneau proposé": "types", "pas de port par défaut": "types", "aucun administrateur": "utilisateurs" };
  for (const issue of d.issues) {
    todo.push(`<li class="todo-warn">Réglage manquant : ${esc(issue)} <a href="#${settings[issue] || "types"}" data-goto="${settings[issue] || "types"}">Régler</a></li>`);
  }
  if (d.caci.pending) todo.push(`<li>${d.caci.pending} certificat(s) médical(aux) à valider <a href="/plongeurs.html">Plongeurs</a></li>`);
  if (d.caci.check && (d.caci.missing || d.caci.expired)) {
    todo.push(`<li class="todo-warn">${d.caci.missing + d.caci.expired} membre(s) sans certificat médical valable (vérification activée : leur inscription est refusée) <a href="/plongeurs.html">Plongeurs</a></li>`);
  }
  if (d.caci.expiring) todo.push(`<li>${d.caci.expiring} certificat(s) médical(aux) expirent dans les 30 jours</li>`);
  if (d.unmarked.length) todo.push(`<li>${d.unmarked.length} créneau(x) des ${d.attendance_days} derniers jours sans présences pointées ${picksLink}</li>`);
  if (c.invitations) todo.push(`<li>${c.invitations} invitation(s) à rejoindre la structure en attente <a href="#utilisateurs" data-goto="utilisateurs">Utilisateurs</a></li>`);
  if (c.pending_accounts) todo.push(`<li>${c.pending_accounts} compte(s) n'ont pas encore choisi leur mot de passe <a href="#utilisateurs" data-goto="utilisateurs">Utilisateurs</a></li>`);
  $("home-todo").innerHTML = todo.join("") || `<li class="muted">Rien à signaler.</li>`;
  const section = (title, list, render) => (list.length ? `<h3>${title}</h3><ul class="home-slots">${list.map(render).join("")}</ul>` : "");
  $("home-slots").innerHTML = [
    section(`Peu remplis (${d.soon_days} prochains jours)`, d.low_fill, s => homeSlot(s)),
    section("Complets, avec file d'attente", d.full, s => homeSlot(s, ` <span class="tag tag-warn">${s.waiting} en attente</span>`)),
    section("Présences à pointer", d.unmarked, s => homeSlot(s)),
  ].join("") || `<p class="muted">Aucun créneau à surveiller.</p>`;
  $("home-recent").innerHTML = d.recent.length ? d.recent.map(journalLine).join("") : `<li class="muted">Aucune action enregistrée.</li>`;
  loadStats();
}

async function loadStats() {
  let s;
  try {
    s = await Session.api(`/api/admin/structure-stats${homeQS({ months: $("stats-months").value })}`);
  } catch (e) {
    $("stats-summary").textContent = e.message;
    return;
  }
  homeStats = s;
  const t = s.totals;
  $("stats-summary").textContent = `Du ${dayLabel(s.start)} au ${dayLabel(s.end)} : ${t.slots} créneau(x), ` +
    `${t.registrations} inscription(s) confirmée(s), remplissage ${pct(t.fill_rate)}, ` +
    `${t.present} présent(s), ${t.absent} absent(s), ${t.excused} excusé(s) (présence ${pct(t.attendance_rate)}).`;
  $("stats-types").innerHTML = s.by_type.length ? s.by_type.map(b => `
    <tr><th scope="row"><span class="type-pill" style="--type-color:${esc(b.color)}">${esc(b.type)}</span></th>
      <td class="num" data-label="Créneaux">${b.slots}</td><td class="num" data-label="Inscriptions">${b.registrations}</td>
      <td class="num" data-label="Remplissage">${pct(b.fill_rate)}</td><td class="num" data-label="Présents">${b.present}</td>
      <td class="num" data-label="Présence">${pct(b.attendance_rate)}</td></tr>`).join("")
    : `<tr><td colspan="6" class="empty">Aucun créneau sur la période.</td></tr>`;
  const fmtMonth = new Intl.DateTimeFormat("fr-FR", { month: "long", year: "numeric" });
  $("stats-months-body").innerHTML = s.by_month.slice().reverse().map(m => `
    <tr><th scope="row">${esc(fmtMonth.format(new Date(`${m.month}-15T12:00:00`)))}</th>
      <td class="num" data-label="Créneaux">${m.slots}</td><td class="num" data-label="Inscriptions">${m.registrations}</td>
      <td class="num" data-label="Présents">${m.present}</td></tr>`).join("");
  $("stats-members").innerHTML = s.members.length ? s.members.map(m => `
    <tr${m.registrations ? "" : ' class="muted"'}><th scope="row">${esc(m.display_name)}</th>
      <td class="num" data-label="Inscriptions">${m.registrations}</td><td class="num" data-label="Présent">${m.present}</td>
      <td class="num" data-label="Absent">${m.absent}</td><td class="num" data-label="Excusé">${m.excused}</td>
      <td data-label="Dernier créneau">${m.last_slot ? esc(dayLabel(m.last_slot)) : "—"}</td></tr>`).join("")
    : `<tr><td colspan="6" class="empty">Aucun membre.</td></tr>`;
}

$("stats-export").addEventListener("click", () => {
  if (!homeStats) return;
  const name = isSuper() ? $("home-structure").selectedOptions[0]?.textContent : Session.user?.structure?.name;
  XlsxExport.download(`statistiques-${XlsxExport.slug(name || "structure")}-${homeStats.start}-au-${homeStats.end}.xlsx`,
    "Membres", [
      { header: "Membre", width: 26, value: m => m.display_name },
      { header: "Identifiant", width: 18, value: m => m.username },
      { header: "Rôle", width: 14, value: m => (m.role === "manager" ? "administration" : "visualisation") },
      { header: "Inscriptions", type: "int", width: 12, value: m => m.registrations },
      { header: "Présent", type: "int", width: 9, value: m => m.present },
      { header: "Absent", type: "int", width: 9, value: m => m.absent },
      { header: "Excusé", type: "int", width: 9, value: m => m.excused },
      { header: "Dernier créneau", type: "date", width: 14, value: m => m.last_slot },
    ], homeStats.members);
});

// ---------------------------------------------------------------------------
// Ma structure : fiche, logo, lien d'adhésion
// ---------------------------------------------------------------------------

let ficheScope = null;    // structure affichée (choix du super administrateur)
let fiche = null;         // fiche chargée
const ficheId = () => (isSuper() ? ficheScope : Session.user?.structure?.id);
const ficheForm = $("fiche-form");

$("fiche-structure").addEventListener("change", e => {
  ficheScope = Number(e.target.value) || null;
  loadFiche();
});

function renderFiche() {
  for (const k of ["name", "address", "contact_email", "contact_phone", "website"]) ficheForm[k].value = fiche[k] ?? "";
  $("fiche-logo").hidden = !fiche.logo_url;
  if (fiche.logo_url) $("fiche-logo").src = fiche.logo_url;
  $("fiche-no-logo").hidden = !!fiche.logo_url;
  $("fiche-logo-delete").hidden = !fiche.logo_url;
  const url = fiche.join_url ? new URL(fiche.join_url, location.origin).href : null;
  $("fiche-join").innerHTML = url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`
    : `<span class="muted">Lien désactivé.</span>`;
  $("fiche-join-create").textContent = url ? "Remplacer le lien" : "Activer le lien";
  $("fiche-join-copy").hidden = $("fiche-join-delete").hidden = !url;
}

async function loadFiche() {
  const sid = ficheId();
  ficheForm.hidden = !sid;
  if (!sid) return;
  try {
    fiche = await Session.api(`/api/admin/structures/${sid}/profile`);
  } catch (e) {
    $("fiche-status").textContent = e.message;
    return;
  }
  renderFiche();
}

ficheForm.addEventListener("submit", async e => {
  e.preventDefault();
  const body = {};
  for (const k of ["name", "address", "contact_email", "contact_phone", "website"]) body[k] = ficheForm[k].value.trim() || null;
  try {
    fiche = await Session.api(`/api/admin/structures/${ficheId()}/profile`, { method: "PATCH", body });
    renderFiche();
    $("fiche-status").textContent = "Fiche enregistrée.";
    const st = structures.find(x => x.id === fiche.id);
    if (st && st.name !== fiche.name) loadStructures();    // nom changé : listes et en-tête
    if (Session.user?.structure?.id === fiche.id) Session.user.structure.name = fiche.name;
  } catch (err) {
    $("fiche-status").textContent = err.message;
  }
});

$("fiche-logo-file").addEventListener("change", async e => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const status = $("fiche-logo-status");
  if (file.size > 200 * 1024) { status.textContent = "Image trop lourde : 200 Ko au plus."; return; }
  try {
    const res = await fetch(`/api/admin/structures/${ficheId()}/logo`, {
      method: "PUT", credentials: "same-origin", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file,
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error(data?.detail || `Erreur ${res.status}`);
    fiche = data;
    renderFiche();
    status.textContent = "Logo enregistré.";
  } catch (err) {
    status.textContent = err.message;
  }
});

$("fiche-logo-delete").addEventListener("click", async () => {
  if (!confirm("Retirer le logo de la structure ?")) return;
  try {
    fiche = await Session.api(`/api/admin/structures/${ficheId()}/logo`, { method: "DELETE" });
    renderFiche();
  } catch (err) {
    $("fiche-logo-status").textContent = err.message;
  }
});

$("fiche-join-create").addEventListener("click", async () => {
  if (fiche?.join_url && !confirm("Remplacer le lien ? L'ancien cessera de fonctionner.")) return;
  try {
    fiche = await Session.api(`/api/admin/structures/${ficheId()}/join-link`, { method: "POST" });
    renderFiche();
    $("fiche-join-status").textContent = "Lien d'adhésion actif.";
  } catch (err) {
    $("fiche-join-status").textContent = err.message;
  }
});

$("fiche-join-delete").addEventListener("click", async () => {
  if (!confirm("Désactiver le lien d'adhésion ? Il cessera de fonctionner.")) return;
  try {
    fiche = await Session.api(`/api/admin/structures/${ficheId()}/join-link`, { method: "DELETE" });
    renderFiche();
    $("fiche-join-status").textContent = "Lien désactivé.";
  } catch (err) {
    $("fiche-join-status").textContent = err.message;
  }
});

$("fiche-join-copy").addEventListener("click", async () => {
  const url = new URL(fiche.join_url, location.origin).href;
  try {
    await navigator.clipboard.writeText(url);
    $("fiche-join-status").textContent = "Lien copié.";
  } catch {
    $("fiche-join-status").textContent = url;
  }
});

// ---------------------------------------------------------------------------
// Communication (super administrateur) : bandeaux d'annonce, e-mail aux administrateurs de structure
// ---------------------------------------------------------------------------

let announcements = [];
const toLocalInput = iso => {           // ISO UTC → valeur d'un <input type=datetime-local> (heure locale)
  const d = new Date(iso);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
const structureNames = ids => ids?.length
  ? ids.map(id => structures.find(s => s.id === id)?.name || `#${id}`).join(", ") : "tout le monde";

async function loadAnnouncements() {
  try {
    announcements = await Session.api("/api/admin/announcements");
  } catch (e) {
    $("announcements-list").innerHTML = `<li class="muted">${esc(e.message)}</li>`;
    return;
  }
  const now = new Date();
  $("announcements-list").innerHTML = announcements.length ? announcements.map(a => {
    const state = new Date(a.ends_at) <= now ? "terminé" : new Date(a.starts_at) > now ? "à venir" : "en cours";
    return `<li data-id="${a.id}">
      <span><span class="tag${state === "en cours" ? "" : " tag-quiet"}">${state}</span>
        ${a.level === "warning" ? '<span class="tag tag-warn">important</span>' : ""} ${esc(a.message)}<br>
        <span class="muted">du ${stamp(a.starts_at)} au ${stamp(a.ends_at)} · ${esc(structureNames(a.structure_ids))}</span></span>
      <span><button type="button" class="btn-quiet btn-small" data-ann="edit">Modifier</button>
        <button type="button" class="btn-danger btn-small" data-ann="delete">Supprimer</button></span>
    </li>`;
  }).join("") : `<li class="muted">Aucun bandeau.</li>`;
}

function structureChecks(selected = []) {
  return `<div class="transfer-list">${structures.filter(s => !s.archived_at).map(s => `
    <label class="check"><input type="checkbox" name="structure" value="${s.id}"${selected.includes(s.id) ? " checked" : ""}> ${esc(s.name)}</label>`).join("")}</div>`;
}

function editAnnouncement(a = null) {
  const start = a ? toLocalInput(a.starts_at) : toLocalInput(new Date().toISOString());
  const end = a ? toLocalInput(a.ends_at) : toLocalInput(new Date(Date.now() + 7 * 86400000).toISOString());
  openDialog({
    title: a ? "Modifier le bandeau" : "Nouveau bandeau d'annonce",
    body: `
      <label>Message <textarea name="message" rows="3" maxlength="500" required>${esc(a?.message ?? "")}</textarea></label>
      <label>Style <select name="level"><option value="info">Information</option>
        <option value="warning"${a?.level === "warning" ? " selected" : ""}>Important (jaune)</option></select></label>
      <div class="inline-form">
        <label>Du <input name="starts_at" type="datetime-local" required value="${start}"></label>
        <label>au <input name="ends_at" type="datetime-local" required value="${end}"></label>
      </div>
      <fieldset class="transfer-mode"><legend>Pour</legend>
        <label class="check"><input type="radio" name="scope" value="all"${a?.structure_ids?.length ? "" : " checked"}> Tout le monde (visiteurs compris)</label>
        <label class="check"><input type="radio" name="scope" value="some"${a?.structure_ids?.length ? " checked" : ""}> Les membres de certaines structures :</label>
        ${structureChecks(a?.structure_ids || [])}
      </fieldset>`,
    onSubmit: async form => {
      const ids = [...form.querySelectorAll("[name=structure]:checked")].map(c => Number(c.value));
      const some = form.scope.value === "some";
      if (some && !ids.length) throw new Error("Cochez au moins une structure.");
      const body = {
        message: form.message.value, level: form.level.value,
        starts_at: new Date(form.starts_at.value).toISOString(), ends_at: new Date(form.ends_at.value).toISOString(),
        structure_ids: some ? ids : null,
      };
      await Session.api(a ? `/api/admin/announcements/${a.id}` : "/api/admin/announcements", { method: a ? "PUT" : "POST", body });
      loadAnnouncements();
      Session.init();     // bandeau de cette page
    },
  });
}

$("announcement-add").addEventListener("click", () => editAnnouncement());
$("announcements-list").addEventListener("click", async e => {
  const act = e.target.closest("[data-ann]")?.dataset.ann;
  if (!act) return;
  const a = announcements.find(x => x.id === Number(e.target.closest("li").dataset.id));
  if (act === "edit") editAnnouncement(a);
  if (act === "delete" && confirm("Supprimer ce bandeau ?")) {
    try {
      await Session.api(`/api/admin/announcements/${a.id}`, { method: "DELETE" });
      loadAnnouncements();
      Session.init();
    } catch (err) {
      flash(esc(err.message));
    }
  }
});

// E-mail aux administrateurs de structure
const broadcastForm = $("broadcast-form");
const broadcastIds = () => broadcastForm.scope.value === "some"
  ? [...broadcastForm.querySelectorAll("[name=structure]:checked")].map(c => Number(c.value)) : [];

async function loadBroadcastRecipients() {
  if (!$("broadcast-structures").dataset.ready) {
    $("broadcast-structures").innerHTML = structureChecks();
    $("broadcast-structures").dataset.ready = "1";
  }
  const ids = broadcastIds();
  const status = $("broadcast-recipients");
  if (broadcastForm.scope.value === "some" && !ids.length) {
    status.textContent = "Cochez au moins une structure.";
    return;
  }
  try {
    const r = await Session.api(`/api/admin/broadcast/recipients${ids.length ? "?" + ids.map(i => `structure_ids=${i}`).join("&") : ""}`);
    status.innerHTML = r.mail_enabled
      ? `<strong>${r.recipients.length}</strong> destinataire(s) : ${r.recipients.map(x => `<span title="${esc(x.email)} · ${esc(x.structures)}">${esc(x.name)}</span>`).join(", ") || "aucun"}.`
      : `<span class="warn">Envoi d'e-mails non configuré sur le serveur (${esc(r.mail_disabled_reason || "")}).</span>`;
    broadcastForm.querySelector("[type=submit]").disabled = !r.mail_enabled || !r.recipients.length;
  } catch (e) {
    status.textContent = e.message;
  }
}

broadcastForm.addEventListener("change", e => { if (e.target.name === "scope" || e.target.name === "structure") loadBroadcastRecipients(); });
broadcastForm.addEventListener("submit", async e => {
  e.preventDefault();
  const status = $("broadcast-status");
  status.textContent = "";
  if (!confirm("Envoyer cet e-mail maintenant ?")) return;
  const btn = broadcastForm.querySelector("[type=submit]");
  btn.disabled = true;
  try {
    const ids = broadcastIds();
    const r = await Session.api("/api/admin/broadcast", {
      method: "POST", body: { subject: broadcastForm.subject.value.trim(), body: broadcastForm.body.value, structure_ids: ids.length ? ids : null },
    });
    status.textContent = `Envoyé à ${r.sent} administrateur(s)` + (r.failed.length ? ` ; échec pour ${r.failed.map(f => f.email).join(", ")}.` : ".");
    broadcastForm.subject.value = "";
    broadcastForm.body.value = "";
  } catch (err) {
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Exploitation (super administrateur) : maintenance, tâches automatiques, sauvegardes, e-mails, qualité
// ---------------------------------------------------------------------------

let maintenanceState = null;
const JOB_STATUS = { queued: "en attente", running: "en cours", succeeded: "réussie", failed: "échec", cancelled: "annulée" };

async function loadMaintenance() {
  try {
    maintenanceState = await Session.api("/api/admin/maintenance");
  } catch (e) {
    $("maintenance-state").textContent = e.message;
    return;
  }
  const m = maintenanceState;
  $("maintenance-state").innerHTML = m.enabled
    ? `<span class="tag tag-warn">activé</span> depuis le ${stamp(m.since)} : seuls les super administrateurs peuvent modifier.`
    : `<span class="tag tag-quiet">désactivé</span> : l'application fonctionne normalement.`;
  if (document.activeElement !== $("maintenance-message")) $("maintenance-message").value = m.enabled ? m.message : "";
  $("maintenance-toggle").textContent = m.enabled ? "Désactiver le mode maintenance" : "Activer le mode maintenance";
  $("maintenance-toggle").className = m.enabled ? "btn-primary btn-small" : "btn-danger btn-small";
}

$("maintenance-toggle").addEventListener("click", async () => {
  const enable = !maintenanceState?.enabled;
  if (enable && !confirm("Passer l'application en lecture seule pour tous sauf les super administrateurs ?")) return;
  try {
    await Session.api("/api/admin/maintenance", { method: "PUT", body: { enabled: enable, message: $("maintenance-message").value.trim() || null } });
    await loadMaintenance();
    Session.init();       // bandeau
  } catch (e) {
    flash(esc(e.message));
  }
});

async function loadSchedule() {
  let data;
  try {
    data = await Session.api("/api/admin/schedule");
  } catch (e) {
    $("schedule-body").innerHTML = `<tr><td colspan="5" class="empty">${esc(e.message)}</td></tr>`;
    return;
  }
  $("schedule-body").innerHTML = data.entries.map(e => {
    const last = e.last_job
      ? `<span class="tag job-${e.last_job.status}">${JOB_STATUS[e.last_job.status] || e.last_job.status}</span> ${stamp(e.last_job.finished_at || e.last_job.created_at)}
         <span class="muted">(${esc(e.last_job.created_by)})</span>` : "";
    const backup = e.last_backup ? `${last ? "<br>" : ""}dernier fichier : ${stamp(e.last_backup.created_at)}` : "";
    return `<tr data-key="${e.key}">
      <th scope="row">${esc(e.label)}<br><code class="muted cron">${esc(e.cron)}</code></th>
      <td data-label="Quand">${esc(e.when)}</td>
      <td data-label="Prochaine">${e.next_run ? stamp(e.next_run) : "–"}</td>
      <td data-label="Dernière">${last || backup ? last + backup : '<span class="muted">—</span>'}</td>
      <td class="actions"><button type="button" class="btn-quiet" data-run="${e.key}">Lancer maintenant</button></td>
    </tr>`;
  }).join("");
}

$("schedule-body").addEventListener("click", async e => {
  const key = e.target.closest("[data-run]")?.dataset.run;
  if (!key) return;
  const label = e.target.closest("tr").querySelector("th").firstChild.textContent;
  if (!confirm(`Lancer maintenant « ${label} » ?`)) return;
  try {
    const r = await Session.api(`/api/admin/schedule/${key}/run`, { method: "POST" });
    flash(r.queued.length ? `${r.queued.length} tâche(s) mise(s) en file : suivez-les dans « Données et tâches ».`
      : "Rien de nouveau mis en file (déjà en attente, ou rien à faire).");
    loadSchedule();
  } catch (err) {
    flash(esc(err.message));
  }
});

async function loadBackups() {
  let data;
  try {
    data = await Session.api("/api/admin/backups");
  } catch (e) {
    $("backups-summary").textContent = e.message;
    return;
  }
  $("backups-dir").textContent = data.directory;
  $("backups-restore").textContent = data.restore_command;
  $("backups-summary").textContent = `${data.backups.length} sauvegarde(s) (${data.keep} gardées au plus) · base : ${fmtBytes(data.database_bytes)} · espace libre : ${fmtBytes(data.free_bytes)}.`;
  $("backups-list").innerHTML = data.backups.length ? data.backups.map((b, i) => `
    <li data-name="${esc(b.name)}">
      <span><strong>${stamp(b.created_at)}</strong>${i === 0 ? ' <span class="tag">la plus récente</span>' : ""}
        <span class="muted">· ${esc(b.name)} · ${fmtBytes(b.size)}</span><span class="backup-check"></span></span>
      <span><a class="btn-quiet btn-small" href="/api/admin/backups/${encodeURIComponent(b.name)}" download>Télécharger</a>
        <button type="button" class="btn-quiet btn-small" data-verify>Vérifier</button></span>
    </li>`).join("") : `<li class="muted">Aucune sauvegarde pour l'instant.</li>`;
}

$("backups-list").addEventListener("click", async e => {
  if (!e.target.closest("[data-verify]")) return;
  const li = e.target.closest("li");
  const out = li.querySelector(".backup-check");
  out.textContent = " · vérification…";
  try {
    const r = await Session.api(`/api/admin/backups/${encodeURIComponent(li.dataset.name)}/verify`, { method: "POST" });
    out.innerHTML = r.ok
      ? ` · <span class="tag">intègre</span> <span class="muted">${r.counts.users} comptes, ${r.counts.structures} structures, ${r.counts.ports} ports, ${r.counts.slot_selections} créneaux</span>`
      : ` · <span class="tag tag-error">invalide</span> ${esc(r.problems.join(" ; "))}`;
  } catch (err) {
    out.textContent = ` · ${err.message}`;
  }
});

$("backup-now").addEventListener("click", async () => {
  try {
    const r = await Session.api("/api/admin/backups", { method: "POST" });
    flash(r.queued ? "Sauvegarde mise en file : elle apparaîtra ici une fois faite par le worker." : "Une sauvegarde est déjà en file.");
  } catch (e) {
    flash(esc(e.message));
  }
});

async function loadMailLog() {
  let data;
  try {
    data = await Session.api(`/api/admin/mail-log?limit=100${$("mail-failed-only").checked ? "&failed=true" : ""}`);
  } catch (e) {
    $("mail-config").textContent = e.message;
    return;
  }
  $("mail-config").innerHTML = (data.enabled
    ? `<span class="tag">configuré</span> envoi ${esc(data.backend)}${data.sender ? ` depuis ${esc(data.sender)}` : ""}.`
    : `<span class="tag tag-warn">non configuré</span> ${esc(data.disabled_reason || "")}.`)
    + ` 30 derniers jours : ${data.last_30_days.total} envoi(s)${data.last_30_days.failed ? `, <strong>${data.last_30_days.failed} échec(s)</strong>` : ""}.`;
  $("mail-test").disabled = !data.enabled;
  $("mail-log-body").innerHTML = data.entries.length ? data.entries.map(m => `
    <tr><td data-label="Date">${stamp(m.at)}</td><td data-label="Destinataire">${esc(m.recipient)}</td>
      <td data-label="Objet">${esc(m.subject)}</td>
      <td data-label="État">${m.status === "sent" ? '<span class="tag">envoyé</span>' : `<span class="tag tag-error">échec</span> ${esc(m.error || "")}`}</td></tr>`).join("")
    : `<tr><td colspan="4" class="empty">Aucun e-mail.</td></tr>`;
}

$("mail-failed-only").addEventListener("change", loadMailLog);
$("mail-test").addEventListener("click", async () => {
  try {
    const r = await Session.api("/api/admin/mail-test", { method: "POST" });
    flash(`E-mail de test envoyé à ${esc(r.sent_to)}.`);
  } catch (e) {
    flash(esc(e.message));
  }
  loadMailLog();
});

async function loadQuality() {
  let data;
  try {
    data = await Session.api("/api/admin/quality");
  } catch (e) {
    $("quality-body").innerHTML = `<tr><td colspan="5" class="empty">${esc(e.message)}</td></tr>`;
    return;
  }
  const m = v => (v == null ? "–" : `${fmtNum(v, 2)} m`);
  $("quality-body").innerHTML = data.ports.length ? data.ports.map(p => {
    const c = p.calibration, cmp = p.comparison;
    return `<tr${p.issues.some(i => i.level === "error") ? ' class="dash-warn"' : ""}>
      <th scope="row">${esc(p.name)}</th>
      <td data-label="Niveau moyen">${m(p.offset_zh_m)}${p.fes_range_m ? `<br><span class="muted">PM/BM ${fmtNum(p.fes_range_m[0], 2)} à ${fmtNum(p.fes_range_m[1], 2)} m</span>` : ""}</td>
      <td data-label="Recalage">${c ? `niveau ${m(c.mean_level_m)}${c.level_diff_m != null ? ` (${c.level_diff_m > 0 ? "+" : ""}${fmtNum(c.level_diff_m, 2)})` : ""}<br><span class="muted">${stamp(c.computed_at)}</span>` : '<span class="muted">aucun</span>'}</td>
      <td data-label="FES / api-maree.fr">${cmp ? `${fmtNum(cmp.mean_time_diff_min, 1)} min, ${cmp.mean_height_diff_m > 0 ? "+" : ""}${fmtNum(cmp.mean_height_diff_m, 2)} m<br><span class="muted">${cmp.pairs} PM/BM</span>` : '<span class="muted">–</span>'}</td>
      <td class="dash-issues" data-label="Points d'attention">${p.issues.length ? p.issues.map(i => `<span class="tag ${i.level === "error" ? "tag-error" : "tag-warn"}">${esc(i.message)}</span>`).join(" ") : '<span class="muted">—</span>'}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="empty">Aucun port.</td></tr>`;
  $("quality-sites").innerHTML = data.sites_without_current.length ? data.sites_without_current.map(s => `
    <li><span><strong>${esc(s.name)}</strong> <span class="muted">· ${esc(s.structure)}</span><br><span class="muted">${esc(s.reason)}</span></span></li>`).join("")
    : `<li class="muted">Tous les sites ont leur courant.</li>`;
}

// ---------------------------------------------------------------------------
// Journal d'activité
// ---------------------------------------------------------------------------

const JOURNAL_PAGE = 100;
let journalLast = null;     // id de la dernière ligne affichée (page suivante)

function journalQS(before) {
  const p = new URLSearchParams({ limit: JOURNAL_PAGE });
  if (isSuper() && $("journal-structure").value) p.set("structure_id", $("journal-structure").value);
  const q = $("journal-q").value.trim();
  if (q) p.set("q", q);
  if (before) p.set("before_id", before);
  return p.toString();
}

async function loadJournal({ more = false } = {}) {
  const list = $("journal-list");
  let rows;
  try {
    rows = await Session.api(`/api/admin/audit?${journalQS(more ? journalLast : null)}`);
  } catch (e) {
    list.innerHTML = `<li class="muted">${esc(e.message)}</li>`;
    return;
  }
  const html = rows.map(journalLine).join("");
  if (more) list.insertAdjacentHTML("beforeend", html);
  else list.innerHTML = html || `<li class="muted">Aucune action enregistrée.</li>`;
  if (rows.length) journalLast = rows[rows.length - 1].id;
  $("journal-more").hidden = rows.length < JOURNAL_PAGE;
}

$("journal-filter").addEventListener("submit", e => { e.preventDefault(); loadJournal(); });
$("journal-structure").addEventListener("change", () => loadJournal());
$("journal-more").addEventListener("click", () => loadJournal({ more: true }));

// Listes déroulantes de structures (types, création de compte, filtre des comptes)
function renderStructureSelects() {
  const opts = (selected, extra = "") => extra + structures.map(st =>
    `<option value="${st.id}"${st.id === selected ? " selected" : ""}>${esc(st.name)}</option>`).join("");

  const typesSel = $("types-structure");
  const keepTypes = typesScope ?? Session.user?.structure?.id ?? structures[0]?.id ?? null;
  typesSel.innerHTML = opts(keepTypes);
  typesScope = typesSel.value ? Number(typesSel.value) : null;

  const ficheSel = $("fiche-structure");
  ficheSel.innerHTML = opts(ficheScope ?? keepTypes);
  ficheScope = ficheSel.value ? Number(ficheSel.value) : null;

  const homeSel = $("home-structure");
  homeSel.innerHTML = opts(homeScope ?? keepTypes);
  homeScope = homeSel.value ? Number(homeSel.value) : null;

  const sitesSel = $("sites-structure");
  sitesSel.innerHTML = opts(sitesScope ?? keepTypes);
  sitesScope = sitesSel.value ? Number(sitesSel.value) : null;

  const jSel = $("journal-structure");
  const keepJournal = jSel.value;
  jSel.innerHTML = `<option value="">Toutes</option>` + opts(null);
  jSel.value = keepJournal;

  const mjSel = $("mailjet-structure");
  mjSel.innerHTML = opts(mailjetScope ?? keepTypes);
  mailjetScope = mjSel.value ? Number(mjSel.value) : null;

  // création de compte : garde le choix en cours (y compris « Aucune »), sinon la structure du super admin
  const newSel = $("new-structure");
  const keepNew = newSel.options.length
    ? (newSel.value ? Number(newSel.value) : null)
    : (Session.user?.structure?.id ?? structures[0]?.id ?? null);
  newSel.innerHTML = opts(keepNew, `<option value="">Aucune (super administrateur seulement)</option>`);
  syncCreateRole();

  const importSel = $("import-structure");
  const keepImport = importSel.options.length ? importSel.value : String(Session.user?.structure?.id ?? "");
  importSel.innerHTML = `<option value="">Aucune (colonne « structure » obligatoire)</option>` + opts(null);
  importSel.value = structures.some(st => String(st.id) === keepImport) ? keepImport : (structures[0] ? String(structures[0].id) : "");

  const inviteSel = $("invite-structure");
  const keepInvite = inviteSel.value ? Number(inviteSel.value) : (Session.user?.structure?.id ?? structures[0]?.id ?? null);
  inviteSel.innerHTML = opts(keepInvite);

  const filter = $("users-filter");
  const keepFilter = filter.value;
  filter.innerHTML = `<option value="">Toutes les structures</option>` + opts(null);
  filter.value = structures.some(st => String(st.id) === keepFilter) ? keepFilter : "";
}

structureForm.addEventListener("submit", async e => {
  e.preventDefault();
  const st = $("structure-form-status");
  st.textContent = "";
  try {
    const created = await Session.api("/api/admin/structures", { method: "POST", body: { name: structureForm.name.value.trim() } });
    st.textContent = `« ${created.name} » créée. Ajoutez-lui au moins un compte en administration.`;
    structureForm.reset();
    loadStructures();
  } catch (err) {
    st.textContent = err.message;
  }
});

// Recherche proposée aux membres de chaque structure (super administrateurs)
const SEARCH_MODES = { tides: "Par étale", heights: "Par hauteur d'eau", both: "Les deux" };

// Fonctions activables par structure (accounts.FEATURES)
const FEATURES = {
  currents: "Courants et sites de plongée",
  map: "Carte des ports et des sites",
  newsletters: "Newsletters",
  divers: "Fiches plongeurs et certificats médicaux (CACI)",
};
const FEATURE_SHORT = { currents: "courants", map: "carte", newsletters: "newsletters", divers: "fiches plongeurs" };

function featureTags(st) {
  const off = Object.keys(FEATURES).filter(f => st.features && !st.features[f]);
  return off.length ? `<br><span class="muted feature-off" title="Fonctions désactivées">sans ${off.map(f => FEATURE_SHORT[f]).join(", ")}</span>` : "";
}

function editFeatures(st) {
  openDialog({
    title: `Fonctions de ${st.name}`,
    body: `<p class="hint">Les fonctions décochées disparaissent pour les membres et les administrateurs de la structure
        (menus, pages, API) ; leurs données sont conservées. La recherche par hauteur d'eau se règle dans la colonne
        « Recherche » ; le contrôle du CACI, par la structure elle-même.</p>
      ${Object.entries(FEATURES).map(([f, label]) => `
        <label class="check"><input type="checkbox" name="feature" value="${f}"${st.features?.[f] !== false ? " checked" : ""}> ${esc(label)}</label>`).join("")}`,
    onSubmit: async form => {
      const features = [...form.querySelectorAll("[name=feature]:checked")].map(c => c.value);
      await Session.api(`/api/admin/structures/${st.id}/settings`, { method: "PATCH", body: { features } });
      flash(`Fonctions de « ${esc(st.name)} » enregistrées.`);
      loadStructures();
      if (st.id === Session.user.structure?.id) Session.init();
    },
  });
}

async function transferMembers(st) {
  let members;
  try {
    members = await Session.api(`/api/admin/users?structure_id=${st.id}`);
  } catch (err) {
    flash(esc(err.message));
    return;
  }
  members = members.filter(u => u.role);
  const others = structures.filter(x => x.id !== st.id);
  if (!others.length || !members.length) {
    flash(!members.length ? `« ${esc(st.name)} » n'a aucun membre.` : "Aucune autre structure.");
    return;
  }
  const form = openDialog({
    title: `Transférer des membres de ${st.name}`,
    submitLabel: "Transférer",
    body: `
      <label>Vers <select name="to">${others.map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select></label>
      <fieldset class="transfer-mode">
        <label class="check"><input type="radio" name="mode" value="copy" checked> Ajouter à l'autre structure (ils restent aussi membres de ${esc(st.name)})</label>
        <label class="check"><input type="radio" name="mode" value="move"> Déplacer (retirés de ${esc(st.name)}, avec leurs inscriptions à ses créneaux)</label>
      </fieldset>
      <p><label class="check"><input type="checkbox" data-all> Tout cocher (${members.length})</label></p>
      <div class="transfer-list">${members.map(u => `
        <label class="check"><input type="checkbox" name="user" value="${u.id}"> ${esc(u.display_name)}
          <span class="muted">${u.role === "manager" ? "administration" : "visualisation"}</span></label>`).join("")}</div>
      <p class="hint">Rôle et profils sont gardés ; s'ils sont déjà membres de l'autre structure, le rôle le plus élevé l'emporte.</p>`,
    onSubmit: async f => {
      const user_ids = [...f.querySelectorAll("[name=user]:checked")].map(c => Number(c.value));
      if (!user_ids.length) throw new Error("Cochez au moins un membre.");
      const r = await Session.api(`/api/admin/structures/${st.id}/transfer`, {
        method: "POST", body: { to_structure_id: Number(f.to.value), user_ids, move: f.mode.value === "move" },
      });
      flash(`${r.transferred} membre(s) ${f.mode.value === "move" ? "déplacé(s)" : "ajouté(s)"} vers « ${esc(r.to.name)} ».`);
      loadStructures();
    },
  });
  form.querySelector("[data-all]").addEventListener("change", e => {
    for (const c of form.querySelectorAll("[name=user]")) c.checked = e.target.checked;
  });
}

structuresBody.addEventListener("change", async e => {
  if (e.target.dataset.act !== "search-modes") return;
  const st = structures.find(x => x.id === Number(e.target.closest("tr").dataset.id));
  try {
    const saved = await Session.api(`/api/admin/structures/${st.id}/settings`, {
      method: "PATCH", body: { search_modes: e.target.value },
    });
    st.search_modes = saved.search_modes;
    flash(`« ${esc(st.name)} » : recherche ${esc(SEARCH_MODES[saved.search_modes].toLowerCase())}.`);
    if (st.id === Session.user.structure?.id) Session.init();   // liens de l'en-tête
  } catch (err) {
    e.target.value = st.search_modes || "tides";
    flash(esc(err.message));
  }
});

structuresBody.addEventListener("click", async e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const st = structures.find(x => x.id === Number(btn.closest("tr").dataset.id));
  switch (btn.dataset.act) {
    case "members":
      $("users-filter").value = String(st.id);
      showTab("utilisateurs");
      break;
    case "types":
      typesScope = st.id;
      $("types-structure").value = String(st.id);
      showTab("types");
      break;
    case "features":
      editFeatures(st);
      break;
    case "transfer":
      transferMembers(st);
      break;
    case "archive":
    case "unarchive":
      if (btn.dataset.act === "archive" && !confirm(`Archiver « ${st.name} » ? Ses membres n'y auront plus accès (créneaux, administration) ; ses données sont gardées et elle peut être réactivée.`)) return;
      try {
        await Session.api(`/api/admin/structures/${st.id}/archive`, { method: btn.dataset.act === "archive" ? "POST" : "DELETE" });
        flash(`« ${esc(st.name)} » ${btn.dataset.act === "archive" ? "archivée" : "réactivée"}.`);
        loadStructures();
      } catch (err) {
        flash(esc(err.message));
      }
      break;
    case "rename":
      openDialog({
        title: `Renommer ${st.name}`,
        body: `<label>Nom <input name="name" required maxlength="80" value="${esc(st.name)}"></label>`,
        onSubmit: async form => {
          await Session.api(`/api/admin/structures/${st.id}`, { method: "PATCH", body: { name: form.name.value.trim() } });
          loadStructures();
          if (st.id === Session.user.structure?.id) Session.init();  // nom affiché dans l'en-tête
        },
      }).name.select();
      break;
    case "delete":
      if (!confirm(`Supprimer « ${st.name} », ses ${st.types} type(s) et ses ${st.selections} créneau(x) choisi(s) ?`)) return;
      try {
        await Session.api(`/api/admin/structures/${st.id}`, { method: "DELETE" });
        flash(`« ${esc(st.name)} » supprimée.`);
        if (typesScope === st.id) typesScope = null;
        loadStructures();
      } catch (err) {
        flash(esc(err.message));
      }
      break;
  }
});

// ---------------------------------------------------------------------------
// Ports
// ---------------------------------------------------------------------------

let ports = [];
let catalog = [];
const portsBody = $("ports-body");
const portForm = $("port-form");
const catalogSelect = $("port-catalog");
const defaultYear = new Date().getMonth() >= 10 ? new Date().getFullYear() + 1 : new Date().getFullYear();
$("annual-year").value = defaultYear;

let waterThresholds = [];   // hauteurs d'eau de tous les ports (recherche par hauteur d'eau)
let diveSites = [];         // sites de plongée de toutes les structures (carte des ports, super administrateur)

async function loadPorts() {
  try {
    [ports, catalog, waterThresholds, diveSites] = await Promise.all([
      Session.api("/api/admin/ports"),
      Session.api("/api/admin/ports/catalog"),
      Session.api("/api/admin/water-thresholds"),
      Session.api("/api/admin/dive-sites"),
    ]);
  } catch (e) {
    flash(esc(e.message));
    return;
  }
  renderPorts();
  loadCurrents();
  catalogSelect.innerHTML = `<option value="">Point personnalisé (saisie libre)</option>` +
    (catalog.length ? `<optgroup label="Catalogue">${catalog.map((p, i) =>
      `<option value="${i}">${esc(p.name)}${p.offset_zh_m ? "" : " (niveau moyen à renseigner)"}</option>`).join("")}</optgroup>` : "");
}

// Année calculée : son modèle en info-bulle, signalée si ce n'est pas le modèle actuel
function yearTag(year, model) {
  const current = status?.fes_model;
  if (!model) return `<span class="tag" title="Modèle non enregistré (calcul antérieur)">${year}</span>`;
  if (current && model !== current) {
    return `<span class="tag tag-stale" title="Calculée avec ${esc(model)} ; modèle actuel : ${esc(current)}. Relancez le calcul pour la mettre à jour.">${year} · ${esc(model)}</span>`;
  }
  return `<span class="tag" title="Calculée avec ${esc(model)}">${year}</span>`;
}

const fmtMin = v => (v == null ? "—" : `${fmtNum(v, 1)} min`);

// Motif empêchant de lancer un recalage, ou "" s'il est possible
function calibrateBlock(p) {
  if (!p.api_maree_site) return "Renseignez d'abord le site api-maree.fr (Modifier)";
  if (status && !status.api_maree_configured) return "Clé api-maree.fr absente : API_MAREE_KEY dans .env";
  return "";
}

const fmtDay = iso => new Date(iso).toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" });

// Mois glissant repris d'api-maree.fr : fin de la fenêtre, signalée si pas rafraîchie depuis 2 jours
function shortTermCell(p) {
  const w = p.short_term;
  if (!w) return p.api_maree_site ? `<span class="muted">30 j : à faire</span>` : "";
  const stale = Date.now() - new Date(w.refreshed_at).getTime() > 2 * 86400e3;
  const lastDay = new Date(new Date(w.window_end).getTime() - 1);   // fin exclue
  const title = [
    `Horaires repris d'api-maree.fr (${w.site}) du ${fmtDay(w.window_start)} au ${fmtDay(lastDay)}, mis à jour le ${new Date(w.refreshed_at).toLocaleString("fr-FR")}`,
    `${w.n_extrema} pleines / basses mers ; plus grand écart avec le calcul FES remplacé : ${fmtMin(w.max_shift_min)}`,
    `Hauteur moyenne api-maree.fr − FES : ${w.level_diff_m == null ? "—" : `${fmtNum(w.level_diff_m, 2)} m`}`,
    stale ? "Pas de mise à jour depuis plus de 2 jours : voir les tâches." : "",
  ].filter(Boolean).join("\n");
  return `<span class="tag${stale ? " tag-stale" : ""}" title="${esc(title)}">30 j → ${fmtDay(lastDay)}</span>`;
}

// Recalage : décalage et amplitude, détail des écarts en info-bulle
function calibrationCell(p) {
  const c = p.calibration;
  if (!c) return p.api_maree_site
    ? `<span class="muted" title="Site ${esc(p.api_maree_site)}">à faire</span>`
    : `<span class="muted">aucun</span>`;
  const legacy = !c.waves.length;   // ancien recalage : décalage et amplitude uniques
  const title = [
    `Site api-maree.fr : ${c.site}, modèle ${c.model}, le ${c.computed_at.slice(0, 10)}`,
    legacy
      ? `Ancien recalage : décalage ${fmtNum(c.time_shift_min, 0)} min, amplitude × ${fmtNum(c.amplitude, 3)}. Relancez-le pour une correction onde par onde.`
      : `Correction par onde : ${c.waves.map(w => `${w.name} ${fmtNum(100 * w.amplitude_m, 0)} cm`).join(", ")}`,
    `Écart moyen des heures de PM/BM sur la fenêtre api-maree.fr : ${fmtMin(c.extrema_dt_before_min)} → ${fmtMin(c.extrema_dt_after_min)}`,
    `Écart quadratique des hauteurs : ${fmtNum(c.rmse_before_m, 3)} m → ${fmtNum(c.rmse_after_m, 3)} m`,
    `Niveau moyen de la référence : ${fmtNum(c.mean_level_m, 2)} m`,
  ].join("\n");
  const stale = legacy || (status?.fes_model && c.model !== status.fes_model);
  const label = `FES recalé : ${fmtMin(c.extrema_dt_before_min)} → ${fmtMin(c.extrema_dt_after_min)}`;
  return `<span class="tag${stale ? " tag-stale" : ""}" title="${esc(title)}${stale && !legacy ? `\nÉtabli pour ${esc(c.model)} : relancez le recalage.` : ""}">${label}</span>`;
}

function renderPorts() {
  portsBody.innerHTML = ports.length ? ports.map(p => {
    const years = p.years.length
      ? p.years.map(y => yearTag(y, p.year_models[y])).join(" ")
      : `<span class="muted">aucune</span>`;
    const calib = calibrationCell(p);
    const offset = p.offset_zh_m != null
      ? `${fmtNum(p.offset_zh_m, 2)} m`
      : `<span class="tag job-failed" title="Obligatoire pour calculer">à renseigner</span>`;
    return `
      <tr data-id="${p.id}">
        <th scope="row">${esc(p.name)}</th>
        <td class="muted" data-label="Coordonnées">${fmtNum(p.latitude, 4)}, ${fmtNum(p.longitude, 4)}</td>
        <td class="num" data-label="NM / ZH">${offset}</td>
        <td data-label="api-maree.fr">${shortTermCell(p)} ${calib}</td>
        <td data-label="Années calculées">${years}</td>
        <td data-label="Recalcul annuel"><input type="checkbox" data-act="auto" ${p.auto_precompute ? "checked" : ""} aria-label="Recalcul annuel de ${esc(p.name)}"></td>
        <td class="actions">
          <input type="number" class="year-input" min="1990" max="2100" value="${defaultYear}" aria-label="Année à calculer">
          <button type="button" class="btn-secondary" data-act="compute" ${p.offset_zh_m == null ? "disabled title=\"Renseignez d'abord le niveau moyen\"" : ""}>Calculer</button>
          <button type="button" class="btn-quiet" data-act="short_term" ${calibrateBlock(p) ? `disabled title="${esc(calibrateBlock(p))}"` : `title="Reprendre maintenant les horaires de J−1 à J+29 depuis api-maree.fr (fait chaque jour à 5 h)"`}>30 jours</button>
          <button type="button" class="btn-quiet" data-act="calibrate" ${calibrateBlock(p) ? `disabled title="${esc(calibrateBlock(p))}"` : `title="Recaler le calcul FES (long terme) sur api-maree.fr"`}>Recaler</button>
          <button type="button" class="btn-quiet" data-act="water" title="Hauteurs d'eau de la recherche par hauteur d'eau">Hauteurs d'eau${waterCount(p) ? ` (${waterCount(p)})` : ""}</button>
          <button type="button" class="btn-quiet" data-act="edit">Modifier</button>
          <button type="button" class="btn-danger" data-act="delete">Supprimer</button>
        </td>
      </tr>`;
  }).join("")
    : `<tr><td colspan="7" class="empty">Aucun port. Ajoutez-en un depuis le catalogue ci-dessus.</td></tr>`;
  renderPortsMap();   // la carte suit les ports et les sites
}

catalogSelect.addEventListener("change", () => {
  const p = catalog[catalogSelect.value];
  portForm.name.value = p ? p.name : "";
  portForm.latitude.value = p ? p.latitude : "";
  portForm.longitude.value = p ? p.longitude : "";
  portForm.offset_zh_m.value = p?.offset_zh_m ?? "";
  (p && !p.offset_zh_m ? portForm.offset_zh_m : portForm.name).focus();
});

const numOrNull = v => (v === "" ? null : Number(v));

portForm.addEventListener("submit", async e => {
  e.preventDefault();
  const status = $("port-form-status");
  status.textContent = "";
  try {
    const p = await Session.api("/api/admin/ports", {
      method: "POST",
      body: {
        name: portForm.name.value.trim(),
        latitude: Number(portForm.latitude.value),
        longitude: Number(portForm.longitude.value),
        offset_zh_m: numOrNull(portForm.offset_zh_m.value),
        auto_precompute: portForm.auto_precompute.checked,
        api_maree_site: portForm.api_maree_site.value.trim() || null,
      },
    });
    portForm.reset();
    status.textContent = p.offset_zh_m != null
      ? `« ${p.name} » ajouté. Lancez son calcul dans le tableau.`
      : `« ${p.name} » ajouté, sans niveau moyen : renseignez-le avant de calculer.`;
    loadPorts();
  } catch (err) {
    status.textContent = err.message;
  }
});

portsBody.addEventListener("change", async e => {
  if (e.target.dataset.act !== "auto") return;
  const id = Number(e.target.closest("tr").dataset.id);
  try {
    await Session.api(`/api/admin/ports/${id}`, { method: "PATCH", body: { auto_precompute: e.target.checked } });
  } catch (err) {
    e.target.checked = !e.target.checked;
    flash(esc(err.message));
  }
});

portsBody.addEventListener("click", async e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const row = btn.closest("tr");
  const port = ports.find(p => p.id === Number(row.dataset.id));
  switch (btn.dataset.act) {
    case "compute": {
      const year = Number(row.querySelector(".year-input").value);
      if (!year) return;
      if (port.years.includes(year) && !confirm(`${year} est déjà calculée pour « ${port.name} ». La recalculer ?`)) return;
      enqueue("precompute", { port_id: port.id, year });
      break;
    }
    case "calibrate":
    case "short_term":
      enqueue(btn.dataset.act, { port_id: port.id });
      break;
    case "water":
      openWaterThresholds(port);
      break;
      break;
    case "edit":
      editPort(port);
      break;
    case "delete":
      if (!confirm(`Supprimer « ${port.name} » et toutes ses données calculées (${port.years.join(", ") || "aucune année"}) ?`)) return;
      try {
        await Session.api(`/api/admin/ports/${port.id}`, { method: "DELETE" });
        flash(`« ${esc(port.name)} » supprimé.`);
        loadPorts();
      } catch (err) {
        flash(esc(err.message));
      }
      break;
  }
});

// ---- Atlas de courants de marée du SHOM : zones téléchargées, téléchargement (tâche de fond), recalcul des sites ----

async function loadCurrents() {
  let data;
  try {
    data = await Session.api("/api/admin/currents");
  } catch (e) {
    $("currents-body").innerHTML = `<tr><td colspan="4" class="empty">${esc(e.message)}</td></tr>`;
    return;
  }
  const portNames = new Set(ports.map(p => p.name.toLowerCase()));
  $("currents-body").innerHTML = data.zones.map(z => {
    const ref = z.ref_port
      ? `${esc(z.ref_port)}${portNames.has(z.ref_port.toLowerCase()) ? "" : ` <span class="tag tag-pending" title="À ajouter dans les ports, avec ses marées calculées">absent</span>`}`
      : `<span class="muted">indiqué dans l'atlas</span>`;
    const state = z.downloaded
      ? `téléchargé le ${esc(new Date(z.updated_at).toLocaleDateString("fr-FR"))} <span class="muted">(${z.files.length} fichier${z.files.length > 1 ? "s" : ""})</span>`
      : `<span class="muted">non téléchargé · ${esc(z.size)}</span>`;
    return `<tr>
      <th scope="row">${esc(z.label)}</th>
      <td data-label="Port de référence">${ref}</td>
      <td data-label="État">${state}</td>
      <td class="actions"><button type="button" class="btn-quiet" data-zone="${esc(z.zone)}">${z.downloaded ? "Mettre à jour" : "Télécharger"}</button></td>
    </tr>`;
  }).join("");
  $("currents-attribution").textContent = `Source : ${data.attribution}. À n'utiliser qu'en complément des cartes et ouvrages nautiques officiels.`;
}

$("currents-body").addEventListener("click", e => {
  const btn = e.target.closest("[data-zone]");
  if (btn) enqueue("currents_atlas", { zone: btn.dataset.zone });
});

$("currents-refresh").addEventListener("click", async () => {
  const btn = $("currents-refresh");
  btn.disabled = true;
  try {
    const res = await Session.api("/api/admin/currents/sites", { method: "POST" });
    diveSites = res.sites;
    $("currents-report").textContent = res.report.length ? res.report.join(" · ") : "Aucun site de plongée.";
    renderPorts();
  } catch (e) {
    $("currents-report").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});

// ---- Carte des ports et des sites (onglet Ports) ----

let portsMap = null;   // { map, layer, fitted }

function renderPortsMap() {
  if (typeof L === "undefined") return;   // Leaflet absent : pas de carte, le reste fonctionne
  if (!portsMap) {
    const map = Carte.create($("ports-map"));
    portsMap = { map, layer: L.layerGroup().addTo(map), fitted: false };
    $("ports-map-legend").innerHTML = Carte.legend();
  }
  const { map, layer } = portsMap;
  layer.clearLayers();
  const points = [];
  for (const p of ports) {
    Carte.portMarker(p).bindPopup(`<strong>${esc(p.name)}</strong>`).addTo(layer);
    points.push([p.latitude, p.longitude]);
  }
  for (const s of diveSites) {
    Carte.siteMarker(s).bindPopup(`<strong>${esc(s.name)}</strong> (${esc(s.structure)})<br>`
      + (s.current ? `Courant : ${esc(s.current.atlas)}` : `<span class="muted">${esc(s.current_status || "pas encore de courant")}</span>`)).addTo(layer);
    points.push([s.lat, s.lon]);
  }
  if (!portsMap.fitted && points.length) {   // cadrage initial seulement : la vue choisie est gardée ensuite
    Carte.fit(map, points, { zoom: 11, maxZoom: 11 });
    portsMap.fitted = true;
  }
  Carte.refresh(map);
}

// Carte de saisie d'un site : les ports, les autres sites de la structure, et le marqueur du site placé d'un clic
// (remplit latitude / longitude ; il se déplace aussi par glisser-déposer ou en tapant les coordonnées).
// Cadrage : les sites existants, à défaut le port par défaut de la structure.
function mountPickMap(el, form, list, editing, { mapPorts = [], homePort = null } = {}) {
  if (!el) return;
  const map = Carte.create(el);
  for (const p of mapPorts) Carte.portMarker(p).addTo(map);
  const points = [];
  if (!list.length && homePort) points.push([homePort.latitude, homePort.longitude]);
  for (const x of list) {
    if (editing && x.id === editing.id) continue;
    Carte.siteMarker(x).addTo(map);
    points.push([x.lat, x.lon]);
  }
  let marker = null;
  const round = v => Math.round(v * 1e6) / 1e6;
  const setInputs = (lat, lon) => { form.lat.value = round(lat); form.lon.value = round(lon); };
  const place = (lat, lon) => {
    if (marker) { marker.setLatLng([lat, lon]); return; }
    marker = L.marker([lat, lon], { draggable: true, title: "Site en cours de saisie" }).addTo(map);
    marker.on("dragend", () => { const ll = marker.getLatLng(); setInputs(ll.lat, ll.lng); });
  };
  map.on("click", e => { place(e.latlng.lat, e.latlng.lng); setInputs(e.latlng.lat, e.latlng.lng); });
  for (const name of ["lat", "lon"]) {
    form[name].addEventListener("change", () => {
      const lat = Number(form.lat.value), lon = Number(form.lon.value);
      if (form.lat.value !== "" && form.lon.value !== "" && Math.abs(lat) <= 90 && Math.abs(lon) <= 180) {
        place(lat, lon);
        map.panTo([lat, lon]);
      }
    });
  }
  if (editing) { place(editing.lat, editing.lon); map.setView([editing.lat, editing.lon], 15); }
  else Carte.fit(map, points, { zoom: 13, maxZoom: 14 });
  Carte.refresh(map);
}

// ---- Sites de plongée de la structure (onglet Créneaux) : position GPS précise, courant de l'atlas du SHOM ----

let structureSites = [];   // sites de la structure affichée
let sitesScope = null;     // structure choisie (super administrateur)
let publicPorts = null;    // ports de la recherche, pour situer les sites (chargés une fois)
let sitesMap = null;       // { map, layer } : carte de l'onglet
const sitesQS = () => (isSuper() && sitesScope ? `?structure_id=${sitesScope}` : "");

$("sites-structure").addEventListener("change", e => {
  sitesScope = Number(e.target.value) || null;
  if (sitesMap) sitesMap.fitted = false;
  loadSites();
});

// Port par défaut de la structure affichée : cadre la carte quand elle n'a pas encore de site
function sitesHomePort() {
  const sid = isSuper() ? sitesScope : Session.user?.structure?.id;
  const st = structures.find(x => x.id === sid);
  const portId = st?.default_port_id ?? (sid === Session.user?.structure?.id ? Session.user?.structure?.default_port_id : null);
  return (publicPorts || []).find(p => p.id === portId) || null;
}
const siteCurrentNote = x => (x.current
  ? `<span class="tag" title="Atlas ${esc(x.current.atlas)}, port de référence ${esc(x.current.ref_port || "?")}">courant ✓</span>`
  : `<span class="muted">${esc(x.current_status || "pas encore de courant")}</span>`);

async function loadSites() {
  const panel = $("sites-panel");
  panel.hidden = isSuper() && !sitesScope;
  if (panel.hidden) return;
  try {
    if (publicPorts === null) publicPorts = await Session.api("/api/ports").catch(() => []);
    structureSites = await Session.api(`/api/admin/dive-sites${sitesQS()}`);
  } catch (e) {
    $("sites-list").innerHTML = `<li class="muted">${esc(e.message)}</li>`;
    return;
  }
  renderSitesPanel();
}

function renderSitesPanel() {
  $("sites-list").innerHTML = structureSites.length ? structureSites.map(x => `
    <li data-id="${x.id}">
      <span><strong>${esc(x.name)}</strong> · <span class="muted">${fmtNum(x.lat, 4)}, ${fmtNum(x.lon, 4)}</span> · ${siteCurrentNote(x)}${x.notes ? `<br><span class="muted">${esc(x.notes)}</span>` : ""}</span>
      <span><button type="button" class="btn-quiet btn-small" data-site="edit">Modifier</button>
      <button type="button" class="btn-danger btn-small" data-site="delete">Supprimer</button></span>
    </li>`).join("")
    : `<li class="muted">Aucun site de plongée pour l'instant.</li>`;
  renderSitesMap();
}

function renderSitesMap() {
  if (typeof L === "undefined") return;   // Leaflet absent : la liste suffit
  if (!sitesMap) {
    const map = Carte.create($("sites-map"));
    sitesMap = { map, layer: L.layerGroup().addTo(map), fitted: false };
  }
  const { map, layer } = sitesMap;
  layer.clearLayers();
  for (const p of publicPorts || []) Carte.portMarker(p).addTo(layer);
  const points = structureSites.map(x => [x.lat, x.lon]);
  for (const x of structureSites) {
    Carte.siteMarker(x).bindPopup(`<strong>${esc(x.name)}</strong><br>${siteCurrentNote(x)}`).addTo(layer);
  }
  if (!sitesMap.fitted) {   // cadrage initial : les sites, à défaut le port par défaut
    const home = sitesHomePort();
    if (points.length) Carte.fit(map, points, { zoom: 13, maxZoom: 13 });
    else if (home) map.setView([home.latitude, home.longitude], 11);
    sitesMap.fitted = true;
  }
  Carte.refresh(map);
}

async function reloadSites() {
  structureSites = await Session.api(`/api/admin/dive-sites${sitesQS()}`);
  renderSitesPanel();
}

$("sites-add").addEventListener("click", async () => {
  if (publicPorts === null) publicPorts = await Session.api("/api/ports").catch(() => []);
  openSiteDialog(null);
});

// Liste des sites : Modifier ouvre la fenêtre pré-remplie, Supprimer après confirmation
$("sites-list").addEventListener("click", async e => {
  const btn = e.target.closest("[data-site]");
  if (!btn) return;
  const x = structureSites.find(w => w.id === Number(btn.closest("li").dataset.id));
  if (!x) return;
  if (btn.dataset.site === "edit") {
    if (publicPorts === null) publicPorts = await Session.api("/api/ports").catch(() => []);
    openSiteDialog(x);
    return;
  }
  if (!confirm(`Supprimer le site « ${x.name} » ?`)) return;
  try {
    await Session.api(`/api/admin/dive-sites/${x.id}`, { method: "DELETE" });
    await reloadSites();
  } catch (e2) {
    flash(esc(e2.message));
  }
});

// Fenêtre d'ajout (site = null) ou de modification d'un site, pré-remplie : carte pour le placer d'un clic
function openSiteDialog(site) {
  const structureName = isSuper() ? $("sites-structure").selectedOptions[0]?.textContent : Session.user?.structure?.name;
  const t = site;
  const d = document.createElement("dialog");
  d.className = "account-dialog water-dialog";
  d.innerHTML = `
    <form method="dialog">
      <h2>${t ? `Modifier « ${esc(t.name)} »` : "Ajouter un site de plongée"}${structureName ? ` <span class="muted">— ${esc(structureName)}</span>` : ""}</h2>
      <p class="dialog-hint">Position GPS précise (degrés décimaux, ex. 48.6612 / -2.7845) : le courant de marée change beaucoup d'un point à l'autre. Il est extrait de l'atlas de courants du SHOM au point le plus proche ; déplacer un site le recalcule.</p>
      ${typeof L === "undefined" ? "" : `<div class="map map-pick" role="region" aria-label="Carte : cliquez pour placer le site"></div>
      <p class="map-hint">Cliquez sur la carte (fond « Photo aérienne » ou « Carte marine » pour repérer roches et épaves) pour placer le site : la latitude et la longitude se remplissent. Le marqueur se déplace aussi en le faisant glisser.</p>`}
      <fieldset class="water-form">
        <label>Nom <input name="name" maxlength="60" required placeholder="ex. Roches de Saint-Quay" value="${esc(t?.name ?? "")}"></label>
        <label>Latitude <input name="lat" type="number" step="0.000001" min="-90" max="90" required placeholder="48.6612" value="${t?.lat ?? ""}"></label>
        <label>Longitude <input name="lon" type="number" step="0.000001" min="-180" max="180" required placeholder="-2.7845" value="${t?.lon ?? ""}"></label>
        <label>Notes <input name="notes" maxlength="300" placeholder="facultatif (profondeur, mouillage…)" value="${esc(t?.notes ?? "")}"></label>
      </fieldset>
      <p class="dialog-error" role="alert"></p>
      <div class="dialog-actions">
        <button type="button" class="btn-quiet" value="cancel">Annuler</button>
        <button type="submit" class="btn-primary">${t ? "Enregistrer" : "Ajouter"}</button>
      </div>
    </form>`;
  document.body.append(d);
  d.addEventListener("close", () => d.remove());
  const form = d.querySelector("form");
  const err = d.querySelector(".dialog-error");
  d.querySelector("[value=cancel]").addEventListener("click", () => d.close());
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const body = { name: form.name.value.trim(), lat: Number(form.lat.value), lon: Number(form.lon.value),
                   notes: form.notes.value.trim() || null };
    if (!body.name || form.lat.value === "" || form.lon.value === "") { err.textContent = "Nom, latitude et longitude obligatoires."; return; }
    if (t && t.current && (t.lat !== body.lat || t.lon !== body.lon)
        && !confirm("Déplacer le site efface son courant (il valait pour l'ancienne position) et le recalcule. Continuer ?")) return;
    const btn = form.querySelector("[type=submit]");
    btn.disabled = true;
    try {
      await Session.api(t ? `/api/admin/dive-sites/${t.id}` : `/api/admin/dive-sites${sitesQS()}`,
        { method: t ? "PUT" : "POST", body });
      if (sitesMap && !t) sitesMap.fitted = false;   // nouveau site : la carte se recadre sur tous les sites
      await reloadSites();
      d.close();
    } catch (e2) {
      err.textContent = e2.message;
    } finally {
      btn.disabled = false;
    }
  });
  d.showModal();
  mountPickMap(d.querySelector(".map-pick"), form, structureSites, t, { mapPorts: publicPorts || [], homePort: sitesHomePort() });
  form.name.focus();
}

// ---- Hauteurs d'eau d'un port : la recherche par hauteur d'eau donne les plages au-dessus (ou au-dessous) ----

const waterCount = p => waterThresholds.filter(t => t.port_id === p.id).length;
const fmtHeightM = h => `${fmtNum(h, 2)} m`;
const DIRECTIONS = { above: "au moins (eau au-dessus)", below: "au plus (eau au-dessous)" };

function openWaterThresholds(port) {
  const d = document.createElement("dialog");
  d.className = "account-dialog water-dialog";
  document.body.append(d);
  d.addEventListener("close", () => d.remove());
  let editing = null;   // hauteur en cours de modification

  const render = () => {
    const list = waterThresholds.filter(t => t.port_id === port.id);
    const t = editing;
    d.innerHTML = `
      <form method="dialog">
        <h2>Hauteurs d'eau — ${esc(port.name)}</h2>
        <p class="dialog-hint">Hauteurs au-dessus du zéro des cartes, comme dans l'annuaire des marées. La recherche par hauteur d'eau donne les plages où l'eau est au-dessus (« au moins ») ou au-dessous (« au plus ») de chacune. Les créneaux déjà choisis gardent la hauteur du moment du choix.</p>
        <ul class="water-list">${list.length ? list.map(x => `
          <li data-id="${x.id}">
            <span><strong>${esc(x.label)}</strong> · ${x.direction === "above" ? "≥" : "≤"} ${fmtHeightM(x.height_m)}${x.uses ? ` <span class="muted">(${x.uses} créneau(x))</span>` : ""}</span>
            <span><button type="button" class="btn-quiet btn-small" data-water="edit">Modifier</button>
            <button type="button" class="btn-danger btn-small" data-water="delete">Supprimer</button></span>
          </li>`).join("") : `<li class="muted">Aucune hauteur d'eau pour ce port.</li>`}
        </ul>
        <fieldset class="water-form">
          <legend>${t ? `Modifier « ${esc(t.label)} »` : "Ajouter une hauteur d'eau"}</legend>
          <label>Libellé <input name="label" maxlength="60" placeholder="ex. Mise à l'eau à la cale" value="${esc(t?.label ?? "")}"></label>
          <label>Hauteur (m) <input name="height_m" type="number" step="0.01" min="-5" max="20" value="${t?.height_m ?? ""}"></label>
          <label>Sens <select name="direction">${Object.entries(DIRECTIONS).map(([v, l]) =>
            `<option value="${v}"${(t?.direction || "above") === v ? " selected" : ""}>${l}</option>`).join("")}</select></label>
        </fieldset>
        <p class="dialog-error" role="alert"></p>
        <div class="dialog-actions">
          <button type="button" class="btn-quiet" value="close">Fermer</button>
          ${t ? `<button type="button" class="btn-quiet" value="cancel-edit">Annuler la modification</button>` : ""}
          <button type="submit" class="btn-primary">${t ? "Enregistrer" : "Ajouter"}</button>
        </div>
      </form>`;
    const form = d.querySelector("form");
    const err = d.querySelector(".dialog-error");
    d.querySelector("[value=close]").addEventListener("click", () => d.close());
    d.querySelector("[value=cancel-edit]")?.addEventListener("click", () => { editing = null; render(); });
    form.addEventListener("submit", async e => {
      e.preventDefault();
      const body = { label: form.label.value.trim(), height_m: Number(form.height_m.value), direction: form.direction.value };
      if (!body.label || form.height_m.value === "") { err.textContent = "Libellé et hauteur obligatoires."; return; }
      try {
        await Session.api(t ? `/api/admin/water-thresholds/${t.id}` : `/api/admin/ports/${port.id}/water-thresholds`,
          { method: t ? "PUT" : "POST", body });
        waterThresholds = await Session.api("/api/admin/water-thresholds");
        editing = null;
        render();
        renderPorts();
      } catch (e2) {
        err.textContent = e2.message;
      }
    });
    d.querySelector(".water-list").addEventListener("click", async e => {
      const btn = e.target.closest("[data-water]");
      if (!btn) return;
      const x = waterThresholds.find(w => w.id === Number(btn.closest("li").dataset.id));
      if (btn.dataset.water === "edit") { editing = x; render(); d.querySelector("[name=label]").focus(); return; }
      if (!confirm(`Supprimer la hauteur d'eau « ${x.label} » ?${x.uses ? ` Ses ${x.uses} créneau(x) choisi(s) restent.` : ""}`)) return;
      try {
        await Session.api(`/api/admin/water-thresholds/${x.id}`, { method: "DELETE" });
        waterThresholds = await Session.api("/api/admin/water-thresholds");
        if (editing?.id === x.id) editing = null;
        render();
        renderPorts();
      } catch (e2) {
        err.textContent = e2.message;
      }
    });
  };
  render();
  d.showModal();
}

function editPort(port) {
  const d = document.createElement("dialog");
  d.className = "account-dialog";
  d.innerHTML = `
    <form method="dialog">
      <h2>Modifier ${esc(port.name)}</h2>
      <label>Nom <input name="name" required maxlength="80" value="${esc(port.name)}"></label>
      <label>Latitude <input name="latitude" type="number" step="0.0001" min="-90" max="90" required value="${port.latitude}"></label>
      <label>Longitude <input name="longitude" type="number" step="0.0001" min="-180" max="180" required value="${port.longitude}"></label>
      <label>Niveau moyen / zéro des cartes (m)
        <input name="offset_zh_m" type="number" step="0.01" min="0.01" max="20" value="${port.offset_zh_m ?? ""}">
      </label>
      <label>Site api-maree.fr (recalage)
        <input name="api_maree_site" maxlength="80" pattern="[a-z0-9][a-z0-9\\-]*" value="${esc(port.api_maree_site ?? "")}">
      </label>
      ${port.calibration ? `<label class="check"><input type="checkbox" name="drop_calibration"> Abandonner le recalage actuel (prochains calculs en FES brut)</label>` : ""}
      <p class="dialog-hint">${port.years.length ? `Les années déjà calculées (${port.years.join(", ")}) ne sont pas recalculées automatiquement.` : ""}</p>
      <p class="dialog-error" role="alert"></p>
      <div class="dialog-actions">
        <button type="button" class="btn-quiet" value="cancel">Annuler</button>
        <button type="submit" class="btn-primary">Enregistrer</button>
      </div>
    </form>`;
  document.body.append(d);
  const form = d.querySelector("form");
  d.addEventListener("close", () => d.remove());
  d.querySelector("[value=cancel]").addEventListener("click", () => d.close());
  form.addEventListener("submit", async e => {
    e.preventDefault();
    try {
      await Session.api(`/api/admin/ports/${port.id}`, {
        method: "PATCH",
        body: {
          name: form.name.value.trim(),
          latitude: Number(form.latitude.value),
          longitude: Number(form.longitude.value),
          offset_zh_m: numOrNull(form.offset_zh_m.value),
          api_maree_site: form.api_maree_site.value.trim() || null,
        },
      });
      if (form.drop_calibration?.checked) {
        await Session.api(`/api/admin/ports/${port.id}/calibration`, { method: "DELETE" });
      }
      d.close();
      loadPorts();
    } catch (err) {
      d.querySelector(".dialog-error").textContent = err.message;
    }
  });
  d.showModal();
}

$("annual-form").addEventListener("submit", async e => {
  e.preventDefault();
  const year = Number($("annual-year").value);
  try {
    const r = await Session.api("/api/admin/jobs/annual", { method: "POST", body: { year } });
    flash(r.created.length
      ? `${r.created.length} calcul(s) ${year} ajouté(s) à la file. ${seeJobs}`
      : `Aucun calcul ajouté : pas de port annuel avec niveau moyen, ou calculs déjà en file.`);
    loadJobs();
  } catch (err) {
    flash(esc(err.message));
  }
});

// ---------------------------------------------------------------------------
// Types de créneaux (liste déroulante des utilisateurs)
// ---------------------------------------------------------------------------

let slotTypes = [];
let typesScope = null;   // structure affichée (choix du super administrateur)
const typesBody = $("types-body");
const typeForm = $("type-form");

// Super administrateur : structure choisie ; administrateur de structure : la sienne (imposée par l'API)
const typesQS = () => (isSuper() && typesScope ? `?structure_id=${typesScope}` : "");

$("types-structure").addEventListener("change", e => {
  typesScope = Number(e.target.value) || null;
  loadTypes();
});

// ---- Règles de la structure : délais d'inscription et de désinscription ----
// N jours : fermé à partir de J-N (possible jusqu'à J-N-1 inclus) ; vide : pas de limite.

const settingsForm = $("settings-form");
// Rappels et alertes : réglage (nombre de jours) → champ ; case décochée : désactivé (null)
const REMINDER_INPUTS = {
  remind_slot_days: "remind-slot-days", alert_low_fill_days: "alert-low-fill-days",
  alert_late_unregister_days: "alert-late-unregister-days", remind_caci_days: "remind-caci-days",
};
const lockInputs = { register_lock_days: $("register-lock-days"), unregister_lock_days: $("lock-days") };
const fmtWeekday = new Intl.DateTimeFormat("fr-FR", { weekday: "long", day: "numeric", month: "long" });

// Structure affichée dans l'onglet : choisie (super administrateur) ou la sienne
const scopedStructure = () =>
  structures.find(st => st.id === (isSuper() ? typesScope : Session.user?.structure?.id));

// "" → null ; entier 0..365 → nombre ; sinon undefined (invalide)
function parseLock(input) {
  const raw = input.value.trim();
  if (raw === "") return null;
  const n = Number(raw);
  return Number.isInteger(n) && n >= 0 && n <= 365 ? n : undefined;
}

function renderLockPreview() {
  const el = $("lock-days-preview");
  const reg = parseLock(lockInputs.register_lock_days);
  const unreg = parseLock(lockInputs.unregister_lock_days);
  if (reg === undefined || unreg === undefined) {
    el.textContent = "Nombre entier de 0 à 365, ou vide pour ne pas limiter.";
    return;
  }
  // exemple parlant : un créneau le dimanche de la semaine prochaine
  const slot = new Date();
  slot.setDate(slot.getDate() + ((7 - slot.getDay()) % 7 || 7) + 7);
  const until = n => {
    if (n === null) return "jusqu'au jour même";
    const d = new Date(slot);
    d.setDate(d.getDate() - n - 1);
    return `jusqu'au ${fmtWeekday.format(d)} inclus`;
  };
  el.textContent = `Exemple, pour un créneau le ${fmtWeekday.format(slot)} : inscription ${until(reg)}, ` +
    `désinscription ${until(unreg)}. Champ vide : pas de limite. ` +
    "Les administrateurs de la structure peuvent toujours retirer une inscription.";
}

// Heure de rendez-vous : étale moins le délai, arrondie aux 5 minutes inférieures
const rdvInputs = { hours: $("rdv-offset-hours"), minutes: $("rdv-offset-minutes") };
const pad2 = n => String(n).padStart(2, "0");
const fmtHM = total => `${Math.floor(total / 60)}h${pad2(total % 60)}`;

// délai en minutes (0 à 12 h), ou undefined si invalide
function parseRdvOffset() {
  const h = Number(rdvInputs.hours.value.trim() || 0);
  const m = Number(rdvInputs.minutes.value.trim() || 0);
  if (!Number.isInteger(h) || !Number.isInteger(m) || h < 0 || m < 0 || m > 59) return undefined;
  const total = h * 60 + m;
  return total <= 720 ? total : undefined;
}

function renderRdvPreview() {
  const el = $("rdv-offset-preview");
  const offset = parseRdvOffset();
  if (offset === undefined) {
    el.textContent = "Délai de 0 h 00 à 12 h 00 (minutes de 0 à 59).";
    return;
  }
  const tide = 9 * 60 + 37;  // étale à 9h37
  let rdv = (tide - offset + 1440) % 1440;
  rdv -= rdv % 5;
  el.textContent = `Exemple : étale à ${fmtHM(tide)} → rendez-vous à ${fmtHM(rdv)}` +
    `${tide - offset < 0 ? " la veille" : ""}. L'heure de rendez-vous est arrondie aux 5 minutes inférieures ; ` +
    "un changement s'applique aussi aux créneaux déjà choisis à venir.";
}

for (const input of Object.values(rdvInputs)) input.addEventListener("input", renderRdvPreview);

// Port affiché par défaut dans la recherche (ports ayant des marées précalculées)
const defaultPortSelect = $("default-port");
let searchPorts = null;  // null : pas encore chargés

async function loadSearchPorts() {
  try {
    searchPorts = await Session.api("/api/ports");
  } catch (e) {
    searchPorts = [];
  }
}

function renderDefaultPort(st) {
  const current = st.default_port_id ?? null;
  const ports = searchPorts || [];
  const opts = [`<option value="">Aucun (premier de la liste)</option>`, ...ports.map(p =>
    `<option value="${p.id}"${p.id === current ? " selected" : ""}>${esc(p.name)}</option>`)];
  // port choisi qui n'a plus de marées précalculées : on le garde visible
  if (current !== null && !ports.some(p => p.id === current)) {
    opts.push(`<option value="${current}" selected>Port n° ${current} (sans données)</option>`);
  }
  defaultPortSelect.innerHTML = opts.join("");
}

function loadSettings() {
  const st = scopedStructure();
  settingsForm.hidden = !st;
  $("settings-form-status").textContent = "";
  if (!st) return;
  for (const [k, input] of Object.entries(lockInputs)) input.value = st[k] ?? "";
  $("default-max-registrations").value = st.default_max_registrations ?? "";
  $("use-api-maree").checked = st.use_api_maree ?? true;
  $("use-calibration").checked = st.use_calibration ?? true;
  $("caci-check").checked = !!st.caci_check;
  $("caci-validity-months").value = st.caci_validity_months ?? 12;
  for (const [key, input] of Object.entries(REMINDER_INPUTS)) {
    const box = settingsForm.querySelector(`[data-reminder=${key}]`);
    box.checked = st[key] != null;
    if (st[key] != null) $(input).value = st[key];
  }
  const offset = st.rdv_offset_minutes ?? 120;
  rdvInputs.hours.value = Math.floor(offset / 60);
  rdvInputs.minutes.value = offset % 60;
  renderRdvPreview();
  renderLockPreview();
  if (searchPorts === null) loadSearchPorts().then(() => { if (scopedStructure() === st) renderDefaultPort(st); });
  renderDefaultPort(st);
}

for (const input of Object.values(lockInputs)) input.addEventListener("input", renderLockPreview);

settingsForm.addEventListener("submit", async e => {
  e.preventDefault();
  const status = $("settings-form-status");
  const st = scopedStructure();
  if (!st) return;
  const body = {};
  for (const [k, input] of Object.entries(lockInputs)) {
    const v = parseLock(input);
    if (v === undefined) {
      status.textContent = "Nombre entier de 0 à 365, ou vide pour ne pas limiter.";
      input.focus();
      return;
    }
    body[k] = v;
  }
  const offset = parseRdvOffset();
  if (offset === undefined) {
    status.textContent = "Délai de rendez-vous de 0 h 00 à 12 h 00 (minutes de 0 à 59).";
    rdvInputs.hours.focus();
    return;
  }
  const places = $("default-max-registrations").value.trim();
  const maxRegistrations = places === "" ? null : Number(places);
  if (maxRegistrations !== null && (!Number.isInteger(maxRegistrations) || maxRegistrations < 1 || maxRegistrations > 500)) {
    status.textContent = "Nombre de places : entier de 1 à 500, ou vide pour ne pas limiter.";
    $("default-max-registrations").focus();
    return;
  }
  body.default_max_registrations = maxRegistrations;
  body.rdv_offset_minutes = offset;
  body.default_port_id = defaultPortSelect.value ? Number(defaultPortSelect.value) : null;
  body.use_api_maree = $("use-api-maree").checked;
  body.use_calibration = $("use-calibration").checked;
  body.caci_check = $("caci-check").checked;
  const months = Number($("caci-validity-months").value);
  if (!Number.isInteger(months) || months < 1 || months > 60) {
    status.textContent = "Validité du certificat : de 1 à 60 mois.";
    $("caci-validity-months").focus();
    return;
  }
  body.caci_validity_months = months;
  for (const [key, input] of Object.entries(REMINDER_INPUTS)) {
    const el = $(input);
    if (!settingsForm.querySelector(`[data-reminder=${key}]`).checked) { body[key] = null; continue; }
    const n = Number(el.value);
    if (!Number.isInteger(n) || n < Number(el.min) || n > Number(el.max)) {
      status.textContent = `Rappels et alertes : nombre de jours de ${el.min} à ${el.max}.`;
      el.focus();
      return;
    }
    body[key] = n;
  }
  status.textContent = "";
  try {
    const saved = await Session.api(`/api/admin/structures/${st.id}/settings`, { method: "PATCH", body });
    const moved = saved.selections_moved;
    delete saved.selections_moved;
    Object.assign(st, saved);
    loadSettings();
    $("settings-form-status").textContent = "Réglages enregistrés."
      + (moved === undefined ? "" : moved
        ? ` Horaires de marée changés : ${moved} créneau(x) à venir recalé(s) sur les nouveaux horaires.`
        : " Horaires de marée changés ; aucun créneau à venir n'a changé d'étale.");
    // sa propre structure : l'heure de RDV affichée ailleurs suit le nouveau délai
    if (Session.user?.structure?.id === st.id) {
      Session.user.structure.rdv_offset_minutes = saved.rdv_offset_minutes;
      Session.user.structure.default_port_id = saved.default_port_id;
      Session.user.structure.default_max_registrations = saved.default_max_registrations;
    }
  } catch (err) {
    status.textContent = err.message;
  }
});

async function loadTypes() {
  const noStructure = isSuper() && !typesScope;
  typeForm.hidden = noStructure;
  loadSettings();
  loadUnavailabilities();
  if (noStructure) {
    slotTypes = [];
    typesBody.innerHTML = `<tr><td colspan="5" class="empty">Créez d'abord une structure (onglet « Structures »).</td></tr>`;
    return;
  }
  try {
    slotTypes = await Session.api(`/api/admin/slot-types${typesQS()}`);
  } catch (e) {
    flash(esc(e.message));
    return;
  }
  renderTypes();
}

// ---- Plages d'indisponibilité : aucun créneau ne peut y être choisi ou créé ----

const unavPanel = $("unav-panel");
const unavBody = $("unav-body");
const unavStatus = $("unav-status");
let unavailabilities = [];

async function loadUnavailabilities() {
  unavPanel.hidden = isSuper() && !typesScope;
  if (unavPanel.hidden) return;
  const qs = new URLSearchParams();
  if (isSuper() && typesScope) qs.set("structure_id", typesScope);
  if ($("unav-past").checked) qs.set("past", "true");
  try {
    unavailabilities = await Session.api(`/api/admin/unavailabilities?${qs}`);
  } catch (e) {
    unavBody.innerHTML = `<tr><td colspan="4" class="empty">${esc(e.message)}</td></tr>`;
    return;
  }
  unavBody.innerHTML = unavailabilities.length ? unavailabilities.map(u => `
    <tr data-id="${u.id}" class="${u.past ? "inactive" : ""}">
      <th scope="row">${esc(u.label.charAt(0).toUpperCase() + u.label.slice(1))}</th>
      <td data-label="Motif">${u.reason ? esc(u.reason) : `<span class="muted">—</span>`}</td>
      <td data-label="Ajoutée par">${u.created_by ? esc(u.created_by) : `<span class="muted">—</span>`}</td>
      <td class="actions">
        <button type="button" class="btn-quiet" data-act="edit">Modifier</button>
        <button type="button" class="btn-danger" data-act="delete">Supprimer</button>
      </td>
    </tr>`).join("")
    : `<tr><td colspan="4" class="empty">Aucune indisponibilité${$("unav-past").checked ? "" : " à venir"}.</td></tr>`;
}

$("unav-past").addEventListener("change", loadUnavailabilities);
$("unav-add").addEventListener("click", () => editUnavailability(null));

const fmtUnavDay = new Intl.DateTimeFormat("fr-FR", { weekday: "short", day: "numeric", month: "short", year: "numeric" });

// Créneaux à venir déjà choisis dans la plage : gardés, mais signalés
function showOverlapping(u, overlapping) {
  if (!overlapping.length) {
    unavStatus.innerHTML = `<p>Indisponibilité enregistrée : ${esc(u.label)}.</p>`;
    return;
  }
  const items = overlapping.map(o => {
    const day = fmtUnavDay.format(new Date(`${o.date}T12:00:00`));
    const regs = o.registrations ? ` — ${o.registrations} inscrit(s)` : "";
    return `<li>${esc(day)}, RDV ${esc(o.rdv.time)} · ${esc(o.type)}${o.note ? ` « ${esc(o.note)} »` : ""} · ${esc(o.place)}${regs}</li>`;
  }).join("");
  unavStatus.innerHTML = `<div class="unav-warning">
      <p><strong>Indisponibilité enregistrée (${esc(u.label)}), mais ${overlapping.length} créneau(x) déjà choisi(s) s'y trouvent.</strong>
      Ils sont conservés, inscrits compris ; retirez-les depuis <a href="mes-creneaux.html">Créneaux choisis</a> si besoin.</p>
      <ul>${items}</ul>
    </div>`;
}

function editUnavailability(u) {
  const qs = isSuper() && typesScope ? `?structure_id=${typesScope}` : "";
  const v = (x) => esc(x ?? "");
  openDialog({
    title: u ? "Modifier l'indisponibilité" : "Ajouter une indisponibilité",
    submitLabel: u ? "Enregistrer" : "Ajouter",
    body: `
      <div class="unav-fields">
        <label>Du <input name="start_date" type="date" required value="${v(u?.start_date)}"></label>
        <label>à partir de <input name="start_time" type="time" value="${v(u?.start_time)}"></label>
        <label>Au <input name="end_date" type="date" value="${v(u?.end_date !== u?.start_date ? u?.end_date : "")}"></label>
        <label>jusqu'à <input name="end_time" type="time" value="${v(u?.end_time)}"></label>
      </div>
      <p class="dialog-hint">« Au » vide : un seul jour. Heures vides : journées entières (dès 0 h le premier jour, jusqu'à minuit le dernier). L'heure de fin est exclue : jusqu'à 12:00, un rendez-vous à 12:00 reste possible.</p>
      <label>Motif (facultatif) <input name="reason" maxlength="80" placeholder="ex. Carénage du bateau" value="${v(u?.reason)}"></label>`,
    onSubmit: async form => {
      const body = {
        start_date: form.start_date.value,
        end_date: form.end_date.value || null,
        start_time: form.start_time.value || null,
        end_time: form.end_time.value || null,
        reason: form.reason.value.trim() || null,
      };
      const r = await Session.api(u ? `/api/admin/unavailabilities/${u.id}${qs}` : `/api/admin/unavailabilities${qs}`,
        { method: u ? "PUT" : "POST", body });
      showOverlapping(r.unavailability, r.overlapping);
      loadUnavailabilities();
    },
  });
}

unavBody.addEventListener("click", async e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const u = unavailabilities.find(x => x.id === Number(btn.closest("tr").dataset.id));
  if (!u) return;
  if (btn.dataset.act === "edit") {
    editUnavailability(u);
    return;
  }
  if (!confirm(`Supprimer l'indisponibilité ${u.label} ?`)) return;
  const qs = isSuper() && typesScope ? `?structure_id=${typesScope}` : "";
  try {
    await Session.api(`/api/admin/unavailabilities/${u.id}${qs}`, { method: "DELETE" });
    unavStatus.innerHTML = "";
  } catch (err) {
    flash(esc(err.message));
  }
  loadUnavailabilities();
});

// Niveaux de plongeur (niveau minimal d'un type de créneau), chargés une fois
let diverLevels = {};
Session.api("/api/divers/levels").then(r => {
  diverLevels = r.levels;
  for (const sel of document.querySelectorAll("select[data-levels]")) {
    sel.insertAdjacentHTML("beforeend", Object.entries(diverLevels).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join(""));
  }
  renderTypes();
}).catch(() => {});
const levelOptions = selected => `<option value="">Aucun</option>` + Object.entries(diverLevels).map(([k, v]) =>
  `<option value="${esc(k)}"${k === selected ? " selected" : ""}>${esc(v)}</option>`).join("");

function renderTypes() {
  typesBody.innerHTML = slotTypes.length ? slotTypes.map((t, i) => `
    <tr data-id="${t.id}" class="${t.active ? "" : "inactive"}">
      <td data-label="Ordre">
        <span class="order">
          <button type="button" class="btn-quiet" data-act="up" ${i === 0 ? "disabled" : ""} aria-label="Monter ${esc(t.label)}">▲</button>
          <button type="button" class="btn-quiet" data-act="down" ${i === slotTypes.length - 1 ? "disabled" : ""} aria-label="Descendre ${esc(t.label)}">▼</button>
        </span>
      </td>
      <th scope="row"><span class="type-pill" style="--type-color:${esc(t.color)}">${esc(t.label)}</span></th>
      <td data-label="Niveau minimal">${t.min_level ? esc(diverLevels[t.min_level] || t.min_level) : `<span class="muted">—</span>`}</td>
      <td data-label="Proposé"><input type="checkbox" data-act="active" ${t.active ? "checked" : ""} aria-label="Proposer ${esc(t.label)}"></td>
      <td class="num" data-label="Utilisé">${t.uses}</td>
      <td class="actions">
        <button type="button" class="btn-quiet" data-act="edit">Modifier</button>
        <button type="button" class="btn-danger" data-act="delete" ${t.uses ? `disabled title="Utilisé : décochez « Proposé » à la place"` : ""}>Supprimer</button>
      </td>
    </tr>`).join("")
    : `<tr><td colspan="6" class="empty">Aucun type : cette structure ne peut pas encore choisir de créneau.</td></tr>`;
}

typeForm.addEventListener("submit", async e => {
  e.preventDefault();
  const st = $("type-form-status");
  st.textContent = "";
  try {
    const t = await Session.api(`/api/admin/slot-types${typesQS()}`, {
      method: "POST",
      body: { label: typeForm.label.value.trim(), color: typeForm.color.value, active: typeForm.active.checked,
              min_level: typeForm.min_level.value || null },
    });
    st.textContent = `« ${t.label} » ajouté.`;
    typeForm.label.value = "";
    typeForm.label.focus();
    loadTypes();
  } catch (err) {
    st.textContent = err.message;
  }
});

typesBody.addEventListener("change", async e => {
  if (e.target.dataset.act !== "active") return;
  const id = Number(e.target.closest("tr").dataset.id);
  try {
    await Session.api(`/api/admin/slot-types/${id}`, { method: "PATCH", body: { active: e.target.checked } });
    loadTypes();
  } catch (err) {
    e.target.checked = !e.target.checked;
    flash(esc(err.message));
  }
});

typesBody.addEventListener("click", async e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = Number(btn.closest("tr").dataset.id);
  const t = slotTypes.find(x => x.id === id);
  try {
    switch (btn.dataset.act) {
      case "up":
      case "down": {
        const ids = slotTypes.map(x => x.id);
        const i = ids.indexOf(id), j = i + (btn.dataset.act === "up" ? -1 : 1);
        [ids[i], ids[j]] = [ids[j], ids[i]];
        slotTypes = await Session.api(`/api/admin/slot-types/order${typesQS()}`, { method: "PUT", body: { ids } });
        renderTypes();
        typesBody.querySelector(`tr[data-id="${id}"] [data-act=${btn.dataset.act}]:not(:disabled)`)?.focus();
        return;
      }
      case "edit":
        editType(t);
        return;
      case "delete":
        if (!confirm(`Supprimer le type « ${t.label} » ?`)) return;
        await Session.api(`/api/admin/slot-types/${id}`, { method: "DELETE" });
        loadTypes();
        return;
    }
  } catch (err) {
    flash(esc(err.message));
    loadTypes();
  }
});

function editType(t) {
  const d = document.createElement("dialog");
  d.className = "account-dialog";
  d.innerHTML = `
    <form method="dialog">
      <h2>Modifier le type</h2>
      <label>Libellé <input name="label" required maxlength="40" value="${esc(t.label)}"></label>
      <label>Couleur <input name="color" type="color" value="${esc(t.color)}"></label>
      <label>Niveau minimal <select name="min_level">${levelOptions(t.min_level)}</select></label>
      <p class="dialog-hint">L'inscription d'un membre qui n'a pas ce niveau (fiche plongeur) est refusée ; un administrateur peut toujours l'inscrire. Un créneau peut fixer son propre niveau.</p>
      <p class="dialog-hint">${t.uses ? `Le nouveau libellé s'appliquera aux ${t.uses} créneau(x) déjà choisi(s).` : ""}</p>
      <p class="dialog-error" role="alert"></p>
      <div class="dialog-actions">
        <button type="button" class="btn-quiet" value="cancel">Annuler</button>
        <button type="submit" class="btn-primary">Enregistrer</button>
      </div>
    </form>`;
  document.body.append(d);
  const form = d.querySelector("form");
  d.addEventListener("close", () => d.remove());
  d.querySelector("[value=cancel]").addEventListener("click", () => d.close());
  form.addEventListener("submit", async e => {
    e.preventDefault();
    try {
      await Session.api(`/api/admin/slot-types/${t.id}`, {
        method: "PATCH",
        body: { label: form.label.value.trim(), color: form.color.value, min_level: form.min_level.value || null },
      });
      d.close();
      loadTypes();
    } catch (err) {
      d.querySelector(".dialog-error").textContent = err.message;
    }
  });
  d.showModal();
}

// ---------------------------------------------------------------------------
// Utilisateurs
// ---------------------------------------------------------------------------

const usersBody = $("users-body");
const usersStatus = $("users-status");
const createForm = $("create-user");
const createStatus = $("create-status");
const mailOn = () => !!Session.config.password_reset;

let users = [];

const ROLE_LABELS = Session.ROLE_LABELS;

function roleCell(u) {
  const tags = [];
  if (u.is_admin) tags.push(`<span class="tag tag-admin">Super admin</span>`);
  if (u.role) tags.push(`<span class="tag role-${u.role}">${ROLE_LABELS[u.role]}</span>`);
  for (const id of u.profiles || []) {
    const p = profileCatalog.find(x => x.id === id);
    tags.push(`<span class="tag tag-profile" title="${esc(p?.description || "")}">${esc(p?.label || id)}</span>`);
  }
  return tags.join(" ");
}

function statusTags(u) {
  const tags = [];
  if (u.pending_invite) {
    const expired = u.invite_expires_at && new Date(u.invite_expires_at) < new Date();
    tags.push(expired || !u.invite_expires_at
      ? `<span class="tag tag-warn" title="Le lien d'invitation a expiré : renvoyez-la">invitation expirée</span>`
      : `<span class="tag tag-pending" title="Lien valable jusqu'au ${stamp(u.invite_expires_at)}">invitation envoyée</span>`);
  } else if (u.must_change_password) {
    tags.push(`<span class="tag tag-pending" title="Mot de passe à changer à la prochaine connexion">mot de passe provisoire</span>`);
  }
  if (!u.profile_complete) tags.push(`<span class="tag tag-warn" title="Prénom, nom ou adresse e-mail manquant">profil incomplet</span>`);
  if (u.suspended) tags.push(`<span class="tag tag-error" title="${esc(u.suspended.reason || "Connexion refusée")}">suspendu</span>`);
  if (u.totp_enabled) tags.push(`<span class="tag" title="Double authentification activée">2FA</span>`);
  return tags.join(" ");
}

// Administrateur de structure : « Retirer » un compte partagé de SA structure (le compte reste), « Supprimer » sinon.
// Super administrateur : « Retirer de la structure » quand la liste est filtrée sur une structure dont le compte
// est membre et qui n'est pas la seule, et toujours « Supprimer le compte ».
function deleteButtons(u) {
  if (!isSuper()) {
    return u.structures_count > 1
      ? `<button type="button" class="btn-danger" data-act="remove" title="Le compte reste membre de ses autres structures">Retirer</button>`
      : `<button type="button" class="btn-danger" data-act="delete">Supprimer</button>`;
  }
  const scoped = $("users-filter").value && u.structures_count > 1;
  return (scoped ? `<button type="button" class="btn-quiet" data-act="remove">Retirer de la structure</button>` : "")
    + `<button type="button" class="btn-danger" data-act="delete">Supprimer${scoped ? " le compte" : ""}</button>`;
}

// Filtres de la liste (en plus de la recherche) : rôle, profil, certificat médical, état du compte
function userMatches(u) {
  const role = $("users-role").value, profile = $("users-profile").value;
  const caci = $("users-caci").value, state = $("users-state").value;
  if (role === "super" ? !u.is_admin : role && u.role !== role) return false;
  if (profile && !(u.profiles || []).includes(profile)) return false;
  const cs = u.caci?.state;
  if (caci === "problem" ? !["missing", "expired"].includes(cs) : caci && cs !== caci) return false;
  if (state === "pending" && !(u.pending_invite || u.must_change_password)) return false;
  if (state === "never" && u.last_login_at) return false;
  if (state === "incomplete" && u.profile_complete) return false;
  if (state === "suspended" && !u.suspended) return false;
  return true;
}

function shownUsers() {
  const q = $("users-search").value.trim().toLowerCase();
  return users.filter(u => userMatches(u) && (!q || [u.display_name, u.username, u.email, u.phone, u.diver?.licence_number]
    .some(v => v && v.toLowerCase().includes(q))));
}

const checkedUsers = new Set();   // comptes cochés pour une action groupée

function renderBulk() {
  const shownIds = new Set(shownUsers().filter(u => u.id !== Session.user.id).map(u => u.id));   // cochables
  for (const id of [...checkedUsers]) if (!shownIds.has(id)) checkedUsers.delete(id);
  const n = checkedUsers.size;
  // super administrateur : une action groupée vaut dans UNE structure (rôle et profils y sont propres)
  const possible = !isSuper() || !!$("users-filter").value;
  $("users-bulk").hidden = !n;
  $("users-bulk-count").textContent = possible ? `${n} compte(s) coché(s)` : `${n} compte(s) coché(s) : choisissez d'abord une structure`;
  $("users-bulk-action").disabled = $("users-bulk-apply").disabled = !possible;
  const all = $("users-check-all");
  all.checked = !!shownIds.size && [...shownIds].every(id => checkedUsers.has(id));
  all.indeterminate = !all.checked && [...shownIds].some(id => checkedUsers.has(id));
}

function renderUsers() {
  const me = Session.user;
  const q = $("users-search").value.trim().toLowerCase();
  const shown = shownUsers();
  const filtered = q || ["users-role", "users-profile", "users-caci", "users-state"].some(id => $(id).value);
  usersBody.innerHTML = shown.length ? shown.map(u => {
    const self = u.id === me.id;
    const contact = [
      u.email ? `<a href="mailto:${esc(u.email)}">${esc(u.email)}</a>` : `<span class="muted">pas d'e-mail</span>`,
      u.phone ? `<a href="tel:${esc(u.phone.replace(/\s/g, ""))}">${esc(u.phone)}</a>` : "",
    ].filter(Boolean).join("<br>");
    return `
      <tr data-id="${u.id}">
        <td class="col-check">${self ? "" : `<input type="checkbox" data-check value="${u.id}"${checkedUsers.has(u.id) ? " checked" : ""} aria-label="Cocher ${esc(u.display_name)}">`}</td>
        <th scope="row">
          <span class="user-name">${esc(u.display_name)}</span>${self ? ` <span class="tag">vous</span>` : ""}
          <span class="user-sub">${esc(u.username)}</span>
          ${statusTags(u)}
        </th>
        <td class="contact" data-label="Contact">${contact}</td>
        <td data-label="Structure">${u.structure ? esc(u.structure.name) : `<span class="muted">–</span>`}</td>
        <td data-label="Rôle">${roleCell(u)}</td>
        <td data-label="Dernière connexion" title="Compte créé le ${stamp(u.created_at)}">${u.last_login_at ? stamp(u.last_login_at) : `<span class="muted">jamais</span>`}</td>
        <td class="actions">
          <button type="button" class="btn-quiet" data-act="edit">Modifier</button>
          ${self || isShared(u) ? "" : `<button type="button" class="btn-quiet" data-act="password">Mot de passe…</button>`}
          ${isSuper() ? `<button type="button" class="btn-quiet" data-act="security" title="Suspension, sessions, double authentification, export des données">Sécurité…</button>`
            : `<a class="btn-quiet btn-small" href="/api/admin/users/${u.id}/export" download title="Données du compte (demande d'accès RGPD), en JSON">Exporter</a>`}
          ${self ? "" : deleteButtons(u)}
        </td>
      </tr>`;
  }).join("")
    : `<tr><td colspan="7" class="empty">${filtered ? "Aucun compte ne correspond à la recherche." : "Aucun compte."}</td></tr>`;
  usersStatus.textContent = filtered ? `${shown.length} compte(s) sur ${users.length}.` : `${users.length} compte(s).`;
  renderBulk();
}

for (const id of ["users-role", "users-profile", "users-caci", "users-state"]) $(id).addEventListener("change", renderUsers);

usersBody.addEventListener("change", e => {
  const box = e.target.closest("[data-check]");
  if (!box) return;
  if (box.checked) checkedUsers.add(Number(box.value)); else checkedUsers.delete(Number(box.value));
  renderBulk();
});
$("users-check-all").addEventListener("change", e => {
  for (const u of shownUsers()) if (u.id !== Session.user.id) {
    if (e.target.checked) checkedUsers.add(u.id); else checkedUsers.delete(u.id);
  }
  renderUsers();
});
$("users-bulk-clear").addEventListener("click", () => { checkedUsers.clear(); renderUsers(); });

$("users-bulk-apply").addEventListener("click", async () => {
  const [action, arg] = $("users-bulk-action").value.split(":");
  if (!action || !checkedUsers.size) return;
  const n = checkedUsers.size;
  const labels = {
    role: `Passer ${n} compte(s) en ${arg === "manager" ? "administration" : "visualisation"} ?`,
    add_profile: `Attribuer le profil « ${profileCatalog.find(p => p.id === arg)?.label} » à ${n} compte(s) ?`,
    remove_profile: `Retirer le profil « ${profileCatalog.find(p => p.id === arg)?.label} » à ${n} compte(s) ?`,
    remove: `Retirer ${n} compte(s) de la structure ? Un compte qui n'appartient à aucune autre structure est supprimé, avec ses inscriptions.`,
  };
  if (!confirm(labels[action])) return;
  const body = { user_ids: [...checkedUsers], action };
  if (action === "role") body.role = arg;
  if (action.endsWith("_profile")) body.profile = arg;
  const scope = isSuper() ? `?structure_id=${$("users-filter").value}` : "";
  try {
    const r = await Session.api(`/api/admin/users/bulk${scope}`, { method: "POST", body });
    checkedUsers.clear();
    const names = id => users.find(u => u.id === id)?.display_name || `n° ${id}`;
    usersStatus.textContent = `${r.done.length} compte(s) modifié(s).` + (r.skipped.length
      ? ` Non traités : ${r.skipped.map(s => `${names(s.id)} (${s.reason})`).join(", ")}.` : "");
    const status = usersStatus.textContent;
    await loadUsers();
    usersStatus.textContent = status;
  } catch (err) {
    usersStatus.textContent = err.message;
  }
});

// Export Excel des comptes affichés (filtres compris)
const CACI_LABELS = { valid: "valable", pending: "à valider", missing: "absent", expired: "expiré" };
$("users-export").addEventListener("click", () => {
  const list = shownUsers();
  if (!list.length) return;
  const name = isSuper() ? ($("users-filter").selectedOptions[0]?.textContent || "tous") : Session.user?.structure?.name;
  const profileLabel = id => profileCatalog.find(p => p.id === id)?.label || id;
  XlsxExport.download(`membres-${XlsxExport.slug(name || "structure")}.xlsx`, "Membres", [
    { header: "Nom", width: 18, value: u => u.last_name || "" },
    { header: "Prénom", width: 16, value: u => u.first_name || "" },
    { header: "Identifiant", width: 18, value: u => u.username },
    { header: "E-mail", width: 28, value: u => u.email || "" },
    { header: "Téléphone", width: 16, value: u => u.phone || "" },
    { header: "Structure", width: 18, value: u => u.structure?.name || "" },
    { header: "Rôle", width: 14, value: u => (u.is_admin ? "super administrateur" : ROLE_LABELS[u.role] || "") },
    { header: "Profils", width: 22, value: u => (u.profiles || []).map(profileLabel).join(", ") },
    { header: "Niveau", width: 16, value: u => u.diver?.diver_level_label || "" },
    { header: "Encadrement", width: 16, value: u => u.diver?.instructor_level_label || "" },
    { header: "Licence", width: 16, value: u => u.diver?.licence_number || "" },
    { header: "CACI (date)", type: "date", width: 12, value: u => u.caci?.date || null },
    { header: "CACI valable jusqu'au", type: "date", width: 14, value: u => u.caci?.valid_until || null },
    { header: "CACI", width: 11, value: u => CACI_LABELS[u.caci?.state] || "" },
    { header: "Dernière connexion", type: "date", width: 14, value: u => (u.last_login_at ? u.last_login_at.slice(0, 10) : null) },
    { header: "Compte créé le", type: "date", width: 14, value: u => (u.created_at ? u.created_at.slice(0, 10) : null) },
  ], list);
});

// ---- Demandes d'adhésion (lien public de la structure) ----

let joinRequests = [];
let joinPending = null;   // demande en cours de traitement : classée quand le compte est créé ou invité
const JOIN_STATUS = { new: "à traiter", done: "traitée", rejected: "classée sans suite" };

async function loadJoinRequests() {
  const filter = isSuper() ? $("users-filter").value : "";
  if (isSuper() && !filter) { $("join-panel").hidden = true; return; }
  try {
    joinRequests = await Session.api(`/api/admin/join-requests${filter ? `?structure_id=${filter}` : ""}`);
  } catch {
    joinRequests = [];
  }
  renderJoinRequests();
}

function renderJoinRequests() {
  const pending = joinRequests.filter(r => r.status === "new").length;
  $("join-panel").hidden = !joinRequests.length;
  $("join-count").hidden = !pending;
  $("join-count").textContent = `${pending} à traiter`;
  $("join-list").innerHTML = joinRequests.map(r => {
    const acts = r.status === "new"
      ? `<button type="button" class="btn-primary btn-small" data-act="join-create">Créer le compte</button>
         <button type="button" class="btn-secondary btn-small" data-act="join-invite" title="La personne a déjà un compte dans une autre structure">Inviter son compte</button>
         <button type="button" class="btn-quiet btn-small" data-act="join-reject">Classer sans suite</button>`
      : `<button type="button" class="btn-quiet btn-small" data-act="join-reopen">Remettre en attente</button>`;
    return `
    <article class="request-card${r.status === "new" ? " is-new" : ""}" data-id="${r.id}">
      <div class="request-head"><strong>${esc(r.first_name)} ${esc(r.last_name)}</strong>
        <span class="tag${r.status === "new" ? " tag-pending" : ""}">${JOIN_STATUS[r.status]}</span></div>
      <p class="request-contact"><a href="mailto:${esc(r.email)}">${esc(r.email)}</a>${r.phone ? ` · <a href="tel:${esc(r.phone.replace(/\s/g, ""))}">${esc(r.phone)}</a>` : ""}</p>
      ${r.message ? `<p class="request-message">${esc(r.message)}</p>` : ""}
      <p class="request-meta muted">Reçue le ${stamp(r.created_at)}${r.handled_by ? ` · traitée par ${esc(r.handled_by)}` : ""}</p>
      <div class="request-acts">${acts}<button type="button" class="btn-danger btn-small" data-act="join-delete">Supprimer</button></div>
    </article>`;
  }).join("");
}

async function setJoinStatus(id, status) {
  const r = await Session.api(`/api/admin/join-requests/${id}`, { method: "PATCH", body: { status } });
  joinRequests = joinRequests.map(x => (x.id === r.id ? r : x));
  renderJoinRequests();
}

$("join-list").addEventListener("click", async e => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const r = joinRequests.find(x => x.id === Number(btn.closest("[data-id]").dataset.id));
  if (!r) return;
  try {
    switch (btn.dataset.act) {
      case "join-create":
        joinPending = r.id;
        createForm.first_name.value = r.first_name;
        createForm.last_name.value = r.last_name;
        createForm.email.value = r.email;
        createForm.phone.value = r.phone || "";
        createForm.role.value = "viewer";
        for (const name of ["first_name", "last_name", "email"]) createForm[name].dispatchEvent(new Event("input", { bubbles: true }));
        createForm.scrollIntoView({ block: "start" });
        flash(`Compte de ${esc(r.first_name)} ${esc(r.last_name)} prérempli : choisissez le rôle et le mot de passe, puis validez. La demande sera classée.`);
        break;
      case "join-invite":
        joinPending = r.id;
        inviteForm.email.value = r.email;
        inviteForm.scrollIntoView({ block: "start" });
        flash(`Invitation préparée pour ${esc(r.email)} : validez pour l'envoyer. La demande sera classée.`);
        break;
      case "join-reject":
        await setJoinStatus(r.id, "rejected");
        break;
      case "join-reopen":
        await setJoinStatus(r.id, "new");
        break;
      case "join-delete":
        if (!confirm(`Supprimer la demande de ${r.first_name} ${r.last_name} ?`)) return;
        await Session.api(`/api/admin/join-requests/${r.id}`, { method: "DELETE" });
        joinRequests = joinRequests.filter(x => x.id !== r.id);
        renderJoinRequests();
        break;
    }
  } catch (err) {
    flash(esc(err.message));
  }
});

// compte créé ou invitation envoyée pour une demande d'adhésion : elle est classée
async function joinHandled() {
  if (joinPending == null) return;
  const id = joinPending;
  joinPending = null;
  try { await setJoinStatus(id, "done"); } catch { /* la demande reste à traiter */ }
}

async function loadUsers() {
  const filter = isSuper() ? $("users-filter").value : "";
  loadJoinRequests();
  try {
    users = await Session.api(`/api/admin/users${filter ? `?structure_id=${filter}` : ""}`);
    renderUsers();
    loadInvitations();
  } catch (e) {
    usersStatus.textContent = e.message;
  }
}

$("users-filter").addEventListener("change", () => { checkedUsers.clear(); loadUsers(); });
$("users-search").addEventListener("input", renderUsers);

// ---- Profils (en plus du rôle, cumulables) ----

let profileCatalog = [];   // [{id, label, description}], chargé au démarrage

function profileChoices(selected = []) {
  return profileCatalog.map(p => `
    <label class="check"><input type="checkbox" name="profile" value="${esc(p.id)}"${selected.includes(p.id) ? " checked" : ""}>
      ${esc(p.label)} <span class="muted">— ${esc(p.description)}</span></label>`).join("");
}

const checkedProfiles = form => [...form.querySelectorAll("[name=profile]:checked")].map(b => b.value);

async function loadProfileCatalog() {
  try {
    profileCatalog = await Session.api("/api/admin/profiles");
  } catch {
    profileCatalog = [];
  }
  const box = $("new-profiles");
  box.querySelectorAll("label").forEach(l => l.remove());
  box.insertAdjacentHTML("beforeend", profileChoices());
  box.hidden = !profileCatalog.length;
  renderInviteProfiles();
  // filtre de la liste et actions groupées
  $("users-profile").innerHTML = `<option value="">Tous</option>` +
    profileCatalog.map(p => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("");
  const bulk = $("users-bulk-action");
  bulk.querySelectorAll("[data-profile], [data-remove]").forEach(o => o.remove());
  bulk.insertAdjacentHTML("beforeend", profileCatalog.map(p =>
    `<option data-profile value="add_profile:${esc(p.id)}">Attribuer le profil « ${esc(p.label)} »</option>
     <option data-profile value="remove_profile:${esc(p.id)}">Retirer le profil « ${esc(p.label)} »</option>`).join("") +
    `<option data-remove value="remove">Retirer de la structure…</option>`);
}

// ---- Création ----

// Sans structure (super administrateur seul), pas de rôle de structure
function syncCreateRole() {
  const noStructure = isSuper() && createForm.structure_id.value === "";
  createForm.role.disabled = noStructure;
  if (noStructure) createForm.is_admin.checked = true;
}
createForm.structure_id.addEventListener("change", syncCreateRole);
createForm.is_admin.addEventListener("change", () => {
  if (!createForm.is_admin.checked && createForm.structure_id.value === "" && structures.length) {
    createForm.structure_id.value = String(structures[0].id);
    syncCreateRole();
  }
});

// Identifiant proposé (le serveur ajoute un suffixe s'il est déjà pris)
function slug(s) {
  return s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}
function suggestUsername() {
  const base = [slug(createForm.first_name.value), slug(createForm.last_name.value)].filter(Boolean).join(".");
  createForm.username.placeholder = base ? base.slice(0, 28) : "prenom.nom";
}
createForm.first_name.addEventListener("input", suggestUsername);
createForm.last_name.addEventListener("input", suggestUsername);

const pwChecklist = { el: null };

function syncPasswordMode() {
  const invite = createForm.pw_mode.value === "invite";
  $("new-password-block").hidden = invite;
  createForm.password.required = !invite;
  createForm.querySelector("[type=submit]").textContent = invite ? "Créer et envoyer l'invitation" : "Créer le compte";
}
for (const r of createForm.pw_mode) r.addEventListener("change", syncPasswordMode);

// À faire une fois la configuration connue (politique, e-mail disponible)
function setupCreateForm() {
  const inviteRadio = createForm.querySelector("[name=pw_mode][value=invite]");
  inviteRadio.disabled = !mailOn();
  $("mode-invite-label").classList.toggle("disabled", !mailOn());
  $("mail-disabled").hidden = mailOn();
  $("mail-disabled").textContent = "Invitations et « mot de passe oublié » indisponibles : l'envoi d'e-mails n'est pas configuré sur le serveur (MAIL_BACKEND, APP_BASE_URL).";
  createForm.pw_mode.value = mailOn() ? "invite" : "password";
  if (!pwChecklist.el) {
    pwChecklist.el = Session.passwordChecklist(createForm.password);
    $("new-password-block").querySelector(".with-action").after(pwChecklist.el);
  }
  // import : l'invitation n'est proposée que si l'e-mail fonctionne
  const importMode = $("import-mode");
  importMode.querySelector("[value=invite]").disabled = !mailOn();
  importMode.value = mailOn() ? "invite" : "password";
  syncPasswordMode();
}

$("gen-password").addEventListener("click", () => {
  createForm.password.value = Session.generatePassword();
  pwChecklist.el?.refresh();
});

createForm.addEventListener("submit", async e => {
  e.preventDefault();
  createStatus.textContent = "";
  const invite = createForm.pw_mode.value === "invite";
  const body = {
    first_name: createForm.first_name.value.trim(),
    last_name: createForm.last_name.value.trim(),
    email: createForm.email.value.trim(),
    phone: createForm.phone.value.trim() || null,
    username: createForm.username.value.trim() || null,
    role: createForm.role.value,
    send_invite: invite,
    profiles: checkedProfiles(createForm),
  };
  if (!invite) {
    body.password = createForm.password.value;
    body.must_change_password = createForm.must_change_password.checked;
  }
  if (isSuper()) {
    body.is_admin = createForm.is_admin.checked;
    body.structure_id = createForm.structure_id.value ? Number(createForm.structure_id.value) : null;
  }
  const btn = createForm.querySelector("[type=submit]");
  btn.disabled = true;
  try {
    const u = await Session.api("/api/admin/users", { method: "POST", body });
    const where = u.structure ? ` dans « ${esc(u.structure.name)} »` : "";
    let msg = `Compte <strong>${esc(u.display_name)}</strong> (identifiant <code>${esc(u.username)}</code>) créé${where}. `;
    if (invite) {
      msg += u.invitation?.sent
        ? `Invitation envoyée à ${esc(u.email)}.`
        : `<span class="warn">L'invitation n'a pas pu être envoyée (${esc(u.invitation?.error || "erreur inconnue")}) : renvoyez-la depuis la liste.</span>`;
    } else {
      msg += `Transmettez-lui son mot de passe : il ne sera plus affiché.`;
    }
    createStatus.innerHTML = msg;
    joinHandled();
    for (const name of ["first_name", "last_name", "email", "phone", "username", "password"]) createForm[name].value = "";
    for (const box of createForm.querySelectorAll("[name=profile]")) box.checked = false;
    suggestUsername();
    pwChecklist.el?.refresh();
    await Promise.all([loadUsers(), isSuper() ? loadStructures() : null]);
  } catch (err) {
    createStatus.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

// ---- Modification ----

// Compte membre de plusieurs structures : son profil et son mot de passe n'appartiennent à aucune d'elles,
// seuls son titulaire et un super administrateur les modifient (l'API le refuse aux autres)
const isShared = user => !isSuper() && user.id !== Session.user.id && user.structures_count > 1;

function editUser(user) {
  const self = user.id === Session.user.id;
  const sup = isSuper();
  const shared = isShared(user);
  // super administrateur : la structure dont on modifie le rôle et les profils (le compte y est rattaché s'il n'en
  // était pas membre) ; sans structure, ni rôle ni profils
  const structOpts = (user.structure ? "" : `<option value="">Aucune</option>`) + structures.map(st =>
    `<option value="${st.id}"${st.id === user.structure?.id ? " selected" : ""}>${esc(st.name)}</option>`).join("");
  const roleOpts = Object.entries(ROLE_LABELS).map(([v, l]) =>
    `<option value="${v}"${v === (user.role || "viewer") ? " selected" : ""}>${l}</option>`).join("");
  const form = openDialog({
    title: `Modifier ${user.display_name}`,
    body: `
      <p class="dialog-hint">Identifiant : <strong>${esc(user.username)}</strong></p>
      ${shared ? `<p class="dialog-notice">Ce compte appartient à plusieurs structures : son profil n'est modifiable que par lui-même ou par un super administrateur. Vous pouvez changer son rôle et ses profils dans votre structure.</p>` : ""}
      <label>Prénom <input name="first_name" maxlength="60" required value="${esc(user.first_name ?? "")}"${shared ? " disabled" : ""}></label>
      <label>Nom <input name="last_name" maxlength="60" required value="${esc(user.last_name ?? "")}"${shared ? " disabled" : ""}></label>
      <label>Adresse e-mail <input name="email" type="email" maxlength="254" required value="${esc(user.email ?? "")}"${shared ? " disabled" : ""}></label>
      <label>Téléphone <input name="phone" type="tel" maxlength="30" value="${esc(user.phone ?? "")}"${shared ? " disabled" : ""}></label>
      ${sup ? `<label>Structure (rôle et profils) <select name="structure_id">${structOpts}</select></label>` : ""}
      <label>Rôle dans la structure <select name="role"${self && !sup ? " disabled" : ""}>${roleOpts}</select></label>
      ${sup ? `<label class="check"><input type="checkbox" name="is_admin"${user.is_admin ? " checked" : ""}${self ? " disabled" : ""}> Super administrateur</label>` : ""}
      ${profileCatalog.length ? `<fieldset class="profiles-field"><legend>Profils</legend>${profileChoices(user.profiles)}</fieldset>` : ""}
      <p class="dialog-hint">${sup ? "Le rôle et les profils valent pour la structure choisie ; un compte peut appartenir à plusieurs structures, avec un rôle différent dans chacune. Pour en retirer un, utilisez « Retirer de la structure » dans la liste." : "Administration : choisit les créneaux et gère la structure. Visualisation : voit les créneaux choisis."}</p>`,
    onSubmit: async f => {
      const body = shared ? {} : {
        first_name: f.first_name.value.trim(),
        last_name: f.last_name.value.trim(),
        email: f.email.value.trim(),
        phone: f.phone.value.trim() || null,
      };
      // champs vides d'un ancien compte : laissés tels quels plutôt que refusés
      for (const k of ["first_name", "last_name", "email"]) if (!body[k] && k in body) delete body[k];
      const noStructure = sup && f.structure_id.value === "";    // rien à dire du rôle ni des profils
      if (!(self && !sup) && !noStructure) body.role = f.role.value;
      if (profileCatalog.length && !noStructure) body.profiles = checkedProfiles(f);
      if (sup) {
        if (!noStructure) body.structure_id = Number(f.structure_id.value);
        if (!self) body.is_admin = f.is_admin.checked;
      }
      await Session.api(`/api/admin/users/${user.id}`, { method: "PATCH", body });
      await Promise.all([loadUsers(), sup ? loadStructures() : null]);
      if (self) Session.init();  // son nom et ses droits ont pu changer
    },
  });
  form.noValidate = true;
  if (sup) {
    const sync = () => { form.role.disabled = form.structure_id.value === ""; };
    form.structure_id.addEventListener("change", sync);
    sync();
  }
}

// ---- Invitation d'un compte existant ----

const inviteForm = $("invite-form");

const inviteScope = () => (isSuper() && inviteForm.structure_id.value ? `?structure_id=${inviteForm.structure_id.value}` : "");

async function loadInvitations() {
  const box = $("invitations-pending");
  try {
    const list = await Session.api(`/api/admin/structure-invitations${inviteScope()}`);
    box.hidden = !list.length;
    $("invitations-body").innerHTML = list.map(i => `
      <tr data-id="${i.id}">
        <th scope="row">${esc(i.email)}</th>
        <td data-label="Rôle">${esc(i.role_label)}${i.profiles.length ? ` <span class="muted">· ${esc(i.profiles.join(", "))}</span>` : ""}</td>
        <td data-label="Expire le">${stamp(i.expires_at)}</td>
        <td class="actions"><button type="button" class="btn-quiet" data-act="cancel-invitation">Annuler</button></td>
      </tr>`).join("");
  } catch {
    box.hidden = true;
  }
}

function renderInviteProfiles() {
  const box = $("invite-profiles");
  box.querySelectorAll("label").forEach(l => l.remove());
  box.insertAdjacentHTML("beforeend", profileChoices());
  box.hidden = !profileCatalog.length;
}

inviteForm.structure_id.addEventListener("change", loadInvitations);

inviteForm.addEventListener("submit", async e => {
  e.preventDefault();
  const status = $("invite-status");
  status.textContent = "";
  if (!inviteForm.email.value.trim()) {
    status.textContent = "Saisissez l'adresse e-mail du compte à inviter.";
    return;
  }
  const body = { email: inviteForm.email.value.trim(), role: inviteForm.role.value, profiles: checkedProfiles(inviteForm) };
  if (isSuper() && inviteForm.structure_id.value) body.structure_id = Number(inviteForm.structure_id.value);
  const btn = inviteForm.querySelector("[type=submit]");
  btn.disabled = true;
  try {
    const r = await Session.api("/api/admin/structure-invitations", { method: "POST", body });
    status.textContent = r.detail;
    joinHandled();
    inviteForm.email.value = "";
    for (const box of inviteForm.querySelectorAll("[name=profile]")) box.checked = false;
    loadInvitations();
  } catch (err) {
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("invitations-body").addEventListener("click", async e => {
  const btn = e.target.closest("[data-act=cancel-invitation]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.id;
  try {
    await Session.api(`/api/admin/structure-invitations/${id}${inviteScope()}`, { method: "DELETE" });
  } catch (err) {
    $("invite-status").textContent = err.message;
  }
  loadInvitations();
});

// ---- Mot de passe : envoi par e-mail ou mot de passe provisoire saisi ----

function askPassword(user) {
  const canMail = mailOn() && !!user.email;
  const linkLabel = user.pending_invite ? "Renvoyer l'invitation par e-mail" : "Envoyer un mot de passe provisoire par e-mail";
  const why = !mailOn() ? "envoi d'e-mails non configuré" : !user.email ? "pas d'adresse e-mail" : "";
  const form = openDialog({
    title: `Mot de passe de ${user.display_name}`,
    submitLabel: canMail ? "Envoyer" : "Changer le mot de passe",
    body: `
      <label class="check"><input type="radio" name="mode" value="link"${canMail ? " checked" : " disabled"}>
        ${linkLabel}${why ? ` <span class="muted">(${why})</span>` : ""}</label>
      ${canMail ? `<p class="dialog-hint">À ${esc(user.email)}. ${user.pending_invite ? "Le lien précédent est remplacé." : "Le mot de passe actuel reste valable tant que le provisoire n'est pas utilisé."}</p>` : ""}
      <label class="check"><input type="radio" name="mode" value="temp"${canMail ? "" : " checked"}> Saisir un mot de passe provisoire</label>
      <div class="temp-block">
        <label>Mot de passe provisoire
          <input name="password" type="text" autocomplete="new-password" spellcheck="false" value="${esc(Session.generatePassword())}">
        </label>
        <label class="check"><input type="checkbox" name="must_change" checked> À changer à la prochaine connexion</label>
        <p class="dialog-hint">Ses sessions ouvertes seront fermées. Transmettez-lui ce mot de passe : il ne sera plus affiché.</p>
      </div>`,
    onSubmit: async f => {
      if (f.mode.value === "link") {
        const r = await Session.api(`/api/admin/users/${user.id}/send-link`, { method: "POST" });
        usersStatus.textContent = r.sent === "invite"
          ? `Invitation renvoyée à ${r.email}.` : `Mot de passe provisoire envoyé à ${r.email}.`;
      } else {
        await Session.api(`/api/admin/users/${user.id}`, {
          method: "PATCH", body: { password: f.password.value, must_change_password: f.must_change.checked },
        });
        usersStatus.textContent = `Mot de passe de « ${user.display_name} » changé.`;
      }
      loadUsers();
    },
  });
  const block = form.querySelector(".temp-block");
  const rules = Session.passwordChecklist(form.password);
  form.password.closest("label").after(rules);
  const sync = () => {
    const temp = form.mode.value === "temp";
    block.hidden = !temp;
    form.querySelector("[type=submit]").textContent = temp ? "Changer le mot de passe" : "Envoyer";
  };
  for (const r of form.mode) r.addEventListener("change", sync);
  sync();
}

usersBody.addEventListener("click", async e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const user = users.find(u => u.id === Number(btn.closest("tr").dataset.id));
  if (!user) return;
  try {
    switch (btn.dataset.act) {
      case "password":
        askPassword(user);
        return;
      case "edit":
        editUser(user);
        return;
      case "security":
        accountSecurity(user);
        return;
      case "remove": {
        const scope = isSuper() ? `?structure_id=${$("users-filter").value}` : "";
        if (!confirm(`Retirer « ${user.display_name} » de cette structure ? Son compte reste membre de ses autres structures ; ses inscriptions à ses créneaux sont retirées.`)) return;
        await Session.api(`/api/admin/users/${user.id}${scope}`, { method: "DELETE" });
        break;
      }
      case "delete":
        if (!confirm(`Supprimer le compte « ${user.display_name} » (${user.username}) et ses préférences ? Les créneaux qu'il a choisis restent à sa structure.`)) return;
        await Session.api(`/api/admin/users/${user.id}`, { method: "DELETE" });
        break;
    }
    await Promise.all([loadUsers(), isSuper() ? loadStructures() : null]);
  } catch (err) {
    usersStatus.textContent = err.message;
  }
});

// ---- Sécurité d'un compte, doublons, comptes inactifs (super administrateur) ----

function accountSecurity(user) {
  const self = user.id === Session.user.id;
  const d = document.createElement("dialog");
  d.className = "account-dialog";
  d.innerHTML = `
    <form method="dialog" class="security-dialog">
      <h2>Sécurité de ${esc(user.display_name)}</h2>
      <section>
        <h3>Suspension</h3>
        ${user.suspended
          ? `<p>Suspendu depuis le ${stamp(user.suspended.at)}${user.suspended.reason ? ` : ${esc(user.suspended.reason)}` : ""}.</p>
             <p><button type="button" class="btn-primary btn-small" data-sec="unsuspend">Réactiver le compte</button></p>`
          : self ? `<p class="muted">Vous ne pouvez pas suspendre votre propre compte.</p>`
          : `<p class="hint">La connexion est refusée et toutes ses sessions sont fermées ; ses données sont conservées.</p>
             <label>Motif (facultatif) <input name="reason" maxlength="300"></label>
             <p><button type="button" class="btn-danger btn-small" data-sec="suspend">Suspendre le compte</button></p>`}
      </section>
      <section>
        <h3>Sessions</h3>
        <p><button type="button" class="btn-secondary btn-small" data-sec="sessions">Fermer toutes ses sessions</button>
          <span class="muted">${self ? "(sauf celle-ci)" : "(déconnecte tous ses appareils)"}</span></p>
      </section>
      <section>
        <h3>Double authentification</h3>
        ${user.totp_enabled
          ? `<p>Activée. <button type="button" class="btn-quiet btn-small" data-sec="totp">Réinitialiser (téléphone perdu)</button></p>`
          : `<p class="muted">Non activée.</p>`}
      </section>
      <section>
        <h3>Données (RGPD)</h3>
        <p><a class="btn-secondary btn-small" href="/api/admin/users/${user.id}/export" download>Exporter ses données (JSON)</a></p>
      </section>
      <p class="dialog-error" role="alert"></p>
      <div class="dialog-actions"><button type="submit" class="btn-primary" value="close">Fermer</button></div>
    </form>`;
  document.body.append(d);
  d.addEventListener("close", () => d.remove());
  const errEl = d.querySelector(".dialog-error");
  d.querySelector("form").addEventListener("click", async e => {
    const act = e.target.closest("[data-sec]")?.dataset.sec;
    if (!act) return;
    errEl.textContent = "";
    try {
      if (act === "suspend") {
        if (!confirm(`Suspendre « ${user.display_name} » ? Il ne pourra plus se connecter.`)) return;
        await Session.api(`/api/admin/users/${user.id}/suspend`, { method: "POST", body: { reason: d.querySelector("[name=reason]").value.trim() || null } });
      }
      if (act === "unsuspend") await Session.api(`/api/admin/users/${user.id}/suspend`, { method: "DELETE" });
      if (act === "sessions") {
        const r = await Session.api(`/api/admin/users/${user.id}/sessions/close`, { method: "POST" });
        flash(`${r.closed} session(s) de « ${esc(user.display_name)} » fermée(s).`);
      }
      if (act === "totp") {
        if (!confirm(`Retirer la double authentification de « ${user.display_name} » ? Il pourra se connecter avec son seul mot de passe${user.is_admin ? " et devra la remettre en place aussitôt" : ""}.`)) return;
        await Session.api(`/api/admin/users/${user.id}/totp`, { method: "DELETE" });
      }
      d.close();
      loadUsers();
    } catch (err) {
      errEl.textContent = err.message;
    }
  });
  d.showModal();
}

const accountLine = a => `
  <strong>${esc(a.display_name)}</strong> <span class="muted">${esc(a.username)}${a.email ? ` · ${esc(a.email)}` : ""}${a.phone ? ` · ${esc(a.phone)}` : ""}${a.licence_number ? ` · licence ${esc(a.licence_number)}` : ""}</span><br>
  <span class="muted">${a.structures.length ? a.structures.map(s => `${esc(s.name)} (${s.role === "manager" ? "admin." : "membre"})`).join(", ") : "aucune structure"}
  · ${a.registrations} inscription(s) · ${a.last_login_at ? `connecté le ${stamp(a.last_login_at)}` : "jamais connecté"}</span>`;

async function loadDuplicates() {
  const box = $("duplicates-list");
  let groups;
  try {
    groups = await Session.api("/api/admin/accounts/duplicates");
  } catch (e) {
    box.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    return;
  }
  box.innerHTML = groups.length ? groups.map((g, gi) => `
    <fieldset class="dup-group" data-group="${gi}">
      <legend>${esc(g.reason)}</legend>
      ${g.accounts.map(a => `<label class="dup-account"><input type="radio" name="keep-${gi}" value="${a.id}"> <span>${accountLine(a)}</span></label>`).join("")}
      <p><button type="button" class="btn-secondary btn-small" data-merge="${gi}">Fusionner dans le compte coché</button></p>
    </fieldset>`).join("") : `<p class="muted">Aucun doublon probable.</p>`;
  box.onclick = async e => {
    const gi = e.target.closest("[data-merge]")?.dataset.merge;
    if (gi === undefined) return;
    const g = groups[gi];
    const keep = Number(box.querySelector(`[name=keep-${gi}]:checked`)?.value);
    if (!keep) { alert("Cochez le compte à conserver."); return; }
    const others = g.accounts.filter(a => a.id !== keep);
    const kept = g.accounts.find(a => a.id === keep);
    if (!confirm(`Fusionner ${others.map(a => `« ${a.display_name} » (${a.username})`).join(", ")} dans « ${kept.display_name} » (${kept.username}) ? `
      + "Les comptes fusionnés sont supprimés : leurs titulaires se connecteront avec l'identifiant et le mot de passe du compte conservé.")) return;
    try {
      for (const o of others) {
        await Session.api("/api/admin/accounts/merge", { method: "POST", body: { keep_id: keep, remove_id: o.id } });
      }
      flash(`Comptes fusionnés dans « ${esc(kept.display_name)} ».`);
      loadDuplicates();
      loadUsers();
    } catch (err) {
      alert(err.message);
    }
  };
}

async function loadInactive() {
  const box = $("inactive-list");
  const months = Number($("inactive-months").value);
  let rows;
  try {
    rows = await Session.api(`/api/admin/accounts/inactive?months=${months}`);
  } catch (e) {
    box.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    return;
  }
  box.innerHTML = rows.length ? `
    <p><label class="check"><input type="checkbox" data-inactive-all> Tout cocher (${rows.length})</label></p>
    <ul class="inactive-list">${rows.map(a => `<li><label class="dup-account"><input type="checkbox" value="${a.id}"> <span>${accountLine(a)}</span></label></li>`).join("")}</ul>
    <p><button type="button" class="btn-danger btn-small" data-inactive-delete>Supprimer les comptes cochés</button></p>`
    : `<p class="muted">Aucun compte inactif depuis cette durée.</p>`;
  box.onchange = e => {
    if (e.target.matches("[data-inactive-all]")) for (const c of box.querySelectorAll(".inactive-list input")) c.checked = e.target.checked;
  };
  box.onclick = async e => {
    if (!e.target.closest("[data-inactive-delete]")) return;
    const ids = [...box.querySelectorAll(".inactive-list input:checked")].map(c => Number(c.value));
    if (!ids.length) { alert("Cochez les comptes à supprimer."); return; }
    if (!confirm(`Supprimer définitivement ${ids.length} compte(s) et leurs données (inscriptions, préférences) ?`)) return;
    try {
      const r = await Session.api("/api/admin/accounts/purge-inactive", { method: "POST", body: { months, ids } });
      flash(`${r.deleted} compte(s) supprimé(s).`);
      loadInactive();
      loadUsers();
    } catch (err) {
      alert(err.message);
    }
  };
}

$("duplicates-load").addEventListener("click", loadDuplicates);
$("inactive-load").addEventListener("click", loadInactive);

// ---- Import CSV ----

const importForm = $("import-form");
const importPreview = $("import-preview");
const importResult = $("import-result");
let importRequest = null;   // corps de la dernière analyse, renvoyé tel quel pour confirmer

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = () => reject(new Error("Lecture du fichier impossible"));
    r.readAsDataURL(file);
  });
}

function resetImport() {
  importRequest = null;
  importPreview.hidden = true;
  $("import-body").innerHTML = "";
}

function renderImportRows(result) {
  $("import-body").innerHTML = result.rows.map(r => {
    const ok = !r.errors.length;
    return `
      <tr class="${ok ? "" : "row-error"}">
        <td class="num" data-label="Ligne">${r.line}</td>
        <th scope="row">${esc([r.first_name, r.last_name].filter(Boolean).join(" ") || "–")}</th>
        <td data-label="Identifiant">${r.username ? `${esc(r.username)}${r.generated_username ? ` <span class="muted" title="Proposé à partir du nom">(auto)</span>` : ""}` : `<span class="muted">–</span>`}</td>
        <td class="contact" data-label="Contact">${esc(r.email || "")}${r.phone ? `<br>${esc(r.phone)}` : ""}</td>
        <td data-label="Structure">${r.structure ? esc(r.structure.name) : `<span class="muted">–</span>`}</td>
        <td data-label="Rôle"><span class="tag role-${r.role}">${ROLE_LABELS[r.role]}</span></td>
        <td class="check-cell" data-label="Vérification">${ok ? `<span class="ok-mark">✓ prêt</span>` : r.errors.map(e => `<span class="err">${esc(e)}</span>`).join("<br>")}</td>
      </tr>`;
  }).join("");
}

importForm.addEventListener("submit", async e => {
  e.preventDefault();
  resetImport();
  importResult.hidden = true;
  const file = $("import-file").files[0];
  if (!file) return;
  const summary = $("import-summary");
  try {
    importRequest = {
      content_b64: await fileToBase64(file),
      role: $("import-role").value,
      mode: $("import-mode").value,
      dry_run: true,
    };
    if (isSuper() && $("import-structure").value) importRequest.structure_id = Number($("import-structure").value);
    const result = await Session.api("/api/admin/users/import", { method: "POST", body: importRequest });
    renderImportRows(result);
    const extra = result.ignored_columns.length ? ` Colonnes ignorées : ${result.ignored_columns.map(esc).join(", ")}.` : "";
    summary.innerHTML = `<strong>${result.valid}</strong> compte(s) prêt(s) à créer`
      + (result.invalid ? `, <strong class="err">${result.invalid}</strong> ligne(s) en erreur qui seront ignorées` : "")
      + `. <span class="muted">Fichier ${esc(result.encoding)}, séparateur ${esc(result.delimiter)}.${extra}</span>`;
    const confirmBtn = $("import-confirm");
    confirmBtn.disabled = !result.valid;
    confirmBtn.textContent = importRequest.mode === "invite"
      ? `Créer ${result.valid} compte(s) et envoyer les invitations`
      : `Créer ${result.valid} compte(s) avec mots de passe provisoires`;
    importPreview.hidden = false;
  } catch (err) {
    importPreview.hidden = true;
    importResult.hidden = false;
    importResult.innerHTML = `<p class="err">${esc(err.message)}</p>`;
    importRequest = null;
  }
});

$("import-cancel").addEventListener("click", () => {
  resetImport();
  importForm.reset();
  setupCreateForm();
});
importForm.addEventListener("change", e => { if (e.target.closest(".import-grid")) resetImport(); });

function downloadCsv(filename, rows) {
  const cell = v => /[";\n\r]/.test(String(v ?? "")) ? `"${String(v).replace(/"/g, '""')}"` : String(v ?? "");
  const text = "\ufeff" + rows.map(r => r.map(cell).join(";")).join("\r\n") + "\r\n";
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$("import-confirm").addEventListener("click", async () => {
  if (!importRequest) return;
  const btn = $("import-confirm");
  btn.disabled = true;
  btn.textContent = "Création en cours…";
  try {
    const result = await Session.api("/api/admin/users/import", { method: "POST", body: { ...importRequest, dry_run: false } });
    const created = result.created;
    const failed = created.filter(c => c.invitation_error);
    let html = `<p><strong>${created.length}</strong> compte(s) créé(s).`;
    if (importRequest.mode === "invite") {
      html += failed.length
        ? ` <span class="err">${failed.length} invitation(s) non envoyée(s)</span> : renvoyez-les depuis la liste (bouton « Mot de passe… »).`
        : " Une invitation a été envoyée à chacun.";
      html += "</p>";
    } else {
      html += ` Leurs mots de passe provisoires ne seront <strong>plus affichés</strong> : téléchargez-les maintenant.</p>
        <p><button type="button" class="btn-primary" id="import-download">Télécharger les identifiants (CSV)</button></p>
        <p class="hint">Ce fichier contient des mots de passe : transmettez-les individuellement puis supprimez-le. Chacun devra changer le sien à sa première connexion.</p>`;
    }
    if (failed.length) {
      html += `<ul class="err-list">${failed.map(c => `<li>${esc(c.first_name)} ${esc(c.last_name)} (${esc(c.email)}) : ${esc(c.invitation_error)}</li>`).join("")}</ul>`;
    }
    importResult.innerHTML = html;
    importResult.hidden = false;
    $("import-download")?.addEventListener("click", () => downloadCsv(
      `identifiants-${new Date().toISOString().slice(0, 10)}.csv`,
      [["nom", "prenom", "email", "identifiant", "mot_de_passe_provisoire", "structure", "role"],
       ...created.map(c => [c.last_name, c.first_name, c.email, c.username, c.password, c.structure, ROLE_LABELS[c.role]])],
    ));
    resetImport();
    importForm.reset();
    setupCreateForm();
    await Promise.all([loadUsers(), isSuper() ? loadStructures() : null]);
  } catch (err) {
    importResult.innerHTML = `<p class="err">${esc(err.message)}</p>`;
    importResult.hidden = false;
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Connexion Mailjet de la structure (administrateurs)
// ---------------------------------------------------------------------------

let mailjetScope = null;   // structure choisie (super administrateur)
let mailjetConf = null;
const mjForm = $("mailjet-form");
const mjStatus = $("mailjet-status");
const mjQS = () => (isSuper() && mailjetScope ? `?structure_id=${mailjetScope}` : "");

$("mailjet-structure").addEventListener("change", e => {
  mailjetScope = Number(e.target.value) || null;
  mjStatus.textContent = "";
  loadMailjet();
});

async function loadMailjet() {
  if (isSuper() && !mailjetScope) {
    mailjetConf = null;
    $("mailjet-state").textContent = "";
    mjForm.hidden = true;
    $("mailjet-check").hidden = false;
    $("mailjet-check").textContent = "Créez d'abord une structure (onglet « Structures »).";
    return;
  }
  mjForm.hidden = false;
  try {
    mailjetConf = await Session.api(`/api/admin/mailjet${mjQS()}`);
  } catch (e) {
    flash(esc(e.message));
    return;
  }
  renderMailjet();
}

function renderMailjet() {
  const c = mailjetConf;
  const state = $("mailjet-state");
  const [label, cls] = !c.configured ? ["non connecté", "tag-warn"]
    : c.check_ok === true ? ["connecté", "job-succeeded"]
    : c.check_ok === false ? ["à corriger", "tag-warn"]
    : ["non testé", "tag-pending"];
  state.textContent = label;
  state.className = `tag ${cls}`;

  const enc = $("mailjet-encryption");
  enc.hidden = c.encryption_ok;
  enc.textContent = c.encryption_ok ? "" : `Enregistrement impossible : ${c.encryption_error}`;

  const check = $("mailjet-check");
  check.hidden = !c.checked_at;
  if (c.checked_at) {
    check.className = `mailjet-check ${c.check_ok ? "ok" : "ko"}`;
    check.textContent = `${c.check_message} (testé le ${stamp(c.checked_at)})`;
  }

  mjForm.sender_email.value = c.sender_email || "";
  mjForm.sender_name.value = c.sender_name || Session.user?.structure?.name || "";
  mjForm.api_key.value = "";
  mjForm.api_secret.value = "";
  mjForm.api_key.placeholder = c.configured ? `•••• ${c.api_key_hint} — vide : inchangée` : "";
  mjForm.api_secret.placeholder = c.configured ? "•••••••• — vide : inchangée" : "";
  $("mailjet-keys-hint").textContent = c.configured
    ? `Clés enregistrées par ${c.updated_by || "?"} le ${stamp(c.updated_at)}. Pour en changer, saisissez la clé API et la clé secrète.`
    : "Les deux clés se trouvent dans votre compte Mailjet (voir l'aide ci-dessous).";
  for (const el of mjForm.elements) el.disabled = !c.encryption_ok && el.type !== "button";
  $("mailjet-test").disabled = !c.configured;
  $("mailjet-test-email").disabled = !c.configured;
  $("mailjet-delete").disabled = !c.configured;

  // suivi des envois (ouvertures, clics, rebonds…) : adresse de suivi déclarée chez Mailjet
  $("mailjet-events").hidden = !c.configured;
  if (c.configured) {
    $("mailjet-events-text").textContent = c.events_enabled
      ? `Activé le ${stamp(c.events_registered_at)} : Mailjet transmet au site les remises, ouvertures, clics, rebonds, signalements comme indésirable et désinscriptions de chaque newsletter.`
      : "Non activé : les rapports des newsletters ne montreront que les envois et les refus. L'activation déclare chez Mailjet l'adresse du site où envoyer ces événements.";
    $("mailjet-events-enable").textContent = c.events_enabled ? "Réactiver le suivi" : "Activer le suivi";
    $("mailjet-events-url").hidden = !c.events_url;
    $("mailjet-events-url").innerHTML = c.events_url
      ? `En cas de besoin, l'adresse à déclarer à la main chez Mailjet (Paramètres du compte → Notifications d'événements) : <code>${esc(c.events_url)}</code>`
      : "";
  }
}

$("mailjet-events-enable").addEventListener("click", async e => {
  e.target.disabled = true;
  mjStatus.textContent = "Activation du suivi…";
  try {
    mailjetConf = await Session.api(`/api/admin/mailjet/events${mjQS()}`, { method: "POST" });
    renderMailjet();
    mjStatus.textContent = "Suivi activé.";
  } catch (err) {
    mjStatus.textContent = err.message;
  } finally {
    e.target.disabled = false;
  }
});

async function mailjetTest() {
  mailjetConf = await Session.api(`/api/admin/mailjet/test${mjQS()}`, { method: "POST" });
  renderMailjet();
  return mailjetConf.check_ok;
}

mjForm.addEventListener("submit", async e => {
  e.preventDefault();
  mjStatus.textContent = "";
  const body = {
    sender_email: mjForm.sender_email.value.trim(),
    sender_name: mjForm.sender_name.value.trim(),
  };
  const key = mjForm.api_key.value.trim(), secret = mjForm.api_secret.value.trim();
  if (key || secret) Object.assign(body, { api_key: key, api_secret: secret });
  const btn = mjForm.querySelector("[type=submit]");
  btn.disabled = true;
  try {
    mailjetConf = await Session.api(`/api/admin/mailjet${mjQS()}`, { method: "PUT", body });
    renderMailjet();
    mjStatus.textContent = "Enregistré. Test en cours…";
    const ok = await mailjetTest();
    mjStatus.textContent = ok ? "Enregistré : connexion réussie." : "Enregistré, mais la connexion est à corriger (voir ci-dessus).";
  } catch (err) {
    mjStatus.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("mailjet-test").addEventListener("click", async e => {
  e.target.disabled = true;
  mjStatus.textContent = "Test en cours…";
  try {
    const ok = await mailjetTest();
    mjStatus.textContent = ok ? "Connexion réussie." : "Connexion à corriger (voir ci-dessus).";
  } catch (err) {
    mjStatus.textContent = err.message;
  } finally {
    e.target.disabled = !mailjetConf?.configured;
  }
});

$("mailjet-test-email").addEventListener("click", async e => {
  e.target.disabled = true;
  mjStatus.textContent = "Envoi en cours…";
  try {
    const r = await Session.api(`/api/admin/mailjet/test-email${mjQS()}`, { method: "POST" });
    mjStatus.textContent = `E-mail de test envoyé à ${r.to} : vérifiez votre boîte de réception (et les indésirables).`;
  } catch (err) {
    mjStatus.textContent = err.message;
  } finally {
    e.target.disabled = !mailjetConf?.configured;
  }
});

$("mailjet-delete").addEventListener("click", async () => {
  if (!confirm("Déconnecter Mailjet ? Les clés enregistrées seront effacées et les newsletters ne pourront plus partir.")) return;
  try {
    await Session.api(`/api/admin/mailjet${mjQS()}`, { method: "DELETE" });
    mjStatus.textContent = "Mailjet déconnecté.";
    loadMailjet();
  } catch (err) {
    mjStatus.textContent = err.message;
  }
});

// ---------------------------------------------------------------------------
// Démarrage
// ---------------------------------------------------------------------------

async function onSessionChange(user) {
  const allowed = !!user?.can.admin_area;
  adminEl.hidden = !allowed;
  gateEl.hidden = allowed;
  clearTimeout(pollTimer);
  if (!user) {
    gateEl.innerHTML = `Connectez-vous avec un compte administrateur. <button type="button" class="btn-primary" id="gate-login">Se connecter</button>`;
    $("gate-login").addEventListener("click", () => Session.openLogin());
    return;
  }
  if (!allowed) {
    gateEl.textContent = `Le compte « ${user.display_name} » est en visualisation : il n'a pas accès à l'administration.`;
    return;
  }
  const sup = isSuper();
  TABS = sup ? ALL_TABS : ALL_TABS.filter(t => !SUPER_TABS.includes(t));
  // fonctions désactivées pour la structure (réglage des super administrateurs)
  const feat = user.structure?.features || {};
  if (!sup && feat.currents === false) TABS = TABS.filter(t => t !== "sites");
  if (!sup && feat.newsletters === false) TABS = TABS.filter(t => t !== "mailjet");
  for (const el of document.querySelectorAll("[data-super]")) el.hidden = !sup;
  for (const btn of document.querySelectorAll(".tabs [role=tab]")) {
    if (!btn.hasAttribute("data-super")) btn.hidden = !TABS.includes(btn.dataset.tab);
  }
  $("worker-banner").hidden = true;
  // administrateur de structure : sa structure, sans choix possible
  $("types-structure").hidden = !sup;
  $("types-structure-name").textContent = sup ? "" : user.structure?.name ?? "";
  $("types-structure").previousElementSibling.hidden = !sup;
  $("types-structure-name").hidden = sup;

  for (const k of ["fiche"]) {
    $(`${k}-structure`).hidden = !sup;
    $(`${k}-structure`).previousElementSibling.hidden = !sup;
    $(`${k}-structure-name`).textContent = sup ? "" : user.structure?.name ?? "";
    $(`${k}-structure-name`).hidden = sup;
  }
  $("home-structure").hidden = !sup;
  $("home-structure").previousElementSibling.hidden = !sup;
  $("home-structure-name").textContent = sup ? "" : user.structure?.name ?? "";
  $("home-structure-name").hidden = sup;

  $("sites-structure").hidden = !sup;
  $("sites-structure").previousElementSibling.hidden = !sup;
  $("sites-structure-name").textContent = sup ? "" : user.structure?.name ?? "";
  $("sites-structure-name").hidden = sup;

  $("mailjet-structure").hidden = !sup;
  $("mailjet-structure").previousElementSibling.hidden = !sup;
  $("mailjet-structure-name").textContent = sup ? "" : user.structure?.name ?? "";
  $("mailjet-structure-name").hidden = sup;

  setupCreateForm();
  await Promise.all([loadStructures(), loadProfileCatalog()]);
  if (sup) {
    loadPorts();
    loadStatus();
    loadHealth();
    loadJobs().then(schedulePoll);
  }
  showTab(location.hash.slice(1));
}

Session.mountAccount(document.getElementById("account"), [Session.LINKS.search, Session.LINKS.heights, Session.LINKS.picks, Session.LINKS.newsletters, Session.LINKS.map, Session.LINKS.divers, Session.LINKS.help]);
Session.onChange(onSessionChange);
Session.init();
