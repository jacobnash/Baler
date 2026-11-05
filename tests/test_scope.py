"""scope.py: which points/equipment a caller may read, by site topic prefix."""

import jwt as pyjwt
import pytest

from baler import scope
from baler.scope import Scope, claims_from_header, scope_for


class Conn:
    def __init__(self, sites):
        self.sites = sites

    def execute(self, sql, params=None):
        sites = self.sites

        class _Cur:
            def fetchall(self):
                return sites

        return _Cur()


@pytest.mark.parametrize("uri, ok", [
    ("urn:point:fbf/hospital-a/ahu-1/dat", True),
    ("urn:equip:fbf/hospital-a/ahu-1", True),
    ("urn:equip:fbf/hospital-a", True),                 # the prefix itself
    ("urn:point:fbf/hospital-ab/ahu-1/dat", False),     # sibling, not a child
    ("urn:point:fbf/hospital-b/x", False),
    ("urn:baler:td#RawPoint", False),                    # not a point/equip
])
def test_prefix_matching(uri, ok):
    assert Scope(("fbf/hospital-a",)).allows(uri) is ok


def test_unrestricted_and_nothing():
    assert Scope.everything().allows("urn:baler:anything")
    assert not Scope.nothing().allows("urn:point:fbf/a/x")
    assert Scope(("a",)).filter(["urn:point:a/1", "urn:point:b/1"]) == ["urn:point:a/1"]


SITES = [
    ("s-a", "org-1", ["fbf/hospital-a/"]),
    ("s-b", "org-1", ["fbf/hospital-b"]),
    ("s-x", "org-2", ["fbf/other-co"]),
]


def test_no_prefixes_anywhere_means_unrestricted():
    assert scope_for(Conn([("s", "o", [])]), None).unrestricted


def test_once_scoping_is_on_no_token_sees_nothing():
    assert scope_for(Conn(SITES), None).prefixes == ()


def test_site_scoped_token_sees_only_its_sites():
    s = scope_for(Conn(SITES), {"org_id": "org-1", "site_ids": ["s-a"]})
    assert s.prefixes == ("fbf/hospital-a",)


def test_org_wide_token_sees_all_its_orgs_sites_and_no_one_elses():
    s = scope_for(Conn(SITES), {"org_id": "org-1", "site_ids": []})
    assert s.prefixes == ("fbf/hospital-a", "fbf/hospital-b")
    assert not s.allows("urn:point:fbf/other-co/x")


def test_site_ids_from_another_org_grant_nothing():
    s = scope_for(Conn(SITES), {"org_id": "org-1", "site_ids": ["s-x"]})
    assert s.prefixes == ()


def test_claims_from_both_bearer_forms(monkeypatch):
    monkeypatch.setenv("BALER_JWT_SECRET", "k" * 32)
    tok = pyjwt.encode({"org_id": "o", "site_ids": ["s"]}, "k" * 32, algorithm="HS256")
    assert claims_from_header(f"Bearer {tok}")["org_id"] == "o"
    assert claims_from_header(f"BEARER authToken={tok}")["site_ids"] == ["s"]
    assert claims_from_header(f"Bearer {tok}x") is None
    assert claims_from_header("Basic abc") is None


def test_normalize_prefix_rejects_empty():
    assert scope.normalize_prefix(" fbf/a/ ") == "fbf/a"
    with pytest.raises(ValueError):
        scope.normalize_prefix("/")
