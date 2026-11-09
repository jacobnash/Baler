"""Off-site backups end to end (#276), against a local S3-compatible server
(BALER_TEST_S3_ENDPOINT, or moto's in-process server): no provider, no
account.

A scratch source stack (project "restore-os") is seeded like
tests/test_restore_drill.py's, then:

- the real scripts/backup.py backs it up with the off-site copy on: the
  bucket holds only age ciphertext;
- the local backups are deleted, and the real scripts/restore_drill.py
  restores from the off-site copy alone into an empty scratch stack
  ("restore-os-drill") and passes;
- backup.py against a bucket it can't write to keeps the local backup,
  exits 2, and /ops/status's Backups card says the off-site copy failed.

Needs docker but not the main stack; both scratch stacks are removed
afterwards with their volumes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid

import psycopg
import pytest

pytest.importorskip("pyrage")
pytest.importorskip("boto3")

from baler import backup_offsite as off
from baler import ops_status

pytestmark = pytest.mark.integration

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
_spec = importlib.util.spec_from_file_location("restore_drill_os", os.path.join(SCRIPTS, "restore_drill.py"))
assert _spec and _spec.loader
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)


@pytest.fixture(scope="module")
def s3_endpoint():
    url = os.environ.get("BALER_TEST_S3_ENDPOINT")
    if url:
        yield url
        return
    moto_server = pytest.importorskip("moto.server", reason="no BALER_TEST_S3_ENDPOINT and no moto[server]")
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture(scope="module")
def stack(s3_endpoint, tmp_path_factory):
    import pyrage
    from test_restore_drill import _seed

    tmp = tmp_path_factory.mktemp("offsite")
    ident = pyrage.x25519.Identity.generate()
    (tmp / "backup-key.txt").write_text(f"# public key: {ident.to_public()}\n{ident}\n")
    os.chmod(tmp / "backup-key.txt", 0o600)
    offsite_env = {
        "BALER_BACKUP_S3_BUCKET": f"baler-drill-{uuid.uuid4().hex[:10]}", "BALER_BACKUP_S3_ENDPOINT": s3_endpoint,
        "BALER_BACKUP_S3_REGION": "us-east-1", "BALER_BACKUP_S3_PREFIX": "nash/",
        "BALER_BACKUP_S3_ACCESS_KEY_ID": os.environ.get("BALER_TEST_S3_ACCESS_KEY_ID", "testing"),
        "BALER_BACKUP_S3_SECRET_ACCESS_KEY": os.environ.get("BALER_TEST_S3_SECRET_ACCESS_KEY", "testing"),
        "BALER_BACKUP_AGE_RECIPIENTS": str(ident.to_public()),
    }
    target = off.target_from_env(offsite_env)
    assert target is not None
    s3 = off.client(target)
    s3.create_bucket(Bucket=target.bucket)
    src = rd.Scratch("restore-os")
    try:
        src.up()
        _seed(src)
        yield {"src": src, "tmp": tmp, "env": {**src.env, **offsite_env}, "target": target, "s3": s3,
               "identity": str(tmp / "backup-key.txt")}
    finally:
        src.down()
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=target.bucket):
            for o in page.get("Contents", []):
                s3.delete_object(Bucket=target.bucket, Key=o["Key"])
        s3.delete_bucket(Bucket=target.bucket)


def _backup(stack, out, env):
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, "backup.py"), "--out-dir", str(out),
                           "--oxigraph-url", stack["src"].oxigraph_url, "--compose-project-dir", REPO],
                          env=env, check=False, capture_output=True, text=True)


def _heartbeat(stack, service):
    with psycopg.connect(stack["src"].dsn, autocommit=True) as conn:
        row = conn.execute("SELECT detail FROM service_heartbeats WHERE service = %s", (service,)).fetchone()
    return row[0] if row else None


def test_backup_uploads_encrypted(stack):
    out = stack["tmp"] / "backups"
    run = _backup(stack, out, stack["env"])
    assert run.returncode == 0, run.stdout + run.stderr
    report = _heartbeat(stack, "backup")
    assert report["ok"] and report["offsite"]["ok"] and report["offsite"]["kept"] == 1
    newest = off.latest_good(stack["target"], stack["s3"])
    assert newest is not None and newest.stamp == report["stamp"]
    # Every data object is age ciphertext: no SQL, no Turtle, no gzip header.
    for key in newest.keys:
        body = stack["s3"].get_object(Bucket=stack["target"].bucket, Key=key)["Body"].read()
        if key.endswith(".age"):
            assert body.startswith(b"age-encryption.org/v1\n")
            assert b"CREATE TABLE" not in body and b"drill-admin@example.com" not in body and b"brickschema" not in body
    assert ops_status.backup_state(report)[0] == "ok"
    stack["stamp"] = report["stamp"]


def test_drill_from_the_offsite_copy_alone(stack):
    """No local backup at all: the drill fetches, decrypts and restores the
    off-site set into an empty scratch stack, and every check passes."""
    assert stack.get("stamp"), "test_backup_uploads_encrypted must run first"
    shutil.rmtree(stack["tmp"] / "backups")
    empty = stack["tmp"] / "no-local-backups"
    empty.mkdir()
    report_path = stack["tmp"] / "drill.json"
    env = {**stack["env"], "BALER_BACKUP_AGE_IDENTITY_FILE": stack["identity"]}
    drill = subprocess.run([sys.executable, os.path.join(SCRIPTS, "restore_drill.py"), "--backup-dir", str(empty),
                            "--project", "restore-os-drill", "--report", str(report_path),
                            "--download-dir", str(stack["tmp"])], env=env, check=False)
    report = json.loads(report_path.read_text())
    failed = [c for c in report["checks"] if not c["ok"]]
    assert drill.returncode == 0 and report["ok"], failed
    assert report["facts"]["source"] == "offsite"  # auto: the only copy there is
    names = {c["check"] for c in report["checks"]}
    assert {"every table has the dump's rows", "point_history samples", "Cx report renders", "graph triples",
            "schema migrations"} <= names
    assert stack["stamp"] in os.path.basename(report["facts"]["postgres_file"])
    assert not list(stack["tmp"].glob("baler-restore-drill-*"))  # the decrypted download is gone
    drilled = _heartbeat(stack, "restore_drill")
    assert drilled["ok"] and drilled["source"] == "offsite"
    # The scratch stack and its volumes are gone too.
    left = subprocess.run(["docker", "volume", "ls", "-q", "--filter", "label=com.docker.compose.project=restore-os-drill"],
                          check=True, capture_output=True, text=True).stdout.split()
    assert left == []


def test_drill_without_the_key_fails_and_says_so(stack):
    other = stack["tmp"] / "other-key.txt"
    import pyrage

    other.write_text(str(pyrage.x25519.Identity.generate()) + "\n")
    report_path = stack["tmp"] / "drill-nokey.json"
    env = {**stack["env"], "BALER_BACKUP_AGE_IDENTITY_FILE": str(other)}
    drill = subprocess.run([sys.executable, os.path.join(SCRIPTS, "restore_drill.py"), "--source", "offsite",
                            "--project", "restore-os-drill", "--report", str(report_path), "--no-status"],
                           env=env, check=False)
    report = json.loads(report_path.read_text())
    assert drill.returncode == 1 and not report["ok"]
    assert any(c["check"] == "fetch the off-site copy" and "can't decrypt" in c["detail"] for c in report["checks"])


def test_upload_failure_keeps_the_local_backup_and_shows_in_ops_status(stack, monkeypatch):
    out = stack["tmp"] / "backups-fail"
    env = {**stack["env"], "BALER_BACKUP_S3_BUCKET": "no-such-bucket-" + uuid.uuid4().hex[:8],
           "BALER_BACKUP_S3_SECRET_ACCESS_KEY": "a-secret-that-must-not-show"}
    run = _backup(stack, out, env)
    assert run.returncode == 2, run.stdout + run.stderr
    assert "OFF-SITE COPY FAILED (the local backup is kept)" in run.stderr
    assert "a-secret-that-must-not-show" not in run.stdout + run.stderr
    local = off.local_sets(str(out))
    assert len(local) == 1 and set(next(iter(local.values()))) == {"postgres", "oxigraph"}
    assert all(os.path.getsize(p) > 0 for p in next(iter(local.values())).values())
    report = _heartbeat(stack, "backup")
    assert report["local"]["ok"] and not report["offsite"]["ok"] and not report["ok"]
    assert "a-secret-that-must-not-show" not in json.dumps(report)
    assert report["offsite"]["last_ok_at"]  # carried from the earlier good upload

    # What GET /ops/status shows, read from that database the way ops_api does.
    monkeypatch.setattr(ops_status.timeseries, "DEFAULT_DSN", stack["src"].dsn)
    building = ops_status._building_signals()
    card = ops_status._probe_one(next(c for c in ops_status.COMPONENTS if c["id"] == "backups"), building, {})
    assert card["state"] == "degraded" and "off-site copy failed" in card["detail"]
    # The good off-site copy is still there: a failed run prunes nothing.
    assert off.latest_good(stack["target"], stack["s3"]).stamp == stack["stamp"]
