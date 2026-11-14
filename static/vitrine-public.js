// Page vitrine publique (app/site.py) : referme le menu mobile après un clic sur une section. Facultatif : la page
// fonctionne sans script.
document.querySelectorAll(".v-burger nav a").forEach(a => a.addEventListener("click", () => {
  a.closest("details").open = false;
}));

// Vidéos YouTube : le lecteur (youtube-nocookie.com) remplace la vignette au clic, rien n'est chargé avant.
document.querySelectorAll("a.v-video-link[data-video]").forEach(a => a.addEventListener("click", e => {
  if (!/^[A-Za-z0-9_-]{11}$/.test(a.dataset.video)) return;
  e.preventDefault();
  const frame = document.createElement("iframe");
  frame.src = `https://www.youtube-nocookie.com/embed/${a.dataset.video}?autoplay=1&rel=0`;
  frame.title = a.dataset.title || "Vidéo";
  frame.allow = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share";
  frame.allowFullscreen = true;
  frame.referrerPolicy = "strict-origin-when-cross-origin";   // YouTube refuse les lecteurs intégrés sans référent
  a.replaceWith(frame);
}));
