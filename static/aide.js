// Pages d'aide : sommaire de la page, et guides qui concernent le compte connecté signalés « Pour vous ».

// Guides qui concernent un compte, d'après son rôle et ses profils dans la structure active
function guidesFor(u) {
  if (!u?.structure) return new Set();
  const mine = new Set(["membre"]);
  if (u.role === "manager") mine.add("administrateur").add("inscriptions");
  if (u.profiles?.includes("gestionnaire")) mine.add("gestionnaire");
  if (u.profiles?.includes("inscriptions")) mine.add("inscriptions");
  return mine;
}

function markMine(u) {
  const mine = guidesFor(u);
  for (const el of document.querySelectorAll("[data-for]")) {
    el.classList.toggle("is-mine", mine.has(el.dataset.for));
    const badge = el.querySelector(".help-badge");
    if (badge) badge.hidden = !mine.has(el.dataset.for);
  }
  const hint = document.querySelector(".help-mine-hint");
  if (hint) hint.hidden = !mine.size;
}

// Sommaire : un lien par section (titre h2)
const toc = document.querySelector(".help-toc");
if (toc) {
  const items = [...document.querySelectorAll(".help-article section[id] > h2")]
    .map(h => `<li><a href="#${h.parentElement.id}">${Session.esc(h.textContent)}</a></li>`);
  toc.innerHTML = `<h2>Sommaire</h2><ol>${items.join("")}</ol>`;
}

// Installer l'application (guide Membre) : bouton quand le navigateur le permet, mention si c'est déjà fait
const installBtn = document.getElementById("install-app");
if (installBtn) {
  const done = document.querySelector(".help-installed");
  done.hidden = !Session.installed();
  Session.onInstallable(ok => { installBtn.closest(".help-install").hidden = !ok; });
  installBtn.addEventListener("click", async () => {
    if (await Session.promptInstall()) done.hidden = false;
  });
}

Session.mountAccount(document.getElementById("account"),
  [Session.LINKS.search, Session.LINKS.heights, Session.LINKS.picks, Session.LINKS.newsletters, Session.LINKS.admin]);
Session.onChange(markMine);
Session.init();
