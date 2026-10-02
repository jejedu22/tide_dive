// Formulaire public : demande de création d'une structure (voir app/contact.py).

const form = document.getElementById("request-form");
const statusEl = document.getElementById("request-status");

form.addEventListener("submit", async e => {
  e.preventDefault();
  statusEl.textContent = "";
  if (!form.reportValidity()) return;
  const f = new FormData(form);
  const text = name => (f.get(name) || "").trim() || null;
  const btn = form.querySelector("[type=submit]");
  btn.disabled = true;
  try {
    await Session.api("/api/structure-requests", {
      method: "POST",
      body: {
        structure_name: text("structure_name"),
        city: text("city"),
        contact_name: text("contact_name"),
        email: text("email"),
        phone: text("phone"),
        message: text("message"),
        consent: f.get("consent") === "on",
        website: text("website"),
      },
    });
    form.hidden = true;
    document.getElementById("request-done").hidden = false;
  } catch (err) {
    statusEl.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});
