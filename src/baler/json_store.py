"""
Generic JSON-file config persistence - lifted verbatim from
fbf/src/windrow/connection_store.py (same operation on a different file, not a
near-duplicate situation, so sharing it directly is the right amount of
DRY here). A JSON file, not sqlite3: this project's config stores
(rules.json, webhooks.json) are each written by exactly one process, so
there's no concurrent-writer problem for sqlite3's transactions to solve,
and the payload (a handful of dicts) has no schema advantage over "a list
of dicts, in a file."

Known, accepted gap: no file locking - single writer per file is an
assumption, not enforced. Fine per YAGNI unless multi-process writes to
the same file are actually needed.
"""

import json
import os
import shutil
import uuid


class RuleCache:
    """Reloads a JSON-list config file only when its mtime changes - a
    cheap stat() per check, not a per-check disk read let alone a live
    query. Generic over any such file (rules.json, derivations.json, ...),
    not specific to fault rules despite the name's origin there."""

    def __init__(self, path: str):
        self.path = path
        self._mtime: float | None = None
        self._records: list[dict] = []

    def get(self) -> list[dict]:
        try:
            mtime = os.stat(self.path).st_mtime
        except FileNotFoundError:
            self._records = []
            self._mtime = None
            return self._records
        if mtime != self._mtime:
            self._records = load(self.path)
            self._mtime = mtime
        return self._records


def load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def save(path: str, records: list[dict]) -> None:
    """Atomic write: temp file + os.replace, both stdlib and POSIX-atomic -
    a crash mid-write never leaves a half-written file."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(records, f, indent=2)
    os.replace(tmp_path, path)


def new_id() -> str:
    return uuid.uuid4().hex


def data_file(name: str) -> str:
    """Where a store file lives (#30). With BALER_DATA_DIR set (compose sets
    /data, a named volume) it's <dir>/<name>, seeded on first use from the
    repo's copy of `name` in the working directory - which is then seed
    data only. Unset (local runs, tests): `name` itself, as before.

    In compose the working directory is the deploy checkout, where these
    files are tracked in git, so writing them there meant every deploy's
    checkout reverted whatever the API had created."""
    data_dir = os.environ.get("BALER_DATA_DIR")
    if not data_dir:
        return name
    path = os.path.join(data_dir, name)
    if not os.path.exists(path) and os.path.exists(name):
        os.makedirs(data_dir, exist_ok=True)
        # copy then rename: two services seeding the same file at once
        # (fault_api and fault_detector share rules.json) can't half-write it
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        shutil.copyfile(name, tmp)
        os.replace(tmp, path)
    return path
