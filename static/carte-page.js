// Page « Carte » : tous les ports (ceux de la recherche) et leurs sites de plongée, ouverte à tous.

const esc = Carte.esc;
const fmtCoord = v => v.toFixed(4).replace(".", ",");

function sitePopup(s) {
  const cur = s.current
    ? `Courant : atlas ${esc(s.current.atlas.replace(/\s*\(.*\)$/, ""))}, port de référence ${esc(s.current.ref_port || "?")}`
    : `<span class="muted">${esc(s.current_status || "Courant non disponible")}</span>`;
  return `<strong>${esc(s.name)}</strong><br>${esc(s.port)} · ${fmtCoord(s.lat)}, ${fmtCoord(s.lon)}`
    + `${s.notes ? `<br>${esc(s.notes)}` : ""}<br>${cur}`;
}

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
  const points = [];
  const markers = new Map();   // "port:id" / "site:id" → marqueur
  for (const p of ports) {
    const n = sites.filter(s => s.port_id === p.id).length;
    const m = Carte.portMarker(p).bindPopup(`<strong>${esc(p.name)}</strong><br>${n ? `${n} site(s) de plongée` : "Aucun site de plongée"}`);
    m.addTo(map);
    markers.set(`port:${p.id}`, m);
    points.push([p.latitude, p.longitude]);
  }
  for (const s of sites) {
    const m = Carte.siteMarker(s).bindPopup(sitePopup(s));
    m.addTo(map);
    markers.set(`site:${s.id}`, m);
    points.push([s.lat, s.lon]);
  }
  Carte.fit(map, points, { zoom: 11, maxZoom: 12 });
  status.textContent = ports.length
    ? `${ports.length} port(s), ${sites.length} site(s) de plongée.`
    : "Aucun port pour l'instant.";

  // Liste : un clic centre la carte sur le port ou le site
  const places = document.getElementById("places");
  const portIds = new Set(ports.map(p => p.id));
  const groups = ports.map(p => ({ id: p.id, name: p.name, sites: sites.filter(s => s.port_id === p.id) }));
  // sites d'un port absent de la recherche (marées pas encore calculées)
  for (const s of sites.filter(x => !portIds.has(x.port_id))) {
    let g = groups.find(x => x.id === s.port_id);
    if (!g) groups.push(g = { id: s.port_id, name: s.port, sites: [], noPort: true });
    g.sites.push(s);
  }
  places.innerHTML = groups.length ? groups.map(g => `
    <div class="map-place">
      ${g.noPort ? `<h3>${esc(g.name)}</h3>` : `<h3><button type="button" class="link-button" data-go="port:${g.id}">${esc(g.name)}</button></h3>`}
      ${g.sites.length ? `<ul>${g.sites.map(s => `
        <li><button type="button" class="link-button" data-go="site:${s.id}">
          <i class="dot" style="background:${s.current ? Carte.COLORS.current : Carte.COLORS.site}"></i>${esc(s.name)}</button>
          ${s.notes ? `<span class="muted"> · ${esc(s.notes)}</span>` : ""}</li>`).join("")}</ul>`
        : `<p class="muted">Aucun site de plongée.</p>`}
    </div>`).join("") : `<p class="muted">Aucun port.</p>`;
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
  [Session.LINKS.search, Session.LINKS.heights, Session.LINKS.picks, Session.LINKS.newsletters, Session.LINKS.admin, Session.LINKS.help]);
Session.init();
init();
