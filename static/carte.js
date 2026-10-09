// Cartes des ports et des sites de plongée (Leaflet, hébergé dans static/vendor/leaflet).
// Deux fonds au choix : plan OpenStreetMap ou photo aérienne de l'IGN (Géoplateforme, sans clé) ; le choix
// est retenu dans le navigateur. Utilisé par la page Carte, l'administration (ports, saisie des sites) et
// la fenêtre « Courants » des créneaux choisis.

const Carte = (() => {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const LAYER_KEY = "calendive.carte.fond";
  const COLORS = { port: "#073b4c", current: "#06d6a0", site: "#9aabb2", picked: "#ef476f" };

  function baseLayers() {
    return {
      plan: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '© <a href="https://www.openstreetmap.org/copyright" rel="noopener">contributeurs OpenStreetMap</a>',
      }),
      photo: L.tileLayer(
        "https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTHOIMAGERY.ORTHOPHOTOS"
          + "&STYLE=normal&TILEMATRIXSET=PM&FORMAT=image/jpeg&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}", {
          maxZoom: 19,
          attribution: 'Photo aérienne © <a href="https://www.ign.fr/" rel="noopener">IGN</a> – Géoplateforme',
        }),
    };
  }

  function savedLayer() {
    try { return localStorage.getItem(LAYER_KEY) === "photo" ? "photo" : "plan"; } catch { return "plan"; }
  }

  // Carte dans el ; molette désactivée tant qu'on n'a pas cliqué dessus (la page reste défilable)
  function create(el, { center = [48.6, -3.2], zoom = 8 } = {}) {
    const map = L.map(el, { scrollWheelZoom: false }).setView(center, zoom);
    const layers = baseLayers();
    layers[savedLayer()].addTo(map);
    L.control.layers({ "Plan (OpenStreetMap)": layers.plan, "Photo aérienne (IGN)": layers.photo }, null,
      { position: "topright" }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);
    map.on("baselayerchange", e => {
      try { localStorage.setItem(LAYER_KEY, e.layer === layers.photo ? "photo" : "plan"); } catch { /* sans stockage */ }
    });
    map.on("click focus", () => map.scrollWheelZoom.enable());
    map.on("mouseout", () => map.scrollWheelZoom.disable());
    return map;
  }

  function portMarker(p) {
    return L.circleMarker([p.latitude, p.longitude], {
      radius: 9, color: "white", weight: 2, fillColor: COLORS.port, fillOpacity: 1,
    }).bindTooltip(esc(p.name), { direction: "top", offset: [0, -8] });
  }

  // Site : vert si son courant est connu, gris sinon
  function siteMarker(s, { color } = {}) {
    return L.circleMarker([s.lat, s.lon], {
      radius: 7, color: "white", weight: 2, fillColor: color || (s.current ? COLORS.current : COLORS.site), fillOpacity: 1,
    }).bindTooltip(esc(s.name), { direction: "top", offset: [0, -6] });
  }

  // Cadre la carte sur les points ([lat, lon]) ; un seul point : zoom fixe
  function fit(map, points, { zoom = 13, maxZoom = 14 } = {}) {
    if (!points.length) return;
    if (points.length === 1) map.setView(points[0], zoom);
    else map.fitBounds(L.latLngBounds(points), { padding: [24, 24], maxZoom });
  }

  // Carte affichée dans un élément d'abord caché (dialogue) : Leaflet doit recalculer sa taille
  const refresh = map => setTimeout(() => map.invalidateSize(), 0);

  const legend = () => `
    <p class="map-legend">
      <span><i style="background:${COLORS.port}"></i>port</span>
      <span><i style="background:${COLORS.current}"></i>site avec courant</span>
      <span><i style="background:${COLORS.site}"></i>site sans courant</span>
    </p>`;

  return { create, portMarker, siteMarker, fit, refresh, legend, esc, COLORS };
})();
