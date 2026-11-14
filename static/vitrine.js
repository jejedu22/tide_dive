// Page vitrine de la structure : édition par blocs, photos, brouillon, aperçu et publication (app/site.py).
// Réservée aux administrateurs de la structure et au profil « Vitrine ».

const $ = id => document.getElementById(id);
const esc = s => Session.esc(s);
let site = null;      // réponse de GET /api/site
let draft = null;     // brouillon en cours de modification
let dirty = false;
const folded = new WeakSet();   // blocs repliés dans l'éditeur (suivis par objet : survit aux déplacements)

const TYPES = {
  hero: { label: "Bandeau d'accueil", make: () => ({ type: "hero", title: site.structure_name, subtitle: "", image: null, button: "dives", button_label: "Prochaines plongées" }) },
  text: { label: "Texte", make: () => ({ type: "text", title: "", body: "Votre texte…" }) },
  gallery: { label: "Galerie photos", make: () => ({ type: "gallery", title: "Photos", photos: [] }) },
  dives: { label: "Prochaines plongées", make: () => ({ type: "dives", title: "Prochaines plongées", days: 60, max_items: 8, guest_link: true }) },
  pricing: { label: "Activités et tarifs", make: () => ({ type: "pricing", title: "Activités et tarifs", items: [{ label: "Baptême", price: "", detail: "" }] }) },
  join: { label: "Nous rejoindre", make: () => ({ type: "join", title: "Nous rejoindre", text: "" }) },
  contact: { label: "Contact et accès", make: () => ({ type: "contact", title: "Contact et accès", text: "", show_map: true, links: { facebook: null, instagram: null, youtube: null, website: null } }) },
  custom: { label: "Bloc personnalisé (texte, photo, boutons)", make: () => ({ type: "custom", title: "Titre", body: "Votre texte…", image: null, image_position: "right", align: "left", buttons: [] }) },
  cards: { label: "Cartes en colonnes", make: () => ({ type: "cards", title: "Nos formations", intro: "", columns: 3, items: [
    { title: "Niveau 1", text: "", image: null, link_label: null, link: null }, { title: "Niveau 2", text: "", image: null, link_label: null, link: null },
    { title: "Niveau 3", text: "", image: null, link_label: null, link: null }] }) },
  faq: { label: "Questions fréquentes", make: () => ({ type: "faq", title: "Questions fréquentes", items: [{ question: "Faut-il savoir nager ?", answer: "Oui, un minimum d'aisance dans l'eau est nécessaire." }] }) },
  stats: { label: "Chiffres clés", make: () => ({ type: "stats", title: "", items: [{ value: "40 ans", label: "d'existence" }, { value: "120", label: "adhérents" }, { value: "25", label: "sites de plongée" }] }) },
};
const STYLES = { card: "Carte blanche", plain: "Sans fond", tinted: "Teinté", primary: "Couleur du club" };
const POSITIONS = { right: "À droite du texte", left: "À gauche du texte", top: "Au-dessus du texte", background: "En fond (texte par-dessus)" };
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
    case "custom":
      return field("Titre", textInput(p("title"), b.title, 80)) + field("Texte", area(p("body"), b.body, 5000, 6))
        + field("Photo", imageSelect(p("image"), b.image), site.images.length ? "" : "Ajoutez d'abord des photos (en bas de la page).")
        + (b.image ? field("Place de la photo", select(p("image_position"), b.image_position, POSITIONS)) : "")
        + field("Alignement du texte", select(p("align"), b.align, { left: "À gauche", center: "Centré" }))
        + `<ol class="vb-list">${b.buttons.map((bt, j) => `<li>
            ${textInput(`${i}.buttons.${j}.label`, bt.label, 40, true).replace("<input", '<input placeholder="Texte du bouton"')}
            ${linkInput(`${i}.buttons.${j}.link`, bt.link)}
            <button type="button" class="btn-quiet btn-small" data-act="remove-item" data-list="${i}.buttons" data-index="${j}">Retirer</button></li>`).join("")}</ol>`
        + (b.buttons.length < 2 ? `<button type="button" class="btn-quiet btn-small" data-act="add-button" data-block="${i}">Ajouter un bouton</button>` : "");
    case "cards":
      return field("Titre", textInput(p("title"), b.title, 80)) + field("Introduction", area(p("intro"), b.intro, 1000, 2))
        + field("Colonnes", select(p("columns"), b.columns, { 2: "2", 3: "3", 4: "4" }).replace("<select", "<select data-number"))
        + `<ol class="vb-list vb-cards">${b.items.map((c, j) => `<li>
            ${textInput(`${i}.items.${j}.title`, c.title, 80, true).replace("<input", '<input placeholder="Titre de la carte"')}
            ${area(`${i}.items.${j}.text`, c.text, 800, 2).replace("<textarea", '<textarea placeholder="Texte (facultatif)"')}
            <span>${imageSelect(`${i}.items.${j}.image`, c.image)}</span>
            ${textInput(`${i}.items.${j}.link_label`, c.link_label, 40).replace("<input", '<input placeholder="Texte du lien (facultatif)"')}
            ${c.link_label ? linkInput(`${i}.items.${j}.link`, c.link) : ""}
            <button type="button" class="btn-quiet btn-small" data-act="remove-item" data-list="${i}.items" data-index="${j}">Retirer la carte</button></li>`).join("")}</ol>`
        + (b.items.length < 12 ? `<button type="button" class="btn-quiet btn-small" data-act="add-card" data-block="${i}">Ajouter une carte</button>` : "");
    case "faq":
      return field("Titre", textInput(p("title"), b.title, 80))
        + `<ol class="vb-list vb-cards">${b.items.map((q, j) => `<li>
            ${textInput(`${i}.items.${j}.question`, q.question, 200, true).replace("<input", '<input placeholder="Question"')}
            ${area(`${i}.items.${j}.answer`, q.answer, 2000, 2).replace("<textarea", '<textarea placeholder="Réponse"')}
            <button type="button" class="btn-quiet btn-small" data-act="remove-item" data-list="${i}.items" data-index="${j}">Retirer</button></li>`).join("")}</ol>`
        + `<button type="button" class="btn-quiet btn-small" data-act="add-question" data-block="${i}">Ajouter une question</button>`;
    case "stats":
      return field("Titre", textInput(p("title"), b.title, 80))
        + `<ol class="vb-list">${b.items.map((x, j) => `<li>
            ${textInput(`${i}.items.${j}.value`, x.value, 20, true).replace("<input", '<input placeholder="Chiffre (ex. 120)"')}
            ${textInput(`${i}.items.${j}.label`, x.label, 60, true).replace("<input", '<input placeholder="Légende (ex. adhérents)"')}
            <button type="button" class="btn-quiet btn-small" data-act="remove-item" data-list="${i}.items" data-index="${j}">Retirer</button></li>`).join("")}</ol>`
        + (b.items.length < 6 ? `<button type="button" class="btn-quiet btn-small" data-act="add-figure" data-block="${i}">Ajouter un chiffre</button>` : "");
    default:
      return "";
  }
}

function summary(b) {
  return [b.menu ? `☰ ${b.menu}` : "", b.title || (b.type === "text" ? b.body.slice(0, 50) : "")].filter(Boolean).join(" · ");
}

function render() {
  const f = $("vb-settings");
  f.slug.value = draft.slug;
  f.title.value = draft.title ?? "";
  f.description.value = draft.description ?? "";
  $("vb-blocks").innerHTML = draft.blocks.map((b, i) => `<li class="vb-block${b.hidden ? " vb-hidden" : ""}${folded.has(b) ? " vb-folded" : ""}" data-index="${i}">
      <div class="vb-block-head">
        <button type="button" class="vb-fold" data-act="fold" aria-expanded="${!folded.has(b)}" title="Replier / déplier">${folded.has(b) ? "▸" : "▾"}
        <strong>${esc(TYPES[b.type]?.label || b.type)}</strong></button><span class="muted">${esc(summary(b))}</span>
        <span class="vb-tools">
          <button type="button" class="btn-quiet btn-small" data-act="up" ${i === 0 ? "disabled" : ""} aria-label="Monter">↑</button>
          <button type="button" class="btn-quiet btn-small" data-act="down" ${i === draft.blocks.length - 1 ? "disabled" : ""} aria-label="Descendre">↓</button>
          <button type="button" class="btn-quiet btn-small" data-act="copy">Dupliquer</button>
          <button type="button" class="btn-quiet btn-small" data-act="toggle">${b.hidden ? "Afficher" : "Masquer"}</button>
          <button type="button" class="btn-danger btn-small" data-act="delete">Supprimer</button>
        </span>
      </div>
      ${folded.has(b) ? "" : `<div class="vb-block-body">${blockForm(b, i)}${commonFields(b, i)}</div>`}</li>`).join("")
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
  if (domains) renderDomains();
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
// rendu à jour quand un choix change la forme du formulaire (libellé de menu → cibles des boutons, texte de lien)
$("vb-blocks").addEventListener("change", e => {
  const path = e.target.dataset.path || "";
  if (/\.(menu|link_label)$/.test(path)) render();
});
// cible d'un bouton choisie dans la liste
$("vb-blocks").addEventListener("input", e => {
  const el = e.target.closest("select[data-link]");
  if (!el) return;
  setPath(el.dataset.link, el.value === "__url" ? "" : el.value);
  changed();
  render();
});
$("vb-blocks").addEventListener("change", e => { if (e.target.type === "checkbox") e.target.dispatchEvent(new Event("input", { bubbles: true })); });

$("vb-blocks").addEventListener("click", e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const li = btn.closest(".vb-block");
  const i = Number(li?.dataset.index);
  const act = btn.dataset.act;
  if (act === "fold") {
    const b = draft.blocks[i];
    if (folded.has(b)) folded.delete(b); else folded.add(b);
    render();
    return;
  }
  if (act === "up" || act === "down") {
    const j = act === "up" ? i - 1 : i + 1;
    [draft.blocks[i], draft.blocks[j]] = [draft.blocks[j], draft.blocks[i]];
  } else if (act === "copy") {
    if (draft.blocks.length >= 30) { status("30 blocs au plus."); return; }
    const copy = structuredClone(draft.blocks[i]);
    copy.menu = null;
    draft.blocks.splice(i + 1, 0, copy);
  } else if (act === "add-button") {
    draft.blocks[Number(btn.dataset.block)].buttons.push({ label: "En savoir plus", link: "join" });
  } else if (act === "add-card") {
    draft.blocks[Number(btn.dataset.block)].items.push({ title: "", text: "", image: null, link_label: null, link: null });
  } else if (act === "add-question") {
    draft.blocks[Number(btn.dataset.block)].items.push({ question: "", answer: "" });
  } else if (act === "add-figure") {
    draft.blocks[Number(btn.dataset.block)].items.push({ value: "", label: "" });
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
  if (draft.blocks.length >= 30) { status("30 blocs au plus."); return; }
  draft.blocks.push(TYPES[$("vb-add-type").value].make());
  changed();
  render();
  $("vb-blocks").lastElementChild?.scrollIntoView({ behavior: "smooth", block: "center" });
});

const select = (path, value, options) => `<select data-path="${path}">${Object.entries(options).map(([k, v]) =>
  `<option value="${esc(k)}"${String(value) === k ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`;

// ancres des sections : même calcul que app/site.py (anchors)
function anchors() {
  const used = new Set();
  return draft.blocks.map(b => {
    let base = { dives: "plongees", contact: "contact" }[b.type] || null;
    if (!base && b.menu?.trim()) {
      base = b.menu.trim().normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase()
        .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 50).replace(/^-+|-+$/g, "") || "section";
    }
    if (!base) return null;
    let a = base, n = 2;
    while (used.has(a)) a = `${base}-${n++}`;
    used.add(a);
    return a;
  });
}

// cible d'un bouton : liste des choix + champ libre (adresse web, e-mail, téléphone)
function linkInput(path, value) {
  const ids = anchors();
  const options = { join: "Lien d'adhésion au club", guest: "Inscription des personnes extérieures" };
  draft.blocks.forEach((b, i) => { if (ids[i] && (b.menu || b.type === "dives" || b.type === "contact")) options[`#${ids[i]}`] = `Section « ${b.menu || TYPES[b.type].label} »`; });
  const known = value != null && value in options;
  return `<span class="vb-link"><select data-link="${path}">${Object.entries(options).map(([k, v]) =>
    `<option value="${esc(k)}"${value === k ? " selected" : ""}>${esc(v)}</option>`).join("")}
    <option value="__url"${known ? "" : " selected"}>Adresse web, e-mail ou téléphone…</option></select>
    ${known ? "" : `<input data-path="${path}" maxlength="300" placeholder="https://… , contact@club.fr ou tel:0296…" value="${esc(value ?? "")}">`}</span>`;
}

function commonFields(b, i) {
  return `<div class="vb-common">${field("Libellé dans le menu", textInput(`${i}.menu`, b.menu, 30), "Facultatif : le bloc apparaît alors dans le menu en haut de la page.")}
    ${b.type === "hero" ? "" : field("Fond", select(`${i}.style`, b.style || "card", STYLES))}</div>`;
}

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

// ---- Noms de domaine (app/site_domains.py) ----

let domains = null;   // réponse de GET /api/site/domains
const domainStatus = text => { $("vb-domain-status").textContent = text; };

function dnsRows(d) {
  const apex = d.domain.split(".").length === 2;
  const rows = [];
  if (!apex && domains.main_host) rows.push(["CNAME", d.domain, domains.main_host]);
  else domains.server_addresses.forEach(a => rows.push([a.includes(":") ? "AAAA" : "A", d.domain, a]));
  rows.push(["TXT", d.txt_name, d.txt_value]);
  return `<table class="vb-dns"><thead><tr><th>Type</th><th>Nom</th><th>Valeur</th></tr></thead><tbody>${rows.map(r =>
    `<tr><td>${r[0]}</td><td><code>${esc(r[1])}</code></td><td><code>${esc(r[2])}</code></td></tr>`).join("")}</tbody></table>
    ${apex && domains.main_host ? `<small class="hint">Domaine sans « www » : enregistrements A / AAAA (ou ALIAS / ANAME vers <code>${esc(domains.main_host)}</code> si votre registraire le permet).</small>` : ""}`;
}

function renderDomains() {
  const full = domains.domains.length >= domains.max;
  $("vb-domain-form").hidden = full;
  $("vb-domains").innerHTML = domains.domains.map(d => `<li class="vb-domain">
    <p><strong>${d.verified ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.domain)}</a>` : esc(d.domain)}</strong>
      <span class="tag ${d.verified ? "job-succeeded" : "tag-warn"}">${d.verified ? (site.published ? "Actif" : "Vérifié · page non publiée") : "À vérifier"}</span></p>
    ${d.verified ? "" : `<p class="hint">Enregistrements à créer chez votre registraire :</p>${dnsRows(d)}`}
    ${d.message ? `<p class="hint">${esc(d.message)}</p>` : ""}
    <p class="vb-domain-actions"><button type="button" class="btn-secondary btn-small" data-verify="${d.id}">Vérifier</button>
      <button type="button" class="btn-quiet btn-small" data-remove="${d.id}">Retirer</button></p>
  </li>`).join("") || `<li class="muted">Aucun nom de domaine déclaré.</li>`;
}

async function loadDomains() {
  try {
    domains = await Session.api("/api/site/domains");
    renderDomains();
  } catch (err) {
    domainStatus(err.message);
  }
}

$("vb-domain-form").addEventListener("submit", async e => {
  e.preventDefault();
  const value = $("vb-domain").value.trim();
  if (!value) return;
  try {
    domains = await Session.api("/api/site/domains", { method: "POST", body: { domain: value } });
    $("vb-domain").value = "";
    renderDomains();
    domainStatus("Domaine déclaré : créez les enregistrements DNS indiqués, puis cliquez sur « Vérifier ».");
  } catch (err) {
    domainStatus(err.message);
  }
});

$("vb-domains").addEventListener("click", async e => {
  const verify = e.target.closest("button[data-verify]");
  const remove = e.target.closest("button[data-remove]");
  try {
    if (verify) {
      verify.disabled = true;
      domainStatus("Vérification en cours…");
      domains = await Session.api(`/api/site/domains/${verify.dataset.verify}/verify`, { method: "POST" });
      domainStatus("");
    } else if (remove) {
      if (!confirm("Retirer ce nom de domaine ? Il cessera d'afficher la page.")) return;
      await Session.api(`/api/site/domains/${remove.dataset.remove}`, { method: "DELETE" });
      domains.domains = domains.domains.filter(d => String(d.id) !== remove.dataset.remove);
      domainStatus("Domaine retiré.");
    } else return;
    renderDomains();
  } catch (err) {
    if (verify) verify.disabled = false;
    domainStatus(err.message);
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
  if (site.saved && draft.blocks.length > 3) draft.blocks.forEach(b => folded.add(b));   // page déjà composée : vue d'ensemble
  $("structure-name").textContent = `· ${site.structure_name}`;
  $("vb-add-type").innerHTML = Object.entries(TYPES).map(([k, t]) => `<option value="${k}">${t.label}</option>`).join("");
  $("editor").hidden = false;
  render();
  loadDomains();
  if (!site.saved) status("Première version proposée : complétez-la, puis enregistrez.");
}

Session.mountAccount($("account"), [Session.LINKS.search, Session.LINKS.picks, Session.LINKS.newsletters, Session.LINKS.admin, Session.LINKS.help]);
Session.onChange(onSessionChange);
Session.init();
