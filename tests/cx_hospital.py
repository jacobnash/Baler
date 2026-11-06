"""
A mock-hospital-shaped site for Cx 3 (GL36/ConStrain, #39): the
equipment kinds and point vocabulary windrow.mock_hospital generates
(AHU, VAV, boiler, chiller; Fahrenheit, BACnet-style units, run statuses
rather than flow sensors), after autotag has given them Brick classes.
One of each, 24 h of 5-minute history ending at NOW.

  AHU-1    Air_Handling_Unit: DAT, RAT, OAT (outdoor air, the site's only
           one), supply fan status. No SAT setpoint - as on the hospital.
  VAV-1    Variable_Air_Volume_Box: zone temp, occupied and unoccupied
           zone setpoints - as on the hospital (no heating/cooling split).
  VAV-2    the same plus Zone_Air_Cooling/Heating setpoints, which the
           hospital doesn't have: lets zone_deadband run (fails 06-08 UTC,
           when the band narrows to 2 F).
  BOILER-1 Boiler: HW supply/return temp, run status (off 00-02 UTC).
           HW supply follows a 180 F @ 20 F OAT -> 140 F @ 60 F OAT reset,
           or, with `reset=False`, sits at 180 F all day.
  CHILLER-1 Chiller: CHW supply temp at a constant 44 F, run status.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from rdflib import RDF, Literal, URIRef

from baler.commissioning.history import InMemoryHistory
from baler.store import BRICK, HAYSTACK, TD, Store

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
SITE = "fbf/hospital-a"
AHU = f"urn:equip:{SITE}/ahu-1"
VAV = f"urn:equip:{SITE}/vav-1"
VAV2 = f"urn:equip:{SITE}/vav-2"
BOILER = f"urn:equip:{SITE}/boiler-1"
CHILLER = f"urn:equip:{SITE}/chiller-1"

# The hw_reset design the boiler follows, as a project would enter it.
HW_RESET_F = {"unit": "F", "temperature_air_outdoor_min": 20, "temperature_air_outdoor_max": 60,
              "temperature_water_hot_setpoint_max": 180, "temperature_water_hot_setpoint_min": 140}

F = "degreesFahrenheit"
EQUIPS = {
    AHU: ("Air_Handling_Unit", {"discharge-air-temp": ("Discharge_Air_Temperature_Sensor", F),
                                "return-air-temp": ("Return_Air_Temperature_Sensor", F),
                                "outside-air-temp": ("Outside_Air_Temperature_Sensor", F),
                                "supply-fan-status": ("Fan_Status", None)}),
    VAV: ("Variable_Air_Volume_Box", {"zone-air-temp": ("Zone_Air_Temperature_Sensor", F),
                                      "zone-temp-occ-sp": ("Occupied_Zone_Air_Temperature_Setpoint", F),
                                      "zone-temp-unocc-sp": ("Unoccupied_Zone_Air_Temperature_Setpoint", F)}),
    VAV2: ("Variable_Air_Volume_Box", {"zone-air-temp": ("Zone_Air_Temperature_Sensor", F),
                                       "zone-clg-sp": ("Zone_Air_Cooling_Temperature_Setpoint", F),
                                       "zone-htg-sp": ("Zone_Air_Heating_Temperature_Setpoint", F)}),
    BOILER: ("Boiler", {"hw-sup-temp": ("Hot_Water_Supply_Temperature_Sensor", F),
                        "hw-ret-temp": ("Hot_Water_Return_Temperature_Sensor", F),
                        "boiler-run-status": ("Run_Status", None)}),
    CHILLER: ("Chiller", {"chw-sup-temp": ("Chilled_Water_Supply_Temperature_Sensor", F),
                          "chiller-run-status": ("Run_Status", None)}),
}


def pt(equip: str, name: str) -> str:
    return equip.replace("urn:equip:", "urn:point:") + "/" + name


def store() -> Store:
    s = Store()
    g = s.graph
    for equip, (cls, points) in EQUIPS.items():
        e = URIRef(equip)
        g.add((e, RDF.type, BRICK[cls]))
        g.add((e, HAYSTACK.dis, Literal(equip.rsplit("/", 1)[-1].upper())))
        for name, (pcls, unit) in points.items():
            p = URIRef(pt(equip, name))
            g.add((p, RDF.type, TD.RawPoint))
            g.add((p, RDF.type, BRICK[pcls]))
            if unit:
                g.add((p, HAYSTACK.unit, Literal(unit)))
            g.add((e, BRICK.hasPoint, p))
            g.add((p, BRICK.isPointOf, e))
    return s


def oat_f(ts: datetime) -> float:
    """15 F at 03:00 UTC up to 65 F at 15:00 UTC: crosses both reset ends."""
    hours = (ts - NOW).total_seconds() / 3600
    return 40.0 - 25.0 * math.cos(2 * math.pi * (hours - 3) / 24)


def hw_reset_f(oat: float) -> float:
    lo, hi = HW_RESET_F["temperature_air_outdoor_min"], HW_RESET_F["temperature_air_outdoor_max"]
    top, bottom = HW_RESET_F["temperature_water_hot_setpoint_max"], HW_RESET_F["temperature_water_hot_setpoint_min"]
    if oat <= lo:
        return top
    if oat >= hi:
        return bottom
    return top + (oat - lo) * (bottom - top) / (hi - lo)


def history(reset: bool = True, now: datetime = NOW) -> InMemoryHistory:
    h = InMemoryHistory()
    t = now - timedelta(hours=24)
    while t <= now:
        oat = oat_f(t)
        hour = t.hour
        h.add(pt(AHU, "outside-air-temp"), t, round(oat, 2))
        h.add(pt(AHU, "discharge-air-temp"), t, 55.0)
        h.add(pt(AHU, "return-air-temp"), t, 72.0)
        h.add(pt(AHU, "supply-fan-status"), t, True)
        h.add(pt(VAV, "zone-air-temp"), t, 71.5)
        h.add(pt(VAV, "zone-temp-occ-sp"), t, 71.0)
        h.add(pt(VAV, "zone-temp-unocc-sp"), t, 62.0)
        h.add(pt(VAV2, "zone-air-temp"), t, 71.5)
        narrow = 6 <= hour < 8
        h.add(pt(VAV2, "zone-clg-sp"), t, 73.0 if narrow else 75.0)
        h.add(pt(VAV2, "zone-htg-sp"), t, 71.0 if narrow else 68.0)
        running = not hour < 2
        h.add(pt(BOILER, "boiler-run-status"), t, "active" if running else "inactive")
        h.add(pt(BOILER, "hw-sup-temp"), t, round(hw_reset_f(oat) if reset else 180.0, 2))
        h.add(pt(BOILER, "hw-ret-temp"), t, 130.0)
        h.add(pt(CHILLER, "chw-sup-temp"), t, 44.0)
        h.add(pt(CHILLER, "chiller-run-status"), t, True)
        t += timedelta(minutes=5)
    return h
