// Page vitrine publique (app/site.py) : referme le menu mobile après un clic sur une section. Facultatif : la page
// fonctionne sans script.
document.querySelectorAll(".v-burger nav a").forEach(a => a.addEventListener("click", () => {
  a.closest("details").open = false;
}));
