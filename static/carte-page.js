// Page « Carte » : tous les ports (ceux de la recherche) et leurs sites de plongée, ouverte à tous.

const esc = Carte.esc;
const fmtCoord = v => v.toFixed(4).replace(".", ",");

function sitePopup(s) {
  const cur = s.current
    ? `Courant : atlas ${esc(s.current.atlas.replace(/\s*\(.*\)$/, ""))}, port de référence ${esc(s.current.ref_port || "?")}`
    : `<span class="muted">${esc(s.current_status || "Courant non disponible")}</span>`;
  return `<strong>${esc(s.name)}</strong><br>${fmtCoord(s.lat)}, ${fmtCoord(s.lon)}`
    + `${s.notes ? `<br>${esc(s.notes)}` : ""}<br>${cur}`;
}

// Les sites de plongée sont propres à chaque structure : ceux de la structure active du compte connecté
async function init() {
  const status = document.getElementById("status");
  const map = Carte.create(document.getElementById("map"));
  document.getElementById("map-legend").innerHTML = Carte.legend();
  let ports = [], sites = [];
  try {
    [ports, sites] = await Promise.all([Session.api("/api/ports"), Session.api("/api/dive-sites")]);
  } catch (e) {
    status.textContent = `Carte non chargée : ${e.message}`;
    return;
  }
  const markers = new Map();   // "port:id" / "site:id" → marqueur
  const portPoints = [], sitePoints = [];
  for (const p of ports) {
    const m = Carte.portMarker(p).bindPopup(`<strong>${esc(p.name)}</strong>`).addTo(map);
    markers.set(`port:${p.id}`, m);
    portPoints.push([p.latitude, p.longitude]);
  }
  for (const s of sites) {
    const m = Carte.siteMarker(s).bindPopup(sitePopup(s)).addTo(map);
    markers.set(`site:${s.id}`, m);
    sitePoints.push([s.lat, s.lon]);
  }
  // cadrage sur les sites de la structure, à défaut sur les ports
  Carte.fit(map, sitePoints.length ? sitePoints : portPoints, { zoom: 12, maxZoom: 12 });
  const structure = Session.user?.structure?.name;
  status.textContent = `${ports.length} port(s)` + (structure ? `, ${sites.length} site(s) de plongée de ${structure}.` : ".");

  const places = document.getElementById("places");
  const siteList = !Session.user
    ? `<p class="muted">Connectez-vous pour voir les sites de plongée de votre structure.</p>`
    : !structure ? `<p class="muted">Votre compte n'est rattaché à aucune structure : pas de sites de plongée.</p>`
    : sites.length ? `<ul>${sites.map(s => `
        <li><button type="button" class="link-button" data-go="site:${s.id}">
          <i class="dot" style="background:${s.current ? Carte.COLORS.current : Carte.COLORS.site}"></i>${esc(s.name)}</button>
          ${s.notes ? `<span class="muted"> · ${esc(s.notes)}</span>` : ""}</li>`).join("")}</ul>`
    : `<p class="muted">Aucun site de plongée pour ${esc(structure)} : ses administrateurs les ajoutent dans Administration → Sites de plongée.</p>`;
  places.innerHTML = `
    <div class="map-place"><h3>Sites de plongée${structure ? ` · ${esc(structure)}` : ""}</h3>${siteList}</div>
    <div class="map-place"><h3>Ports</h3>${ports.length ? `<ul>${ports.map(p => `
      <li><button type="button" class="link-button" data-go="port:${p.id}">${esc(p.name)}</button></li>`).join("")}</ul>`
      : `<p class="muted">Aucun port.</p>`}</div>`;
  places.addEventListener("click", e => {
    const btn = e.target.closest("[data-go]");
    const m = btn && markers.get(btn.dataset.go);
    if (!m) return;
    map.setView(m.getLatLng(), btn.dataset.go.startsWith("site") ? 14 : 12);
    m.openPopup();
    document.getElementById("map").scrollIntoView({ behavior: "smooth", block: "center" });
  });
}

Session.mountAccount(document.getElementById("account"),
  [Session.LINKS.search, Session.LINKS.heights, Session.LINKS.picks, Session.LINKS.newsletters, Session.LINKS.admin, Session.LINKS.divers, Session.LINKS.help]);
// la session d'abord : les sites dépendent de la structure du compte (connexion ou changement de structure :
// la page se recharge)
Session.init().then(() => {
  init();
  Session.onChange(() => location.reload());
});
