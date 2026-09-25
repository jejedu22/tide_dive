const portSelect = document.getElementById("port");
const startInput = document.getElementById("start");
const endInput = document.getElementById("end");
const tidePhaseSelect = document.getElementById("tide_phase");
const maxCoefInput = document.getElementById("max_coefficient");
const coefVal = document.getElementById("coef_val");
const marginInput = document.getElementById("margin_minutes");
const marginVal = document.getElementById("margin_val");
const daylightSelect = document.getElementById("daylight");
const searchBtn = document.getElementById("search");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results-list");

maxCoefInput.addEventListener("input", () => coefVal.textContent = maxCoefInput.value);
marginInput.addEventListener("input", () => marginVal.textContent = marginInput.value);

function todayISO(offsetDays = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

async function loadPorts() {
  const res = await fetch("/api/ports");
  const ports = await res.json();
  portSelect.innerHTML = ports.map(p => `<option value="${p.id}">${p.name}</option>`).join("");
  if (ports.length === 0) {
    statusEl.textContent = "Aucun port en base. Lance d'abord precompute.py pour un port et une année (voir README).";
  }
}

function renderResults(data) {
  resultsEl.innerHTML = "";
  if (data.results.length === 0) {
    statusEl.textContent = "Aucun créneau ne correspond à ces critères sur la période choisie.";
    return;
  }
  statusEl.textContent = `${data.results.length} créneau(x) trouvé(s) pour ${data.port}.`;

  for (const r of data.results) {
    const card = document.createElement("div");
    card.className = `day-card ${r.kind}`;
    const coefBadge = r.coefficient != null ? `<span class="coef">coef ${r.coefficient}</span>` : "";
    const phase = r.kind === "PM" ? "Étale de pleine mer" : "Étale de basse mer";
    const daylightInfo = r.daylight_bounds
      ? `jour ${r.daylight_bounds.start}–${r.daylight_bounds.end}`
      : "";
    card.innerHTML = `
      <div class="date">${r.date} · ${r.time}</div>
      <div class="meta">${phase} · fenêtre ${r.window.start}–${r.window.end} · hauteur ${r.height_m} m · ${daylightInfo}</div>
      ${coefBadge}
    `;
    resultsEl.appendChild(card);
  }
}

async function search() {
  const portId = portSelect.value;
  if (!portId) return;
  statusEl.textContent = "Recherche…";
  const params = new URLSearchParams({
    port_id: portId,
    start: startInput.value,
    end: endInput.value,
    max_coefficient: maxCoefInput.value,
    tide_phase: tidePhaseSelect.value,
    daylight: daylightSelect.value,
    margin_minutes: marginInput.value,
  });
  try {
    const res = await fetch(`/api/dive-windows?${params}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      statusEl.textContent = `Erreur : ${err.detail || res.statusText}`;
      return;
    }
    renderResults(await res.json());
  } catch (e) {
    statusEl.textContent = "Erreur réseau : le serveur est-il lancé ?";
  }
}

searchBtn.addEventListener("click", search);

startInput.value = todayISO();
endInput.value = todayISO(13);

loadPorts();
