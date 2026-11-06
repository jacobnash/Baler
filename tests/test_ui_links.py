"""Absolute links in the UI must point at a real gateway route.

ui/derivations.html linked "Derivation API" to /api/derivation and his.html
linked /api/ingest; /api/ is the Haystack API, so both answered 403 "no
policy defined for this route". Every absolute href has to start with a
location the gateway actually serves, and an API link has to be its docs page."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
_CONF = (ROOT / "gateway" / "nginx.conf").read_text()
LOCATIONS = set(re.findall(r"location\s+(?:=\s*)?(/[a-z]*/?)", _CONF)) | set(
    re.findall(r"location\s+~\s+\^(/[a-z]+)", _CONF))  # regex locations, e.g. ~ ^/docs/?$
API_PREFIXES = {"/ingest/", "/fault/", "/windrow/", "/derivation/", "/validate/", "/ops/", "/cx/", "/auth/"}


def _absolute_links():
    for f in sorted((ROOT / "ui").glob("*.html")) + sorted((ROOT / "ui").glob("*.js")):
        for m in re.finditer(r'href="(/[^"#?]*)', f.read_text()):
            yield f.name, m.group(1)


def test_every_absolute_ui_link_hits_a_gateway_location():
    bad = [(f, h) for f, h in _absolute_links()
           if not any(h == loc.rstrip("/") or h.startswith(loc) for loc in LOCATIONS if loc != "/")]
    assert not bad, f"links to paths the gateway doesn't serve: {bad}"


def test_api_links_go_to_docs_not_bare_paths():
    bad = [(f, h) for f, h in _absolute_links()
           if any(h.startswith(p) for p in API_PREFIXES) and not h.endswith(("/docs", "/openapi.yaml"))]
    assert "/docs" in LOCATIONS, "the site docs redirect (gateway/nginx.conf) is gone"
    assert not bad, f"API links should open the API's docs page: {bad}"
    assert not [(f, h) for f, h in _absolute_links() if h.startswith("/api/")], "/api/ is the Haystack API"
