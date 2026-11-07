"""Swagger UI shell for /docs. Identical on every API process - each one
serves its own /openapi.yaml at the same relative path, so the page never
needs to know which process it's running in."""

FAVICON_DATA_URI = (
    "data:image/png;base64,"
    "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"
)

SWAGGER_UI_HTML = f"""<!DOCTYPE html>
<html>
<head>
<title>Baler API Docs</title>
<link rel="icon" href="{FAVICON_DATA_URI}">
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
<style>.topbar {{ background-color: #4B6043 !important; }}</style>
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
// Relative, not /openapi.yaml: behind the gateway this page is
// /<api>/docs and its spec is /<api>/openapi.yaml - the absolute path hit
// the site root and 404'd ("Failed to load API definition", #68). And the
// specs' servers name localhost:8080, so "Try it out" also gets this page's
// own base (e.g. https://host/derivation) as the first server.
window.onload = () => {{
  const base = location.origin + location.pathname.replace(new RegExp("/docs/?$"), "");
  const ui = SwaggerUIBundle({{
    url: "openapi.yaml",
    dom_id: "#swagger-ui",
    onComplete: () => {{
      const spec = ui.specSelectors.specJson().toJS();
      const rest = (spec.servers || []).filter((s) => s.url !== base);
      ui.specActions.updateJsonSpec({{...spec, servers: [{{url: base, description: "this server"}}, ...rest]}});
    }},
  }});
}};
</script>
</body>
</html>
""".encode()


def serve(handler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html")
    handler.send_header("Content-Length", str(len(SWAGGER_UI_HTML)))
    handler.end_headers()
    handler.wfile.write(SWAGGER_UI_HTML)
