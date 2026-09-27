// Session utilisateur partagée par index.html et admin.html :
// appels API JSON, connexion/déconnexion, changement de mot de passe,
// et encart « compte » dans l'en-tête.

const Session = (() => {
  let user = null;
  const listeners = [];

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  // Message lisible à partir d'une erreur FastAPI (detail texte ou liste de validation)
  function errorMessage(body, res) {
    const d = body && body.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d) && d.length) {
      const e = d[0];
      const field = e.loc ? e.loc[e.loc.length - 1] : "";
      if (field === "username") return "Nom invalide : 3 à 32 caractères parmi lettres, chiffres, . _ -";
      if (field === "password" || field === "new_password") return "Le mot de passe doit faire au moins 8 caractères.";
      return e.msg || "Données invalides.";
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
      const err = new Error(errorMessage(data, res));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function setUser(u) {
    user = u;
    for (const fn of listeners) fn(user);
  }

  async function init() {
    try {
      setUser((await api("/api/auth/me")).user);
    } catch {
      setUser(null);
    }
    return user;
  }

  // Page où envoyer l'utilisateur juste après sa connexion : fonction(user) -> URL ou null.
  // Réglée par la page (la recherche envoie les membres vers leurs créneaux choisis).
  let redirectAfterLogin = null;

  async function login(username, password) {
    const u = (await api("/api/auth/login", { method: "POST", body: { username, password } })).user;
    const target = redirectAfterLogin?.(u);
    if (target) {
      location.assign(target);  // pas de setUser : inutile de recharger la page qu'on quitte
      return;
    }
    setUser(u);
  }

  async function logout() {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    setUser(null);
  }

  // ---- Dialogues ----

  let dialog = null;

  function ensureDialog() {
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.className = "account-dialog";
    document.body.append(dialog);
    dialog.addEventListener("click", e => { if (e.target === dialog) dialog.close(); });
    return dialog;
  }

  function openForm({ title, fields, submitLabel, onSubmit }) {
    const d = ensureDialog();
    d.innerHTML = `
      <form method="dialog">
        <h2>${esc(title)}</h2>
        ${fields.map(f => `
          <label>${esc(f.label)}
            <input name="${f.name}" type="${f.type || "text"}" autocomplete="${f.autocomplete || "off"}" required>
          </label>`).join("")}
        <p class="dialog-error" role="alert"></p>
        <div class="dialog-actions">
          <button type="button" class="btn-quiet" value="cancel">Annuler</button>
          <button type="submit" class="btn-primary">${esc(submitLabel)}</button>
        </div>
      </form>`;
    const form = d.querySelector("form");
    const errEl = d.querySelector(".dialog-error");
    d.querySelector("[value=cancel]").addEventListener("click", () => d.close());
    form.addEventListener("submit", async e => {
      e.preventDefault();
      errEl.textContent = "";
      const values = Object.fromEntries(new FormData(form));
      const btn = form.querySelector("[type=submit]");
      btn.disabled = true;
      try {
        await onSubmit(values);
        d.close();
      } catch (err) {
        errEl.textContent = err.message;
      } finally {
        btn.disabled = false;
      }
    });
    d.showModal();
    form.querySelector("input").focus();
  }

  function openLogin() {
    openForm({
      title: "Connexion",
      submitLabel: "Se connecter",
      fields: [
        { name: "username", label: "Identifiant", autocomplete: "username" },
        { name: "password", label: "Mot de passe", type: "password", autocomplete: "current-password" },
      ],
      onSubmit: v => login(v.username, v.password),
    });
  }

  function openPasswordChange() {
    openForm({
      title: "Changer de mot de passe",
      submitLabel: "Changer le mot de passe",
      fields: [
        { name: "current_password", label: "Mot de passe actuel", type: "password", autocomplete: "current-password" },
        { name: "new_password", label: "Nouveau mot de passe (8 caractères min.)", type: "password", autocomplete: "new-password" },
        { name: "confirm", label: "Confirmation", type: "password", autocomplete: "new-password" },
      ],
      onSubmit: async v => {
        if (v.new_password !== v.confirm) throw new Error("Les deux saisies diffèrent.");
        await api("/api/me/password", {
          method: "POST",
          body: { current_password: v.current_password, new_password: v.new_password },
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
        <span class="account-name">${esc(u.username)}${where}</span>
        ${extra}
        <button type="button" class="account-btn" data-act="password">Mot de passe</button>
        <button type="button" class="account-btn" data-act="logout">Se déconnecter</button>`;
    };
    el.addEventListener("click", e => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "login") openLogin();
      if (act === "password") openPasswordChange();
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
    return parts.join(" · ");
  }

  // Liens d'en-tête communs aux pages
  const LINKS = {
    search: { href: "index.html", label: "Recherche" },  // "./" renvoie les membres vers leurs créneaux
    picks: { href: "mes-creneaux.html", label: "Créneaux choisis", show: u => u.can.view_selections },
    admin: { href: "admin.html", label: "Administration", show: u => u.can.admin_area },
  };

  return {
    ROLE_LABELS, LINKS, roleLabel,
    get user() { return user; },
    onChange: fn => listeners.push(fn),
    set redirectAfterLogin(fn) { redirectAfterLogin = fn; },
    init, login, logout, api, esc, openLogin, mountAccount, setStructures,
  };
})();
