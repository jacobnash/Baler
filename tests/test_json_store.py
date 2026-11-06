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


def test_data_file_lives_in_the_data_dir_and_is_seeded_once(tmp_path, monkeypatch):
    """#30: with BALER_DATA_DIR set, a store file lives there (a volume in
    compose), seeded from the repo copy in the working directory, and
    never written back into the checkout."""
    from baler import json_store

    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "rules.json").write_text('[{"id": "seed"}]')
    monkeypatch.chdir(repo)

    monkeypatch.delenv("BALER_DATA_DIR", raising=False)
    assert json_store.data_file("rules.json") == "rules.json"  # unset: as before

    monkeypatch.setenv("BALER_DATA_DIR", str(tmp_path / "data"))
    path = json_store.data_file("rules.json")
    assert path == str(tmp_path / "data" / "rules.json")
    assert json_store.load(path) == [{"id": "seed"}]
    json_store.save(path, [{"id": "made-by-the-api"}])
    assert json_store.data_file("rules.json") == path  # not re-seeded over API data
    assert json_store.load(path) == [{"id": "made-by-the-api"}]
    assert (repo / "rules.json").read_text() == '[{"id": "seed"}]'  # checkout untouched
    assert json_store.data_file("webhooks.json").endswith("webhooks.json")  # no seed: fine
