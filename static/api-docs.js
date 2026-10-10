// Documentation interactive de l'API (Swagger UI, hébergé dans vendor/swagger-ui) : voir app/openapi_doc.py
window.ui = SwaggerUIBundle({
  url: "/api/openapi.json",
  dom_id: "#swagger-ui",
  deepLinking: true,
  docExpansion: "none",           // catégories repliées : 200 routes
  filter: true,                   // recherche dans les routes
  tryItOutEnabled: false,
  persistAuthorization: false,    // le jeton collé n'est pas gardé dans le navigateur
  displayRequestDuration: true,
  defaultModelsExpandDepth: 0,
  validatorUrl: null,             // pas d'appel au validateur en ligne de swagger.io
  presets: [SwaggerUIBundle.presets.apis],
});
