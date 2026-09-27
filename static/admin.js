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

const ALL_TABS = ["structures", "ports", "donnees", "types", "utilisateurs"];
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
  if (name === "structures") loadStructures();
  if (name === "donnees") { loadStatus(); loadJobs(); }
  if (name === "types") loadTypes();
  if (name === "utilisateurs") loadUsers();
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
  try {
    status = await Session.api("/api/admin/status");
  } catch (e) {
    return;
  }
  renderWorkerBanner();
  renderSources();
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

  const sel = $("fes-model");
  if (!sel.options.length) {
    sel.innerHTML = status.fes_models.map(x => `<option${x === status.fes_default ? " selected" : ""}>${esc(x)}</option>`).join("");
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
      <td class="num">${j.id}</td>
      <th scope="row">${esc(j.label)}</th>
      <td>${statusTag(j)}</td>
      <td>${esc(j.created_by)}</td>
      <td>${stamp(j.created_at)}</td>
      <td class="num">${fmtDuration(j.duration_s)}</td>
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
      if (j.kind === "precompute") refreshPorts = true;
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
        <td class="num">${st.managers || `<span class="tag job-failed" title="Personne ne peut choisir de créneaux ni gérer cette structure">aucun</span>`}</td>
        <td class="num">${st.viewers}</td>
        <td class="num">${st.types}</td>
        <td class="num">${st.selections}</td>
        <td class="actions">
          <button type="button" class="btn-quiet" data-act="members">Membres</button>
          <button type="button" class="btn-quiet" data-act="types">Types</button>
          <button type="button" class="btn-quiet" data-act="rename">Renommer</button>
          <button type="button" class="btn-danger" data-act="delete" ${members ? `disabled title="Encore ${members} membre(s)"` : ""}>Supprimer</button>
        </td>
      </tr>`;
  }).join("")
    : `<tr><td colspan="6" class="empty">Aucune structure. Créez-en une, puis ajoutez-lui des comptes dans « Utilisateurs ».</td></tr>`;
}

// Listes déroulantes de structures (types, création de compte, filtre des comptes)
function renderStructureSelects() {
  const opts = (selected, extra = "") => extra + structures.map(st =>
    `<option value="${st.id}"${st.id === selected ? " selected" : ""}>${esc(st.name)}</option>`).join("");

  const typesSel = $("types-structure");
  const keepTypes = typesScope ?? Session.user?.structure?.id ?? structures[0]?.id ?? null;
  typesSel.innerHTML = opts(keepTypes);
  typesScope = typesSel.value ? Number(typesSel.value) : null;

  // création de compte : garde le choix en cours (y compris « Aucune »), sinon la structure du super admin
  const newSel = $("new-structure");
  const keepNew = newSel.options.length
    ? (newSel.value ? Number(newSel.value) : null)
    : (Session.user?.structure?.id ?? structures[0]?.id ?? null);
  newSel.innerHTML = opts(keepNew, `<option value="">Aucune (super administrateur seulement)</option>`);
  syncCreateRole();

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

async function loadPorts() {
  try {
    [ports, catalog] = await Promise.all([
      Session.api("/api/admin/ports"),
      Session.api("/api/admin/ports/catalog"),
    ]);
  } catch (e) {
    flash(esc(e.message));
    return;
  }
  renderPorts();
  catalogSelect.innerHTML = `<option value="">Point personnalisé (saisie libre)</option>` +
    (catalog.length ? `<optgroup label="Catalogue">${catalog.map((p, i) =>
      `<option value="${i}">${esc(p.name)}${p.offset_zh_m ? "" : " (niveau moyen à renseigner)"}</option>`).join("")}</optgroup>` : "");
}

function renderPorts() {
  portsBody.innerHTML = ports.length ? ports.map(p => {
    const years = p.years.length
      ? p.years.map(y => `<span class="tag">${y}</span>`).join(" ")
      : `<span class="muted">aucune</span>`;
    const offset = p.offset_zh_m != null
      ? `${fmtNum(p.offset_zh_m, 2)} m`
      : `<span class="tag job-failed" title="Obligatoire pour calculer">à renseigner</span>`;
    return `
      <tr data-id="${p.id}">
        <th scope="row">${esc(p.name)}</th>
        <td class="muted">${fmtNum(p.latitude, 4)}, ${fmtNum(p.longitude, 4)}</td>
        <td class="num">${offset}</td>
        <td>${years}</td>
        <td><input type="checkbox" data-act="auto" ${p.auto_precompute ? "checked" : ""} aria-label="Recalcul annuel de ${esc(p.name)}"></td>
        <td class="actions">
          <input type="number" class="year-input" min="1990" max="2100" value="${defaultYear}" aria-label="Année à calculer">
          <button type="button" class="btn-secondary" data-act="compute" ${p.offset_zh_m == null ? "disabled title=\"Renseignez d'abord le niveau moyen\"" : ""}>Calculer</button>
          <button type="button" class="btn-quiet" data-act="edit">Modifier</button>
          <button type="button" class="btn-danger" data-act="delete">Supprimer</button>
        </td>
      </tr>`;
  }).join("")
    : `<tr><td colspan="6" class="empty">Aucun port. Ajoutez-en un depuis le catalogue ci-dessus.</td></tr>`;
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
        },
      });
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

async function loadTypes() {
  const noStructure = isSuper() && !typesScope;
  typeForm.hidden = noStructure;
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

function renderTypes() {
  typesBody.innerHTML = slotTypes.length ? slotTypes.map((t, i) => `
    <tr data-id="${t.id}" class="${t.active ? "" : "inactive"}">
      <td>
        <span class="order">
          <button type="button" class="btn-quiet" data-act="up" ${i === 0 ? "disabled" : ""} aria-label="Monter ${esc(t.label)}">▲</button>
          <button type="button" class="btn-quiet" data-act="down" ${i === slotTypes.length - 1 ? "disabled" : ""} aria-label="Descendre ${esc(t.label)}">▼</button>
        </span>
      </td>
      <th scope="row"><span class="type-pill" style="--type-color:${esc(t.color)}">${esc(t.label)}</span></th>
      <td><input type="checkbox" data-act="active" ${t.active ? "checked" : ""} aria-label="Proposer ${esc(t.label)}"></td>
      <td class="num">${t.uses}</td>
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

// Mot de passe lisible : sans 0/O ni 1/l/I, facile à dicter
function generatePassword(length = 12) {
  const alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = crypto.getRandomValues(new Uint32Array(length));
  return Array.from(bytes, b => alphabet[b % alphabet.length]).join("");
}

$("gen-password").addEventListener("click", () => {
  createForm.password.value = generatePassword();
});

let users = [];

const ROLE_LABELS = Session.ROLE_LABELS;

function roleCell(u) {
  const tags = [];
  if (u.is_admin) tags.push(`<span class="tag tag-admin">Super admin</span>`);
  if (u.role) tags.push(`<span class="tag role-${u.role}">${ROLE_LABELS[u.role]}</span>`);
  return tags.join(" ");
}

function renderUsers() {
  const me = Session.user;
  usersBody.innerHTML = users.length ? users.map(u => {
    const self = u.id === me.id;
    return `
      <tr data-id="${u.id}">
        <th scope="row">${esc(u.username)}${self ? ` <span class="tag">vous</span>` : ""}</th>
        <td>${u.structure ? esc(u.structure.name) : `<span class="muted">–</span>`}</td>
        <td>${roleCell(u)}</td>
        <td>${stamp(u.created_at)}</td>
        <td>${stamp(u.last_login_at)}</td>
        <td class="actions">
          <button type="button" class="btn-quiet" data-act="password">Nouveau mot de passe</button>
          ${self && !isSuper() ? "" : `<button type="button" class="btn-quiet" data-act="edit">Modifier</button>`}
          ${self ? "" : `<button type="button" class="btn-danger" data-act="delete">Supprimer</button>`}
        </td>
      </tr>`;
  }).join("")
    : `<tr><td colspan="6" class="empty">Aucun compte.</td></tr>`;
  usersStatus.textContent = `${users.length} compte(s).`;
}

async function loadUsers() {
  const filter = isSuper() ? $("users-filter").value : "";
  try {
    users = await Session.api(`/api/admin/users${filter ? `?structure_id=${filter}` : ""}`);
    renderUsers();
  } catch (e) {
    usersStatus.textContent = e.message;
  }
}

$("users-filter").addEventListener("change", loadUsers);

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

createForm.addEventListener("submit", async e => {
  e.preventDefault();
  createStatus.textContent = "";
  const body = {
    username: createForm.username.value.trim(),
    password: createForm.password.value,
    role: createForm.role.value,
  };
  if (isSuper()) {
    body.is_admin = createForm.is_admin.checked;
    body.structure_id = createForm.structure_id.value ? Number(createForm.structure_id.value) : null;
  }
  try {
    const u = await Session.api("/api/admin/users", { method: "POST", body });
    createStatus.textContent = `Compte « ${u.username} » créé${u.structure ? ` dans « ${u.structure.name} »` : ""}. Transmettez-lui son mot de passe : il ne sera plus affiché.`;
    createForm.username.value = "";
    createForm.password.value = "";
    await Promise.all([loadUsers(), isSuper() ? loadStructures() : null]);
  } catch (err) {
    createStatus.textContent = err.message;
  }
});

// Modifier un compte : rôle (et, pour le super administrateur, structure et droit de super administrateur)
function editUser(user) {
  const self = user.id === Session.user.id;
  const sup = isSuper();
  const structOpts = `<option value="">Aucune</option>` + structures.map(st =>
    `<option value="${st.id}"${st.id === user.structure?.id ? " selected" : ""}>${esc(st.name)}</option>`).join("");
  const roleOpts = Object.entries(ROLE_LABELS).map(([v, l]) =>
    `<option value="${v}"${v === (user.role || "viewer") ? " selected" : ""}>${l}</option>`).join("");
  const form = openDialog({
    title: `Modifier ${user.username}`,
    body: `
      ${sup ? `<label>Structure <select name="structure_id">${structOpts}</select></label>` : ""}
      <label>Rôle dans la structure <select name="role"${self && !sup ? " disabled" : ""}>${roleOpts}</select></label>
      ${sup ? `<label class="check"><input type="checkbox" name="is_admin"${user.is_admin ? " checked" : ""}${self ? " disabled" : ""}> Super administrateur</label>` : ""}
      <p class="dialog-hint">${sup ? "Un compte sans structure doit être super administrateur. Ses créneaux déjà choisis restent à son ancienne structure." : "Administration : choisit les créneaux et gère la structure. Visualisation : voit les créneaux choisis."}</p>`,
    onSubmit: async f => {
      const body = { role: f.role.value };
      if (sup) {
        body.structure_id = f.structure_id.value ? Number(f.structure_id.value) : null;
        if (!self) body.is_admin = f.is_admin.checked;
      }
      await Session.api(`/api/admin/users/${user.id}`, { method: "PATCH", body });
      await Promise.all([loadUsers(), sup ? loadStructures() : null]);
      if (self) Session.init();  // ses propres droits ont pu changer
    },
  });
  if (sup) {
    const sync = () => { form.role.disabled = form.structure_id.value === ""; };
    form.structure_id.addEventListener("change", sync);
    sync();
  }
}

// Dialogue « nouveau mot de passe » (le mot de passe est affiché pour être transmis)
function askNewPassword(user) {
  const d = document.createElement("dialog");
  d.className = "account-dialog";
  d.innerHTML = `
    <form method="dialog">
      <h2>Nouveau mot de passe pour ${esc(user.username)}</h2>
      <label>Mot de passe (8 caractères min.)
        <input name="password" type="text" required minlength="8" value="${generatePassword()}" autocomplete="new-password">
      </label>
      <p class="dialog-hint">Ses sessions ouvertes seront fermées.</p>
      <p class="dialog-error" role="alert"></p>
      <div class="dialog-actions">
        <button type="button" class="btn-quiet" value="cancel">Annuler</button>
        <button type="submit" class="btn-primary">Changer le mot de passe</button>
      </div>
    </form>`;
  document.body.append(d);
  const form = d.querySelector("form");
  d.addEventListener("close", () => d.remove());
  d.querySelector("[value=cancel]").addEventListener("click", () => d.close());
  form.addEventListener("submit", async e => {
    e.preventDefault();
    try {
      await Session.api(`/api/admin/users/${user.id}`, { method: "PATCH", body: { password: form.password.value } });
      usersStatus.textContent = `Mot de passe de « ${user.username} » changé.`;
      d.close();
    } catch (err) {
      d.querySelector(".dialog-error").textContent = err.message;
    }
  });
  d.showModal();
  form.password.select();
}

usersBody.addEventListener("click", async e => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const user = users.find(u => u.id === Number(btn.closest("tr").dataset.id));
  if (!user) return;
  try {
    switch (btn.dataset.act) {
      case "password":
        askNewPassword(user);
        return;
      case "edit":
        editUser(user);
        return;
      case "delete":
        if (!confirm(`Supprimer le compte « ${user.username} » et ses préférences ? Les créneaux qu'il a choisis restent à sa structure.`)) return;
        await Session.api(`/api/admin/users/${user.id}`, { method: "DELETE" });
        break;
    }
    await Promise.all([loadUsers(), isSuper() ? loadStructures() : null]);
  } catch (err) {
    usersStatus.textContent = err.message;
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
    $("gate-login").addEventListener("click", Session.openLogin);
    return;
  }
  if (!allowed) {
    gateEl.textContent = `Le compte « ${user.username} » est en visualisation : il n'a pas accès à l'administration.`;
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

  await loadStructures();
  if (sup) {
    loadPorts();
    loadStatus();
    loadJobs().then(schedulePoll);
  }
  showTab(location.hash.slice(1));
}

Session.mountAccount(document.getElementById("account"), [Session.LINKS.search, Session.LINKS.picks]);
Session.onChange(onSessionChange);
Session.init();
