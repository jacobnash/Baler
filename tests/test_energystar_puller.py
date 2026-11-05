"""
Pull-cycle logic tested against a fake EnergyStarClient (canned
property_list/property_metrics, same idiom as FakeHaystackClient in
test_haystack_puller.py) plus an in-memory Store() and real Postgres for
history. Needs live Postgres, hence integration.
"""

import pytest

from baler.energystar_puller import _latest_complete_period, pull_once
from baler.store import Store
from baler.timeseries import connect, read_latest

ACCOUNT_ID = "test-account"


class FakeEnergyStarClient:
    def __init__(self, properties, metrics_by_property):
        self._properties = properties
        self._metrics_by_property = metrics_by_property
        self.metrics_calls = []

    def property_list(self, account_id):
        return self._properties

    def property_metrics(self, property_id, year, month, metrics):
        self.metrics_calls.append((property_id, year, month, tuple(metrics)))
        return self._metrics_by_property.get(property_id, {})


@pytest.fixture
def ts_conn():
    conn = connect()
    conn.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (f"urn:point:energystar/{ACCOUNT_ID}/%",))
    return conn


def test_latest_complete_period_rolls_back_a_year_in_january():
    import datetime

    assert _latest_complete_period(datetime.date(2026, 1, 15)) == (2025, 12)
    assert _latest_complete_period(datetime.date(2026, 8, 26)) == (2026, 7)


@pytest.mark.integration
def test_pull_once_ingests_one_reading_per_metric_per_property(ts_conn):
    store = Store()
    client = FakeEnergyStarClient(
        properties=[{"id": "111", "name": "Building A"}],
        metrics_by_property={"111": {"score": "87", "siteIntensity": "45.2"}},
    )

    count = pull_once(client, store, ts_conn, ACCOUNT_ID, ["score", "siteIntensity"])

    assert count == 2
    assert read_latest(ts_conn, f"urn:point:energystar/{ACCOUNT_ID}/111/score") == 87.0
    assert read_latest(ts_conn, f"urn:point:energystar/{ACCOUNT_ID}/111/siteIntensity") == 45.2


@pytest.mark.integration
def test_pull_once_requests_the_given_metrics_for_every_property(ts_conn):
    store = Store()
    client = FakeEnergyStarClient(
        properties=[{"id": "111", "name": "A"}, {"id": "222", "name": "B"}],
        metrics_by_property={"111": {"score": "10"}, "222": {"score": "20"}},
    )

    pull_once(client, store, ts_conn, ACCOUNT_ID, ["score"])

    called_property_ids = {call[0] for call in client.metrics_calls}
    assert called_property_ids == {"111", "222"}


@pytest.mark.integration
def test_non_numeric_metric_value_still_ingests_as_text(ts_conn):
    store = Store()
    client = FakeEnergyStarClient(
        properties=[{"id": "111", "name": "A"}],
        metrics_by_property={"111": {"generationDate": "2026-08-01"}},
    )

    count = pull_once(client, store, ts_conn, ACCOUNT_ID, ["generationDate"])

    assert count == 1
