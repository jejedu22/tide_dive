// Page ouverte depuis un e-mail d'invitation ou de réinitialisation :
//   mot-de-passe.html#token=…
// Le jeton est dans le fragment (#) : il n'est jamais envoyé au serveur dans
// l'URL (ni logs du reverse proxy, ni Referer). On le retire aussitôt de la
// barre d'adresse et de l'historique.

const $ = id => document.getElementById(id);
const esc = Session.esc;

const token = new URLSearchParams(location.hash.slice(1)).get("token");
history.replaceState(null, "", location.pathname);

const form = $("reset-form");
const intro = $("reset-intro");
const title = $("reset-title");
const errEl = $("reset-error");

function showInvalid(message) {
  title.textContent = "Lien expiré ou déjà utilisé";
  intro.textContent = message;
  form.hidden = true;
  $("reset-actions").hidden = !Session.config.password_reset;
}

$("reset-forgot").addEventListener("click", () => Session.openForgot());

$("reset-show").addEventListener("change", e => {
  for (const input of form.querySelectorAll("input[type=password], input[data-pw]")) {
    input.dataset.pw = "1";
    input.type = e.target.checked ? "text" : "password";
  }
});

async function start() {
  await Session.init().catch(() => {});
  if (!token) {
    showInvalid("Ce lien est incomplet. Ouvrez-le directement depuis l'e-mail reçu, ou demandez-en un nouveau.");
    return;
  }
  let info;
  try {
    info = await Session.api("/api/auth/token-info", { method: "POST", body: { token } });
  } catch (err) {
    showInvalid(err.message + (Session.config.password_reset ? " Vous pouvez recevoir un mot de passe provisoire par e-mail." : " Demandez-en un nouveau à votre administrateur."));
    return;
  }
  const invite = info.purpose === "invite";
  title.textContent = invite ? "Bienvenue ! Choisissez votre mot de passe" : "Nouveau mot de passe";
  intro.innerHTML = `Compte <strong>${esc(info.display_name)}</strong> — identifiant <strong>${esc(info.username)}</strong>`
    + (info.email ? `, adresse <strong>${esc(info.email)}</strong>` : "")
    + `.<br>Vous pourrez vous connecter avec l'identifiant ou l'adresse e-mail.`;
  $("reset-username").value = info.username;

  const list = Session.passwordChecklist(form.new_password, form.confirm);
  form.new_password.closest("label").after(list);
  form.hidden = false;
  form.new_password.focus();

  form.addEventListener("submit", async e => {
    e.preventDefault();
    errEl.textContent = "";
    if (form.new_password.value !== form.confirm.value) {
      errEl.textContent = "Les deux saisies diffèrent.";
      return;
    }
    const btn = form.querySelector("[type=submit]");
    btn.disabled = true;
    try {
      await Session.api("/api/auth/reset-password", {
        method: "POST", body: { token, new_password: form.new_password.value },
      });
      // connecté : accueil (le serveur envoie les membres sur leurs créneaux choisis)
      location.replace("./");
    } catch (err) {
      errEl.textContent = err.message;
      if (err.status === 400) showInvalid(err.message);
    } finally {
      btn.disabled = false;
    }
  });
}

start();
