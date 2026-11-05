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
