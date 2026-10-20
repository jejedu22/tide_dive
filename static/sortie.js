// Page publique : inscription d'une personne extérieure à une plongée de la structure (/sortie.html#<jeton>), ou
// annulation de son inscription (/sortie.html#annuler=<jeton>, lien reçu par e-mail). Voir app/guests.py. Les jetons
// sont dans le fragment : jamais envoyés au serveur dans l'adresse de la page.

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const hash = decodeURIComponent(location.hash.slice(1));
const form = $("guest-form");
let token = null;
let slots = [];
let chosen = null;

const day = iso => new Date(`${iso}T12:00:00`).toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" });

function invalid(message) {
  $("guest-invalid").hidden = false;
  $("guest-invalid-text").textContent = message;
}

function places(s) {
  if (s.max_registrations == null) return `${s.confirmed_count} inscrit(s)`;
  const left = s.max_registrations - s.confirmed_count;
  return left > 0 ? `${left} place(s) restante(s) sur ${s.max_registrations}`
    : `<span class="full">Complet</span> : inscription en file d'attente${s.waiting_count ? ` (${s.waiting_count} en attente)` : ""}`;
}

function slotHtml(s) {
  const tide = s.kind ? ` · étale de ${s.kind === "PM" ? "pleine" : "basse"} mer à ${esc(s.time)}${s.coefficient ? `, coef ${Math.round(s.coefficient)}` : ""}`
    : s.water ? ` · ${esc(s.water)}` : "";
  const span = s.end_date ? ` au ${esc(day(s.end_date))}` : "";
  return `<li class="guest-slot" style="--type-color:${esc(s.type.color)}">
    <h3>${esc(day(s.date))}${span} — rendez-vous ${esc(s.rdv.time)}</h3>
    <p>${esc(s.type.label)}${s.note ? ` · ${esc(s.note)}` : ""} · ${esc(s.port)}${s.site ? ` · site : ${esc(s.site)}` : ""}${tide}</p>
    ${s.min_level ? `<p>Niveau minimum : ${esc(s.min_level)}</p>` : ""}
    <p class="places">${places(s)}</p>
    <p><button type="button" class="btn-secondary btn-small" data-slot="${s.id}">${s.full ? "M'inscrire en file d'attente" : "M'inscrire"}</button></p>
  </li>`;
}

async function load() {
  if (!hash) return invalid("Ce lien est incomplet : demandez le lien d'inscription à la structure.");
  if (hash.startsWith("annuler=")) {
    $("guest-cancel").hidden = false;
    return;
  }
  token = hash;
  let data;
  try {
    data = await Session.api(`/api/guest/${encodeURIComponent(token)}`);
  } catch (err) {
    return invalid(err.message);
  }
  const st = data.structure;
  document.title = `Plongées de ${st.name} — Calendive`;
  $("guest-name").textContent = st.name;
  $("guest-details").textContent = [st.address, st.contact_phone, st.contact_email].filter(Boolean).join(" · ");
  if (st.logo_url) { $("guest-logo").src = st.logo_url; $("guest-logo").hidden = false; }
  slots = data.selections;
  $("guest-list").innerHTML = slots.map(slotHtml).join("");
  $("guest-empty").hidden = slots.length > 0;
  $("guest-view").hidden = false;
}

$("guest-list").addEventListener("click", e => {
  const btn = e.target.closest("[data-slot]");
  if (!btn) return;
  chosen = slots.find(s => s.id === Number(btn.dataset.slot));
  $("guest-chosen").innerHTML = `Plongée du <strong>${esc(day(chosen.date))}</strong>, rendez-vous ${esc(chosen.rdv.time)}, ${esc(chosen.port)}.`
    + (chosen.full ? " Elle est complète : vous serez en file d'attente et prévenu(e) si une place se libère." : "");
  $("guest-view").hidden = true;
  $("guest-form-panel").hidden = false;
  form.hidden = false;
  $("guest-done").hidden = true;
  form.first_name.focus();
});

$("guest-back").addEventListener("click", () => {
  $("guest-form-panel").hidden = true;
  $("guest-view").hidden = false;
});

form.addEventListener("submit", async e => {
  e.preventDefault();
  $("guest-status").textContent = "";
  if (!form.reportValidity()) return;
  const f = new FormData(form);
  const text = name => (f.get(name) || "").trim();
  const btn = form.querySelector("[type=submit]");
  btn.disabled = true;
  try {
    const r = await Session.api(`/api/guest/${encodeURIComponent(token)}/selections/${chosen.id}`, {
      method: "POST",
      body: {
        first_name: text("first_name"), last_name: text("last_name"), email: text("email"), phone: text("phone"),
        consent: f.get("consent") === "on", website: text("website") || null,
      },
    });
    form.hidden = true;
    $("guest-done").hidden = false;
    $("guest-done").innerHTML = (r.status === "waiting"
      ? `<p><strong>Vous êtes inscrit(e) en file d'attente (position ${r.position}).</strong> Vous serez prévenu(e) par e-mail si une place se libère.</p>`
      : `<p><strong>Votre inscription est enregistrée.</strong> À bientôt sous l'eau !</p>`)
      + (r.email_sent ? `<p>Un e-mail de confirmation vous a été envoyé, avec un lien pour annuler si besoin.</p>`
        : `<p>Si vous ne pouvez plus venir, prévenez la structure.</p>`);
  } catch (err) {
    $("guest-status").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("guest-cancel-btn").addEventListener("click", async e => {
  e.target.disabled = true;
  try {
    const r = await Session.api("/api/guest/cancel", { method: "POST", body: { token: hash.slice("annuler=".length) } });
    $("guest-cancel-status").textContent = `Inscription annulée : ${r.selection.when}, ${r.selection.place}. Merci d'avoir prévenu !`;
    e.target.hidden = true;
  } catch (err) {
    $("guest-cancel-status").textContent = err.message;
    e.target.disabled = false;
  }
});

load();
