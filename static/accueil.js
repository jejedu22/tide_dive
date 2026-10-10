// Page d'accueil de présentation (accueil.html) : menu du compte, boutons selon la connexion, marées du jour.
// Les marées viennent de l'API publique (/api/ports, /api/ports/{id}/tides) : aucun compte requis.
(() => {
  const $ = id => document.getElementById(id);
  const esc = Session.esc;
  const fmtM = n => `${String(Math.round(n * 100) / 100).replace(".", ",")} m`;
  const KINDS = { PM: "Pleine mer", BM: "Basse mer" };
  const PORT_KEY = "calendive.accueil.port";
  const read = () => { try { return localStorage.getItem(PORT_KEY); } catch { return null; } };
  const write = v => { try { localStorage.setItem(PORT_KEY, v); } catch { /* sans stockage */ } };

  // Boutons : ceux du visiteur, ceux d'un compte connecté ; « J'ai déjà un compte » ouvre la connexion
  function onSession(u) {
    for (const el of document.querySelectorAll("[data-when=visitor]")) el.hidden = !!u;
    for (const el of document.querySelectorAll("[data-when=user]")) el.hidden = !u;
  }
  document.addEventListener("click", e => {
    if (e.target.closest("[data-login]")) Session.openLogin();
  });

  // Marées d'aujourd'hui du port choisi (heure locale du navigateur : pour des plongeurs, celle du port)
  const today = () => new Date().toLocaleDateString("sv-SE");   // AAAA-MM-JJ

  async function showTides(portId) {
    const list = $("today-list");
    try {
      const day = today();
      const r = await fetch(`/api/ports/${portId}/tides?start=${day}&end=${day}`);
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      list.innerHTML = data.tides.length ? data.tides.map(t => `
        <li class="${t.kind === "PM" ? "pm" : "bm"}">
          <span class="today-kind">${KINDS[t.kind]}</span>
          <span class="today-time">${esc(t.time)}</span>
          <span class="today-meta">${fmtM(t.height_m)}${t.coefficient != null ? ` · coefficient ${t.coefficient}` : ""}</span>
        </li>`).join("") : `<li><span class="today-meta">Pas d'étale enregistrée aujourd'hui pour ce port.</span></li>`;
      $("today-note").textContent = "Horaires indicatifs (heure locale, hauteurs au-dessus du zéro des cartes) : vérifiez-les auprès du Shom avant de plonger.";
    } catch {
      list.innerHTML = `<li><span class="today-meta">Les marées sont momentanément indisponibles.</span></li>`;
      $("today-note").textContent = "";
    }
  }

  async function initToday() {
    let ports;
    try {
      const r = await fetch("/api/ports");
      ports = r.ok ? await r.json() : [];
    } catch {
      ports = [];
    }
    if (!ports.length) return;                      // rien à montrer : la section reste cachée
    const select = $("today-port");
    select.innerHTML = ports.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
    const saved = read();
    if (saved && ports.some(p => String(p.id) === saved)) select.value = saved;
    select.addEventListener("change", () => { write(select.value); showTides(select.value); });
    $("today").hidden = false;
    showTides(select.value);
  }

  // Connexion depuis cette page : un adhérent va directement à ses créneaux choisis (comme depuis la recherche)
  Session.redirectAfterLogin = u => (u.can.view_selections ? "mes-creneaux.html" : null);
  Session.mountAccount($("account"), [Session.LINKS.search, Session.LINKS.heights, Session.LINKS.map, Session.LINKS.admin, Session.LINKS.help]);
  Session.onChange(onSession);
  Session.init();
  initToday();
})();
