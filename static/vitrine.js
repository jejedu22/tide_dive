// Page vitrine de la structure : édition par blocs, photos, brouillon, aperçu et publication (app/site.py).
// Réservée aux administrateurs de la structure et au profil « Vitrine ».

const $ = id => document.getElementById(id);
const esc = s => Session.esc(s);
let site = null;      // réponse de GET /api/site
let draft = null;     // brouillon en cours de modification
let dirty = false;

const TYPES = {
  hero: { label: "Bandeau d'accueil", make: () => ({ type: "hero", title: site.structure_name, subtitle: "", image: null, button: "dives", button_label: "Prochaines plongées" }) },
  text: { label: "Texte", make: () => ({ type: "text", title: "", body: "Votre texte…" }) },
  gallery: { label: "Galerie photos", make: () => ({ type: "gallery", title: "Photos", photos: [] }) },
  dives: { label: "Prochaines plongées", make: () => ({ type: "dives", title: "Prochaines plongées", days: 60, max_items: 8, guest_link: true }) },
  pricing: { label: "Activités et tarifs", make: () => ({ type: "pricing", title: "Activités et tarifs", items: [{ label: "Baptême", price: "", detail: "" }] }) },
  join: { label: "Nous rejoindre", make: () => ({ type: "join", title: "Nous rejoindre", text: "" }) },
  contact: { label: "Contact et accès", make: () => ({ type: "contact", title: "Contact et accès", text: "", show_map: true, links: { facebook: null, instagram: null, youtube: null, website: null } }) },
};
const BUTTONS = { none: "Aucun bouton", dives: "Vers les prochaines plongées", join: "Vers « Nous rejoindre » (lien d'adhésion)", contact: "Vers le contact" };

function flash(html) { $("flash").innerHTML = html; }
function status(text) { $("vb-status").textContent = text; }
function changed() { dirty = true; status("Modifications non enregistrées."); }

// ---- Champs ----

const field = (label, input, hint = "") => `<label class="vb-field">${label}${input}${hint ? `<small class="hint">${hint}</small>` : ""}</label>`;
const textInput = (path, value, max, required = false) =>
  `<input data-path="${path}" maxlength="${max}" value="${esc(value ?? "")}"${required ? " required" : ""}>`;
const area = (path, value, max, rows = 4) => `<textarea data-path="${path}" maxlength="${max}" rows="${rows}">${esc(value ?? "")}</textarea>`;
const check = (path, value, label) => `<label class="check"><input type="checkbox" data-path="${path}"${value ? " checked" : ""}> ${label}</label>`;
const imageSelect = (path, value, allowNone = true) => `<select data-path="${path}">
  ${allowNone ? `<option value="">— aucune —</option>` : ""}${site.images.map((img, i) =>
    `<option value="${esc(img.key)}"${img.key === value ? " selected" : ""}>Photo ${i + 1}</option>`).join("")}</select>
  ${value ? `<img class="vb-thumb" src="/api/sites/images/${esc(value)}" alt="">` : ""}`;

function blockForm(b, i) {
  const p = k => `${i}.${k}`;
  switch (b.type) {
    case "hero":
      return field("Titre", textInput(p("title"), b.title, 80, true)) + field("Accroche", area(p("subtitle"), b.subtitle, 200, 2))
        + field("Photo de fond", imageSelect(p("image"), b.image), site.images.length ? "" : "Ajoutez d'abord des photos (en bas de la page).")
        + field("Bouton", `<select data-path="${p("button")}">${Object.entries(BUTTONS).map(([k, v]) =>
          `<option value="${k}"${b.button === k ? " selected" : ""}>${v}</option>`).join("")}</select>`)
        + field("Texte du bouton", textInput(p("button_label"), b.button_label, 40));
    case "text":
      return field("Titre", textInput(p("title"), b.title, 80)) + field("Texte", area(p("body"), b.body, 5000, 8));
    case "gallery":
      return field("Titre", textInput(p("title"), b.title, 80))
        + `<ol class="vb-list">${b.photos.map((ph, j) => `<li>${imageSelect(`${i}.photos.${j}.image`, ph.image, false)}
            ${textInput(`${i}.photos.${j}.caption`, ph.caption, 120)}
            <button type="button" class="btn-quiet btn-small" data-act="remove-item" data-list="${i}.photos" data-index="${j}">Retirer</button></li>`).join("")}</ol>`
        + (site.images.length ? `<button type="button" class="btn-quiet btn-small" data-act="add-photo" data-block="${i}">Ajouter une photo à la galerie</button>`
          : `<p class="hint">Ajoutez d'abord des photos (en bas de la page).</p>`);
    case "dives":
      return field("Titre", textInput(p("title"), b.title, 80))
        + field("Période", `<input type="number" data-path="${p("days")}" data-number min="7" max="180" value="${b.days}"> jours`)
        + field("Nombre de plongées au plus", `<input type="number" data-path="${p("max_items")}" data-number min="1" max="30" value="${b.max_items}">`)
        + check(p("guest_link"), b.guest_link, `Bouton « S'inscrire à une plongée » pour les personnes extérieures${site.has_guest_link ? "" : " (activez d'abord leur inscription dans Ma structure)"}`)
        + `<p class="hint">Les plongées s'affichent toutes seules, d'après les créneaux de la structure.</p>`;
    case "pricing":
      return field("Titre", textInput(p("title"), b.title, 80))
        + `<ol class="vb-list">${b.items.map((it, j) => `<li class="vb-price">
            ${textInput(`${i}.items.${j}.label`, it.label, 80, true).replace("<input", '<input placeholder="Activité"')}
            ${textInput(`${i}.items.${j}.price`, it.price, 30).replace("<input", '<input placeholder="Prix (ex. 35 €)"')}
            ${textInput(`${i}.items.${j}.detail`, it.detail, 200).replace("<input", '<input placeholder="Précision (facultatif)"')}
            <button type="button" class="btn-quiet btn-small" data-act="remove-item" data-list="${i}.items" data-index="${j}">Retirer</button></li>`).join("")}</ol>
          <button type="button" class="btn-quiet btn-small" data-act="add-price" data-block="${i}">Ajouter une ligne</button>`;
    case "join":
      return field("Titre", textInput(p("title"), b.title, 80)) + field("Texte", area(p("text"), b.text, 500, 3))
        + (site.has_join_link ? "" : `<p class="hint">Ce bloc n'apparaît qu'une fois le lien d'adhésion activé (Ma structure).</p>`);
    case "contact":
      return field("Titre", textInput(p("title"), b.title, 80)) + field("Texte", area(p("text"), b.text, 500, 2))
        + check(p("show_map"), b.show_map, "Lien vers la carte du port d'attache")
        + `<p class="hint">Adresse, téléphone et e-mail : ceux de la fiche de la structure (Ma structure).</p>`
        + ["facebook", "instagram", "youtube", "website"].map(k => field({ facebook: "Facebook", instagram: "Instagram", youtube: "YouTube", website: "Autre site" }[k],
          textInput(`${i}.links.${k}`, b.links?.[k], 300))).join("");
    default:
      return "";
  }
}

function summary(b) {
  return b.title || (b.type === "text" ? b.body.slice(0, 50) : "");
}

function render() {
  const f = $("vb-settings");
  f.slug.value = draft.slug;
  f.title.value = draft.title ?? "";
  f.description.value = draft.description ?? "";
  $("vb-blocks").innerHTML = draft.blocks.map((b, i) => `<li class="vb-block${b.hidden ? " vb-hidden" : ""}" data-index="${i}">
      <div class="vb-block-head">
        <strong>${esc(TYPES[b.type]?.label || b.type)}</strong><span class="muted">${esc(summary(b))}</span>
        <span class="vb-tools">
          <button type="button" class="btn-quiet btn-small" data-act="up" ${i === 0 ? "disabled" : ""} aria-label="Monter">↑</button>
          <button type="button" class="btn-quiet btn-small" data-act="down" ${i === draft.blocks.length - 1 ? "disabled" : ""} aria-label="Descendre">↓</button>
          <button type="button" class="btn-quiet btn-small" data-act="toggle">${b.hidden ? "Afficher" : "Masquer"}</button>
          <button type="button" class="btn-danger btn-small" data-act="delete">Supprimer</button>
        </span>
      </div>
      <div class="vb-block-body">${blockForm(b, i)}</div></li>`).join("")
    || `<li class="empty">Aucun bloc : ajoutez-en un.</li>`;
  $("vb-images").innerHTML = site.images.map((img, i) => `<li><img src="${esc(img.url)}" alt="Photo ${i + 1}">
      <span>Photo ${i + 1} · ${Math.round(img.size / 1024)} Ko</span>
      <button type="button" class="btn-quiet btn-small" data-key="${esc(img.key)}">Supprimer</button></li>`).join("")
    || `<li class="empty">Aucune photo.</li>`;
  renderState();
}

function renderState() {
  const st = $("vb-state");
  st.textContent = site.published ? (site.unpublished_changes ? "Publiée · modifications à publier" : "Publiée") : "Non publiée";
  st.className = `tag ${site.published ? "job-succeeded" : "tag-warn"}`;
  $("vb-url").innerHTML = site.published
    ? `Page publique : <a href="${esc(site.public_url)}" target="_blank" rel="noopener">${esc(site.public_url)}</a>`
    : `Adresse une fois publiée : <code>${esc(site.public_url)}</code>`;
  $("vb-unpublish").hidden = !site.published;
  $("vb-slug-prefix").textContent = site.public_url.replace(/[^/]+$/, "");
}

// chemin « 2.photos.0.caption » → valeur dans le brouillon
function setPath(path, value) {
  const keys = path.split(".");
  let obj = draft.blocks;
  for (const k of keys.slice(0, -1)) obj = obj[k];
  obj[keys.at(-1)] = value;
}

$("vb-blocks").addEventListener("input", e => {
  const el = e.target.closest("[data-path]");
  if (!el) return;
  const value = el.type === "checkbox" ? el.checked : el.hasAttribute("data-number") ? Number(el.value)
    : el.tagName === "SELECT" && el.value === "" ? null : el.value;
  setPath(el.dataset.path, value);
  changed();
  if (el.tagName === "SELECT" && el.dataset.path.endsWith("image")) render();   // vignette à jour
});
$("vb-blocks").addEventListener("change", e => { if (e.target.type === "checkbox") e.target.dispatchEvent(new Event("input", { bubbles: true })); });

$("vb-blocks").addEventListener("click", e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const li = btn.closest(".vb-block");
  const i = Number(li?.dataset.index);
  const act = btn.dataset.act;
  if (act === "up" || act === "down") {
    const j = act === "up" ? i - 1 : i + 1;
    [draft.blocks[i], draft.blocks[j]] = [draft.blocks[j], draft.blocks[i]];
  } else if (act === "toggle") {
    draft.blocks[i].hidden = !draft.blocks[i].hidden;
  } else if (act === "delete") {
    if (!confirm("Supprimer ce bloc ?")) return;
    draft.blocks.splice(i, 1);
  } else if (act === "add-photo") {
    draft.blocks[Number(btn.dataset.block)].photos.push({ image: site.images[0].key, caption: "" });
  } else if (act === "add-price") {
    draft.blocks[Number(btn.dataset.block)].items.push({ label: "", price: "", detail: "" });
  } else if (act === "remove-item") {
    const [bi, list] = btn.dataset.list.split(".");
    draft.blocks[Number(bi)][list].splice(Number(btn.dataset.index), 1);
  }
  changed();
  render();
});

$("vb-settings").addEventListener("input", e => {
  const el = e.target;
  draft[el.name] = el.name === "slug" ? el.value.trim().toLowerCase() : (el.value.trim() || null);
  changed();
});

$("vb-add").addEventListener("click", () => {
  draft.blocks.push(TYPES[$("vb-add-type").value].make());
  changed();
  render();
  $("vb-blocks").lastElementChild?.scrollIntoView({ behavior: "smooth", block: "center" });
});

// ---- Enregistrer, aperçu, publier ----

async function save() {
  site = await Session.api("/api/site", { method: "PUT", body: draft });
  draft = site.draft;
  dirty = false;
  render();
}

async function run(action, done) {
  try {
    await action();
    status(done);
  } catch (err) {
    status(err.message);
  }
}

$("vb-save").addEventListener("click", () => run(save, "Brouillon enregistré."));
$("vb-preview").addEventListener("click", async () => {
  const tab = window.open("about:blank", "_blank");       // ouvert tout de suite : pas bloqué par le navigateur
  try {
    await save();
    if (tab) tab.location = `/club/${encodeURIComponent(site.slug)}?apercu=1`;
    status("Brouillon enregistré : aperçu ouvert dans un nouvel onglet.");
  } catch (err) {
    tab?.close();
    status(err.message);
  }
});
$("vb-publish").addEventListener("click", () => run(async () => {
  await save();
  site = await Session.api("/api/site/publish", { method: "POST" });
  draft = site.draft;
  render();
}, "Page publiée."));
$("vb-unpublish").addEventListener("click", () => {
  if (!confirm("Retirer la page publique ? Le brouillon est gardé.")) return;
  run(async () => {
    site = await Session.api("/api/site/publish", { method: "DELETE" });
    render();
  }, "Page retirée du public.");
});

// ---- Photos ----

$("vb-upload").addEventListener("change", async e => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (file.size > 1024 * 1024) { status("Photo trop lourde : 1 Mo au plus."); return; }
  try {
    const res = await fetch("/api/site/images", { method: "POST", credentials: "same-origin", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error(data?.detail || `Erreur ${res.status}`);
    site.images.push(data);
    render();
    status("Photo ajoutée.");
  } catch (err) {
    status(err.message);
  }
});

$("vb-images").addEventListener("click", async e => {
  const btn = e.target.closest("button[data-key]");
  if (!btn || !confirm("Supprimer cette photo ?")) return;
  try {
    await Session.api(`/api/site/images/${encodeURIComponent(btn.dataset.key)}`, { method: "DELETE" });
    site.images = site.images.filter(i => i.key !== btn.dataset.key);
    render();
    status("Photo supprimée.");
  } catch (err) {
    status(err.message);
  }
});

window.addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

// ---- Démarrage ----

async function onSessionChange(user) {
  const allowed = !!user?.can.edit_site;
  $("gate").hidden = allowed;
  $("editor").hidden = true;
  if (!user) {
    $("gate").innerHTML = `Connectez-vous pour modifier la page vitrine de votre structure. <button type="button" class="btn-primary" id="gate-login">Se connecter</button>`;
    $("gate-login").addEventListener("click", () => Session.openLogin());
    return;
  }
  if (!allowed) {
    $("gate").textContent = "La page vitrine se modifie par les administrateurs de la structure et le profil « Vitrine ». Un administrateur peut vous attribuer ce profil (Administration → Utilisateurs).";
    return;
  }
  try {
    site = await Session.api("/api/site");
  } catch (e) {
    flash(esc(e.message));
    return;
  }
  draft = site.draft;
  $("structure-name").textContent = `· ${site.structure_name}`;
  $("vb-add-type").innerHTML = Object.entries(TYPES).map(([k, t]) => `<option value="${k}">${t.label}</option>`).join("");
  $("editor").hidden = false;
  render();
  if (!site.saved) status("Première version proposée : complétez-la, puis enregistrez.");
}

Session.mountAccount($("account"), [Session.LINKS.search, Session.LINKS.picks, Session.LINKS.newsletters, Session.LINKS.admin, Session.LINKS.help]);
Session.onChange(onSessionChange);
Session.init();
