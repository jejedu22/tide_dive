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

  const importSel = $("import-structure");
  const keepImport = importSel.options.length ? importSel.value : String(Session.user?.structure?.id ?? "");
  importSel.innerHTML = `<option value="">Aucune (colonne « structure » obligatoire)</option>` + opts(null);
  importSel.value = structures.some(st => String(st.id) === keepImport) ? keepImport : (structures[0] ? String(structures[0].id) : "");

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

// Année calculée : son modèle en info-bulle, signalée si ce n'est pas le modèle actuel
function yearTag(year, model) {
  const current = status?.fes_model;
  if (!model) return `<span class="tag" title="Modèle non enregistré (calcul antérieur)">${year}</span>`;
  if (current && model !== current) {
    return `<span class="tag tag-stale" title="Calculée avec ${esc(model)} ; modèle actuel : ${esc(current)}. Relancez le calcul pour la mettre à jour.">${year} · ${esc(model)}</span>`;
  }
  return `<span class="tag" title="Calculée avec ${esc(model)}">${year}</span>`;
}

function renderPorts() {
  portsBody.innerHTML = ports.length ? ports.map(p => {
    const years = p.years.length
      ? p.years.map(y => yearTag(y, p.year_models[y])).join(" ")
      : `<span class="muted">aucune</span>`;
    const offset = p.offset_zh_m != null
      ? `${fmtNum(p.offset_zh_m, 2)} m`
      : `<span class="tag job-failed" title="Obligatoire pour calculer">à renseigner</span>`;
    return `
      <tr data-id="${p.id}">
        <th scope="row">${esc(p.name)}</th>
        <td class="muted" data-label="Coordonnées">${fmtNum(p.latitude, 4)}, ${fmtNum(p.longitude, 4)}</td>
        <td class="num" data-label="NM / ZH">${offset}</td>
        <td data-label="Années calculées">${years}</td>
        <td data-label="Recalcul annuel"><input type="checkbox" data-act="auto" ${p.auto_precompute ? "checked" : ""} aria-label="Recalcul annuel de ${esc(p.name)}"></td>
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

function loadSettings() {
  const st = scopedStructure();
  settingsForm.hidden = !st;
  $("settings-form-status").textContent = "";
  if (!st) return;
  for (const [k, input] of Object.entries(lockInputs)) input.value = st[k] ?? "";
  const offset = st.rdv_offset_minutes ?? 120;
  rdvInputs.hours.value = Math.floor(offset / 60);
  rdvInputs.minutes.value = offset % 60;
  renderRdvPreview();
  renderLockPreview();
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
  body.rdv_offset_minutes = offset;
  status.textContent = "";
  try {
    const saved = await Session.api(`/api/admin/structures/${st.id}/settings`, { method: "PATCH", body });
    Object.assign(st, saved);
    loadSettings();
    $("settings-form-status").textContent = "Réglages enregistrés.";
    // sa propre structure : l'heure de RDV affichée ailleurs suit le nouveau délai
    if (Session.user?.structure?.id === st.id) Session.user.structure.rdv_offset_minutes = saved.rdv_offset_minutes;
  } catch (err) {
    status.textContent = err.message;
  }
});

async function loadTypes() {
  const noStructure = isSuper() && !typesScope;
  typeForm.hidden = noStructure;
  loadSettings();
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
          ${self ? "" : `<button type="button" class="btn-quiet" data-act="password">Mot de passe…</button>`}
          ${self ? "" : `<button type="button" class="btn-danger" data-act="delete">Supprimer</button>`}
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
  } catch (e) {
    usersStatus.textContent = e.message;
  }
}

$("users-filter").addEventListener("change", loadUsers);
$("users-search").addEventListener("input", renderUsers);

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

function editUser(user) {
  const self = user.id === Session.user.id;
  const sup = isSuper();
  const structOpts = `<option value="">Aucune</option>` + structures.map(st =>
    `<option value="${st.id}"${st.id === user.structure?.id ? " selected" : ""}>${esc(st.name)}</option>`).join("");
  const roleOpts = Object.entries(ROLE_LABELS).map(([v, l]) =>
    `<option value="${v}"${v === (user.role || "viewer") ? " selected" : ""}>${l}</option>`).join("");
  const form = openDialog({
    title: `Modifier ${user.display_name}`,
    body: `
      <p class="dialog-hint">Identifiant : <strong>${esc(user.username)}</strong></p>
      <label>Prénom <input name="first_name" maxlength="60" required value="${esc(user.first_name ?? "")}"></label>
      <label>Nom <input name="last_name" maxlength="60" required value="${esc(user.last_name ?? "")}"></label>
      <label>Adresse e-mail <input name="email" type="email" maxlength="254" required value="${esc(user.email ?? "")}"></label>
      <label>Téléphone <input name="phone" type="tel" maxlength="30" value="${esc(user.phone ?? "")}"></label>
      ${sup ? `<label>Structure <select name="structure_id">${structOpts}</select></label>` : ""}
      <label>Rôle dans la structure <select name="role"${self && !sup ? " disabled" : ""}>${roleOpts}</select></label>
      ${sup ? `<label class="check"><input type="checkbox" name="is_admin"${user.is_admin ? " checked" : ""}${self ? " disabled" : ""}> Super administrateur</label>` : ""}
      <p class="dialog-hint">${sup ? "Un compte sans structure doit être super administrateur. Ses créneaux déjà choisis restent à son ancienne structure." : "Administration : choisit les créneaux et gère la structure. Visualisation : voit les créneaux choisis."}</p>`,
    onSubmit: async f => {
      const body = {
        first_name: f.first_name.value.trim(),
        last_name: f.last_name.value.trim(),
        email: f.email.value.trim(),
        phone: f.phone.value.trim() || null,
      };
      // champs vides d'un ancien compte : laissés tels quels plutôt que refusés
      for (const k of ["first_name", "last_name", "email"]) if (!body[k]) delete body[k];
      if (!(self && !sup)) body.role = f.role.value;
      if (sup) {
        body.structure_id = f.structure_id.value ? Number(f.structure_id.value) : null;
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

  setupCreateForm();
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
