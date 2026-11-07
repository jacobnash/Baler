"""The Swagger page behind the gateway (#68): /<api>/docs must load its spec
relative to itself, not from the site root."""

from baler import docs_ui


def test_spec_url_is_relative_and_this_server_is_offered():
    html = docs_ui.SWAGGER_UI_HTML.decode()
    assert 'url: "openapi.yaml"' in html
    assert '"/openapi.yaml"' not in html
    assert "this server" in html
