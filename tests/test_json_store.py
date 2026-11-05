import os
import tempfile

from baler.json_store import load, new_id, save


def test_load_missing_file_returns_empty_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        assert load(os.path.join(tmpdir, "rules.json")) == []


def test_save_and_load_round_trips():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "rules.json")
        records = [{"id": new_id(), "name": "high-temp", "type": "range", "min": 60.0, "max": 80.0}]

        save(path, records)

        assert load(path) == records


def test_save_leaves_no_lingering_tmp_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "webhooks.json")
        save(path, [])
        assert set(os.listdir(tmpdir)) == {"webhooks.json"}


def test_new_id_is_unique():
    assert new_id() != new_id()
