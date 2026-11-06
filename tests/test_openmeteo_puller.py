"""
Pull-cycle logic tested against a fake OpenMeteoClient (canned current()
response), same idiom as FakeEnergyStarClient in test_energystar_puller.py.
Needs live Postgres, hence integration.
"""

import pytest
from rdflib import RDF, URIRef

from baler.openmeteo_puller import DEFAULT_VARIABLE_TAGS, pull_once
from baler.store import BRICK, Store
from baler.timeseries import connect, read_latest

STATION_ID = "test-station"


class FakeOpenMeteoClient:
    def __init__(self, current: dict):
        self._current = current
        self.requested_variables = None

    def current(self, variables):
        self.requested_variables = list(variables)
        return self._current


@pytest.fixture
def ts_conn():
    conn = connect()
    conn.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (f"urn:point:weather/{STATION_ID}/%",))
    return conn


@pytest.mark.integration
def test_pull_once_ingests_a_reading_per_variable_and_classifies_into_brick(ts_conn):
    store = Store()
    client = FakeOpenMeteoClient({
        "temperature_2m": 23.2,
        "relative_humidity_2m": 61,
        "wind_speed_10m": 10.5,
        "wind_direction_10m": 262,
    })

    counts = pull_once(client, store, ts_conn, STATION_ID, DEFAULT_VARIABLE_TAGS)

    assert counts == {
        "equip": "direct",
        "temperature_2m": "direct",
        "relative_humidity_2m": "direct",
        "wind_speed_10m": "direct",
        "wind_direction_10m": "direct",
    }
    assert read_latest(ts_conn, f"urn:point:weather/{STATION_ID}/temperature_2m") == 23.2
    assert read_latest(ts_conn, f"urn:point:weather/{STATION_ID}/wind_speed_10m") == 10.5

    equip_uri = URIRef(f"urn:equip:weather/{STATION_ID}")
    assert (equip_uri, RDF.type, BRICK.Weather_Station) in store.graph
    point_uri = URIRef(f"urn:point:weather/{STATION_ID}/wind_direction_10m")
    assert (point_uri, RDF.type, BRICK.Wind_Direction_Sensor) in store.graph


@pytest.mark.integration
def test_pull_once_requests_every_configured_variable(ts_conn):
    store = Store()
    client = FakeOpenMeteoClient({var: 1.0 for var in DEFAULT_VARIABLE_TAGS})

    pull_once(client, store, ts_conn, STATION_ID, DEFAULT_VARIABLE_TAGS)

    assert set(client.requested_variables) == set(DEFAULT_VARIABLE_TAGS)


@pytest.mark.integration
def test_pull_once_skips_a_variable_missing_from_the_response(ts_conn):
    store = Store()
    client = FakeOpenMeteoClient({"temperature_2m": 23.2})  # the API omits a variable it has no data for
    variable_tags = {
        "temperature_2m": {"weather": True, "air": True, "temp": True, "sensor": True},
        "wind_speed_10m": {"weather": True, "wind": True, "speed": True, "sensor": True},
    }

    counts = pull_once(client, store, ts_conn, STATION_ID, variable_tags)

    assert counts == {"equip": "direct", "temperature_2m": "direct"}
    assert read_latest(ts_conn, f"urn:point:weather/{STATION_ID}/temperature_2m") == 23.2


class FakeUnitsClient(FakeOpenMeteoClient):
    """Like Open-Meteo with explicit units: also returns current_units."""

    def __init__(self, current: dict, units: dict):
        super().__init__(current)
        self._units = units

    def current_with_units(self, variables):
        self.requested_variables = list(variables)
        return self._current, self._units


def test_observation_time_is_open_meteos_not_the_fetch_time():
    from baler.openmeteo_puller import observed_at

    assert observed_at({"time": "2026-11-06T20:45"}, 0.0) == 1793997900.0  # GMT, as requested
    assert observed_at({}, 123.0) == 123.0
    assert observed_at({"time": "garbage"}, 123.0) == 123.0


def test_client_asks_for_explicit_units_and_maps_them_to_haystack(monkeypatch):
    from baler import openmeteo_client

    sent = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"current": {"time": "2026-11-06T20:45", "temperature_2m": 51.3, "wind_speed_10m": 3.2},
                    "current_units": {"time": "iso8601", "interval": "seconds",
                                      "temperature_2m": "°F", "wind_speed_10m": "mp/h"}}

    client = openmeteo_client.OpenMeteoClient(37.76, -122.39, "imperial")
    monkeypatch.setattr(client._session, "get", lambda url, params: sent.update(params) or Resp())
    current, units = client.current_with_units(["temperature_2m", "wind_speed_10m"])
    assert sent["temperature_unit"] == "fahrenheit" and sent["wind_speed_unit"] == "mph"
    assert units == {"temperature_2m": "°F", "wind_speed_10m": "mph"}
    assert current["temperature_2m"] == 51.3
    with pytest.raises(ValueError):
        openmeteo_client.OpenMeteoClient(0, 0, "kelvin")


@pytest.mark.integration
def test_points_carry_their_unit_and_switching_units_replaces_it(ts_conn):
    """#53: a °C weather point next to a °F building must say so."""
    from rdflib import Literal

    from baler.store import HAYSTACK

    store = Store()
    metric = FakeUnitsClient({"time": "2026-11-06T20:45", "temperature_2m": 10.7},
                             {"temperature_2m": "°C"})
    pull_once(metric, store, ts_conn, STATION_ID, {"temperature_2m": DEFAULT_VARIABLE_TAGS["temperature_2m"]})
    point = URIRef(f"urn:point:weather/{STATION_ID}/temperature_2m")
    assert set(store.graph.objects(point, HAYSTACK.unit)) == {Literal("°C")}

    imperial = FakeUnitsClient({"time": "2026-11-06T21:00", "temperature_2m": 51.3}, {"temperature_2m": "°F"})
    pull_once(imperial, store, ts_conn, STATION_ID, {"temperature_2m": DEFAULT_VARIABLE_TAGS["temperature_2m"]})
    assert set(store.graph.objects(point, HAYSTACK.unit)) == {Literal("°F")}
    assert read_latest(ts_conn, str(point)) == 51.3
