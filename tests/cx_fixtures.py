"""
Shared commissioning test building (Cx 1, #37). Two orgs' sites side by
side so every test can check scoping, the way real installs look after
#16:

  site A (prefix fbf/a):
    AHU-1  brick:AHU (alias of Air_Handling_Unit)
      SAT  Discharge_Air_Temperature_Sensor (equivalent of Supply_...)
      SF-S Fan_Status
      RAT  Return_Air_Temperature_Sensor
      X    no Brick class, no markers (unclassified)
    VAV-1  Variable_Air_Volume_Box
      ZNT  Zone_Air_Temperature_Sensor
      DMP  no Brick class, Haystack markers damper+cmd (Haystack-only)
    MTR-1  Electrical_Meter (no template)
  site B (prefix fbf/b, another org):
    AHU-9  Air_Handling_Unit, SAT sensor

PR #5's building (AHU + two VAVs + unseen chiller + non-networked EF,
with spec tags that differ from field names) joins this file when its
ladder/alignment tests are ported in Cx 2 and Cx 7.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rdflib import RDF, Literal, URIRef

from baler.commissioning.history import InMemoryHistory
from baler.store import BRICK, HAYSTACK, TD, Store

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

AHU = "urn:equip:fbf/a/ahu-1"
VAV = "urn:equip:fbf/a/vav-1"
MTR = "urn:equip:fbf/a/mtr-1"
AHU_B = "urn:equip:fbf/b/ahu-9"


def pt(equip: str, name: str) -> str:
    return equip.replace("urn:equip:", "urn:point:") + "/" + name


EQUIPS = {
    AHU: ("AHU", {"SAT": "Discharge_Air_Temperature_Sensor", "SF-S": "Fan_Status",
                  "RAT": "Return_Air_Temperature_Sensor", "X": None}),
    VAV: ("Variable_Air_Volume_Box", {"ZNT": "Zone_Air_Temperature_Sensor", "DMP": None}),
    MTR: ("Electrical_Meter", {"KW": "Electric_Power_Sensor"}),
    AHU_B: ("Air_Handling_Unit", {"SAT": "Supply_Air_Temperature_Sensor"}),
}
MARKERS = {pt(VAV, "DMP"): ("damper", "cmd", "point"), pt(VAV, "ZNT"): ("zone", "air", "temp", "sensor", "point")}


def store() -> Store:
    s = Store()
    g = s.graph
    for equip, (cls, points) in EQUIPS.items():
        e = URIRef(equip)
        g.add((e, RDF.type, BRICK[cls]))
        g.add((e, HAYSTACK.dis, Literal(equip.rsplit("/", 1)[-1].upper())))
        g.add((e, HAYSTACK.hasTag, Literal("equip")))
        for name, pcls in points.items():
            p = URIRef(pt(equip, name))
            g.add((p, RDF.type, TD.RawPoint))
            if pcls:
                g.add((p, RDF.type, BRICK[pcls]))
            g.add((e, BRICK.hasPoint, p))
            g.add((p, BRICK.isPointOf, e))
            for tag in MARKERS.get(str(p), ()):
                g.add((p, HAYSTACK.hasTag, Literal(tag)))
    g.add((URIRef(AHU), HAYSTACK.hasTag, Literal("ahu")))
    return s


def history(now: datetime = NOW) -> InMemoryHistory:
    """Every point fresh except: AHU RAT stale (3 days old), AHU X and VAV
    DMP never reported."""
    h = InMemoryHistory()
    for equip, (_cls, points) in EQUIPS.items():
        for name in points:
            uri = pt(equip, name)
            if uri in (pt(AHU, "X"), pt(VAV, "DMP")):
                continue
            ts = now - (timedelta(days=3) if uri == pt(AHU, "RAT") else timedelta(minutes=5))
            h.add(uri, ts, 55.0)
    return h
