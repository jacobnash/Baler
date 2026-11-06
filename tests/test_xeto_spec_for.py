"""hisquery.xeto_spec_for (lifted out of haystack_api.best_spec, #37):
the same answers haystack_api's /api records give, without openhay, so
commissioning can read Brick and Haystack as one model."""

from baler import hisquery


def test_markers_and_brick_class():
    assert hisquery.xeto_spec_for({"equip", "ahu"}) == "ph::Ahu"
    assert hisquery.xeto_spec_for({"equip"}, "AHU") == "ph::Ahu"  # Brick alias, no ahu marker
    assert hisquery.xeto_spec_for({"point"}, "Zone_Air_Temperature_Sensor") == "ph.points::ZoneAirTempSensor"
    assert hisquery.xeto_spec_for({"point"}) == "ph::Point"  # not BoolPoint: same markers, base spec wins
    assert hisquery.xeto_spec_for(set()) is None
    assert hisquery.xeto_spec_for(set(), "No_Such_Class") is None


def test_markers_of_brick_class_spreads_over_subclasses():
    m = hisquery.markers_of_brick_class()
    assert m["Supply_Air_Temperature_Sensor"] == m["Discharge_Air_Temperature_Sensor"]
    assert {"damper", "cmd"} <= m["Damper_Position_Command"]
