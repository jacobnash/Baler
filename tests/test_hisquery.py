"""
hisquery.py's three pure layers - parser, filter->SPARQL compiler, span/
interval resolution - plus the compiled SPARQL actually run against an
in-memory rdflib Store (same graph shape ingest.py writes), so "compiles"
means "selects the right entities", not just "is a string". No live
services; the GET /his round trip is tests/test_his_route.py.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from rdflib import OWL, RDF, RDFS, Literal, Namespace, URIRef

from baler import hisquery as h
from baler.store import BRICK, HAYSTACK, TD, Store

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 9, 8, 2, 30, tzinfo=UTC)  # a Tuesday


# --- parser ---------------------------------------------------------------


def test_full_chain_parses_into_mode_filter_span_rollup():
    q = h.parse_query("readAll(air and ahu).hisRead(thisMonth).hisRollup(avg, 1hr)")
    assert q.mode == "readAll"
    assert q.filter == h.And(h.Has("air"), h.Has("ahu"))
    assert q.filter_text == "air and ahu"
    assert q.span_text == "thisMonth"
    assert q.rollup == h.Rollup("avg", "1hr")


def test_read_singular_mode():
    assert h.parse_query("read(ahu).hisRead(today)").mode == "read"


def test_bare_filter_is_readall_with_no_span():
    q = h.parse_query("air and temp and sensor")
    assert q.mode == "readAll"
    assert q.span_text is None
    assert q.filter == h.And(h.And(h.Has("air"), h.Has("temp")), h.Has("sensor"))


def test_bare_filter_can_still_chain_hisread():
    q = h.parse_query("ahu.hisRead(yesterday)")
    assert q.filter == h.Has("ahu") and q.span_text == "yesterday"


def test_span_function_call_form_is_kept_verbatim():
    assert h.parse_query("readAll(ahu).hisRead(today())").span_text == "today()"


def test_date_month_year_and_range_spans_parse():
    for span in ["2026-09-03", "2026-09", "2026", "2026-09-01..2026-09-07", "2026-01..2026-03"]:
        assert h.parse_query(f"readAll(ahu).hisRead({span})").span_text == span


@pytest.mark.parametrize(
    "text, expected",
    [
        ("not vav", h.Not(h.Has("vav"))),
        ("a or b and c", h.Or(h.Has("a"), h.And(h.Has("b"), h.Has("c")))),
        ("(a or b) and c", h.And(h.Or(h.Has("a"), h.Has("b")), h.Has("c"))),
        ("not (a or b)", h.Not(h.Or(h.Has("a"), h.Has("b")))),
        ('dis == "Zone Temp"', h.Cmp("dis", "==", "Zone Temp")),
        ('dis != "x\\"y"', h.Cmp("dis", "!=", 'x"y')),
        ("temp > 72", h.Cmp("temp", ">", h.Num(72.0))),
        ("temp >= 72.5°F", h.Cmp("temp", ">=", h.Num(72.5, "°F"))),
        ("area < -5", h.Cmp("area", "<", h.Num(-5.0))),
        ("equipRef == @AHU-1", h.Cmp("equipRef", "==", h.Ref("AHU-1"))),
        ("id == @urn:point:fbf/mock-ahu-1/analogValue,1", h.Cmp("id", "==", h.Ref("urn:point:fbf/mock-ahu-1/analogValue,1"))),
        ("cur == true", h.Cmp("cur", "==", True)),
        ("writable == false", h.Cmp("writable", "==", False)),
    ],
)
def test_filter_grammar(text, expected):
    assert h.parse_query(text).filter == expected


def test_and_binds_tighter_than_or_and_not_tighter_than_and():
    assert h.parse_query("not a and b or c").filter == h.Or(h.And(h.Not(h.Has("a")), h.Has("b")), h.Has("c"))


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("", "empty query"),
        ("   ", "empty query"),
        ("ahu and", "expected a tag name"),
        ("ahu or (vav", "expected ')'"),
        ("ahu)", "unexpected ')'"),
        ("equipRef-> == 1", "tag name after '->'"),
        ("ph::NoSuchSpec", "unknown Xeto spec"),
        ("temp and equipRef->(vav)", "tag name after '->'"),
        ("readAll(ahu).foo()", "unsupported function 'foo'"),
        ("readAll(ahu).hisRead()", "needs a span"),
        ("readAll(ahu).hisRead(nope)", "unknown span 'nope'"),
        ("readAll(ahu).hisRead(today).hisRead(today)", "only appear once"),
        ("readAll(ahu).hisRead(2026-09-07..2026-09-01)", "ends before it starts"),
        ("readAll(ahu).hisRollup(median, 1hr)", "unknown rollup fold 'median'"),
        ("readAll(ahu).hisRollup(avg, 1fortnight)", "unknown interval unit 'fortnight'"),
        ("readAll(ahu).hisRollup(avg, 0hr)", "must be positive"),
        ("readAll(ahu).hisRollup(avg)", "expected ','"),
        ("readAll(ahu).hisRollup(avg, 1hr).hisRollup(max, 1day)", "only appear once"),
        ("dis == null", "comparing to null"),
        ("dis ==", "expected a value"),
        ("id == 5", "id must be compared to a @ref"),
        ("id < @x", "id only supports"),
        ("equipRef > @x", "only applies to numbers and strings"),
        ("temp > true", "only applies to numbers and strings"),
        ("a $ b", "unexpected character '$'"),
    ],
)
def test_parse_errors_name_the_problem(text, fragment):
    with pytest.raises(h.HisQueryError) as exc:
        h.parse_query(text)
    assert fragment in str(exc.value)


# --- spans -----------------------------------------------------------------


def _span(text, tz="UTC"):
    s = h.resolve_span(text, NOW, tz)
    return s.start.isoformat(), s.end.isoformat()


def test_default_span_is_today():
    assert h.resolve_span(None, NOW).label == "today"


@pytest.mark.parametrize(
    "text, start, end",
    [
        ("today", "2026-09-08T00:00:00+00:00", "2026-09-09T00:00:00+00:00"),
        ("today()", "2026-09-08T00:00:00+00:00", "2026-09-09T00:00:00+00:00"),
        ("yesterday", "2026-09-07T00:00:00+00:00", "2026-09-08T00:00:00+00:00"),
        ("thisWeek", "2026-09-07T00:00:00+00:00", "2026-09-14T00:00:00+00:00"),  # Monday start
        ("lastWeek", "2026-08-31T00:00:00+00:00", "2026-09-07T00:00:00+00:00"),
        ("pastWeek", "2026-09-01T00:00:00+00:00", "2026-09-09T00:00:00+00:00"),
        ("thisMonth", "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
        ("lastMonth", "2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
        ("pastMonth", "2026-08-09T00:00:00+00:00", "2026-09-09T00:00:00+00:00"),
        ("thisQuarter", "2026-07-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
        ("lastQuarter", "2026-04-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
        ("thisYear", "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
        ("lastYear", "2025-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        ("pastYear", "2025-09-08T00:00:00+00:00", "2026-09-09T00:00:00+00:00"),
        ("2026-09-03", "2026-09-03T00:00:00+00:00", "2026-09-04T00:00:00+00:00"),
        ("2026-09", "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
        ("2024-02", "2024-02-01T00:00:00+00:00", "2024-03-01T00:00:00+00:00"),  # leap year
        ("2026-12", "2026-12-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),  # year rollover
        ("2026", "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
        ("2026-09-01..2026-09-07", "2026-09-01T00:00:00+00:00", "2026-09-08T00:00:00+00:00"),
        ("2026-02..2026-03", "2026-02-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00"),
    ],
)
def test_spans_are_half_open_midnight_aligned(text, start, end):
    assert _span(text) == (start, end)


def test_span_resolves_today_in_the_requested_zone():
    # 02:30 UTC on the 8th is still the evening of the 7th in New York.
    start, end = _span("today", "America/New_York")
    assert start == "2026-09-07T00:00:00-04:00" and end == "2026-09-08T00:00:00-04:00"


def test_lastmonth_across_a_year_boundary():
    s = h.resolve_span("lastMonth", datetime(2026, 1, 15, tzinfo=UTC))
    assert (s.start.month, s.start.year, s.end.month, s.end.year) == (12, 2025, 1, 2026)


def test_unknown_timezone_is_a_query_error():
    with pytest.raises(h.HisQueryError, match="unknown timezone"):
        h.resolve_span("today", NOW, "Mars/Olympus")


# --- intervals ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text, seconds, months",
    [("15min", 900, None), ("1hr", 3600, None), ("2h", 7200, None), ("1day", 86400, None), ("1wk", 604800, None), ("30sec", 30, None), ("1mo", None, 1), ("3mo", None, 3), ("1yr", None, 12)],
)
def test_parse_interval(text, seconds, months):
    iv = h.parse_interval(text)
    assert (iv.seconds, iv.months) == (seconds, months)


@pytest.mark.parametrize("text", ["2mo", "6mo", "2yr", "13mo"])
def test_unsupported_calendar_interval_is_a_parse_error(text):
    # A query error, not a read_rollup error: read_rollup never runs when the
    # filter matched nothing, so validating only there would return 200.
    with pytest.raises(h.HisQueryError, match="calendar rollups support"):
        h.parse_interval(text)
    with pytest.raises(h.HisQueryError, match="calendar rollups support"):
        h.parse_query(f"readAll(nothing).hisRead(today).hisRollup(avg, {text})")


# --- compiler, run against a real (in-memory) graph -------------------------

AHU = URIRef("urn:equip:fbf/ahu-1")
VAV = URIRef("urn:equip:fbf/vav-1")
HX_VAV = URIRef("urn:equip:haystack:VAV-9")
DAT = URIRef("urn:point:fbf/ahu-1/dat")
FAN = URIRef("urn:point:fbf/ahu-1/fan")
ZT = URIRef("urn:point:fbf/vav-1/zt")
HX_ZT = URIRef("urn:point:hx/VAV-9/zt")
B_RTU = URIRef("urn:equip:brick/rtu-7")
B_ZT = URIRef("urn:point:brick/rtu-7/zt")
B_FAN = URIRef("urn:point:brick/rtu-7/fan")
PROJ = Namespace("urn:baler:proj#")
TAG = Namespace("https://brickschema.org/schema/BrickTag#")
# A Brick extension in its own namespace, written the way Brick's extension
# guidance says to (docs.brickschema.org/extra/extending.html) - the reason
# to pick Brick in the first place is that this is allowed.
ACME = Namespace("https://acme.example/brick-ext#")
REC = Namespace("https://w3id.org/rec#")
HRAHU = URIRef("urn:equip:acme/hrahu-1")  # acme:Heat_Recovery_AHU, one rdfs:subClassOf below brick:Air_Handling_Unit
LAB_HRAHU = URIRef("urn:equip:acme/hrahu-2")  # acme:Lab_Heat_Recovery_AHU, two levels below
HRU = URIRef("urn:equip:acme/hrahu-3")  # acme:HRU, an owl:equivalentClass alias the extension declares
WHEEL = URIRef("urn:point:acme/hrahu-1/wheel")  # acme:Wheel_Speed_Sensor, with its own tag word
BYPASS = URIRef("urn:point:acme/hrahu-1/bypass")  # a class declared only by rdfs:subClassOf, no owl:Class


@pytest.fixture
def graph():
    s = Store()
    s.add_many([
        (AHU, HAYSTACK.hasTag, Literal("ahu")), (AHU, RDF.type, BRICK.Air_Handling_Unit),
        (AHU, BRICK.hasPoint, DAT), (AHU, BRICK.hasPoint, FAN),
        (VAV, HAYSTACK.hasTag, Literal("vav")), (VAV, BRICK.hasPoint, ZT), (VAV, HAYSTACK.dis, Literal("VAV 1")),
        (DAT, RDF.type, TD.RawPoint), (DAT, TD.sourceTopic, Literal("fbf/ahu-1/dat")), (DAT, BRICK.isPointOf, AHU),
        (DAT, HAYSTACK.hasTag, Literal("temp")), (DAT, HAYSTACK.hasTag, Literal("air")), (DAT, HAYSTACK.hasTag, Literal("discharge")),
        (DAT, HAYSTACK.unit, Literal("°F")), (DAT, RDF.type, BRICK.Discharge_Air_Temperature_Sensor),
        (FAN, RDF.type, TD.RawPoint), (FAN, BRICK.isPointOf, AHU), (FAN, HAYSTACK.hasTag, Literal("fan")),
        (FAN, HAYSTACK.kind, Literal("Bool")), (FAN, HAYSTACK.dis, Literal("Fan Status")),
        (ZT, RDF.type, TD.RawPoint), (ZT, BRICK.isPointOf, VAV), (ZT, HAYSTACK.hasTag, Literal("temp")),
        (ZT, HAYSTACK.hasTag, Literal("zone")), (ZT, HAYSTACK.temp, Literal(72.5)),
        # A Haystack-sourced point that only knows its equip via the equipRef
        # tag (no brick:isPointOf yet - link_equip_ref hasn't run).
        (HX_ZT, HAYSTACK.hasTag, Literal("point")), (HX_ZT, HAYSTACK.hasTag, Literal("zone")), (HX_ZT, HAYSTACK.equipRef, Literal("VAV-9")),
        (HX_VAV, HAYSTACK.hasTag, Literal("vav")), (HX_VAV, HAYSTACK.hasTag, Literal("equip")),
        # --- Brick-only: typed, never tagged (a loaded Brick model, or a
        # derivation_engine point) - the case "Brick is hard to query" is about.
        (B_RTU, RDF.type, BRICK.RTU),  # Brick's alias for Rooftop_Unit, itself under Air_Handling_Unit
        (B_RTU, BRICK.hasPoint, B_ZT), (B_RTU, BRICK.hasPoint, B_FAN),
        (B_ZT, RDF.type, BRICK.Zone_Air_Temperature_Sensor), (B_ZT, BRICK.isPointOf, B_RTU),
        # mapping.py's PROJ fallback: a project class one rdfs:subClassOf below a Brick class.
        (B_FAN, RDF.type, PROJ.Fan_Status_Variant), (B_FAN, BRICK.isPointOf, B_RTU),
        (PROJ.Fan_Status_Variant, RDFS.subClassOf, BRICK.Fan_Status),
        # Ontology-level subjects, as if Brick itself were loaded into the store:
        # schema, not entities - must never come back from a filter.
        (BRICK.Zone_Air_Temperature_Sensor, RDF.type, OWL.Class),
        (BRICK.Zone_Air_Temperature_Sensor, RDFS.subClassOf, BRICK.Air_Temperature_Sensor),
        (TAG.Temperature, RDF.type, BRICK.Tag),
        # ... including the ontology *individuals* Brick carries, which are typed
        # as brick:Quantity, not owl:Class, and the REC classes Brick 1.4 embeds.
        (BRICK.Active_Energy, RDF.type, BRICK.Quantity),
        (REC.Building, RDF.type, OWL.Class), (REC.Building, RDFS.subClassOf, REC.Architecture),
        # --- a Brick extension ontology (see ACME above) plus entities typed with it ---
        (ACME.Heat_Recovery_AHU, RDF.type, OWL.Class), (ACME.Heat_Recovery_AHU, RDFS.subClassOf, BRICK.Air_Handling_Unit),
        (ACME.Heat_Recovery_AHU, BRICK.hasAssociatedTag, TAG.Heat), (ACME.Heat_Recovery_AHU, BRICK.hasAssociatedTag, TAG.Recovery),
        (ACME.Lab_Heat_Recovery_AHU, RDF.type, OWL.Class), (ACME.Lab_Heat_Recovery_AHU, RDFS.subClassOf, ACME.Heat_Recovery_AHU),
        (ACME.HRU, RDF.type, OWL.Class), (ACME.HRU, OWL.equivalentClass, ACME.Heat_Recovery_AHU),
        (ACME.Rotor, RDF.type, BRICK.Tag),
        (ACME.Wheel_Speed_Sensor, RDF.type, OWL.Class), (ACME.Wheel_Speed_Sensor, RDFS.subClassOf, BRICK.Speed_Sensor),
        (ACME.Wheel_Speed_Sensor, BRICK.hasAssociatedTag, ACME.Rotor), (ACME.Wheel_Speed_Sensor, BRICK.hasAssociatedTag, TAG.Speed),
        (ACME.Bypass_Damper_Position_Sensor, RDFS.subClassOf, BRICK.Damper_Position_Sensor),
        (HRAHU, RDF.type, ACME.Heat_Recovery_AHU), (HRAHU, HAYSTACK.dis, Literal("HR-AHU 1")),
        (HRAHU, BRICK.hasPoint, WHEEL), (HRAHU, BRICK.hasPoint, BYPASS),
        (WHEEL, RDF.type, ACME.Wheel_Speed_Sensor), (WHEEL, BRICK.isPointOf, HRAHU),
        (BYPASS, RDF.type, ACME.Bypass_Damper_Position_Sensor), (BYPASS, BRICK.isPointOf, HRAHU),
        (LAB_HRAHU, RDF.type, ACME.Lab_Heat_Recovery_AHU),
        (HRU, RDF.type, ACME.HRU),
    ])
    return s


ALL_EQUIP = [AHU, VAV, HX_VAV, B_RTU, HRAHU, LAB_HRAHU, HRU]
ALL_AHUS = [AHU, B_RTU, HRAHU, LAB_HRAHU, HRU]
ALL_POINTS = [DAT, FAN, ZT, HX_ZT, B_ZT, B_FAN, WHEEL, BYPASS]


def _match(graph, text):
    return h.match_entities(graph, h.parse_query(text).filter)


@pytest.mark.parametrize(
    "text, expected",
    [
        # --- Haystack markers and value tags, as written by ingest ---
        ("temp and not (air or zone)", []),
        ("discharge or fan", [DAT, FAN, B_FAN]),  # B_FAN: Fan_Status carries Brick tag:Fan
        ("temp > 70", [ZT]),
        ("temp > 80", []),
        # Haystack != (haxall FilterNe): the tag is present and isn't that value.
        # DAT's temp is a marker, B_ZT has temp via its Brick class; ZT's is 72.5.
        ("temp != 72.5", [DAT, B_ZT]),
        ('dis == "Fan Status"', [FAN]),
        ('kind == "Bool"', [FAN]),
        ("id == @urn:equip:fbf/vav-1", [VAV]),
        ("equipRef == @urn:equip:fbf/ahu-1", [DAT, FAN]),  # full URI -> brick:isPointOf
        ("equipRef == @VAV-9", [HX_ZT]),  # bare Haystack id -> equipRef literal
        # --- Xeto spec names (Brick + Xeto interop): the spec's markers, each
        # matched the way a bare tag is - so Brick-only data fits too ---
        ("ph::Ahu", ALL_AHUS),
        ("Ahu", ALL_AHUS),  # bare: the Brick class or the ph spec
        ("ph.points::ZoneAirTempSensor", [B_ZT]),
        ("ph::Vav", [VAV, HX_VAV]),
        ("ph::Ahu and not ph::Vav", ALL_AHUS),
        # --- tag paths: follow the ref, test the far end ---
        ("equipRef->vav", [ZT, HX_ZT]),  # ZT via brick:isPointOf; HX_ZT via its raw equipRef literal
        ("equipRef->ahu", [DAT, FAN, B_ZT, B_FAN, WHEEL, BYPASS]),  # Brick-typed equips count, as for `ahu`
        ('equipRef->dis == "VAV 1"', [ZT]),
        ("equipRef->id == @urn:equip:fbf/ahu-1", [DAT, FAN]),
        ("point and not equipRef->ahu", [ZT, HX_ZT]),  # Haystack: a missing path is false, so `not` is true
        ("equipRef->siteRef->dis", []),  # no site links in this graph
        # --- the same words also reach Brick-only entities (B_*: typed, never tagged) ---
        ("ahu", ALL_AHUS),  # marker on AHU; Brick tag:AHU on RTU (via Rooftop_Unit <- Air_Handling_Unit) and on the extension's AHUs
        ("temp and air", [DAT, B_ZT]),  # `temp` is Haystack for Brick's tag:Temperature
        ("temp and not air", [ZT]),
        ("zone and not temp", [HX_ZT]),
        ("zone and air and temp and sensor", [B_ZT]),  # every word from the class name Zone_Air_Temperature_Sensor
        ("temperature and sensor", [DAT, B_ZT]),  # Brick's own spelling; DAT is typed Discharge_Air_Temperature_Sensor
        ("fan and run", [B_FAN]),  # no Brick tag for `run`: rules/haystack_to_brick.yaml [fan, run, sensor] -> Fan_Status, inverted
        ("point", ALL_POINTS),
        ("equip", ALL_EQUIP),
        ("point and id != @urn:point:fbf/ahu-1/dat", [FAN, ZT, HX_ZT, B_ZT, B_FAN, WHEEL, BYPASS]),
        ("point and equipRef != @urn:equip:fbf/ahu-1", [ZT, HX_ZT, B_ZT, B_FAN, WHEEL, BYPASS]),
        # --- Brick class names: hierarchy, aliases, case ---
        ("Air_Handling_Unit", ALL_AHUS),  # B_RTU is typed brick:RTU, an alias of a *subclass*; HRAHU/LAB_HRAHU/HRU via the extension
        ("AHU", ALL_AHUS),  # Brick's alias name for the class
        ("air_handling_unit", ALL_AHUS),  # case-insensitive
        ("Rooftop_Unit", [B_RTU]),
        ("Temperature_Sensor", [DAT, B_ZT]),  # subclasses
        ("Zone_Air_Temperature_Sensor", [B_ZT]),
        ("Fan_Status", [B_FAN]),  # via the PROJ subclass the graph declares
        ("Fan_Status_Variant", [B_FAN]),  # the PROJ class by its own name
        ("Point", ALL_POINTS),  # the Brick class or the ph::Point spec: Brick-typed and Haystack-tagged points
        ("Equipment", ALL_AHUS),
        ("Sensor and not Temperature_Sensor", [WHEEL, BYPASS]),
        ("Not_A_Brick_Class", []),
        # --- a Brick extension in its own namespace: reachable every way Brick itself is ---
        ("Heat_Recovery_AHU", [HRAHU, LAB_HRAHU, HRU]),  # by its own name: subtree + alias
        ("heat_recovery_ahu", [HRAHU, LAB_HRAHU, HRU]),  # case-insensitive, same as Brick names
        ("Lab_Heat_Recovery_AHU", [LAB_HRAHU]),  # two levels below Brick
        ("HRU", [HRAHU, LAB_HRAHU, HRU]),  # the extension's own owl:equivalentClass alias, either direction
        ("hru", [HRAHU, LAB_HRAHU, HRU]),  # as a plain word too - Brick's `ahu` works only because Brick ships a tag:AHU
        ("heat and recovery", [HRAHU, LAB_HRAHU, HRU]),  # the tag words the extension declares (brick:hasAssociatedTag), inherited downward
        ("Heat_Recovery_AHU and not Lab_Heat_Recovery_AHU", [HRAHU, HRU]),
        ("Speed_Sensor", [WHEEL]),  # under its Brick parent
        ("speed and sensor", [WHEEL]),  # Brick's tag words, declared on the extension class
        ("rotor", [WHEEL]),  # a tag word Brick doesn't have - the extension's own brick:Tag
        ("Damper_Position_Sensor", [BYPASS]),  # a class declared by rdfs:subClassOf alone still counts under its parent
        ("Bypass_Damper_Position_Sensor", [BYPASS]),  # ... and by name
        ('read(hru and dis == "HR-AHU 1")', [HRAHU]),
        # --- ontology-level subjects are never entities, whichever ontology they belong to ---
        ("Quantity", []),  # brick:Active_Energy is typed brick:Quantity - an individual, not an owl:Class
        ("Building", []),  # the REC class Brick 1.4 embeds
        ("not point", ALL_EQUIP),  # no owl:Class, brick:Tag, brick:Quantity, REC class, or extension class leaks through a negation
        ("not ahu and not point", [VAV, HX_VAV]),
    ],
)
def test_compiled_filter_selects_the_right_entities(graph, text, expected):
    assert _match(graph, text) == sorted(str(u) for u in expected)


def test_compiled_filters_agree_on_oxigraph(graph):
    """Unit tests run on rdflib; production runs Oxigraph, and the two
    differ at the edges (rdflib doesn't scope subquery variables; Oxigraph
    leaves BIND(IRI(..)) unbound on a relative IRI). Every filter case
    above must select the same subjects on both."""
    ox = pytest.importorskip("pyoxigraph")
    oxs = ox.Store()
    oxs.load(graph.graph.serialize(format="nt").encode(), ox.RdfFormat.N_TRIPLES)
    vocab = h.graph_vocab(graph)
    cases = test_compiled_filter_selects_the_right_entities.pytestmark[0].args[1]
    for text, _ in cases:
        q = h.compile_filter(h.parse_query(text).filter, vocab)
        on_oxigraph = sorted(r["s"].value for r in oxs.query(q))
        on_rdflib = sorted(str(r.s) for r in graph.query(q))
        assert on_oxigraph == on_rdflib, text


def test_ontology_subjects_are_never_entities(graph):
    # brick:Zone_Air_Temperature_Sensor, tag:Temperature, brick:Active_Energy,
    # rec:Building and the acme:* classes/tag are all in the graph (as a loaded
    # ontology would put them) - no filter may list any of them.
    vocab_prefixes = ("https://brickschema.org/", "https://w3id.org/rec#", "https://acme.example/", "http://www.w3.org/")
    for text in ("Class", "Tag", "Quantity", "Zone_Air_Temperature_Sensor", "temperature", "rotor", "not point", "not ahu"):
        assert not any(uri.startswith(vocab_prefixes) for uri in _match(graph, text)), text


def test_graph_vocab_reads_the_extension_and_skips_bricks_own_triples(graph):
    g = h.graph_vocab(graph)
    assert g.children[str(BRICK.Air_Handling_Unit)] == {str(ACME.Heat_Recovery_AHU)}
    assert g.children[str(ACME.Heat_Recovery_AHU)] == {str(ACME.Lab_Heat_Recovery_AHU)}
    assert g.equivalents[str(ACME.HRU)] == {str(ACME.Heat_Recovery_AHU)} and g.equivalents[str(ACME.Heat_Recovery_AHU)] == {str(ACME.HRU)}
    assert g.classes_named("HEAT_RECOVERY_ahu") == {str(ACME.Heat_Recovery_AHU)}
    assert g.classes_tagged("rotor") == {str(ACME.Wheel_Speed_Sensor)} and g.classes_tagged("Heat") == {str(ACME.Heat_Recovery_AHU)}
    assert g.expand({str(BRICK.Air_Handling_Unit)}) == {str(BRICK.Air_Handling_Unit), str(ACME.Heat_Recovery_AHU), str(ACME.Lab_Heat_Recovery_AHU), str(ACME.HRU)}
    # Brick's own subClassOf (Zone_Air_Temperature_Sensor -> Air_Temperature_Sensor) is
    # in the fixture too; brick_vocab owns that, so it must not be re-read here.
    assert str(BRICK.Air_Temperature_Sensor) not in g.children
    assert g.classes_named("Zone_Air_Temperature_Sensor") == set()
    # Everything the extension declares is schema, including a tag and a class
    # that only appears as the subject of an rdfs:subClassOf.
    for iri in (ACME.Heat_Recovery_AHU, ACME.HRU, ACME.Rotor, ACME.Bypass_Damper_Position_Sensor, REC.Building, BRICK.Active_Energy):
        assert g.is_schema(str(iri)), iri
    assert not g.is_schema(str(HRAHU))


def test_graph_vocab_is_one_query(graph):
    calls = []
    original = graph.query
    graph.query = lambda sparql: calls.append(sparql) or original(sparql)
    h.graph_vocab(graph)
    assert len(calls) == 1


def test_hints_name_near_miss_brick_vocabulary_only_when_nothing_matched(graph):
    hints = h.hints_for_empty_result(h.parse_query("Air_Handeling_Unit and temperture and hisqRunMarker").filter)
    assert {hint["token"]: hint["suggestions"][0] for hint in hints} == {"Air_Handeling_Unit": "Air_Handling_Unit", "temperture": "temperature"}
    # A valid class, tag word, Haystack rule marker, or structural alias is never "corrected".
    assert h.hints_for_empty_result(h.parse_query("Air_Handling_Unit and temp and run and point").filter) == []
    # Nor is an extension's class, alias, or tag word - even when Brick has a near-miss for it.
    g = h.graph_vocab(graph)
    assert h.hints_for_empty_result(h.parse_query("Wheel_Speed_Sensor and hru and rotor").filter, g) == []
    # ... which without the graph *would* be "corrected" (to Wind_Speed_Sensor / motor).
    assert {hint["token"] for hint in h.hints_for_empty_result(h.parse_query("Wheel_Speed_Sensor and hru and rotor").filter)} == {"Wheel_Speed_Sensor", "rotor"}


def test_compiled_sparql_never_walks_the_hierarchy_in_the_query(graph):
    # Oxigraph takes 1-50s on rdfs:subClassOf paths inside a filter with the
    # full ontology loaded, and ~0.4s on a per-candidate NOT EXISTS; every
    # hop is resolved in Python beforehand and schema subjects dropped after.
    sparql = h.compile_filter(h.parse_query("Temperature_Sensor and temperature").filter, h.graph_vocab(graph))
    assert "subClassOf" not in sparql and "EXISTS" not in sparql
    assert "Zone_Air_Temperature_Sensor" in sparql and "Fan_Status_Variant" not in sparql
    sparql = h.compile_filter(h.parse_query("AHU").filter, h.graph_vocab(graph))
    assert str(ACME.Lab_Heat_Recovery_AHU) in sparql and str(ACME.HRU) in sparql


def test_string_values_are_escaped_not_interpolated(graph):
    # A closing quote inside the value must not break out of the literal.
    assert _match(graph, 'dis == "x\\" } . ?s ?p ?o . FILTER(true) #"') == []


def test_expand_to_points_walks_equipment_and_keeps_direct_points(graph):
    assert h.expand_to_points(graph, [str(AHU), str(ZT)]) == sorted([str(DAT), str(FAN), str(ZT)])


def test_expand_to_points_follows_owl_sameas_merges(graph):
    from rdflib import OWL

    other = URIRef("urn:equip:haystack:AHU-1")
    graph.add_many([(AHU, OWL.sameAs, other), (other, OWL.sameAs, AHU), (other, BRICK.hasPoint, HX_ZT)])
    assert str(HX_ZT) in h.expand_to_points(graph, [str(AHU)])


def test_describe_points_metadata_shape(graph):
    meta = h.describe_points(graph, [str(DAT), str(FAN), "urn:point:nowhere"])
    dat = meta[str(DAT)]
    assert dat["dis"] == "dat"  # no dis tag -> last topic segment
    assert dat["point"] == "fbf/ahu-1/dat"
    assert dat["unit"] == "°F"
    assert dat["brickClass"] == "Discharge_Air_Temperature_Sensor"
    assert dat["tags"] == ["air", "discharge", "temp"]
    assert dat["equip"] == str(AHU) and dat["equipDis"] == "fbf/ahu-1"
    fan = meta[str(FAN)]
    assert fan["dis"] == "Fan Status" and fan["kind"] == "Bool" and fan["brickClass"] is None
    # An extension class is a class: reported by its local name, not dropped as null.
    assert h.describe_points(graph, [str(WHEEL)])[str(WHEEL)]["brickClass"] == "Wheel_Speed_Sensor"
    # Unknown point still gets a complete, null-filled record rather than a KeyError downstream.
    assert meta["urn:point:nowhere"]["dis"] == "nowhere" and meta["urn:point:nowhere"]["tags"] == []


def test_run_query_read_singular_with_no_match_is_nomatcherror(graph):
    class NoTs:  # never reached - the filter matches nothing first
        pass

    with pytest.raises(h.NoMatchError):
        h.run_query(graph, NoTs(), h.parse_query("read(nothing).hisRead(today)"), now=NOW)


def test_auto_interval_keeps_charts_under_budget():
    day = 86400
    # one point, a day: 2000/series budget -> 43s wanted -> 60s buckets
    assert h.auto_interval(day, 1, 2000) == 60
    # 614 points (nash-srv's readAll(point)) under 200k values: 325/series -> 5min
    assert h.auto_interval(day, 614, 200_000) == 300
    # an hour of 10 points fits raw (budget allows < 10s buckets)
    assert h.auto_interval(3600, 10, 200_000) is None
    # floor of 100/series however many points; a year tops out at 1 day
    assert h.auto_interval(day, 100_000, 200_000) == 900
    assert h.auto_interval(365 * day, 1000, 1000) == 86400
    assert h.auto_interval(day, 0, 1000) is None


def test_post_read_calls_parse_and_lambdas_get_a_clear_error():
    """Baler#18."""
    q = h.parse_query('ahu.hisRead(yesterday).hisClip(today).hisInterpolate().sortr("dis").first()')
    assert q.post == (("hisClip", "today"), ("hisInterpolate",), ("sortr", "dis"), ("first",))
    assert h.parse_query("readAll(equip).sort()").post == (("sort", "dis"),)
    with pytest.raises(h.HisQueryError, match="lambdas"):
        h.parse_query("readAll(point).map(p=>p->equipRef->dis)")
    with pytest.raises(h.HisQueryError, match="supported: hisRead"):
        h.parse_query("ahu.size()")


def test_his_interpolate_sort_first_and_clip():
    from datetime import timezone

    now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    series = [
        {"dis": "b", "tags": [], "history": [{"ts": 0, "value": 0.0}, {"ts": 10, "value": 10.0}]},
        {"dis": "A", "tags": [], "history": [{"ts": 5, "value": True}, {"ts": 20, "value": False}]},
    ]
    out = h._apply_post(series, (("hisInterpolate",), ("sort", "dis")), now, "UTC")
    assert [s["dis"] for s in out] == ["A", "b"]  # case-insensitive
    assert out[1]["history"] == [{"ts": 0, "value": 0.0}, {"ts": 5, "value": 5.0}, {"ts": 10, "value": 10.0}]  # linear
    assert [p["value"] for p in out[0]["history"]] == [True, True, False]  # previous value, own range only
    assert [s["dis"] for s in h._apply_post(series, (("sortr", "dis"), ("first",)), now, "UTC")] == ["b"]
    day = datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp()
    clipped = h._apply_post([{"dis": "x", "tags": [], "history": [{"ts": day - 60, "value": 1}, {"ts": day + 60, "value": 2}]}],
                            (("hisClip", "today"),), now, "UTC")
    assert [p["value"] for p in clipped[0]["history"]] == [2]
