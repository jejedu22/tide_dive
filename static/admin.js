// Administration : ports, données et tâches (précalcul, FES, vacances), utilisateurs.
// Toutes les vérifications de droits sont faites côté serveur (/api/admin/*).
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

// ---------------------------------------------------------------------------
// Onglets
// ---------------------------------------------------------------------------

const TABS = ["ports", "donnees", "utilisateurs"];
let activeTab = "ports";

function showTab(name) {
  if (!TABS.includes(name)) name = "ports";
  activeTab = name;
  for (const btn of document.querySelectorAll("[role=tab]")) {
    btn.setAttribute("aria-selected", String(btn.dataset.tab === name));
  }
  for (const t of TABS) $(`tab-${t}`).hidden = t !== name;
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "donnees") { loadStatus(); loadJobs(); }
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

function renderUsers() {
  const me = Session.user;
  usersBody.innerHTML = users.map(u => {
    const self = u.id === me.id;
    return `
      <tr data-id="${u.id}">
        <th scope="row">${esc(u.username)}${self ? ` <span class="tag">vous</span>` : ""}</th>
        <td>${u.is_admin ? `<span class="tag tag-admin">Admin</span>` : "Utilisateur"}</td>
        <td>${stamp(u.created_at)}</td>
        <td>${stamp(u.last_login_at)}</td>
        <td class="actions">
          <button type="button" class="btn-quiet" data-act="password">Nouveau mot de passe</button>
          ${self ? "" : `
            <button type="button" class="btn-quiet" data-act="toggle-admin">${u.is_admin ? "Retirer les droits admin" : "Rendre admin"}</button>
            <button type="button" class="btn-danger" data-act="delete">Supprimer</button>`}
        </td>
      </tr>`;
  }).join("");
  usersStatus.textContent = `${users.length} compte(s).`;
}

async function loadUsers() {
  try {
    users = await Session.api("/api/admin/users");
    renderUsers();
  } catch (e) {
    usersStatus.textContent = e.message;
  }
}

createForm.addEventListener("submit", async e => {
  e.preventDefault();
  createStatus.textContent = "";
  const body = {
    username: createForm.username.value.trim(),
    password: createForm.password.value,
    is_admin: createForm.is_admin.checked,
  };
  try {
    const u = await Session.api("/api/admin/users", { method: "POST", body });
    createStatus.textContent = `Compte « ${u.username} » créé. Transmettez-lui son mot de passe : il ne sera plus affiché.`;
    createForm.reset();
    await loadUsers();
  } catch (err) {
    createStatus.textContent = err.message;
  }
});

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
      case "toggle-admin":
        await Session.api(`/api/admin/users/${user.id}`, { method: "PATCH", body: { is_admin: !user.is_admin } });
        break;
      case "delete":
        if (!confirm(`Supprimer le compte « ${user.username} » et ses préférences ?`)) return;
        await Session.api(`/api/admin/users/${user.id}`, { method: "DELETE" });
        break;
    }
    await loadUsers();
  } catch (err) {
    usersStatus.textContent = err.message;
  }
});

// ---------------------------------------------------------------------------
// Démarrage
// ---------------------------------------------------------------------------

function onSessionChange(user) {
  const allowed = !!user && user.is_admin;
  adminEl.hidden = !allowed;
  gateEl.hidden = allowed;
  clearTimeout(pollTimer);
  if (!user) {
    gateEl.innerHTML = `Connectez-vous avec un compte administrateur. <button type="button" class="btn-primary" id="gate-login">Se connecter</button>`;
    $("gate-login").addEventListener("click", Session.openLogin);
  } else if (!user.is_admin) {
    gateEl.textContent = `Le compte « ${user.username} » n'a pas accès à l'administration.`;
  } else {
    loadPorts();
    loadUsers();
    loadStatus();
    loadJobs().then(schedulePoll);
    showTab(location.hash.slice(1));
  }
}

Session.mountAccount(document.getElementById("account"));
Session.onChange(onSessionChange);
Session.init();
