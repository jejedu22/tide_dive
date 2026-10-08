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

const ALL_TABS = ["structures", "ports", "donnees", "types", "utilisateurs", "mailjet"];
const SUPER_TABS = ["structures", "ports", "donnees"];
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
  if (name === "structures") { loadStructures(); loadRequests(); }
  if (name === "donnees") { loadStatus(); loadJobs(); }
  if (name === "types") loadTypes();
  if (name === "utilisateurs") loadUsers();
  if (name === "mailjet") loadMailjet();
}

document.querySelector(".tabs").addEventListener("click", e => {
  const btn = e.target.closest("[role=tab]");
  if (btn) showTab(btn.dataset.tab);
});
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
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
async function loadHealth() {
  const box = $("health-banner");
  let health;
  try {
    health = await Session.api("/api/admin/health");
  } catch (e) {
    return;
  }
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
        <th scope="row">${esc(st.name)}</th>
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
          <button type="button" class="btn-quiet" data-act="rename">Renommer</button>
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

// Listes déroulantes de structures (types, création de compte, filtre des comptes)
function renderStructureSelects() {
  const opts = (selected, extra = "") => extra + structures.map(st =>
    `<option value="${st.id}"${st.id === selected ? " selected" : ""}>${esc(st.name)}</option>`).join("");

  const typesSel = $("types-structure");
  const keepTypes = typesScope ?? Session.user?.structure?.id ?? structures[0]?.id ?? null;
  typesSel.innerHTML = opts(keepTypes);
  typesScope = typesSel.value ? Number(typesSel.value) : null;

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
let diveSites = [];         // sites de plongée de tous les ports (courants de marée)

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
          <button type="button" class="btn-quiet" data-act="sites" title="Sites de plongée du port : position précise, courant de marée">Sites${siteCount(p) ? ` (${siteCount(p)})` : ""}</button>
          <button type="button" class="btn-quiet" data-act="edit">Modifier</button>
          <button type="button" class="btn-danger" data-act="delete">Supprimer</button>
        </td>
      </tr>`;
  }).join("")
    : `<tr><td colspan="7" class="empty">Aucun port. Ajoutez-en un depuis le catalogue ci-dessus.</td></tr>`;
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
    case "sites":
      openDiveSites(port);
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

// ---- Sites de plongée d'un port : position GPS précise (le courant de marée y est extrait de l'atlas du SHOM) ----

const siteCount = p => diveSites.filter(x => x.port_id === p.id).length;

function openDiveSites(port) {
  const d = document.createElement("dialog");
  d.className = "account-dialog water-dialog";
  document.body.append(d);
  d.addEventListener("close", () => d.remove());
  let editing = null;   // site en cours de modification

  const currentNote = x => (x.current
    ? `<span class="tag" title="Atlas ${esc(x.current.atlas)}, port de référence ${esc(x.current.ref_port || "?")}">courant ✓</span>`
    : `<span class="muted">${esc(x.current_status || "pas encore de courant")}</span>`);
  const render = () => {
    const list = diveSites.filter(x => x.port_id === port.id);
    const t = editing;
    d.innerHTML = `
      <form method="dialog">
        <h2>Sites de plongée — ${esc(port.name)}</h2>
        <p class="dialog-hint">Position GPS précise de chaque site (degrés décimaux, ex. 48.6612 / -2.7845) : le courant de marée change beaucoup d'un point à l'autre. Il est extrait de l'atlas de courants du SHOM au point le plus proche ; déplacer un site efface son courant, à réimporter.</p>
        <ul class="water-list">${list.length ? list.map(x => `
          <li data-id="${x.id}">
            <span><strong>${esc(x.name)}</strong> · <span class="muted">${fmtNum(x.lat, 4)}, ${fmtNum(x.lon, 4)}</span> · ${currentNote(x)}${x.notes ? `<br><span class="muted">${esc(x.notes)}</span>` : ""}</span>
            <span><button type="button" class="btn-quiet btn-small" data-site="edit">Modifier</button>
            <button type="button" class="btn-danger btn-small" data-site="delete">Supprimer</button></span>
          </li>`).join("") : `<li class="muted">Aucun site de plongée pour ce port.</li>`}
        </ul>
        <fieldset class="water-form">
          <legend>${t ? `Modifier « ${esc(t.name)} »` : "Ajouter un site"}</legend>
          <label>Nom <input name="name" maxlength="60" placeholder="ex. Roches de Saint-Quay" value="${esc(t?.name ?? "")}"></label>
          <label>Latitude <input name="lat" type="number" step="0.000001" min="-90" max="90" placeholder="48.6612" value="${t?.lat ?? ""}"></label>
          <label>Longitude <input name="lon" type="number" step="0.000001" min="-180" max="180" placeholder="-2.7845" value="${t?.lon ?? ""}"></label>
          <label>Notes <input name="notes" maxlength="300" placeholder="facultatif (profondeur, mouillage…)" value="${esc(t?.notes ?? "")}"></label>
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
      const body = { name: form.name.value.trim(), lat: Number(form.lat.value), lon: Number(form.lon.value),
                     notes: form.notes.value.trim() || null };
      if (!body.name || form.lat.value === "" || form.lon.value === "") { err.textContent = "Nom, latitude et longitude obligatoires."; return; }
      if (t && t.current && (t.lat !== body.lat || t.lon !== body.lon)
          && !confirm("Déplacer le site efface son courant (il valait pour l'ancienne position). Continuer ?")) return;
      try {
        await Session.api(t ? `/api/admin/dive-sites/${t.id}` : `/api/admin/ports/${port.id}/dive-sites`,
          { method: t ? "PUT" : "POST", body });
        diveSites = await Session.api("/api/admin/dive-sites");
        editing = null;
        render();
        renderPorts();
      } catch (e2) {
        err.textContent = e2.message;
      }
    });
    d.querySelector(".water-list").addEventListener("click", async e => {
      const btn = e.target.closest("[data-site]");
      if (!btn) return;
      const x = diveSites.find(w => w.id === Number(btn.closest("li").dataset.id));
      if (btn.dataset.site === "edit") { editing = x; render(); d.querySelector("[name=name]").focus(); return; }
      if (!confirm(`Supprimer le site « ${x.name} » ?`)) return;
      try {
        await Session.api(`/api/admin/dive-sites/${x.id}`, { method: "DELETE" });
        diveSites = await Session.api("/api/admin/dive-sites");
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
      <td data-label="Proposé"><input type="checkbox" data-act="active" ${t.active ? "checked" : ""} aria-label="Proposer ${esc(t.label)}"></td>
      <td class="num" data-label="Utilisé">${t.uses}</td>
      <td class="actions">
        <button type="button" class="btn-quiet" data-act="edit">Modifier</button>
        <button type="button" class="btn-danger" data-act="delete" ${t.uses ? `disabled title="Utilisé : décochez « Proposé » à la place"` : ""}>Supprimer</button>
      </td>
    </tr>`).join("")
    : `<tr><td colspan="5" class="empty">Aucun type : cette structure ne peut pas encore choisir de créneau.</td></tr>`;
}

typeForm.addEventListener("submit", async e => {
  e.preventDefault();
  const st = $("type-form-status");
  st.textContent = "";
  try {
    const t = await Session.api(`/api/admin/slot-types${typesQS()}`, {
      method: "POST",
      body: { label: typeForm.label.value.trim(), color: typeForm.color.value, active: typeForm.active.checked },
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
        body: { label: form.label.value.trim(), color: form.color.value },
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

function renderUsers() {
  const me = Session.user;
  const q = $("users-search").value.trim().toLowerCase();
  const shown = q
    ? users.filter(u => [u.display_name, u.username, u.email, u.phone].some(v => v && v.toLowerCase().includes(q)))
    : users;
  usersBody.innerHTML = shown.length ? shown.map(u => {
    const self = u.id === me.id;
    const contact = [
      u.email ? `<a href="mailto:${esc(u.email)}">${esc(u.email)}</a>` : `<span class="muted">pas d'e-mail</span>`,
      u.phone ? `<a href="tel:${esc(u.phone.replace(/\s/g, ""))}">${esc(u.phone)}</a>` : "",
    ].filter(Boolean).join("<br>");
    return `
      <tr data-id="${u.id}">
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
          ${self ? "" : deleteButtons(u)}
        </td>
      </tr>`;
  }).join("")
    : `<tr><td colspan="6" class="empty">${q ? "Aucun compte ne correspond à la recherche." : "Aucun compte."}</td></tr>`;
  usersStatus.textContent = q ? `${shown.length} compte(s) sur ${users.length}.` : `${users.length} compte(s).`;
}

async function loadUsers() {
  const filter = isSuper() ? $("users-filter").value : "";
  try {
    users = await Session.api(`/api/admin/users${filter ? `?structure_id=${filter}` : ""}`);
    renderUsers();
    loadInvitations();
  } catch (e) {
    usersStatus.textContent = e.message;
  }
}

$("users-filter").addEventListener("change", loadUsers);
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
  for (const el of document.querySelectorAll("[data-super]")) el.hidden = !sup;
  $("worker-banner").hidden = true;
  // administrateur de structure : sa structure, sans choix possible
  $("types-structure").hidden = !sup;
  $("types-structure-name").textContent = sup ? "" : user.structure?.name ?? "";
  $("types-structure").previousElementSibling.hidden = !sup;
  $("types-structure-name").hidden = sup;

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

Session.mountAccount(document.getElementById("account"), [Session.LINKS.search, Session.LINKS.heights, Session.LINKS.picks, Session.LINKS.newsletters, Session.LINKS.help]);
Session.onChange(onSessionChange);
Session.init();
