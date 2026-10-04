// Newsletters de la structure (profil « Gestionnaire ») : liste, édition avec
// aperçu, envoi immédiat ou programmé, rapport d'envoi et suivi.
// Adresses : #  (liste)   #nouvelle   #n/<id>  (édition, ou rapport si envoyée)

const $ = id => document.getElementById(id);
const esc = Session.esc;
const flashEl = $("flash");
const flash = (html = "") => { flashEl.innerHTML = html; };

const STATUS = {
  draft: ["Brouillon", "tag-pending"],
  scheduled: ["Programmée", "tag-stale"],
  queued: ["En file d'attente", "job-queued"],
  sending: ["Envoi en cours", "job-running"],
  sent: ["Envoyée", "job-succeeded"],
  failed: ["Échec", "job-failed"],
};
const statusTag = s => `<span class="tag ${STATUS[s]?.[1] || ""}">${STATUS[s]?.[0] || esc(s)}</span>`;

const fmtStamp = new Intl.DateTimeFormat("fr-FR", { dateStyle: "short", timeStyle: "short" });
const fmtLong = new Intl.DateTimeFormat("fr-FR", { dateStyle: "full", timeStyle: "short" });
const stamp = iso => (iso ? fmtStamp.format(new Date(iso)) : "–");
const pct = (n, total) => (total ? `${Math.round((100 * n) / total)} %` : "–");

let settings = null;     // état Mailjet de la structure
let newsletters = [];
let audiences = [];      // audiences proposées, avec leurs effectifs
let current = null;      // newsletter éditée ou affichée
let dirty = false;
let reportTimer = null;

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

function showView(name) {
  for (const v of ["list", "edit", "report"]) $(`view-${v}`).hidden = v !== name;
  clearTimeout(reportTimer);
  window.scrollTo(0, 0);
}

function confirmLeave() {
  return !dirty || confirm("Des modifications ne sont pas enregistrées. Quitter quand même ?");
}

let lastHash = location.hash;
window.addEventListener("hashchange", () => {
  if (!confirmLeave()) {
    history.replaceState(null, "", lastHash);
    return;
  }
  dirty = false;
  route();
});
window.addEventListener("beforeunload", e => {
  if (dirty) e.preventDefault();
});

async function route() {
  lastHash = location.hash;
  flash();
  const m = location.hash.match(/^#n\/(\d+)$/);
  if (location.hash === "#nouvelle") return openEditor(null);
  if (m) {
    try {
      const nl = await Session.api(`/api/newsletters/${m[1]}`);
      return ["draft", "scheduled"].includes(nl.status) ? openEditor(nl) : openReport(nl.id);
    } catch (e) {
      flash(esc(e.message));
    }
  }
  return openList();
}

const go = hash => { if (location.hash === hash) route(); else location.hash = hash; };

// ---------------------------------------------------------------------------
// Liste
// ---------------------------------------------------------------------------

function mailjetWarning() {
  const w = $("mailjet-warning");
  const msg = !settings.mailjet_ready || settings.mailjet_warning ? settings.mailjet_warning : null;
  w.hidden = !msg;
  if (msg) {
    w.innerHTML = esc(msg) + (settings.can_configure
      ? ` <a href="admin.html#mailjet">Configurer Mailjet</a>`
      : " Demandez à un administrateur de la structure de configurer Mailjet.");
  }
}

function dateCell(n) {
  if (n.status === "scheduled") return `programmée pour le ${stamp(n.scheduled_at)}`;
  if (n.finished_at) return `envoyée le ${stamp(n.finished_at)}`;
  if (n.started_at) return `démarrée le ${stamp(n.started_at)}`;
  return `modifiée le ${stamp(n.updated_at)}`;
}

async function openList() {
  showView("list");
  try {
    [newsletters] = await Promise.all([Session.api("/api/newsletters"), loadUnsubscribes(), loadGroups()]);
  } catch (e) {
    flash(esc(e.message));
    return;
  }
  mailjetWarning();
  $("nl-list-body").innerHTML = newsletters.length ? newsletters.map(n => {
    const s = n.stats;
    const sentish = ["sent", "sending", "failed"].includes(n.status);
    return `
      <tr>
        <th scope="row"><a href="#n/${n.id}">${esc(n.subject)}</a>
          <span class="user-sub">${esc(n.created_by || "")}</span></th>
        <td data-label="Statut">${statusTag(n.status)}</td>
        <td data-label="Destinataires">${esc(n.audience_label)}${sentish ? ` <span class="muted">(${s.total})</span>` : ""}</td>
        <td data-label="Date">${dateCell(n)}</td>
        <td class="num" data-label="Envoyés">${sentish ? s.sent : "–"}</td>
        <td class="num" data-label="Ouverts">${sentish ? `${s.opened} <span class="muted">${pct(s.opened, s.sent)}</span>` : "–"}</td>
        <td class="num" data-label="Cliqués">${sentish ? `${s.clicked} <span class="muted">${pct(s.clicked, s.sent)}</span>` : "–"}</td>
      </tr>`;
  }).join("")
    : `<tr><td colspan="7" class="empty">Aucune newsletter pour le moment. Commencez par « Nouvelle newsletter ».</td></tr>`;
}

const SOURCES = { lien: "lien de désinscription", compte: "Mon compte", plainte: "signalé comme indésirable", mailjet: "Mailjet" };

async function loadUnsubscribes() {
  const list = await Session.api("/api/newsletters/unsubscribes");
  $("unsub-count").textContent = list.length;
  $("unsub-list").innerHTML = list.length
    ? list.map(u => `<li><strong>${esc(u.email)}</strong> <span class="muted">— ${esc(SOURCES[u.source] || u.source)}, le ${stamp(u.created_at)}</span></li>`).join("")
    : `<li class="muted">Aucune désinscription.</li>`;
}

$("new-newsletter").addEventListener("click", () => go("#nouvelle"));

// ---- Groupes d'envoi ----

let groups = [];
let members = null;   // comptes de la structure, chargés au premier besoin

async function loadGroups() {
  groups = await Session.api("/api/newsletters/groups");
  $("groups-body").innerHTML = groups.length ? groups.map(g => `
    <tr data-id="${g.id}">
      <th scope="row">${esc(g.name)}${g.description ? `<span class="user-sub">${esc(g.description)}</span>` : ""}</th>
      <td class="num" data-label="Membres">${g.members}</td>
      <td class="actions">
        <button type="button" class="btn-quiet btn-small" data-act="edit">Modifier</button>
        <button type="button" class="btn-danger btn-small" data-act="delete">Supprimer</button>
      </td>
    </tr>`).join("")
    : `<tr><td colspan="3" class="empty">Aucun groupe. Créez-en un pour cibler une partie des membres.</td></tr>`;
}

const ROLE = { manager: "administration", viewer: "visualisation" };

async function editGroup(group = null) {
  members ??= await Session.api("/api/newsletters/members");
  const chosen = new Set(group?.member_ids || []);
  const d = document.createElement("dialog");
  d.className = "account-dialog nl-group-dialog";
  d.innerHTML = `
    <form method="dialog">
      <h2>${group ? `Modifier « ${esc(group.name)} »` : "Nouveau groupe d'envoi"}</h2>
      <label>Nom <input name="name" maxlength="60" required value="${esc(group?.name || "")}" placeholder="ex. Encadrants, Préparants N1"></label>
      <label>Description <span class="field-hint">(facultatif)</span> <input name="description" maxlength="200" value="${esc(group?.description || "")}"></label>
      <fieldset class="nl-members">
        <legend>Membres <span class="muted" data-count></span></legend>
        <div class="nl-members-bar">
          <label class="visually-hidden" for="member-search">Rechercher</label>
          <input id="member-search" type="search" placeholder="Rechercher un membre…">
          <button type="button" class="btn-quiet btn-small" data-all="1">Tout cocher</button>
          <button type="button" class="btn-quiet btn-small" data-all="0">Tout décocher</button>
        </div>
        <ul class="nl-member-list">${members.map(m => `
          <li data-search="${esc(`${m.name} ${m.email || ""}`.toLowerCase())}">
            <label class="check"><input type="checkbox" name="member" value="${m.id}"${chosen.has(m.id) ? " checked" : ""}>
              <span>${esc(m.name)} <span class="muted">${m.email ? esc(m.email) : "pas d'adresse e-mail"} · ${ROLE[m.role] || ""}${m.unsubscribed ? " · désinscrit" : ""}</span></span></label>
          </li>`).join("")}</ul>
      </fieldset>
      <p class="dialog-hint">Les membres sans adresse e-mail ou désinscrits restent dans le groupe mais ne reçoivent pas les newsletters.</p>
      <p class="dialog-error" role="alert"></p>
      <div class="dialog-actions">
        <button type="button" class="btn-quiet" value="cancel">Annuler</button>
        <button type="submit" class="btn-primary">${group ? "Enregistrer" : "Créer le groupe"}</button>
      </div>
    </form>`;
  document.body.append(d);
  const f = d.querySelector("form");
  const boxes = () => [...f.querySelectorAll("[name=member]")];
  const count = () => { f.querySelector("[data-count]").textContent = `(${boxes().filter(b => b.checked).length} sur ${members.length})`; };
  f.addEventListener("change", count);
  count();
  f.querySelector("#member-search").addEventListener("input", e => {
    const q = e.target.value.trim().toLowerCase();
    for (const li of f.querySelectorAll(".nl-member-list li")) li.hidden = !!q && !li.dataset.search.includes(q);
  });
  f.querySelectorAll("[data-all]").forEach(b => b.addEventListener("click", () => {
    // seulement les membres affichés (après recherche)
    for (const box of boxes()) if (!box.closest("li").hidden) box.checked = b.dataset.all === "1";
    count();
  }));
  d.addEventListener("close", () => d.remove());
  d.querySelector("[value=cancel]").addEventListener("click", () => d.close());
  f.addEventListener("submit", async ev => {
    ev.preventDefault();
    const body = {
      name: f.name.value.trim(),
      description: f.description.value.trim() || null,
      member_ids: boxes().filter(b => b.checked).map(b => Number(b.value)),
    };
    try {
      if (!body.name) throw new Error("Nom du groupe obligatoire.");
      await Session.api(group ? `/api/newsletters/groups/${group.id}` : "/api/newsletters/groups",
        { method: group ? "PUT" : "POST", body });
      d.close();
      loadGroups();
    } catch (err) {
      d.querySelector(".dialog-error").textContent = err.message;
    }
  });
  d.showModal();
  f.name.focus();
}

$("new-group").addEventListener("click", () => editGroup().catch(e => flash(esc(e.message))));
$("groups-body").addEventListener("click", async e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const g = groups.find(x => x.id === Number(btn.closest("tr").dataset.id));
  try {
    if (btn.dataset.act === "edit") await editGroup(g);
    if (btn.dataset.act === "delete") {
      if (!confirm(`Supprimer le groupe « ${g.name} » ? Les newsletters déjà envoyées ne changent pas ; un brouillon qui le vise n'aura plus de destinataires.`)) return;
      await Session.api(`/api/newsletters/groups/${g.id}`, { method: "DELETE" });
      loadGroups();
    }
  } catch (err) {
    flash(esc(err.message));
  }
});

// ---------------------------------------------------------------------------
// Édition
// ---------------------------------------------------------------------------

const form = $("nl-form");
const editStatus = $("edit-status-line");

const audienceKey = a => (a.kind === "selection" ? `selection:${a.selection_id}`
  : a.kind === "group" ? `group:${a.group_id}` : a.kind);

function renderAudiences(selected) {
  const sel = $("nl-audience");
  const key = selected ? audienceKey(selected) : "all";
  const options = [...audiences];
  // créneau passé ou supprimé : gardé tel quel dans la liste
  if (!options.some(a => audienceKey(a) === key) && selected) {
    options.push({ ...selected, label: current?.audience_label || "Créneau passé", recipients: null });
  }
  const opt = a => `<option value="${esc(audienceKey(a))}"${audienceKey(a) === key ? " selected" : ""}>${esc(a.label)}${a.recipients != null ? ` — ${a.recipients} destinataire${a.recipients > 1 ? "s" : ""}` : ""}</option>`;
  const general = options.filter(a => !["selection", "group"].includes(a.kind));
  const groups = options.filter(a => a.kind === "group");
  const slots = options.filter(a => a.kind === "selection");
  sel.innerHTML = general.map(opt).join("") +
    (groups.length ? `<optgroup label="Groupes d'envoi">${groups.map(opt).join("")}</optgroup>` : "") +
    (slots.length ? `<optgroup label="Inscrits à un créneau à venir">${slots.map(opt).join("")}</optgroup>` : "");
  audienceHint();
}

function selectedAudience() {
  const v = $("nl-audience").value;
  if (v.startsWith("selection:")) return { kind: "selection", selection_id: Number(v.split(":")[1]) };
  if (v.startsWith("group:")) return { kind: "group", group_id: Number(v.split(":")[1]) };
  return { kind: v };
}

function audienceHint() {
  const a = audiences.find(x => audienceKey(x) === $("nl-audience").value);
  $("audience-hint").textContent = a
    ? `${a.recipients} destinataire${a.recipients > 1 ? "s" : ""} avec une adresse e-mail${a.unsubscribed ? `, ${a.unsubscribed} désinscrit${a.unsubscribed > 1 ? "s" : ""} écarté${a.unsubscribed > 1 ? "s" : ""}` : ""}. La liste est figée au moment de l'envoi.`
    : "";
}

async function openEditor(nl) {
  current = nl;
  dirty = false;
  showView("edit");
  editStatus.textContent = "";
  try {
    audiences = await Session.api("/api/newsletters/audiences");
  } catch (e) {
    flash(esc(e.message));
  }
  const editable = !nl || nl.status === "draft";
  $("edit-title").textContent = nl ? "Newsletter" : "Nouvelle newsletter";
  $("edit-status").outerHTML = `<span id="edit-status">${nl ? statusTag(nl.status) : statusTag("draft")}</span>`;
  form.subject.value = nl?.subject || "";
  form.preheader.value = nl?.preheader || "";
  form.body.value = nl?.body ?? `Bonjour {{prenom}},\n\n`;
  renderAudiences(nl?.audience);
  for (const el of form.querySelectorAll("input, select, textarea, .nl-toolbar button")) el.disabled = !editable;
  $("draft-actions").hidden = !editable;
  $("scheduled-actions").hidden = nl?.status !== "scheduled";
  for (const id of ["send-test", "send-now", "schedule", "delete"]) $(id).disabled = !nl;
  if (nl?.status === "scheduled") {
    editStatus.textContent = `Envoi programmé pour le ${fmtLong.format(new Date(nl.scheduled_at))} (à 5 minutes près). Annulez la programmation pour la modifier.`;
  } else if (!nl) {
    editStatus.textContent = "Enregistrez le brouillon pour pouvoir vous envoyer un test, puis l'envoyer.";
  }
  if (!settings.mailjet_ready) flash(esc(settings.mailjet_warning));
  refreshPreview();
  if (editable) form.subject.focus();
}

const body = () => ({
  subject: form.subject.value.trim(),
  preheader: form.preheader.value.trim() || null,
  body: form.body.value,
  audience: selectedAudience(),
});

async function save() {
  if (!form.subject.value.trim()) {
    form.subject.focus();
    throw new Error("Objet obligatoire.");
  }
  if (current) {
    current = await Session.api(`/api/newsletters/${current.id}`, { method: "PATCH", body: body() });
  } else {
    current = await Session.api("/api/newsletters", { method: "POST", body: body() });
    history.replaceState(null, "", `#n/${current.id}`);
    lastHash = location.hash;
    for (const id of ["send-test", "send-now", "schedule", "delete"]) $(id).disabled = false;
  }
  dirty = false;
  return current;
}

form.addEventListener("input", () => { dirty = true; schedulePreview(); });
$("nl-audience").addEventListener("change", () => { dirty = true; audienceHint(); });

form.addEventListener("submit", async e => {
  e.preventDefault();
  try {
    await save();
    editStatus.textContent = `Brouillon enregistré à ${new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}.`;
  } catch (err) {
    editStatus.textContent = err.message;
  }
});

// Barre de mise en forme : insère la syntaxe autour de la sélection.
// Blocs (titre, liste, bouton…) : séparés du texte voisin par exactement une ligne vide.
const SNIPPETS = {
  title: sel => ["## ", sel || "Titre", "", true],
  bold: sel => ["**", sel || "texte en gras", "**"],
  italic: sel => ["*", sel || "texte en italique", "*"],
  link: sel => ["[", sel || "texte du lien", "](https://)"],
  list: sel => ["- ", sel || "premier élément", "\n- deuxième élément", true],
  button: sel => ["[[", sel || "Texte du bouton", "|https://]]", true],
  image: sel => ["![", sel || "Description de l'image", "](https://)", true],
  rule: () => ["---", "", "", true],
  slots: () => null,   // dialogue : période des créneaux (voir askSlots)
  firstname: () => ["{{prenom}}", "", ""],
};

// sauts de ligne à ajouter pour qu'il y ait une ligne vide entre le bloc et le texte voisin
function blockGap(text) {
  if (!text.trim()) return "";
  const newlines = text.match(/\n*$/)[0].length;
  return "\n".repeat(Math.max(0, 2 - newlines));
}

document.querySelector(".nl-toolbar").addEventListener("click", e => {
  const btn = e.target.closest("button[data-insert]");
  if (!btn) return;
  const ta = form.body;
  const [start, end] = [ta.selectionStart, ta.selectionEnd];
  if (btn.dataset.insert === "slots") return askSlots(start, end);
  let [before, middle, after, block] = SNIPPETS[btn.dataset.insert](ta.value.slice(start, end));
  if (block) {
    before = blockGap(ta.value.slice(0, start)) + before;
    const rest = ta.value.slice(end);
    after += rest.trim() ? "\n".repeat(Math.max(0, 2 - rest.match(/^\n*/)[0].length)) : "\n\n";
  }
  ta.setRangeText(before + middle + after, start, end, "end");
  // sélectionne le texte à remplacer (libellé par défaut), sinon place le curseur après
  ta.setSelectionRange(start + before.length, start + before.length + middle.length);
  ta.focus();
  dirty = true;
  schedulePreview();
});

// Bloc « créneaux choisis » : les N prochains jours, ou entre deux dates
function insertBlock(start, end, text) {
  const ta = form.body;
  const rest = ta.value.slice(end);
  const after = rest.trim() ? "\n".repeat(Math.max(0, 2 - rest.match(/^\n*/)[0].length)) : "\n\n";
  ta.setRangeText(blockGap(ta.value.slice(0, start)) + text + after, start, end, "end");
  ta.focus();
  dirty = true;
  schedulePreview();
}

function askSlots(start, end) {
  const iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const today = new Date();
  const d = document.createElement("dialog");
  d.className = "account-dialog";
  d.innerHTML = `
    <form method="dialog">
      <h2>Insérer les créneaux choisis</h2>
      <p class="dialog-hint">La liste des créneaux de la structure sur cette période sera calculée au moment de l'envoi, avec un lien pour s'inscrire.</p>
      <label class="check"><input type="radio" name="mode" value="days" checked> Les prochains jours, à partir du jour de l'envoi</label>
      <label>Nombre de jours <input type="number" name="days" min="1" max="366" value="30" required></label>
      <label class="check"><input type="radio" name="mode" value="range"> Entre deux dates</label>
      <label>Du <input type="date" name="from" value="${iso(today)}"></label>
      <label>Au <input type="date" name="to" value="${iso(new Date(today.getTime() + 29 * 86400e3))}"></label>
      <p class="dialog-error" role="alert"></p>
      <div class="dialog-actions">
        <button type="button" class="btn-quiet" value="cancel">Annuler</button>
        <button type="submit" class="btn-primary">Insérer</button>
      </div>
    </form>`;
  document.body.append(d);
  const f = d.querySelector("form");
  const sync = () => {
    const byDays = f.mode.value === "days";
    f.days.disabled = !byDays;
    f.from.disabled = f.to.disabled = byDays;
  };
  f.addEventListener("change", sync);
  sync();
  d.addEventListener("close", () => d.remove());
  d.querySelector("[value=cancel]").addEventListener("click", () => d.close());
  f.addEventListener("submit", ev => {
    ev.preventDefault();
    const err = d.querySelector(".dialog-error");
    let block;
    if (f.mode.value === "days") {
      const n = Number(f.days.value);
      if (!Number.isInteger(n) || n < 1 || n > 366) { err.textContent = "Entre 1 et 366 jours."; return; }
      block = `[[creneaux:${n}]]`;
    } else {
      if (!f.from.value || !f.to.value || f.to.value < f.from.value) { err.textContent = "Choisissez deux dates, la seconde après la première."; return; }
      if ((new Date(f.to.value) - new Date(f.from.value)) / 86400e3 >= 366) { err.textContent = "Une année au plus."; return; }
      block = `[[creneaux:${f.from.value}:${f.to.value}]]`;
    }
    d.close();
    insertBlock(start, end, block);
  });
  d.showModal();
}

// Aperçu (rendu par le serveur, identique à l'envoi ; iframe sans script)
let previewTimer = null;
let previewMode = "html";
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 400);
}
async function refreshPreview() {
  try {
    const p = await Session.api("/api/newsletters/preview", {
      method: "POST", body: { subject: form.subject.value, preheader: form.preheader.value || null, body: form.body.value },
    });
    $("preview-subject").innerHTML = `<strong>Objet :</strong> ${esc(p.subject)}${settings.sender ? ` · <strong>De :</strong> ${esc(settings.sender)}` : ""}`;
    $("preview-frame").srcdoc = p.html;
    $("preview-text").textContent = p.text;
  } catch (err) {
    $("preview-subject").textContent = err.message;
  }
}
document.querySelector(".nl-preview .view-switch").addEventListener("click", e => {
  const btn = e.target.closest("button[data-preview]");
  if (!btn) return;
  previewMode = btn.dataset.preview;
  for (const b of e.currentTarget.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b === btn));
  $("preview-frame").hidden = previewMode !== "html";
  $("preview-text").hidden = previewMode !== "text";
});

async function action(btn, fn) {
  btn.disabled = true;
  editStatus.textContent = "";
  try {
    await fn();
  } catch (err) {
    editStatus.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

$("send-test").addEventListener("click", e => action(e.target, async () => {
  if (dirty) await save();
  editStatus.textContent = "Envoi du test…";
  const r = await Session.api(`/api/newsletters/${current.id}/test`, { method: "POST" });
  editStatus.textContent = `Test envoyé à ${r.to}, avec « [TEST] » dans l'objet.`;
}));

function audienceCount() {
  return audiences.find(x => audienceKey(x) === $("nl-audience").value)?.recipients ?? "?";
}

$("send-now").addEventListener("click", e => action(e.target, async () => {
  if (dirty) await save();
  const n = audienceCount();
  if (!confirm(`Envoyer « ${current.subject} » maintenant à ${n} destinataire${n > 1 ? "s" : ""} ?\nL'envoi ne pourra pas être annulé.`)) return;
  await Session.api(`/api/newsletters/${current.id}/send`, { method: "POST" });
  dirty = false;
  go(`#n/${current.id}`);
}));

$("schedule").addEventListener("click", e => action(e.target, async () => {
  if (dirty) await save();
  const min = new Date(Date.now() + 6 * 60000);
  const local = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const d = document.createElement("dialog");
  d.className = "account-dialog";
  d.innerHTML = `
    <form method="dialog">
      <h2>Programmer l'envoi</h2>
      <label>Date et heure d'envoi <input type="datetime-local" name="at" required min="${local(min)}" value="${local(new Date(min.getTime() + 54 * 60000))}"></label>
      <p class="dialog-hint">Envoi à ${audienceCount()} destinataire(s), à 5 minutes près. Jusque-là, vous pouvez annuler la programmation.</p>
      <p class="dialog-error" role="alert"></p>
      <div class="dialog-actions">
        <button type="button" class="btn-quiet" value="cancel">Annuler</button>
        <button type="submit" class="btn-primary">Programmer</button>
      </div>
    </form>`;
  document.body.append(d);
  d.addEventListener("close", () => d.remove());
  d.querySelector("[value=cancel]").addEventListener("click", () => d.close());
  d.querySelector("form").addEventListener("submit", async ev => {
    ev.preventDefault();
    try {
      // heure saisie dans le fuseau du navigateur, transmise avec ce fuseau (gestionnaire en voyage…)
      const at = new Date(ev.target.at.value);
      if (Number.isNaN(at.getTime())) throw new Error("Date et heure d'envoi obligatoires.");
      current = await Session.api(`/api/newsletters/${current.id}/schedule`, { method: "POST", body: { at: at.toISOString() } });
      d.close();
      openEditor(current);
    } catch (err) {
      d.querySelector(".dialog-error").textContent = err.message;
    }
  });
  d.showModal();
}));

$("unschedule").addEventListener("click", e => action(e.target, async () => {
  current = await Session.api(`/api/newsletters/${current.id}/unschedule`, { method: "POST" });
  openEditor(current);
  editStatus.textContent = "Programmation annulée : la newsletter est redevenue un brouillon.";
}));

async function duplicate() {
  const copy = await Session.api(`/api/newsletters/${current.id}/duplicate`, { method: "POST" });
  go(`#n/${copy.id}`);
}
$("duplicate-scheduled").addEventListener("click", e => action(e.target, duplicate));

async function remove() {
  if (!confirm(`Supprimer « ${current.subject} »${current.status === "sent" ? " et ses statistiques d'envoi" : ""} ?`)) return;
  await Session.api(`/api/newsletters/${current.id}`, { method: "DELETE" });
  dirty = false;
  go("#");
}
$("delete").addEventListener("click", e => action(e.target, remove));

// ---------------------------------------------------------------------------
// Rapport d'envoi
// ---------------------------------------------------------------------------

let report = null;

const TILES = [
  ["total", "Destinataires"],
  ["sent", "Envoyés"],
  ["delivered", "Délivrés", "sent"],
  ["opened", "Ouverts", "sent"],
  ["clicked", "Cliqués", "sent"],
  ["bounced", "Rebonds", "sent", "bad"],
  ["blocked", "Bloqués", "sent", "bad"],
  ["spam", "Indésirables", "sent", "bad"],
  ["unsubscribed", "Désinscrits", "sent", "bad"],
  ["failed", "Refusés", "total", "bad"],
];

function recipientState(r) {
  if (r.status === "failed") return ["refusé", "job-failed"];
  if (r.status === "queued") return ["en attente", "job-queued"];
  if (r.spam_at) return ["indésirable", "job-failed"];
  if (r.bounced_at) return ["rebond", "job-failed"];
  if (r.blocked_at) return ["bloqué", "job-failed"];
  if (r.unsubscribed_at) return ["désinscrit", "tag-warn"];
  if (r.clicked_at) return ["a cliqué", "job-succeeded"];
  if (r.opened_at) return ["a ouvert", "job-succeeded"];
  if (r.delivered_at) return ["délivré", "tag-pending"];
  return ["envoyé", "tag-pending"];
}

const FILTERS = {
  opened: r => !!r.opened_at,
  unopened: r => r.status === "sent" && !r.opened_at,
  clicked: r => !!r.clicked_at,
  problem: r => r.status === "failed" || r.bounced_at || r.blocked_at || r.spam_at,
  unsubscribed: r => !!r.unsubscribed_at,
};

function visibleRecipients() {
  const f = FILTERS[$("recip-filter").value];
  const q = $("recip-search").value.trim().toLowerCase();
  return report.recipients.filter(r => (!f || f(r)) && (!q || [r.email, r.name].some(v => v && v.toLowerCase().includes(q))));
}

function renderRecipients() {
  const list = visibleRecipients();
  $("recip-body").innerHTML = list.length ? list.map(r => {
    const [label, cls] = recipientState(r);
    return `
      <tr>
        <th scope="row">${esc(r.name || r.email)}${r.name ? `<span class="user-sub">${esc(r.email)}</span>` : ""}</th>
        <td data-label="État"><span class="tag ${cls}">${label}</span></td>
        <td data-label="Ouvert">${r.opened_at ? `${stamp(r.opened_at)}${r.open_count > 1 ? ` <span class="muted">×${r.open_count}</span>` : ""}` : `<span class="muted">–</span>`}</td>
        <td data-label="Cliqué">${r.clicked_at ? `${stamp(r.clicked_at)}${r.click_count > 1 ? ` <span class="muted">×${r.click_count}</span>` : ""}` : `<span class="muted">–</span>`}</td>
        <td data-label="Détail" class="muted">${esc(r.error || "")}</td>
      </tr>`;
  }).join("")
    : `<tr><td colspan="5" class="empty">${report.recipients.length ? "Aucun destinataire avec ce filtre." : "Les destinataires apparaîtront au début de l'envoi."}</td></tr>`;
  $("recip-count").textContent = `${list.length} destinataire(s) affiché(s) sur ${report.recipients.length}.`;
}

$("recip-filter").addEventListener("change", renderRecipients);
$("recip-search").addEventListener("input", renderRecipients);

async function openReport(id) {
  showView("report");
  await loadReport(id);
}

async function loadReport(id) {
  clearTimeout(reportTimer);
  try {
    report = await Session.api(`/api/newsletters/${id}/report`);
  } catch (e) {
    flash(esc(e.message));
    return;
  }
  const n = current = report.newsletter;
  const s = n.stats;
  $("report-title").textContent = n.subject;
  $("report-status").outerHTML = `<span id="report-status">${statusTag(n.status)}</span>`;
  $("report-meta").textContent = [
    n.audience_label,
    n.sent_by && `envoi lancé par ${n.sent_by}`,
    n.started_at && `démarré le ${stamp(n.started_at)}`,
    n.finished_at && `terminé le ${stamp(n.finished_at)}`,
  ].filter(Boolean).join(" · ");
  $("report-error").hidden = !n.error;
  $("report-error").textContent = n.error || "";
  const running = ["queued", "sending"].includes(n.status);
  const progress = $("report-progress");
  progress.hidden = !running;
  progress.firstElementChild.style.width = s.total ? `${Math.round((100 * (s.sent + s.failed)) / s.total)}%` : "5%";
  $("report-tiles").innerHTML = TILES.map(([key, label, base, bad]) => `
    <li class="nl-tile${bad && s[key] ? " bad" : ""}">
      <span class="nl-tile-value">${s[key]}</span>
      <span class="nl-tile-label">${label}${base && s[base] ? ` · ${pct(s[key], s[base])}` : ""}</span>
    </li>`).join("");
  $("report-tracking").textContent = settings.tracking
    ? "Délivrés, ouverts, cliqués, rebonds et désinscriptions arrivent de Mailjet au fil de l'eau : actualisez pour les voir. Les ouvertures sont sous-estimées (images bloquées par certaines messageries)."
    : "Le suivi Mailjet n'est pas activé : seuls les envois et refus sont connus. Un administrateur peut l'activer dans Administration → Mailjet.";
  $("report-resume").hidden = !(["failed", "sending"].includes(n.status) && s.queued > 0);
  $("report-delete").hidden = running;
  $("links-panel").hidden = !report.links.length;
  $("links-body").innerHTML = report.links.map(l => `
    <tr><th scope="row"><a href="${esc(l.url)}" rel="noopener" target="_blank">${esc(l.url)}</a></th>
      <td class="num">${l.clicks}</td><td class="num">${l.people}</td></tr>`).join("");
  renderRecipients();
  if (running) reportTimer = setTimeout(() => loadReport(id), 4000);   // suit l'envoi en cours
}

$("report-refresh").addEventListener("click", () => loadReport(current.id));
$("report-duplicate").addEventListener("click", e => action(e.target, duplicate));
$("report-delete").addEventListener("click", async () => {
  try { await remove(); } catch (err) { flash(esc(err.message)); }
});
$("report-resume").addEventListener("click", async e => {
  e.target.disabled = true;
  try {
    await Session.api(`/api/newsletters/${current.id}/resume`, { method: "POST" });
    await loadReport(current.id);
  } catch (err) {
    flash(esc(err.message));
  } finally {
    e.target.disabled = false;
  }
});

$("report-content").addEventListener("click", async () => {
  const nl = await Session.api(`/api/newsletters/${current.id}`);
  const p = await Session.api("/api/newsletters/preview", {
    method: "POST", body: { subject: nl.subject, preheader: nl.preheader, body: nl.body },
  });
  const d = document.createElement("dialog");
  d.className = "account-dialog nl-content-dialog";
  d.innerHTML = `
    <form method="dialog">
      <h2>${esc(p.subject)}</h2>
      <iframe class="nl-frame" title="Contenu de la newsletter" sandbox=""></iframe>
      <div class="dialog-actions"><button type="submit" class="btn-primary">Fermer</button></div>
    </form>`;
  d.querySelector("iframe").srcdoc = p.html;
  document.body.append(d);
  d.addEventListener("close", () => d.remove());
  d.showModal();
});

$("report-export").addEventListener("click", () => {
  const list = visibleRecipients();
  if (!list.length) return;
  const columns = [
    { header: "Nom", width: 22, value: r => r.name || "" },
    { header: "Adresse", width: 28, value: r => r.email },
    { header: "État", width: 14, value: r => recipientState(r)[0] },
    { header: "Envoyé le", width: 17, value: r => stamp(r.sent_at) },
    { header: "Délivré le", width: 17, value: r => stamp(r.delivered_at) },
    { header: "Ouvert le", width: 17, value: r => stamp(r.opened_at) },
    { header: "Ouvertures", type: "int", width: 11, value: r => r.open_count },
    { header: "Cliqué le", width: 17, value: r => stamp(r.clicked_at) },
    { header: "Clics", type: "int", width: 8, value: r => r.click_count },
    { header: "Détail", width: 36, value: r => r.error || "" },
  ];
  XlsxExport.download(`newsletter-${XlsxExport.slug(current.subject)}.xlsx`, "Destinataires", columns, list);
});

// ---------------------------------------------------------------------------
// Démarrage
// ---------------------------------------------------------------------------

async function onSessionChange(user) {
  const allowed = !!user?.can.newsletters;
  const gate = $("gate");
  gate.hidden = allowed;
  for (const v of ["list", "edit", "report"]) if (!allowed) $(`view-${v}`).hidden = true;
  if (!user) {
    gate.innerHTML = `Connectez-vous pour gérer les newsletters de votre structure. <button type="button" class="btn-primary" id="gate-login">Se connecter</button>`;
    $("gate-login").addEventListener("click", () => Session.openLogin());
    return;
  }
  if (!allowed) {
    gate.textContent = "Les newsletters sont réservées au profil « Gestionnaire » de la structure. Un administrateur de la structure peut vous l'attribuer (Administration → Utilisateurs).";
    return;
  }
  try {
    settings = await Session.api("/api/newsletters/settings");
  } catch (e) {
    flash(esc(e.message));
    return;
  }
  $("structure-name").textContent = `· ${settings.structure}`;
  route();
}

Session.mountAccount($("account"), [Session.LINKS.search, Session.LINKS.picks, Session.LINKS.admin]);
Session.onChange(onSessionChange);
Session.init();
