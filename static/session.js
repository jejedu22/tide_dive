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
    const res = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
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
    notify();
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

  function structureSelect(u) {
    const list = structures || (u.structure ? [u.structure] : []);
    const opts = [`<option value=""${u.structure ? "" : " selected"}>Aucune structure</option>`]
      .concat(list.map(st =>
        `<option value="${st.id}"${st.id === u.structure?.id ? " selected" : ""}>${esc(st.name)}</option>`));
    return ` <select class="account-structure-select" data-act="structure" aria-label="Ma structure"
      title="Changer de structure (super administrateur)">${opts.join("")}</select>`;
  }

  // Encart compte dans l'en-tête ; links = [{ href, label, show(user) }]
  function mountAccount(el, links = []) {
    const render = u => {
      if (!u) {
        el.innerHTML = `<button type="button" class="account-btn" data-act="login">Se connecter</button>`;
        return;
      }
      const extra = links
        .filter(l => !l.show || l.show(u))
        .map(l => `<a class="account-btn" href="${l.href}">${esc(l.label)}</a>`).join("");
      if (u.is_admin && structures === null) loadStructures().then(() => { if (user === u) render(u); });
      const where = u.is_admin
        ? structureSelect(u)
        : u.structure
          ? ` <span class="account-structure" title="${esc(roleLabel(u))}">· ${esc(u.structure.name)}</span>`
          : "";
      el.innerHTML = `
        <span class="account-name" title="${esc(u.username)}">${esc(u.display_name)}${where}</span>
        ${extra}
        <button type="button" class="account-btn" data-act="profile">Mon compte${u.profile_complete ? "" : ` <span class="account-dot" title="Profil à compléter">!</span>`}</button>
        <button type="button" class="account-btn" data-act="logout">Se déconnecter</button>`;
    };
    el.addEventListener("click", e => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "login") openLogin();
      if (act === "profile") openProfile();
      if (act === "logout") logout();
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
    return parts.join(" · ");
  }

  // Liens d'en-tête communs aux pages
  const LINKS = {
    search: { href: "index.html", label: "Recherche" },  // "./" renvoie les membres vers leurs créneaux
    picks: { href: "mes-creneaux.html", label: "Créneaux choisis", show: u => u.can.view_selections },
    admin: { href: "admin.html", label: "Administration", show: u => u.can.admin_area },
    newsletters: { href: "newsletters.html", label: "Newsletters", show: u => u.can.newsletters },
  };

  return {
    ROLE_LABELS, LINKS, roleLabel,
    get user() { return user; },
    get config() { return config; },
    onChange: fn => listeners.push(fn),
    set redirectAfterLogin(fn) { redirectAfterLogin = fn; },
    init, login, logout, api, esc, openLogin, openForgot, openProfile, openPasswordChange, openMessage,
    mountAccount, passwordChecklist, generatePassword, setUser, setStructures,
  };
})();
