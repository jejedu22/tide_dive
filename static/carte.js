// Cartes des ports et des sites de plongée (Leaflet, hébergé dans static/vendor/leaflet).
// Trois fonds au choix : plan OpenStreetMap, carte marine (cartes du SHOM assemblées par l'IGN, « Carte
// littorale » de la Géoplateforme, sans clé) ou photo aérienne de l'IGN ; balisage OpenSeaMap à superposer.
// Les choix sont retenus dans le navigateur. Utilisé par la page Carte, l'administration (ports, saisie des sites) et
// la fenêtre « Courants » des créneaux choisis.

const Carte = (() => {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const LAYER_KEY = "calendive.carte.fond";
  const SEAMARKS_KEY = "calendive.carte.balisage";
  const IGN = layer => "https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=" + layer
    + "&STYLE=normal&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}";
  const COLORS = { port: "#073b4c", current: "#06d6a0", site: "#9aabb2", picked: "#ef476f" };

  function baseLayers() {
    return {
      plan: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '© <a href="https://www.openstreetmap.org/copyright" rel="noopener">contributeurs OpenStreetMap</a>',
      }),
      // tuiles du niveau 6 au niveau 16 : au-delà, agrandies
      marine: L.tileLayer(IGN("GEOGRAPHICALGRIDSYSTEMS.COASTALMAPS") + "&FORMAT=image/png", {
        minZoom: 6, maxNativeZoom: 16, maxZoom: 19,
        attribution: 'Carte marine © <a href="https://www.shom.fr/" rel="noopener">Shom</a> / '
          + '<a href="https://www.ign.fr/" rel="noopener">IGN</a> – Géoplateforme (pas pour la navigation)',
      }),
      photo: L.tileLayer(IGN("ORTHOIMAGERY.ORTHOPHOTOS") + "&FORMAT=image/jpeg", {
        maxZoom: 19,
        attribution: 'Photo aérienne © <a href="https://www.ign.fr/" rel="noopener">IGN</a> – Géoplateforme',
      }),
    };
  }

  // Bouées, balises, feux, épaves (OpenSeaMap) : par-dessus n'importe quel fond
  const seamarksLayer = () => L.tileLayer("https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png", {
    maxNativeZoom: 18, maxZoom: 19,
    attribution: 'Balisage © <a href="https://www.openseamap.org/" rel="noopener">OpenSeaMap</a> (CC BY-SA)',
  });

  const read = key => { try { return localStorage.getItem(key); } catch { return null; } };
  const write = (key, value) => { try { localStorage.setItem(key, value); } catch { /* sans stockage */ } };

  function savedLayer() {
    const v = read(LAYER_KEY);
    return v === "photo" || v === "marine" ? v : "plan";
  }

  // Carte dans el ; molette désactivée tant qu'on n'a pas cliqué dessus (la page reste défilable)
  function create(el, { center = [48.6, -3.2], zoom = 8 } = {}) {
    const map = L.map(el, { scrollWheelZoom: false }).setView(center, zoom);
    const layers = baseLayers();
    const seamarks = seamarksLayer();
    layers[savedLayer()].addTo(map);
    if (read(SEAMARKS_KEY) === "1") seamarks.addTo(map);
    L.control.layers(
      { "Plan (OpenStreetMap)": layers.plan, "Carte marine (Shom/IGN)": layers.marine, "Photo aérienne (IGN)": layers.photo },
      { "Balisage (OpenSeaMap)": seamarks },
      { position: "topright" }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);
    map.on("baselayerchange", e => {
      write(LAYER_KEY, Object.keys(layers).find(k => layers[k] === e.layer) || "plan");
    });
    map.on("overlayadd overlayremove", e => {
      if (e.layer === seamarks) write(SEAMARKS_KEY, e.type === "overlayadd" ? "1" : "0");
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
