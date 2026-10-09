// « Plongeurs » : fiches plongeurs des membres de la structure (niveaux, licence, certificat médical).
// Administrateurs de la structure et profil « Gestionnaire » ; seul ce dernier valide les CACI.

const esc = Session.esc;
const $ = id => document.getElementById(id);
const fmtDate = iso => (iso ? new Date(iso + "T12:00:00").toLocaleDateString("fr-FR") : "");
const CACI_LABEL = { missing: "aucun", pending: "à valider", valid: "valide", expired: "expiré" };

let data = null;

function caciCell(d) {
  const c = d.caci;
  const tag = `<span class="caci-tag ${c.state}">${CACI_LABEL[c.state]}</span>`;
  if (c.state === "missing") return tag;
  const title = c.validated ? `Validé par ${c.validated_by || "?"}` : "Saisi par le membre, en attente de validation";
  return `${tag} <span class="muted" title="${esc(title)}">du ${fmtDate(c.date)}, jusqu'au ${fmtDate(c.valid_until)}</span>`;
}

function shownDivers() {
  const filter = $("divers-filter").value;
  return data.divers.filter(d => !filter
    || (filter === "pending" && d.caci.state === "pending")
    || (filter === "blocked" && ["missing", "expired"].includes(d.caci.state)));
}

function render() {
  const filter = $("divers-filter").value;
  const rows = shownDivers();
  $("divers-body").innerHTML = rows.length ? rows.map(d => `
    <tr data-id="${d.id}">
      <th scope="row">${esc(d.display_name)}</th>
      <td data-label="Niveau">${esc(d.diver_level_label || "—")}${d.qualifications ? `<br><span class="muted">${esc(d.qualifications)}</span>` : ""}</td>
      <td data-label="Encadrement">${esc(d.instructor_level_label || "—")}</td>
      <td data-label="Licence">${esc(d.licence_number || "—")}${d.licence_url
        ? ` <button type="button" class="btn-quiet btn-small" data-act="qr" title="QR code de la licence">QR</button>` : ""}</td>
      <td data-label="CACI">${caciCell(d)}</td>
      <td class="c-actions">
        ${data.can_validate && d.caci.state === "pending" ? `<button type="button" class="btn-primary btn-small" data-act="validate">Valider</button>` : ""}
        <button type="button" class="btn-quiet btn-small" data-act="edit">Modifier</button>
      </td>
    </tr>`).join("")
    : `<tr><td colspan="6" class="empty">Aucun membre${filter ? " avec ce filtre" : ""}.</td></tr>`;
  const pending = data.divers.filter(d => d.caci.state === "pending").length;
  $("caci-policy").innerHTML = (data.caci_check
    ? `<strong>Vérification du CACI activée</strong> : inscription refusée sans certificat valable le jour de la plongée (${data.caci_validity_months} mois).`
    : "Vérification du CACI désactivée : les inscriptions ne dépendent pas du certificat.")
    + (pending ? ` · <strong>${pending}</strong> certificat(s) à valider.` : "");
}

async function load() {
  try {
    data = await Session.api("/api/divers");
  } catch (e) {
    $("gate").hidden = false;
    $("gate").textContent = e.message;
    $("divers-view").hidden = true;
    return;
  }
  $("gate").hidden = true;
  $("divers-view").hidden = false;
  render();
}

function openQr(d) {
  Session.loadQr().then(() => {
    const dlg = document.createElement("dialog");
    dlg.className = "account-dialog";
    dlg.innerHTML = `<form method="dialog"><h2>Licence de ${esc(d.display_name)}</h2>
      <div class="licence-qr licence-qr-big">${Session.qrSvg(d.licence_url, 6)}</div>
      <p><a href="${esc(d.licence_url)}" target="_blank" rel="noopener noreferrer">Ouvrir la fiche du licencié</a></p>
      <div class="dialog-actions"><button type="submit" class="btn-primary">Fermer</button></div></form>`;
    document.body.append(dlg);
    dlg.addEventListener("close", () => dlg.remove());
    dlg.showModal();
  });
}

function openEdit(d) {
  const opts = (catalog, value) => `<option value="">—</option>` + Object.entries(catalog).map(([k, label]) =>
    `<option value="${k}"${k === value ? " selected" : ""}>${esc(label)}</option>`).join("");
  const today = new Date().toISOString().slice(0, 10);
  Session.openForm({
    title: `Fiche plongeur — ${d.display_name}`,
    submitLabel: "Enregistrer",
    fields: [],
    extra: `
      <label>Niveau de plongeur <select name="diver_level">${opts(data.levels, d.diver_level)}</select></label>
      <label>Niveau d'encadrement <select name="instructor_level">${opts(data.instructor_levels, d.instructor_level)}</select></label>
      <label>Autres qualifications <input name="qualifications" maxlength="200" value="${esc(d.qualifications ?? "")}"></label>
      <label>Numéro de licence FFESSM <input name="licence_number" maxlength="30" value="${esc(d.licence_number ?? "")}"></label>
      <label>Lien du QR code de la licence <input name="licence_url" type="url" maxlength="500" placeholder="https://…" value="${esc(d.licence_url ?? "")}"></label>
      <label>Date du certificat médical (CACI) <input name="caci_date" type="date" max="${today}" value="${esc(d.caci.date ?? "")}">
        <small class="field-hint">${data.can_validate ? "Une date que vous saisissez est validée d'office."
          : "Une date modifiée devra être validée par un gestionnaire."}</small></label>`,
    onSubmit: async v => {
      const body = Object.fromEntries(["diver_level", "instructor_level", "qualifications", "licence_number",
                                       "licence_url", "caci_date"].map(k => [k, v[k] || null]));
      const saved = await Session.api(`/api/divers/${d.id}`, { method: "PUT", body });
      Object.assign(d, saved);
      render();
    },
  });
}

$("divers-body").addEventListener("click", async e => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const d = data.divers.find(x => x.id === Number(btn.closest("tr").dataset.id));
  if (btn.dataset.act === "qr") return openQr(d);
  if (btn.dataset.act === "edit") return openEdit(d);
  if (btn.dataset.act === "validate") {
    btn.disabled = true;
    try {
      Object.assign(d, await Session.api(`/api/divers/${d.id}/caci/validate`, { method: "POST" }));
      render();
    } catch (err) {
      $("flash").textContent = err.message;
      btn.disabled = false;
    }
  }
});
$("divers-filter").addEventListener("change", render);

// Export Excel des fiches affichées (filtre compris)
const CACI_STATES = { valid: "valable", pending: "à valider", missing: "absent", expired: "expiré" };
$("divers-export").addEventListener("click", () => {
  const rows = data ? shownDivers() : [];
  if (!rows.length) return;
  XlsxExport.download(`plongeurs-${XlsxExport.slug(Session.user?.structure?.name || "structure")}.xlsx`, "Plongeurs", [
    { header: "Membre", width: 26, value: d => d.display_name },
    { header: "Identifiant", width: 18, value: d => d.username },
    { header: "Niveau", width: 18, value: d => d.diver_level_label || "" },
    { header: "Autres qualifications", width: 26, value: d => d.qualifications || "" },
    { header: "Encadrement", width: 18, value: d => d.instructor_level_label || "" },
    { header: "Licence", width: 16, value: d => d.licence_number || "" },
    { header: "CACI (date)", type: "date", width: 12, value: d => d.caci.date },
    { header: "Valable jusqu'au", type: "date", width: 14, value: d => d.caci.valid_until },
    { header: "CACI", width: 11, value: d => CACI_STATES[d.caci.state] || "" },
    { header: "Validé par", width: 18, value: d => d.caci.validated_by || "" },
  ], rows);
});

Session.mountAccount($("account"), [Session.LINKS.search, Session.LINKS.picks, Session.LINKS.newsletters, Session.LINKS.admin,
                                    Session.LINKS.map, Session.LINKS.help]);
Session.onChange(u => {
  $("structure-name").textContent = u?.structure ? `· ${u.structure.name}` : "";
  if (!u) {
    $("gate").hidden = false;
    $("gate").textContent = "Connectez-vous pour voir les plongeurs de votre structure.";
    $("divers-view").hidden = true;
    return;
  }
  if (!u.can.view_divers) {
    $("gate").hidden = false;
    $("gate").textContent = "Réservé aux administrateurs de la structure et au profil « Gestionnaire ».";
    $("divers-view").hidden = true;
    return;
  }
  load();
});
Session.init();
