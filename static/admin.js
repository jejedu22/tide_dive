// Administration des comptes : création, mot de passe, droits, suppression.
// Toutes les vérifications de droits sont faites côté serveur (/api/admin/*).

const gateEl = document.getElementById("gate");
const adminEl = document.getElementById("admin");
const usersBody = document.getElementById("users-body");
const usersStatus = document.getElementById("users-status");
const createForm = document.getElementById("create-user");
const createStatus = document.getElementById("create-status");
const esc = Session.esc;

const fmtStamp = new Intl.DateTimeFormat("fr-FR", { dateStyle: "short", timeStyle: "short" });
const stamp = iso => (iso ? fmtStamp.format(new Date(iso)) : "–");

// Mot de passe lisible : sans 0/O ni 1/l/I, facile à dicter
function generatePassword(length = 12) {
  const alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = crypto.getRandomValues(new Uint32Array(length));
  return Array.from(bytes, b => alphabet[b % alphabet.length]).join("");
}

document.getElementById("gen-password").addEventListener("click", () => {
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

function onSessionChange(user) {
  const allowed = !!user && user.is_admin;
  adminEl.hidden = !allowed;
  gateEl.hidden = allowed;
  if (!user) {
    gateEl.innerHTML = `Connectez-vous avec un compte administrateur. <button type="button" class="btn-primary" id="gate-login">Se connecter</button>`;
    document.getElementById("gate-login").addEventListener("click", Session.openLogin);
  } else if (!user.is_admin) {
    gateEl.textContent = `Le compte « ${user.username} » n'a pas accès à l'administration.`;
  } else {
    loadUsers();
  }
}

Session.mountAccount(document.getElementById("account"));
Session.onChange(onSessionChange);
Session.init();
