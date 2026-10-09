// Page publique : demande d'adhésion à une structure, par son lien (/rejoindre.html#<jeton>, voir
// app/structure_profile.py). Le jeton est dans le fragment : jamais envoyé au serveur dans l'adresse de la page.

const token = decodeURIComponent(location.hash.slice(1));
const form = document.getElementById("request-form");
const statusEl = document.getElementById("request-status");
const $ = id => document.getElementById(id);

function invalid(message) {
  $("join-invalid").hidden = false;
  $("join-invalid-text").textContent = message;
}

async function load() {
  if (!token) return invalid("Ce lien est incomplet : demandez le lien d'adhésion à votre structure.");
  let st;
  try {
    st = await Session.api(`/api/join/${encodeURIComponent(token)}`);
  } catch (err) {
    return invalid(err.message);
  }
  document.title = `Rejoindre ${st.name} — Calendive`;
  $("join-name").textContent = st.name;
  const details = [st.address, st.website].filter(Boolean);
  $("join-details").textContent = details.join(" · ");
  if (st.logo_url) {
    $("join-logo").src = st.logo_url;
    $("join-logo").hidden = false;
  }
  $("join-view").hidden = false;
}

form.addEventListener("submit", async e => {
  e.preventDefault();
  statusEl.textContent = "";
  if (!form.reportValidity()) return;
  const f = new FormData(form);
  const text = name => (f.get(name) || "").trim() || null;
  const btn = form.querySelector("[type=submit]");
  btn.disabled = true;
  try {
    await Session.api(`/api/join/${encodeURIComponent(token)}`, {
      method: "POST",
      body: {
        first_name: text("first_name"), last_name: text("last_name"), email: text("email"), phone: text("phone"),
        message: text("message"), consent: f.get("consent") === "on", website: text("website"),
      },
    });
    form.hidden = true;
    $("request-done").hidden = false;
  } catch (err) {
    statusEl.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

load();
