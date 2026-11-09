"""The building document store (#201), with no services: content
addressing, page text, search with citations, links, integrity."""

import io
from datetime import datetime, timezone

import pytest

from baler.lifecycle import documents, routes, rows


class Client:
    def __init__(self, org="org1", role="operator", allows=lambda _u: True, store=None):
        self.rows = store or rows.MemoryRows()
        self.org, self.role, self.allows = org, role, allows

    def __call__(self, method, path, body=None, raw=None, ctype="", **query):
        req = routes.Request(method=method, path=path, rows=self.rows, org_id=self.org, body=body, raw=raw,
                             content_type=ctype, query={k: str(v) for k, v in query.items()}, role=self.role,
                             who={"sub": "u1", "email": "eng@example.com"}, allows=self.allows,
                             now=datetime(2026, 11, 9, tzinfo=timezone.utc))
        return routes.dispatch(req)

    def ok(self, *a, **kw):
        status, out = self(*a, **kw)
        assert status in (200, 201), out
        return out


def _pdf(pages: list[str]) -> bytes:
    reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
    buf = io.BytesIO()
    c = reportlab.Canvas(buf)
    for text in pages:
        y = 800
        for line in text.split("\n"):
            c.drawString(72, y, line)
            y -= 14
        c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def c():
    return Client()


def test_upload_is_content_addressed_and_duplicates_are_free(c):
    data = b"AHU-1 O&M\fFilter: replace MERV 14, 24x24x2, every 90 days"
    d = c.ok("POST", "/documents", raw=data, ctype="text/plain", filename="ahu1-om.txt", kind="om_manual")
    assert d["sha256"] == documents.sha256(data) and d["page_count"] == 2 and not d["duplicate"]
    again = c("POST", "/documents", raw=data, ctype="text/plain", filename="copy.txt")
    assert again[0] == 200 and again[1]["duplicate"] and again[1]["id"] == d["id"]
    assert len(c.rows.find("lc_blob_data")) == 1
    # another org uploading the same file gets its own document, the same blob
    other = Client(org="org2", store=c.rows)
    d2 = other.ok("POST", "/documents", raw=data, ctype="text/plain", filename="x.txt")
    assert d2["id"] != d["id"] and d2["sha256"] == d["sha256"] and len(c.rows.find("lc_blobs")) == 1
    assert other("GET", f"/documents/{d['id']}")[0] == 404


def test_content_round_trips_and_verifies(c):
    data = b"\x89PNG not really"
    d = c.ok("POST", "/documents", raw=data, ctype="image/png", filename="nameplate.png", kind="photo")
    st, raw = c("GET", f"/documents/{d['id']}/content")
    assert st == 200 and raw.body == data and raw.content_type == "image/png" and raw.filename == "nameplate.png"
    assert c.ok("GET", f"/documents/{d['id']}/verify")["ok"] is True
    c.rows.update("lc_blob_data", d["sha256"], {"data": b"tampered"})
    assert c.ok("GET", f"/documents/{d['id']}/verify")["ok"] is False
    assert d["text_status"] == "unsupported"


def test_pdf_pages_are_searchable_with_citations(c):
    pdf = _pdf(["Supply fan motor\nBearing lubrication every 2000 hours", "Warranty: 5 years parts from start-up"])
    d = c.ok("POST", "/documents", raw=pdf, ctype="application/pdf", filename="sf-om.pdf", kind="om_manual",
             title="SF motor O&M")
    assert d["page_count"] == 2 and d["text_status"] == "extracted"
    hits = c.ok("GET", "/documents/search", q="warranty parts")
    assert [(h["document_id"], h["page"]) for h in hits] == [(d["id"], 2)]
    assert hits[0]["citation"] == "SF motor O&M, p. 2" and "Warranty" in hits[0]["snippet"]
    assert c.ok("GET", f"/documents/{d['id']}/pages/1")["text"].startswith("Supply fan motor")
    assert c.ok("GET", "/documents/search", q="warranty", kind="drawing") == []
    assert c("GET", "/documents/search")[0] == 400


def test_links_scope_and_filters(c):
    d = c.ok("POST", "/documents", raw=b"pump curve", ctype="text/plain", filename="p.txt", kind="submittal")
    pos = "urn:equip:fbf/hosp/chw-p1"
    link = c.ok("POST", f"/documents/{d['id']}/links", {"target_kind": "position", "target": pos, "page": 1})
    c.ok("POST", f"/documents/{d['id']}/links", {"target_kind": "phase", "target": "construction"})
    assert c("POST", f"/documents/{d['id']}/links", {"target_kind": "galaxy", "target": "x"})[0] == 400
    assert c("POST", f"/documents/{d['id']}/links", {"target_kind": "asset", "target": "a", "page": 0})[0] == 400
    assert [x["id"] for x in c.ok("GET", "/documents", target_kind="position", target=pos)] == [d["id"]]
    assert c.ok("GET", "/documents/search", q="curve", target=pos)[0]["document_id"] == d["id"]
    narrow = Client(allows=lambda u: "chw" not in u, store=c.rows)
    assert len(narrow.ok("GET", f"/documents/{d['id']}")["links"]) == 1  # the position link is hidden
    assert narrow("POST", f"/documents/{d['id']}/links", {"target_kind": "position", "target": pos})[0] == 404
    c.ok("DELETE", f"/documents/{d['id']}/links/{link['id']}")
    assert len(c.ok("GET", f"/documents/{d['id']}")["links"]) == 1


def test_edit_and_delete_are_audited(c):
    d = c.ok("POST", "/documents", raw=b"x", ctype="text/plain", filename="a.txt")
    c.ok("PATCH", f"/documents/{d['id']}", {"title": "Start-up guide", "kind": "startup_guide"})
    assert c("PATCH", f"/documents/{d['id']}", {"sha256": "0" * 64})[0] == 400
    assert c("DELETE", f"/documents/{d['id']}")[0] == 403  # admin only
    admin = Client(role="admin", store=c.rows)
    admin.ok("DELETE", f"/documents/{d['id']}")
    assert c("GET", f"/documents/{d['id']}")[0] == 404
    actions = [a["action"] for a in c.rows.audit_log(rows.entity_uri("document", d["id"]))]
    assert actions == ["document.delete", "document.edit", "document.add"]
    # re-uploading after a delete makes a new live document over the kept blob
    assert not c.ok("POST", "/documents", raw=b"x", ctype="text/plain", filename="a.txt")["duplicate"]


def test_upload_needs_a_file_and_a_known_kind(c):
    assert c("POST", "/documents", body={"x": 1})[0] == 415
    assert c("POST", "/documents", raw=b"x", ctype="text/plain", kind="poem")[0] == 400


def test_damaged_pdf_is_stored_but_not_searchable(c):
    pytest.importorskip("pdfplumber")
    d = c.ok("POST", "/documents", raw=b"%PDF-1.4 garbage", ctype="application/pdf", filename="bad.pdf")
    assert d["text_status"].startswith("failed") and d["page_count"] is None
