// Pages « Horaires de marée » (rendues par le serveur : app/seo.py) : seulement le menu du compte.
(() => {
  Session.mountAccount(document.getElementById("account"), [Session.LINKS.search, Session.LINKS.heights, Session.LINKS.map, Session.LINKS.admin, Session.LINKS.help]);
  Session.init();
})();
