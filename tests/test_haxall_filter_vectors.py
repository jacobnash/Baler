"""Baler's filter compiler against haxall's own FilterTest cases.

The record sets and expected matches come from openhay.vectors (generated
by running haxall's FilterTest through the reference runtime). Records are
stored exactly the way a Haystack import stores them
(haystack_import._row -> ingest_haystack_equip_tags), then matched by
hisquery's filter -> SPARQL compiler: the same path /api read and
/ingest/his use. So a failure here is a real difference between what a
Haystack client asks Baler and what Haxall would answer.

Differences we know about are in KNOWN_GAPS as strict xfails: fixing one
forces removing it from the list.
"""

from __future__ import annotations

import pytest

oh = pytest.importorskip("openhay")
vectors = pytest.importorskip("openhay.vectors")

from baler import hisquery
from baler.haystack_import import _row
from baler.ingest import haystack_ref_to_equip_uri, ingest_haystack_equip_tags
from baler.store import Store

V = vectors.load("haxall/testHaystack-4.0.6")

# case id -> why Baler differs from the reference today
KNOWN_GAPS: dict[str, str] = {
    'FilterTest.testInclude:date==2016-01-02': 'no date literals in the grammar',
    'FilterTest.testInclude:date != 2016-01-02': 'no date literals in the grammar',
    'FilterTest.testInclude:date < 2016-01-04': 'no date literals in the grammar',
    'FilterTest.testInclude:foo < "c"': 'comparisons across kinds (Str vs Number) are not false',
    'FilterTest.testInclude:date <= 2016-01-02': 'no date literals in the grammar',
    'FilterTest.testInclude:foo <= "baz"': 'comparisons across kinds (Str vs Number) are not false',
    'FilterTest.testInclude:date > 2016-01-02': 'no date literals in the grammar',
    'FilterTest.testInclude:foo > 12': 'comparisons across kinds (Str vs Number) are not false',
    'FilterTest.testInclude:date >= 2016-01-02': 'no date literals in the grammar',
    'FilterTest.testInclude:foo >= 12': 'comparisons across kinds (Str vs Number) are not false',
    'FilterTest.testInclude:ref->thru': 'nested Dict values are stored as a JSON string, so paths into them fail',
    'FilterTest.testInclude:ref->thru=="e"': 'nested Dict values are stored as a JSON string, so paths into them fail',
    'FilterTest.testInclude:^foo-bar': 'no ^symbol (def conjunct) terms in the grammar',
    'FilterTest.testInclude:^foo': 'no ^symbol (def conjunct) terms in the grammar',
    'FilterTest.testInclude:^foo-thru': 'no ^symbol (def conjunct) terms in the grammar',
    'FilterTest.testIncludeNumbers:num==75': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num==75°F': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num==75°C': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num > 70': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num > 70°F': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num >= 70°F': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num >= 75°F': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num <  80°F': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num <= 80°F': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num <= 75°F': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num > 70°C': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num > 1min': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:num > 60sec': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeNumbers:dis >  75': 'units are ignored in comparisons (the unit is stored apart from the number); mixed kinds compare',
    'FilterTest.testIncludeList:ref==@a': 'Ref lists are stored as a JSON string by the Haystack import, so list membership and paths through lists fail',
    'FilterTest.testIncludeList:ref->dis=="a"': 'Ref lists are stored as a JSON string by the Haystack import, so list membership and paths through lists fail',
    'FilterTest.testIncludeList:refx==@c': 'Ref lists are stored as a JSON string by the Haystack import, so list membership and paths through lists fail',
    'FilterTest.testIncludeList:refx->dis=="c"': 'Ref lists are stored as a JSON string by the Haystack import, so list membership and paths through lists fail',
    'FilterTest.testIncludeList:refx->ref': 'Ref lists are stored as a JSON string by the Haystack import, so list membership and paths through lists fail',
    'FilterTest.testIncludeList:refx->ref==@a': 'Ref lists are stored as a JSON string by the Haystack import, so list membership and paths through lists fail',
    'FilterTest.testIncludeList:refx->air': 'Ref lists are stored as a JSON string by the Haystack import, so list membership and paths through lists fail',
    'FilterTest.testIncludeList:refx->water': 'Ref lists are stored as a JSON string by the Haystack import, so list membership and paths through lists fail',
    'FilterTest.testIncludeList:not refx->air': 'Ref lists are stored as a JSON string by the Haystack import, so list membership and paths through lists fail',
    'FilterTest.testIncludeDateTime:ts==2020-09-02': 'no date literals in the grammar',
    'FilterTest.testIncludeDateTime:ts->bad == 2020-09-02': 'no date literals in the grammar',
    'FilterTest.testIncludeDateTime:ts->date==2020-09-02': 'no date literals in the grammar',
    'FilterTest.testIncludeDateTime:ts->date <= 2020-09-02': 'no date literals in the grammar',
    'FilterTest.testIncludeDateTime:ts->date >= 2020-09-02': 'no date literals in the grammar',
    'FilterTest.testIncludeDateTime:ts->date->foo >= 2020-09-02': 'no date literals in the grammar',
    'FilterTest.testIncludeDateTime:ts->time == 02:00': 'no time literals in the grammar',
    'FilterTest.testIncludeDateTime:ts->time <= 03:00': 'no time literals in the grammar',
    'FilterTest.testIncludeDateTime:ts->time > 03:00': 'no time literals in the grammar',
    'FilterTest.testIncludeDateTime:ts->tz == "New_York"': 'no DateTime virtual paths (ts->tz)',
}


def _store_for(records):
    store = Store()
    by_uri = {}
    for hay in records:
        rec = oh.from_hayson(hay)
        uri = ingest_haystack_equip_tags(store, rec["id"].id, _row(rec))
        by_uri[str(uri)] = rec["dis"]
    return store, by_uri


def _cases():
    for s in V["filter_eval"]:
        for c in s["cases"]:
            key = f"{s['src']}:{c['filter']}"
            yield pytest.param(s["records"], c, key, id=key)


_STORES: dict[int, tuple] = {}


@pytest.mark.parametrize("records, case, key", list(_cases()))
def test_filter_matches_reference(records, case, key, request):
    why = KNOWN_GAPS.get(key)  # not callspec.id: pytest escapes non-ASCII (°) there
    if why:
        request.applymarker(pytest.mark.xfail(reason=why, strict=True))
    key = id(records)
    if key not in _STORES:
        _STORES[key] = _store_for(records)
    store, by_uri = _STORES[key]
    q = hisquery.parse_query(case["filter"])
    got = sorted(by_uri[u] for u in hisquery.match_entities(store, q.filter) if u in by_uri)
    assert got == sorted(case["expected"])


def test_known_gaps_name_real_cases():
    ids = {f"{s['src']}:{c['filter']}" for s in V["filter_eval"] for c in s["cases"]}
    assert set(KNOWN_GAPS) <= ids, set(KNOWN_GAPS) - ids


def test_records_land_as_haystack_equip():
    _store, by_uri = _store_for(V["filter_eval"][0]["records"])
    assert str(haystack_ref_to_equip_uri("a")) in by_uri
