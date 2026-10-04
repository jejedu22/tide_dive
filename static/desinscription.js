// Désinscription des newsletters d'une structure, par le lien personnel d'un e-mail.
// Rien ne se passe à l'ouverture de la page : les messageries et antivirus ouvrent
// les liens tout seuls. Il faut cliquer sur le bouton.

const text = document.getElementById("unsub-text");
const button = document.getElementById("unsub-confirm");
const token = new URLSearchParams(location.search).get("t");
const esc = Session.esc;

async function load() {
  if (!token) {
    text.textContent = "Ce lien de désinscription est incomplet. S'il provient d'un e-mail de test, c'est normal : le lien personnel n'est ajouté qu'aux envois réels.";
    return;
  }
  try {
    const info = await Session.api(`/api/newsletters/unsubscribe/${encodeURIComponent(token)}`);
    if (info.unsubscribed) {
      text.innerHTML = `L'adresse <strong>${esc(info.email)}</strong> ne reçoit déjà plus les newsletters de <strong>${esc(info.structure)}</strong>.`;
      return;
    }
    text.innerHTML = `Ne plus recevoir les newsletters de <strong>${esc(info.structure)}</strong> à l'adresse <strong>${esc(info.email)}</strong> ?`;
    button.hidden = false;
  } catch (e) {
    text.textContent = e.message;
  }
}

button.addEventListener("click", async () => {
  button.disabled = true;
  try {
    const info = await Session.api(`/api/newsletters/unsubscribe/${encodeURIComponent(token)}`, { method: "POST" });
    text.innerHTML = `C'est fait : <strong>${esc(info.email)}</strong> ne recevra plus les newsletters de <strong>${esc(info.structure)}</strong>.`;
    button.hidden = true;
  } catch (e) {
    text.textContent = e.message;
    button.disabled = false;
  }
});

load();
