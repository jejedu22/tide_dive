// Session utilisateur partagée par les pages :
// appels API JSON, connexion/déconnexion, mot de passe oublié, profil,
// changement de mot de passe (y compris forcé après un mot de passe provisoire),
// liste de contrôle de la politique de mot de passe, encart « compte » de l'en-tête.

const Session = (() => {
  let user = null;
  const listeners = [];
  // complété par /api/auth/config au démarrage
  let config = {
    password_reset: false,
    password_reset_minutes: 60,
    password_policy: { min_length: 12, max_length: 128, classes: [] },
  };

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  const FIELD_LABELS = {
    username: "Identifiant", first_name: "Prénom", last_name: "Nom", email: "Adresse e-mail",
    phone: "Téléphone", password: "Mot de passe", new_password: "Mot de passe",
    structure_name: "Nom de la structure", contact_name: "Votre nom", consent: "Consentement",
  };

  // Message lisible à partir d'une erreur FastAPI (detail texte ou liste de validation)
  function errorMessage(body, res) {
    const d = body && body.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d) && d.length) {
      const e = d[0];
      const field = e.loc ? e.loc[e.loc.length - 1] : "";
      const msg = String(e.msg || "Données invalides.").replace(/^Value error, /, "");
      if (e.type === "missing") return `${FIELD_LABELS[field] || field} : champ obligatoire.`;
      return msg;
    }
    return `Erreur ${res.status}`;
  }

  async function api(path, { method = "GET", body } = {}) {
    let res;
    try {
      res = await fetch(path, {
        method,
        credentials: "same-origin",
        headers: body !== undefined ? { "Content-Type": "application/json" } : {},
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (e) {     // serveur injoignable (hors connexion) : message lisible plutôt que « Failed to fetch »
      const err = new Error(navigator.onLine === false
        ? "Pas de connexion internet : vérifiez votre réseau, puis réessayez."
        : "Serveur injoignable : vérifiez votre connexion, puis réessayez.");
      err.status = 0;
      throw err;
    }
    if (res.status === 204) return null;
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      if (res.headers.get("X-Password-Change-Required")) init();  // mot de passe provisoire
      const err = new Error(errorMessage(data, res));
      err.status = res.status;
      err.body = data;
      throw err;
    }
    return data;
  }

  function notify() {
    for (const fn of listeners) fn(user);
  }

  function setUser(u) {
    // Mot de passe provisoire : rien d'autre n'est accessible tant qu'il n'est pas changé.
    // Les pages voient un visiteur anonyme derrière le dialogue.
    if (u?.must_change_password) {
      user = null;
      notify();
      openPasswordChange({ forced: u });
      return;
    }
    user = u;
    renderPreviewBanner(u);
    notify();
  }

  // Aperçu d'un super administrateur : bandeau sur toutes les pages, avec le rôle vu et le bouton pour en sortir
  const PREVIEW_ROLE_LABELS = {
    manager: "administrateur de structure", viewer: "membre (visualisation)", none: "compte sans structure",
  };

  function previewLabel(u) {
    const profiles = (u.profiles || []).map(p => ({ gestionnaire: "Gestionnaire", inscriptions: "Inscriptions" })[p] || p);
    return PREVIEW_ROLE_LABELS[u.preview.role] + (profiles.length ? ` + ${profiles.join(", ")}` : "")
      + (u.structure ? ` de ${u.structure.name}` : "");
  }

  function renderPreviewBanner(u) {
    let banner = document.getElementById("preview-banner");
    if (!u?.preview) {
      banner?.remove();
      return;
    }
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "preview-banner";
      banner.className = "preview-banner";
      banner.setAttribute("role", "status");
      document.body.prepend(banner);
      banner.addEventListener("click", e => {
        if (e.target.closest("[data-preview=change]")) openPreview();
        if (e.target.closest("[data-preview=stop]")) stopPreview();
      });
    }
    banner.innerHTML = `
      <span><strong>Aperçu</strong> : vous voyez Calendive comme <strong>${esc(previewLabel(u))}</strong>.
        Lecture seule : aucune modification possible.</span>
      <span class="preview-actions">
        <button type="button" class="btn-quiet" data-preview="change">Changer</button>
        <button type="button" class="btn-primary" data-preview="stop">Quitter l'aperçu</button>
      </span>`;
  }

  async function stopPreview() {
    try {
      await api("/api/me/preview", { method: "DELETE" });
      location.reload();
    } catch (err) {
      openMessage("Aperçu", `<p>${esc(err.message)}</p>`);
    }
  }

  // « Voir comme… » : le super administrateur choisit le rôle, la structure et les profils de l'aperçu
  async function openPreview() {
    let catalog = [];
    try {
      [catalog] = await Promise.all([
        api("/api/admin/profiles").catch(() => []),
        user?.preview ? null : (structures === null ? loadStructures() : null),
      ]);
    } catch { /* catalogue indisponible : pas de profils proposés */ }
    const list = structures?.length ? structures : (user?.structure ? [user.structure] : []);
    const current = user?.structure?.id;
    const role = user?.preview?.role || "viewer";
    openForm({
      title: "Voir comme…",
      intro: `<p class="dialog-hint">Affichez Calendive comme un autre rôle, pour vérifier ce qu'il voit. L'aperçu est
        <strong>en lecture seule</strong> et ne concerne que ce navigateur ; « Quitter l'aperçu » vous rend vos droits.</p>`,
      fields: [],
      extra: `
        <label>Rôle
          <select name="role">${Object.entries(PREVIEW_ROLE_LABELS).map(([k, label]) =>
            `<option value="${k}"${k === role ? " selected" : ""}>${esc(label[0].toUpperCase() + label.slice(1))}</option>`).join("")}
          </select>
        </label>
        <label class="preview-structure">Structure
          <select name="structure_id">${list.map(st =>
            `<option value="${st.id}"${st.id === current ? " selected" : ""}>${esc(st.name)}</option>`).join("")}
          </select>
        </label>
        <fieldset class="preview-profiles">
          <legend>Profils</legend>
          ${catalog.map(p => `<label class="check" title="${esc(p.description || "")}"><input type="checkbox" name="profiles"
            value="${esc(p.id)}"${user?.profiles?.includes(p.id) && user?.preview ? " checked" : ""}> ${esc(p.label)}
            <span class="muted">— ${esc(p.description || "")}</span></label>`).join("")}
        </fieldset>`,
      submitLabel: "Voir comme ce rôle",
      setup(form) {
        const sync = () => {
          const none = form.role.value === "none";
          form.querySelector(".preview-structure").hidden = none;
          form.querySelector(".preview-profiles").hidden = none || !catalog.length;
        };
        form.role.addEventListener("change", sync);
        sync();
      },
      async onSubmit(values, form) {
        const body = {
          role: values.role,
          structure_id: values.role === "none" || !values.structure_id ? null : Number(values.structure_id),
          profiles: values.role === "none" ? []
            : [...form.querySelectorAll("input[name=profiles]:checked")].map(i => i.value),
        };
        await api("/api/me/preview", { method: "PUT", body });
        location.assign("./");     // page d'arrivée du rôle (créneaux choisis pour un membre)
        return true;
      },
    });
  }

  async function init() {
    const [me, cfg] = await Promise.all([
      api("/api/auth/me").catch(() => ({ user: null })),
      api("/api/auth/config").catch(() => null),
    ]);
    if (cfg) config = cfg;
    setUser(me.user);
    return user;
  }

  // Page où envoyer l'utilisateur juste après sa connexion : fonction(user) -> URL ou null.
  // Réglée par la page (la recherche envoie les membres vers leurs créneaux choisis).
  let redirectAfterLogin = null;

  function goAfterLogin(u) {
    const target = redirectAfterLogin?.(u);
    if (target) location.assign(target);  // pas de setUser : inutile de redessiner la page qu'on quitte
    return !!target;
  }

  async function login(username, password) {
    const u = (await api("/api/auth/login", { method: "POST", body: { username, password } })).user;
    // mot de passe provisoire : d'abord le changement obligatoire (voir openPasswordChange)
    if (!u.must_change_password && goAfterLogin(u)) return;
    setUser(u);
  }

  async function logout() {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    setUser(null);
  }

  // ---- Politique de mot de passe ----

  const CLASS_TESTS = {
    lower: c => c !== c.toUpperCase() && c === c.toLowerCase(),
    upper: c => c !== c.toLowerCase() && c === c.toUpperCase(),
    digit: c => /\p{Nd}/u.test(c),
    special: c => !/[\p{L}\p{N}]/u.test(c),
  };

  // Liste de contrôle mise à jour à la frappe ; le serveur revérifie tout
  // (et refuse en plus : nom / e-mail dans le mot de passe, mots de passe courants).
  function passwordChecklist(input, confirm = null) {
    const p = config.password_policy;
    const rules = [
      { label: `${p.min_length} caractères au moins`, test: v => [...v].length >= p.min_length },
      ...p.classes.map(c => ({ label: c.label, test: v => [...v].some(CLASS_TESTS[c.key] || (() => true)) })),
    ];
    if (confirm) rules.push({ label: "confirmation identique", test: v => v !== "" && v === confirm.value });
    const ul = document.createElement("ul");
    ul.className = "pw-rules";
    ul.setAttribute("aria-live", "polite");
    const render = () => {
      const v = input.value;
      ul.innerHTML = rules.map(r => {
        const ok = r.test(v);
        return `<li class="${ok ? "ok" : ""}"><span aria-hidden="true">${ok ? "✓" : "○"}</span> ${esc(r.label)}<span class="visually-hidden">${ok ? " : respecté" : " : manquant"}</span></li>`;
      }).join("");
    };
    input.addEventListener("input", render);
    confirm?.addEventListener("input", render);
    render();
    ul.refresh = render;
    ul.hint = "Évitez d'y mettre votre nom ou votre adresse e-mail.";
    return ul;
  }

  // Mot de passe aléatoire conforme : sans 0/O ni 1/l/I, facile à dicter
  function generatePassword() {
    const pools = ["abcdefghjkmnpqrstuvwxyz", "ABCDEFGHJKLMNPQRSTUVWXYZ", "23456789", "-_!?@#%+="];
    const all = pools.join("");
    const length = Math.max(config.password_policy.min_length + 2, 14);
    const rnd = n => crypto.getRandomValues(new Uint32Array(1))[0] % n;
    const chars = pools.map(p => p[rnd(p.length)]);
    while (chars.length < length) chars.push(all[rnd(all.length)]);
    for (let i = chars.length - 1; i > 0; i--) {
      const j = rnd(i + 1);
      [chars[i], chars[j]] = [chars[j], chars[i]];
    }
    return chars.join("");
  }

  // ---- Dialogues ----

  let dialog = null;

  function ensureDialog() {
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.className = "account-dialog";
    document.body.append(dialog);
    dialog.addEventListener("click", e => {
      if (e.target === dialog && !dialog.dataset.locked) dialog.close();
    });
    // Échap : refusé sur un dialogue obligatoire
    dialog.addEventListener("cancel", e => { if (dialog.dataset.locked) e.preventDefault(); });
    return dialog;
  }

  /**
   * fields : [{ name, label, type, autocomplete, required (défaut true), value, hint }]
   * intro / extra : HTML avant / après les champs
   * setup(form) : branchements supplémentaires
   * locked : pas de fermeture par Échap ni clic extérieur ; cancelLabel / onCancel
   */
  function openForm({ title, intro = "", fields, extra = "", submitLabel, onSubmit, setup,
                      locked = false, cancelLabel = "Annuler", onCancel }) {
    const d = ensureDialog();
    d.dataset.locked = locked ? "1" : "";
    d.innerHTML = `
      <form method="dialog" novalidate>
        <h2>${esc(title)}</h2>
        ${intro}
        ${fields.map(f => `
          <label>${esc(f.label)}
            <input name="${f.name}" type="${f.type || "text"}" autocomplete="${f.autocomplete || "off"}"
                   ${f.required === false ? "" : "required"} value="${esc(f.value ?? "")}">
            ${f.hint ? `<small class="field-hint">${esc(f.hint)}</small>` : ""}
          </label>`).join("")}
        ${extra}
        <p class="dialog-error" role="alert"></p>
        <div class="dialog-actions">
          <button type="button" class="btn-quiet" value="cancel">${esc(cancelLabel)}</button>
          <button type="submit" class="btn-primary">${esc(submitLabel)}</button>
        </div>
      </form>`;
    const form = d.querySelector("form");
    const errEl = d.querySelector(".dialog-error");
    d.querySelector("[value=cancel]").addEventListener("click", () => {
      d.close();
      onCancel?.();
    });
    form.addEventListener("submit", async e => {
      e.preventDefault();
      errEl.textContent = "";
      const missing = [...form.querySelectorAll("input[required]")].find(i => !i.value.trim());
      if (missing) {
        errEl.textContent = `${missing.closest("label").firstChild.textContent.trim()} : champ obligatoire.`;
        missing.focus();
        return;
      }
      const values = Object.fromEntries(new FormData(form));
      const btn = form.querySelector("[type=submit]");
      btn.disabled = true;
      try {
        const keepOpen = await onSubmit(values, form);
        // form.isConnected : onSubmit a pu ouvrir un autre dialogue à la place
        // (ex. connexion → changement obligatoire du mot de passe provisoire)
        if (keepOpen !== true && form.isConnected) d.close();
      } catch (err) {
        errEl.textContent = err.message;
      } finally {
        btn.disabled = false;
      }
    });
    setup?.(form);
    if (!d.open) d.showModal();
    form.querySelector("input")?.focus();
    return form;
  }

  function openMessage(title, html) {
    const d = ensureDialog();
    d.dataset.locked = "";
    d.innerHTML = `
      <form method="dialog">
        <h2>${esc(title)}</h2>
        <div class="dialog-message">${html}</div>
        <div class="dialog-actions"><button type="submit" class="btn-primary">OK</button></div>
      </form>`;
    if (!d.open) d.showModal();
    d.querySelector("button").focus();
  }

  function openLogin(prefill = "") {
    openForm({
      title: "Connexion",
      submitLabel: "Se connecter",
      fields: [
        { name: "username", label: "Identifiant ou adresse e-mail", autocomplete: "username", value: prefill },
        { name: "password", label: "Mot de passe", type: "password", autocomplete: "current-password" },
      ],
      extra: (config.password_reset
        ? `<p class="dialog-links"><button type="button" class="link-btn" data-forgot>Mot de passe oublié ?</button></p>`
        : "") +
        `<p class="dialog-links">Votre club n'a pas encore de compte ? <a href="demande-structure.html">Demander la création d'une structure</a></p>`,
      onSubmit: v => login(v.username.trim(), v.password),
      setup: form => form.querySelector("[data-forgot]")?.addEventListener("click", () => {
        openForgot(form.username.value.trim());
      }),
    });
  }

  function openForgot(prefill = "") {
    openForm({
      title: "Mot de passe oublié",
      intro: `<p class="dialog-hint">Indiquez votre identifiant ou votre adresse e-mail : vous recevrez un mot de passe provisoire, à remplacer par le vôtre dès la connexion.</p>`,
      submitLabel: "Recevoir un mot de passe provisoire",
      fields: [{ name: "login", label: "Identifiant ou adresse e-mail", autocomplete: "username", value: prefill }],
      onSubmit: async v => {
        const login = v.login.trim();
        await api("/api/auth/forgot-password", { method: "POST", body: { login } });
        openMessage("E-mail envoyé",
          `<p>Si un compte correspond à « ${esc(login)} », un e-mail contenant un mot de passe provisoire vient de lui être envoyé.</p>
           <p class="dialog-hint">Il est valable ${esc(durationLabel(config.password_reset_minutes))} ; votre mot de passe actuel reste valable en attendant. Pensez à regarder dans les indésirables.</p>
           <p><button type="button" class="btn-secondary" data-back-login>Se connecter</button></p>`);
        document.querySelector("dialog[open] [data-back-login]")?.addEventListener("click", () => openLogin(login));
        return true;  // le dialogue affiche déjà le message
      },
    });
  }

  function durationLabel(minutes = 60) {
    if (minutes % 1440 === 0) return `${minutes / 1440} jour${minutes > 1440 ? "s" : ""}`;
    if (minutes % 60 === 0) return `${minutes / 60} heure${minutes > 60 ? "s" : ""}`;
    return `${minutes} minutes`;
  }

  function attachChecklist(form, inputName = "new_password", confirmName = "confirm") {
    const list = passwordChecklist(form[inputName], form[confirmName]);
    form[inputName].closest("label").after(list);
  }

  // forced : compte dont le mot de passe provisoire doit être changé avant tout
  function openPasswordChange({ forced = null } = {}) {
    openForm({
      title: forced ? "Choisissez votre mot de passe" : "Changer de mot de passe",
      intro: forced
        ? `<p class="dialog-hint">Bonjour ${esc(forced.first_name || forced.username)}, le mot de passe que vous avez utilisé est provisoire : choisissez le vôtre pour continuer.</p>`
        : "",
      submitLabel: "Changer le mot de passe",
      locked: !!forced,
      cancelLabel: forced ? "Se déconnecter" : "Annuler",
      onCancel: forced ? logout : undefined,
      fields: [
        { name: "current_password", label: forced ? "Mot de passe provisoire" : "Mot de passe actuel", type: "password", autocomplete: "current-password" },
        { name: "new_password", label: "Nouveau mot de passe", type: "password", autocomplete: "new-password" },
        { name: "confirm", label: "Confirmation", type: "password", autocomplete: "new-password" },
      ],
      setup: form => attachChecklist(form),
      onSubmit: async v => {
        if (v.new_password !== v.confirm) throw new Error("Les deux saisies diffèrent.");
        await api("/api/me/password", {
          method: "POST",
          body: { current_password: v.current_password, new_password: v.new_password },
        });
        if (forced) {
          const u = (await api("/api/auth/me")).user;
          if (!goAfterLogin(u)) setUser(u);
        }
      },
    });
  }

  function openProfile() {
    const u = user;
    const form = openForm({
      title: "Mon compte",
      intro: `<p class="dialog-hint">Identifiant : <strong>${esc(u.username)}</strong>${u.structure ? ` · ${esc(u.structure.name)} (${esc(roleLabel(u))})` : ""}</p>`,
      submitLabel: "Enregistrer",
      fields: [
        { name: "first_name", label: "Prénom", autocomplete: "given-name", value: u.first_name },
        { name: "last_name", label: "Nom", autocomplete: "family-name", value: u.last_name },
        { name: "email", label: "Adresse e-mail", type: "email", autocomplete: "email", value: u.email,
          hint: "Sert à la connexion et à la réinitialisation du mot de passe." },
        { name: "phone", label: "Téléphone (facultatif)", type: "tel", autocomplete: "tel", value: u.phone, required: false },
        { name: "current_password", label: "Mot de passe actuel (pour changer d'adresse e-mail)", type: "password",
          autocomplete: "current-password", required: false },
      ],
      extra: `<label class="check" data-newsletters hidden><input type="checkbox" name="newsletters">
                 Recevoir les newsletters de ma structure</label>
               <p class="dialog-links"><button type="button" class="link-btn" data-change-password>Changer de mot de passe</button></p>`,
      onSubmit: async (v, f) => {
        const body = { first_name: v.first_name, last_name: v.last_name, email: v.email, phone: v.phone };
        if (v.current_password) body.current_password = v.current_password;
        const saved = (await api("/api/me/profile", { method: "PATCH", body })).user;
        const box = f.newsletters;
        if (!box.closest("label").hidden && box.checked !== (box.dataset.initial === "1")) {
          await api("/api/me/newsletters", { method: "PUT", body: { subscribed: box.checked } });
        }
        setUser(saved);
      },
      setup: f => {
        // abonnement aux newsletters : proposé à un compte rattaché à une structure, avec une adresse
        api("/api/me/newsletters").then(s => {
          if (!s.available || !f.isConnected) return;
          f.newsletters.checked = s.subscribed;
          f.newsletters.dataset.initial = s.subscribed ? "1" : "0";
          f.newsletters.closest("label").hidden = false;
        }).catch(() => {});
        const pwLabel = f.current_password.closest("label");
        const sync = () => { pwLabel.hidden = f.email.value.trim().toLowerCase() === (u.email || ""); };
        f.email.addEventListener("input", sync);
        sync();
        f.querySelector("[data-change-password]").addEventListener("click", () => openPasswordChange());
      },
    });
    if (!u.profile_complete) form.querySelector(".dialog-hint").insertAdjacentHTML(
      "afterend", `<p class="dialog-notice">Complétez votre profil : prénom, nom et adresse e-mail.</p>`);
  }

  // ---- Fiche plongeur : niveaux, licence (QR code), certificat médical (CACI) ----

  const fmtDate = iso => (iso ? new Date(iso + "T12:00:00").toLocaleDateString("fr-FR") : "");
  const CACI_STATES = { missing: "aucun", pending: "en attente de validation", valid: "validé", expired: "expiré" };
  let levelsCatalog = null;

  // État du CACI en une ligne (fiche du membre, liste des plongeurs)
  function caciText(c) {
    if (!c || c.state === "missing") return "Aucun certificat médical (CACI) enregistré.";
    const until = `valable jusqu'au ${fmtDate(c.valid_until)}`;
    if (c.state === "expired") return `Certificat du ${fmtDate(c.date)} : expiré (${until}).`;
    if (c.state === "pending") return `Certificat du ${fmtDate(c.date)}, ${until} : en attente de validation par un gestionnaire.`;
    return `Certificat du ${fmtDate(c.date)}, ${until} : validé${c.validated_by ? ` par ${esc(c.validated_by)}` : ""}.`;
  }

  // QR code (SVG) d'une adresse : lien de la licence numérique, à montrer à un encadrant
  function qrSvg(text, cell = 4) {
    if (!text || typeof qrcode === "undefined") return "";
    const qr = qrcode(0, "M");
    qr.addData(text);
    qr.make();
    return qr.createSvgTag({ cellSize: cell, margin: 2, scalable: true, alt: "QR code de la licence" });
  }

  // le CACI empêche-t-il de s'inscrire (structure qui le vérifie, certificat absent ou expiré) ?
  const caciBlocks = u => !!(u?.structure?.caci_check && ["missing", "expired"].includes(u.caci?.state));

  // bibliothèque QR code (vendor/qrcode, licence MIT) chargée à la demande
  let qrLoading = null;
  function loadQr() {
    if (typeof qrcode !== "undefined") return Promise.resolve();
    qrLoading ??= new Promise(resolve => {
      const sc = document.createElement("script");
      sc.src = "vendor/qrcode/qrcode.js";
      sc.onload = sc.onerror = () => resolve();
      document.head.append(sc);
    });
    return qrLoading;
  }

  async function openDiver() {
    [levelsCatalog] = await Promise.all([
      levelsCatalog ?? api("/api/divers/levels").catch(() => ({ levels: {}, instructor_levels: {} })),
      loadQr(),
    ]);
    const u = user, dv = u.diver || {};
    const opts = (catalog, value) => `<option value="">—</option>` + Object.entries(catalog).map(([k, label]) =>
      `<option value="${k}"${k === value ? " selected" : ""}>${esc(label)}</option>`).join("");
    const today = new Date().toISOString().slice(0, 10);
    const check = u.structure?.caci_check;
    openForm({
      title: "Ma fiche plongeur",
      intro: `<p class="dialog-hint">Vos niveaux, votre licence FFESSM et votre certificat médical (CACI), vus par les
        administrateurs et gestionnaires de votre structure.</p>`,
      submitLabel: "Enregistrer",
      fields: [],
      extra: `
        <label>Niveau de plongeur <select name="diver_level">${opts(levelsCatalog.levels, dv.diver_level)}</select></label>
        <label>Niveau d'encadrement <select name="instructor_level">${opts(levelsCatalog.instructor_levels, dv.instructor_level)}</select></label>
        <label>Autres qualifications <input name="qualifications" maxlength="200" placeholder="ex. Nitrox confirmé, RIFAP, TIV"
          value="${esc(dv.qualifications ?? "")}"></label>
        <label>Numéro de licence FFESSM <input name="licence_number" maxlength="30" placeholder="ex. A-14-123456"
          value="${esc(dv.licence_number ?? "")}"></label>
        <label>Lien du QR code de la licence <input name="licence_url" type="url" maxlength="500" placeholder="https://…"
          value="${esc(dv.licence_url ?? "")}">
          <small class="field-hint">Scannez le QR code de votre licence numérique avec votre téléphone et collez ici l'adresse obtenue :
            un encadrant pourra le scanner depuis l'application.</small></label>
        <div class="licence-qr" data-qr>${dv.licence_url ? qrSvg(dv.licence_url) : ""}</div>
        <fieldset class="caci-box${caciBlocks(u) ? " caci-alert" : ""}">
          <legend>Certificat médical (CACI)</legend>
          <p class="caci-status">${caciText(u.caci)}</p>
          ${check ? `<p class="dialog-hint">Votre structure vérifie le CACI : sans certificat valable le jour de la plongée
            (${u.structure.caci_validity_months} mois à compter de sa date), l'inscription est refusée.</p>` : ""}
          <label>Date du certificat <input name="caci_date" type="date" max="${today}" value="${esc(u.caci?.date ?? "")}">
            <small class="field-hint">Une nouvelle date doit être validée par un gestionnaire de votre structure.</small></label>
        </fieldset>`,
      onSubmit: async v => {
        const body = Object.fromEntries(["diver_level", "instructor_level", "qualifications", "licence_number",
                                         "licence_url", "caci_date"].map(k => [k, v[k] || null]));
        setUser((await api("/api/me/profile", { method: "PATCH", body })).user);
      },
      setup: f => {
        f.licence_url.addEventListener("input", () => {
          const url = f.licence_url.value.trim();
          f.querySelector("[data-qr]").innerHTML = /^https:\/\/\S+$/.test(url) ? qrSvg(url) : "";
        });
      },
    });
  }

  // Super administrateur : structures proposées dans le sélecteur de l'en-tête
  let structures = null;      // null : pas encore chargées
  let structuresLoading = null;

  function loadStructures() {
    structuresLoading ??= api("/api/admin/structures")
      .then(list => { structures = list; })
      .catch(() => { structures = []; })
      .finally(() => { structuresLoading = null; });
    return structuresLoading;
  }

  const accountRenders = [];

  // Liste à jour fournie par la page d'administration (création, renommage, suppression)
  function setStructures(list) {
    structures = list;
    for (const r of accountRenders) r(user);
  }

  async function switchStructure(structureId) {
    const res = await api("/api/me/structure", { method: "PUT", body: { structure_id: structureId } });
    setUser(res.user);
  }

  // Sélecteur de structure : un super administrateur choisit parmi toutes (ou aucune), un autre compte parmi les
  // siennes. Un compte d'une seule structure n'a pas de sélecteur.
  function structureSelect(u) {
    const list = u.is_admin ? (structures || (u.structure ? [u.structure] : [])) : (u.structures || []);
    const opts = (u.is_admin ? [`<option value=""${u.structure ? "" : " selected"}>Aucune structure</option>`] : [])
      .concat(list.map(st =>
        `<option value="${st.id}"${st.id === u.structure?.id ? " selected" : ""}>${esc(st.name)}</option>`));
    return ` <select class="account-structure-select" data-act="structure" aria-label="Ma structure"
      title="${u.is_admin ? "Changer de structure (super administrateur)" : "Changer de structure"}">${opts.join("")}</select>`;
  }

  const hasSeveralStructures = u => u.is_admin || (u.structures?.length ?? 0) > 1;

  // Invitations à rejoindre une structure : accepter ou refuser
  async function openInvitations() {
    const d = ensureDialog();
    d.dataset.locked = "";
    let list;
    try {
      list = await api("/api/me/invitations");
    } catch (err) {
      openMessage("Invitations", `<p>${esc(err.message)}</p>`);
      return;
    }
    const render = () => {
      d.innerHTML = `
        <form method="dialog">
          <h2>Invitations</h2>
          ${list.length ? "" : `<p class="dialog-hint">Aucune invitation en attente.</p>`}
          <ul class="invitations">${list.map(i => `
            <li data-id="${i.id}">
              <p><strong>${esc(i.structure.name)}</strong> vous invite à la rejoindre
                 (${esc(i.role_label.toLowerCase())}${i.profiles.length ? ` · ${esc(i.profiles.join(", "))}` : ""}).
                 <span class="muted">${i.invited_by ? `Invitation de ${esc(i.invited_by)}.` : ""}</span></p>
              <div class="dialog-actions">
                <button type="button" class="btn-quiet" data-answer="decline">Refuser</button>
                <button type="button" class="btn-primary" data-answer="accept">Accepter</button>
              </div>
            </li>`).join("")}</ul>
          <p class="dialog-error" role="alert"></p>
          <div class="dialog-actions"><button type="submit" class="btn-quiet">Fermer</button></div>
        </form>`;
      d.querySelector("ul")?.addEventListener("click", async e => {
        const btn = e.target.closest("[data-answer]");
        if (!btn) return;
        const li = btn.closest("li");
        const id = Number(li.dataset.id);
        const accept = btn.dataset.answer === "accept";
        const errEl = d.querySelector(".dialog-error");
        btn.disabled = true;
        try {
          const res = await api(`/api/me/invitations/${id}/${btn.dataset.answer}`, { method: "POST" });
          list = list.filter(i => i.id !== id);
          if (accept && res?.user) setUser(res.user);
          else await refreshMe();
          render();
        } catch (err) {
          errEl.textContent = err.message;
          btn.disabled = false;
        }
      });
    };
    render();
    if (!d.open) d.showModal();
  }

  async function refreshMe() {
    const res = await api("/api/auth/me");
    setUser(res.user);
  }

  // Icônes (trait, 24 px) des liens et du menu de l'en-tête
  const ICONS = {
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    wave: '<path d="M2 12c2.5-3 5-3 7.5 0s5 3 7.5 0 3.5-2.5 5 0"/><path d="M2 18c2.5-3 5-3 7.5 0s5 3 7.5 0 3.5-2.5 5 0"/><path d="M12 3v5"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 3v4M16 3v4M3 10h18"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14"/><path d="M12 17.3v.01"/>',
    eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/>',
    logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 17l-5-5 5-5M5 12h11"/>',
    invite: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    map: '<path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2z"/><path d="M9 4v14M15 6v14"/>',
  };
  const icon = (name, size = 18) => `<svg class="icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;

  // « Mon agenda » : abonnements calendrier (Android, iPhone), un par structure du compte. Le lien n'est montré
  // qu'à sa création (le serveur n'en garde que l'empreinte) ; un fichier .ics de la structure en dépannage.
  const fmtStamp = new Intl.DateTimeFormat("fr-FR", { dateStyle: "short", timeStyle: "short" });

  function feedChoice(f, types) {
    const t = types.find(x => x.id === f.type_id);
    return (f.mine ? "vos inscriptions" : "tous les créneaux") + (t ? `, type « ${t.label} »` : "");
  }

  function feedStatus(f, types) {
    if (!f.active) return "Pas d'abonnement.";
    return `Abonnement actif (${feedChoice(f, types)}), créé le ${fmtStamp.format(new Date(f.created_at))}`
      + (f.last_used_at ? `, lu par votre calendrier le ${fmtStamp.format(new Date(f.last_used_at))}.` : ", pas encore lu par un calendrier.");
  }

  function createdHtml(created) {
    return `
      <p class="calendar-ok">Lien créé. Il n'est affiché qu'une fois : ajoutez-le maintenant.</p>
      <p><a class="btn-primary btn-small" href="${esc(created.webcal_url)}">Ouvrir dans le calendrier</a>
        <span class="muted">iPhone, iPad, Mac, Outlook</span></p>
      <div class="calendar-link">
        <input type="text" readonly value="${esc(created.url)}" aria-label="Lien d'abonnement">
        <button type="button" class="btn-secondary btn-small" data-copy>Copier</button>
      </div>
      <ul class="calendar-help">
        <li><strong>iPhone</strong> : « Ouvrir dans le calendrier », puis « S'abonner ».</li>
        <li><strong>Android</strong> (Google Agenda) : copiez le lien ; sur un ordinateur, ouvrez calendar.google.com →
          « Autres agendas » <strong>+</strong> → « À partir de l'URL » et collez-le. L'agenda apparaît ensuite sur le téléphone.</li>
      </ul>
      <p class="dialog-hint">Lien personnel : ne le partagez pas.</p>`;
  }

  async function openAgenda() {
    const d = document.createElement("dialog");
    d.className = "account-dialog calendar-dialog";
    d.innerHTML = `
      <form method="dialog">
        <h2>Mon agenda</h2>
        <p class="dialog-hint">Les créneaux de vos structures dans le calendrier de votre téléphone (Android, iPhone) ou de
          votre ordinateur. Un <strong>abonnement</strong> se met à jour tout seul : créneaux ajoutés, déplacés ou retirés.
          Pour un seul créneau : bouton « Agenda » sur le créneau.</p>
        <div class="agenda-list"><p class="muted">Chargement…</p></div>
        <p class="dialog-error" role="alert"></p>
        <div class="dialog-actions"><button type="submit" class="btn-quiet">Fermer</button></div>
      </form>`;
    document.body.append(d);
    d.addEventListener("close", () => d.remove());
    d.addEventListener("click", e => { if (e.target === d) d.close(); });
    const listEl = d.querySelector(".agenda-list");
    const errEl = d.querySelector(".dialog-error");
    d.showModal();
    let entries;
    try {
      entries = await api("/api/me/calendar-feeds");
    } catch (err) {
      listEl.innerHTML = "";
      errEl.textContent = err.message;
      return;
    }
    if (!entries.length) {
      listEl.innerHTML = `<p>Votre compte n'est rattaché à aucune structure : il n'a pas de créneaux à suivre.</p>`;
      return;
    }
    const byId = new Map(entries.map(e => [e.structure.id, e]));
    const choiceOf = sec => ({
      mine: sec.querySelector("input[name^=mine]:checked")?.value === "1",
      type_id: sec.querySelector("select[name=type_id]").value ? Number(sec.querySelector("select[name=type_id]").value) : null,
    });
    const downloadHref = (sid, c) =>
      `/api/selections.ics?structure_id=${sid}&mine=${c.mine}${c.type_id != null ? `&type_id=${c.type_id}` : ""}`;
    const renderEntry = (e, created = null) => {
      const f = e.feed, sid = e.structure.id;
      const mine = f.active ? f.mine : true;
      return `
        <section class="calendar-part agenda-structure" data-sid="${sid}">
          <h3>${esc(e.structure.name)}</h3>
          <p class="agenda-status">${esc(feedStatus(f, e.types))}</p>
          <div class="agenda-choice">
            <label class="check"><input type="radio" name="mine-${sid}" value="1"${mine ? " checked" : ""}> Mes inscriptions</label>
            <label class="check"><input type="radio" name="mine-${sid}" value="0"${mine ? "" : " checked"}> Tous les créneaux</label>
            <select name="type_id" aria-label="Type de créneau">
              <option value="">Tous les types</option>
              ${e.types.map(t => `<option value="${t.id}"${f.active && f.type_id === t.id ? " selected" : ""}>${esc(t.label)}</option>`).join("")}
            </select>
          </div>
          ${created ? createdHtml(created) : ""}
          <p class="calendar-actions">
            <button type="button" class="btn-${f.active ? "secondary" : "primary"} btn-small" data-feed="create">${f.active ? "Nouveau lien" : "S'abonner"}</button>
            ${f.active ? `<button type="button" class="btn-quiet btn-small" data-feed="delete">Désactiver</button>` : ""}
            <a class="btn-quiet btn-small" data-download download href="#" title="Les créneaux d'aujourd'hui, à importer ; à refaire après un changement">Fichier .ics</a>
          </p>
          ${f.active && !created ? `<p class="dialog-hint">Le lien n'est affiché qu'à sa création : pour un autre appareil, créez un nouveau lien (l'ancien cesse de marcher).</p>` : ""}
        </section>`;
    };
    const syncDownload = sec => { sec.querySelector("[data-download]").href = downloadHref(Number(sec.dataset.sid), choiceOf(sec)); };
    const replace = (sec, html) => {
      sec.insertAdjacentHTML("afterend", html);
      const next = sec.nextElementSibling;
      sec.remove();
      syncDownload(next);
      return next;
    };
    listEl.innerHTML = entries.map(e => renderEntry(e)).join("");
    listEl.querySelectorAll(".agenda-structure").forEach(syncDownload);
    listEl.addEventListener("change", e => {
      const sec = e.target.closest(".agenda-structure");
      if (sec) syncDownload(sec);
    });
    listEl.addEventListener("click", async e => {
      const copy = e.target.closest("[data-copy]");
      if (copy) {
        const input = copy.parentElement.querySelector("input");
        input.select();
        try { await navigator.clipboard.writeText(input.value); copy.textContent = "Copié"; } catch { document.execCommand?.("copy"); }
        return;
      }
      const btn = e.target.closest("[data-feed]");
      if (!btn) return;
      const sec = btn.closest(".agenda-structure");
      const sid = Number(sec.dataset.sid);
      const entry = byId.get(sid);
      errEl.textContent = "";
      btn.disabled = true;
      try {
        if (btn.dataset.feed === "create") {
          const c = choiceOf(sec);
          const created = await api(`/api/me/calendar-feeds/${sid}`, { method: "POST", body: c });
          entry.feed = { active: true, mine: created.mine, type_id: created.type_id,
                         created_at: created.created_at, last_used_at: created.last_used_at };
          replace(sec, renderEntry(entry, created));
        } else {
          await api(`/api/me/calendar-feeds/${sid}`, { method: "DELETE" });
          entry.feed = { active: false };
          replace(sec, renderEntry(entry));
        }
      } catch (err) {
        errEl.textContent = err.message;
        btn.disabled = false;
      }
    });
  }

  // Initiales du compte (pastille du menu)
  function initials(u) {
    const words = String(u.display_name || u.username).split(/[\s.@_-]+/).filter(Boolean);
    return ((words[0]?.[0] || "") + (words.length > 1 ? words[words.length - 1][0] : "")).toUpperCase() || "?";
  }

  // En-tête : liens des pages (ordinateur) et menu du compte. Sur téléphone, tout passe dans le menu (bouton ☰) :
  // compte et rôle, structure, pages, puis Voir comme / Mon compte / Se déconnecter.
  // links = [{ href, label, icon, show(user) }]
  function mountAccount(el, links = []) {
    const menuId = `account-menu-${Math.random().toString(36).slice(2, 8)}`;
    const closeMenu = () => {
      el.querySelector(".account-menu")?.setAttribute("hidden", "");
      el.querySelector("[data-act=menu]")?.setAttribute("aria-expanded", "false");
    };
    const render = u => {
      if (!u) {
        el.innerHTML = `<button type="button" class="account-btn" data-act="login">Se connecter</button>`;
        return;
      }
      const shown = links.filter(l => !l.show || l.show(u));
      const pageLinks = cls => shown.map(l =>
        `<a class="${cls}" href="${l.href}">${l.icon ? icon(l.icon) : ""}<span>${esc(l.label)}</span></a>`).join("");
      if (u.is_admin && structures === null) loadStructures().then(() => { if (user === u) render(u); });
      const several = hasSeveralStructures(u);
      const where = several
        ? structureSelect(u)
        : u.structure ? `<span class="account-structure" title="${esc(roleLabel(u))}">${esc(u.structure.name)}</span>` : "";
      const attention = u.invitations || !u.profile_complete;
      el.innerHTML = `
        <span class="account-links">${pageLinks("account-link")}</span>
        <span class="account-where">${where}</span>
        <button type="button" class="account-menu-btn" data-act="menu" aria-expanded="false" aria-controls="${menuId}"
                aria-label="Menu du compte" title="${esc(u.display_name)}">
          <span class="account-avatar">${esc(initials(u))}</span>${icon("menu", 24)}
          ${attention ? `<span class="account-dot account-menu-dot" title="${u.invitations ? "Invitations en attente" : "Profil à compléter"}">!</span>` : ""}
        </button>
        <div class="account-menu" id="${menuId}" hidden>
          <div class="account-who">${icon("user", 26)}<div><strong>${esc(u.display_name)}</strong>
            <span>${esc(roleLabel(u) || "Sans structure")}</span></div></div>
          ${several ? `<div class="account-menu-structure">Structure ${structureSelect(u)}</div>` : ""}
          ${shown.length ? `<nav class="account-menu-pages" aria-label="Pages">${pageLinks("account-item")}</nav><hr>` : ""}
          ${u.invitations ? `<button type="button" class="account-item" data-act="invitations">${icon("invite")}
            <span>Invitations <span class="account-dot">${u.invitations}</span></span></button>` : ""}
          ${u.is_admin ? `<button type="button" class="account-item" data-act="preview"
            title="Voir l'application comme un autre rôle (lecture seule)">${icon("eye")}<span>Voir comme…</span></button>` : ""}
          ${u.structures?.length || u.structure ? `<button type="button" class="account-item" data-act="agenda"
            title="Les créneaux de vos structures dans le calendrier de votre téléphone">${icon("calendar")}<span>Mon agenda</span></button>` : ""}
          <button type="button" class="account-item" data-act="profile">${icon("user")}<span>Mon compte${u.profile_complete ? ""
            : ` <span class="account-dot" title="Profil à compléter">!</span>`}</span></button>
          ${u.structure ? `<button type="button" class="account-item" data-act="diver"
            title="Niveaux, licence FFESSM, certificat médical (CACI)">${icon("wave")}<span>Ma fiche plongeur${caciBlocks(u)
            ? ` <span class="account-dot" title="Certificat médical à jour requis pour s'inscrire">!</span>` : ""}</span></button>` : ""}
          <button type="button" class="account-item" data-act="logout">${icon("logout")}<span>Se déconnecter</span></button>
        </div>`;
    };
    el.addEventListener("click", e => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "menu") {
        const menu = el.querySelector(".account-menu");
        const open = menu.hasAttribute("hidden");
        if (open) {
          menu.removeAttribute("hidden");
          e.target.closest("[data-act]").setAttribute("aria-expanded", "true");
          menu.querySelector("a, button, select")?.focus();
        } else closeMenu();
        return;
      }
      if (act && act !== "structure") closeMenu();
      if (act === "login") openLogin();
      if (act === "profile") openProfile();
      if (act === "diver") openDiver();
      if (act === "logout") logout();
      if (act === "invitations") openInvitations();
      if (act === "preview") openPreview();
      if (act === "agenda") openAgenda();
    });
    document.addEventListener("click", e => { if (!el.contains(e.target)) closeMenu(); });
    document.addEventListener("keydown", e => {
      if (e.key !== "Escape" || el.querySelector(".account-menu")?.hasAttribute("hidden") !== false) return;
      closeMenu();
      el.querySelector("[data-act=menu]")?.focus();
    });
    el.addEventListener("change", async e => {
      const sel = e.target.closest("select[data-act=structure]");
      if (!sel) return;
      sel.disabled = true;
      try {
        await switchStructure(sel.value ? Number(sel.value) : null);
      } catch (err) {
        alert(`Changement de structure impossible : ${err.message}`);
        render(user);
      }
    });
    listeners.push(render);
    accountRenders.push(render);
    render(user);
  }

  const ROLE_LABELS = { viewer: "Visualisation", manager: "Administration" };

  function roleLabel(u) {
    const parts = [];
    if (u.is_admin) parts.push("Super administrateur");
    if (u.role) parts.push(`${ROLE_LABELS[u.role]} de la structure`);
    if (u.profiles?.includes("gestionnaire")) parts.push("Gestionnaire");
    if (u.profiles?.includes("inscriptions")) parts.push("Inscriptions");
    return parts.join(" · ");
  }

  // Recherche proposée au compte : « tides » (étales), « heights » (hauteur d'eau) ou « both ». Réglée par structure
  // par les super administrateurs ; un super administrateur a les deux, un visiteur la recherche par étale.
  function searchModes(u) {
    if (!u) return "tides";
    if (u.is_admin) return "both";
    return u.structure?.search_modes || "tides";
  }

  // Horaires de marée vus par la structure du compte (réglages de ses administrateurs : mois glissant
  // api-maree.fr, correction du calcul FES) ; visiteur ou compte sans structure : tout activé
  function tideSources(u) {
    return u?.structure?.tide_sources || { api_maree: true, calibration: true };
  }

  // Liens d'en-tête communs aux pages
  const LINKS = {
    // "./" renvoie les membres vers leurs créneaux
    search: { href: "index.html", label: "Recherche", icon: "search", show: u => searchModes(u) !== "heights" },
    heights: { href: "hauteurs.html", label: "Hauteurs d'eau", icon: "wave", show: u => searchModes(u) !== "tides" },
    picks: { href: "mes-creneaux.html", label: "Créneaux choisis", icon: "calendar", show: u => u.can.view_selections },
    admin: { href: "admin.html", label: "Administration", icon: "gear", show: u => u.can.admin_area },
    newsletters: { href: "newsletters.html", label: "Newsletters", icon: "mail", show: u => u.can.newsletters },
    divers: { href: "plongeurs.html", label: "Plongeurs", icon: "user", show: u => u.can.view_divers },
    map: { href: "carte.html", label: "Carte", icon: "map" },
    help: { href: "aide.html", label: "Aide", icon: "help" },
  };

  // Application installable (PWA) : service worker (interface disponible hors connexion, voir sw.js) et
  // proposition d'installation du navigateur, gardée pour un bouton « Installer » (page d'aide)
  let installPrompt = null;
  const installListeners = [];
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
  }
  window.addEventListener("beforeinstallprompt", e => {
    e.preventDefault();
    installPrompt = e;
    for (const fn of installListeners) fn(true);
  });
  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    for (const fn of installListeners) fn(false);
  });
  async function promptInstall() {
    if (!installPrompt) return false;
    installPrompt.prompt();
    const { outcome } = await installPrompt.userChoice;
    installPrompt = null;
    for (const fn of installListeners) fn(false);
    return outcome === "accepted";
  }
  const installed = () => window.matchMedia?.("(display-mode: standalone)").matches || navigator.standalone === true;

  return {
    ROLE_LABELS, LINKS, roleLabel, searchModes, tideSources,
    get user() { return user; },
    get config() { return config; },
    onChange: fn => listeners.push(fn),
    set redirectAfterLogin(fn) { redirectAfterLogin = fn; },
    init, login, logout, api, esc, openForm, openLogin, openForgot, openProfile, openPasswordChange, openMessage,
    openDiver, caciText, qrSvg, caciBlocks, loadQr,
    mountAccount, passwordChecklist, generatePassword, setUser, setStructures,
    openPreview, openAgenda, icon, promptInstall, installed, onInstallable: fn => { installListeners.push(fn); fn(!!installPrompt); },
  };
})();
