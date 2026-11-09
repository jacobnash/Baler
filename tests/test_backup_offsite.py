"""Encrypted off-site backups (#276): retention, configuration, encryption,
and the bucket round trip against a local S3-compatible server.

The bucket tests use BALER_TEST_S3_ENDPOINT when it's set (any
S3-compatible server: MinIO, moto_server, ...; BALER_TEST_S3_ACCESS_KEY_ID/
_SECRET_ACCESS_KEY, default "testing"), else moto's in-process server when
moto[server] is installed (the dev extra), else they skip. No provider or
account is involved.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from baler import backup_offsite as off

pyrage = pytest.importorskip("pyrage")
boto3 = pytest.importorskip("boto3")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ts(s: str) -> datetime:
    return off.parse_stamp(s)


def _read(path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _stamp(dt: datetime) -> str:
    return dt.strftime(off.STAMP_FORMAT)


# --- retention ------------------------------------------------------------------


def test_select_keep_daily_and_weekly():
    # 60 nightly backups at 03:30 plus a second one on the newest day.
    end = datetime(2026, 11, 8, 3, 30, tzinfo=timezone.utc)
    stamps = [end - timedelta(days=i) for i in range(60)] + [end + timedelta(hours=5)]
    keep = off.select_keep(stamps, 7, 4)
    newest = max(stamps)
    assert newest in keep and end not in keep  # one per day: the newest of that day
    days = {k.date() for k in keep}
    assert len([d for d in days if d > (end - timedelta(days=7)).date()]) == 7
    weeks = {k.isocalendar()[:2] for k in keep}
    assert len(weeks) == 4
    # 2026-11-08 is a Sunday: the 7 daily copies are one ISO week (Mon-Sun),
    # so weekly adds the newest of each of the 3 weeks before it.
    assert len(keep) == 7 + 3


@pytest.mark.parametrize("daily,weekly", [(0, 0), (1, 0), (0, 1)])
def test_select_keep_always_keeps_the_newest(daily, weekly):
    stamps = [_ts("20260101T000000Z"), _ts("20260102T000000Z"), _ts("20250101T000000Z")]
    assert _ts("20260102T000000Z") in off.select_keep(stamps, daily, weekly)


def test_prune_local(tmp_path):
    end = datetime(2026, 11, 8, 3, 30, tzinfo=timezone.utc)
    for i in range(20):
        s = _stamp(end - timedelta(days=i))
        (tmp_path / f"postgres-{s}.sql.gz").write_bytes(b"x")
        (tmp_path / f"oxigraph-{s}.ttl.gz").write_bytes(b"x")
    (tmp_path / "backup-status.json").write_text("{}")
    (tmp_path / "unrelated.txt").write_text("keep me")
    out = off.prune_local(str(tmp_path), 3, 2)
    left = off.local_sets(str(tmp_path))
    assert _stamp(end) in left and len(left) == len(out["kept"]) and len(out["removed"]) == 20 - len(left)
    assert all(set(files) == {"postgres", "oxigraph"} for files in left.values())
    assert (tmp_path / "unrelated.txt").exists() and (tmp_path / "backup-status.json").exists()


# --- configuration ----------------------------------------------------------------


def test_off_by_default():
    assert off.target_from_env({}) is None
    assert off.target_from_env({"BALER_BACKUP_S3_BUCKET": "  "}) is None


def test_target_never_shows_its_credentials():
    t = off.target_from_env({"BALER_BACKUP_S3_BUCKET": "b", "BALER_BACKUP_S3_PREFIX": "site-a",
                             "BALER_BACKUP_S3_ACCESS_KEY_ID": "AKIDSECRETID", "BALER_BACKUP_S3_SECRET_ACCESS_KEY": "s3cr3t"})
    assert t is not None and t.prefix == "site-a/"
    for text in (repr(t), str(t), t.describe()):
        assert "s3cr3t" not in text and "AKIDSECRETID" not in text
    assert off.redact("denied for AKIDSECRETID / s3cr3t", t) == "denied for <redacted> / <redacted>"


def test_half_a_credential_pair_is_refused():
    with pytest.raises(off.OffsiteError):
        off.target_from_env({"BALER_BACKUP_S3_BUCKET": "b", "BALER_BACKUP_S3_ACCESS_KEY_ID": "x"})


def test_recipients_are_required_and_checked(tmp_path):
    with pytest.raises(off.OffsiteError, match="unencrypted"):
        off.recipients_from_env({})
    with pytest.raises(off.OffsiteError, match="age1"):
        off.recipients_from_env({"BALER_BACKUP_AGE_RECIPIENTS": "ssh-rsa AAAA"})
    a, b = (str(pyrage.x25519.Identity.generate().to_public()) for _ in range(2))
    f = tmp_path / "recipients.txt"
    f.write_text(f"# recovery key, kept offline\n{b}\n")
    assert off.recipients_from_env({"BALER_BACKUP_AGE_RECIPIENTS": a, "BALER_BACKUP_AGE_RECIPIENTS_FILE": str(f)}) == [a, b]


def test_retention_env():
    assert off.retention_from_env({}) == (7, 4)
    assert off.retention_from_env({"BALER_BACKUP_KEEP_DAILY": "14", "BALER_BACKUP_KEEP_WEEKLY": "0"}) == (14, 0)
    with pytest.raises(off.OffsiteError):
        off.retention_from_env({"BALER_BACKUP_KEEP_DAILY": "-1"})


# --- encryption ---------------------------------------------------------------------


def _keypair(tmp_path, name="key.txt"):
    ident = pyrage.x25519.Identity.generate()
    path = tmp_path / name
    path.write_text(f"# created: test\n# public key: {ident.to_public()}\n{ident}\n")
    return str(ident.to_public()), str(path)


def _dump(tmp_path, stamp="20261108T033000Z") -> dict[str, str]:
    pg = tmp_path / f"postgres-{stamp}.sql.gz"
    ox = tmp_path / f"oxigraph-{stamp}.ttl.gz"
    with gzip.open(pg, "wt") as f:
        f.write("COPY public.orgs (id, name) FROM stdin;\n1\tSECRET-ORG-NAME\n\\.\n" * 2000)
    with gzip.open(ox, "wt") as f:
        f.write("<urn:equip:fbf/a> a <https://brickschema.org/schema/Brick#AHU> .\n")
    return {"postgres": str(pg), "oxigraph": str(ox)}


def test_encrypt_round_trip_and_wrong_key(tmp_path):
    pub, key = _keypair(tmp_path)
    _, other = _keypair(tmp_path, "other.txt")
    src = _dump(tmp_path)["postgres"]
    off.encrypt_file(src, str(tmp_path / "x.age"), [pub])
    blob = (tmp_path / "x.age").read_bytes()
    assert blob.startswith(b"age-encryption.org/v1") and b"\x1f\x8b" not in blob[:200]
    off.decrypt_file(str(tmp_path / "x.age"), str(tmp_path / "back.gz"), off.read_identities(key))
    assert (tmp_path / "back.gz").read_bytes() == _read(src)
    with pytest.raises(off.OffsiteError, match="can't decrypt"):
        off.decrypt_file(str(tmp_path / "x.age"), str(tmp_path / "no.gz"), off.read_identities(other))


# --- the bucket ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def s3_server():
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


@pytest.fixture
def target(s3_server):
    env = {"BALER_BACKUP_S3_BUCKET": f"baler-test-{uuid.uuid4().hex[:12]}", "BALER_BACKUP_S3_ENDPOINT": s3_server,
           "BALER_BACKUP_S3_REGION": "us-east-1", "BALER_BACKUP_S3_PREFIX": "site-a/",
           "BALER_BACKUP_S3_ACCESS_KEY_ID": os.environ.get("BALER_TEST_S3_ACCESS_KEY_ID", "testing"),
           "BALER_BACKUP_S3_SECRET_ACCESS_KEY": os.environ.get("BALER_TEST_S3_SECRET_ACCESS_KEY", "testing")}
    t = off.target_from_env(env)
    assert t is not None
    s3 = off.client(t)
    s3.create_bucket(Bucket=t.bucket)
    yield t, env
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=t.bucket):
        for o in page.get("Contents", []):
            s3.delete_object(Bucket=t.bucket, Key=o["Key"])
    s3.delete_bucket(Bucket=t.bucket)


def test_upload_is_encrypted_and_unreadable_without_the_key(target, tmp_path):
    t, _ = target
    pub, key = _keypair(tmp_path)
    _, other = _keypair(tmp_path, "other.txt")
    files = _dump(tmp_path)
    ops: list[str] = []
    s3 = off.client(t)
    s3.meta.events.register("before-call.s3", lambda model, **kw: ops.append(model.name))
    manifest = off.upload_set(t, [pub], "20261108T033000Z", files, str(tmp_path), s3)
    assert not list(tmp_path.glob("*.partial"))  # the encrypted temp files are gone
    assert os.path.exists(files["postgres"])  # the local backup is only read

    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=t.bucket)["Contents"])
    assert keys == ["site-a/20261108T033000Z/manifest.json", "site-a/20261108T033000Z/oxigraph-20261108T033000Z.ttl.gz.age",
                    "site-a/20261108T033000Z/postgres-20261108T033000Z.sql.gz.age"]
    raw = s3.get_object(Bucket=t.bucket, Key=keys[2])["Body"].read()
    assert raw.startswith(b"age-encryption.org/v1")
    assert b"SECRET-ORG-NAME" not in raw and b"COPY public" not in raw
    with pytest.raises(OSError):
        gzip.decompress(raw)  # not a readable gzip either
    stored = json.loads(s3.get_object(Bucket=t.bucket, Key=keys[0])["Body"].read())
    assert stored["recipients"] == [pub] and "AGE-SECRET-KEY" not in json.dumps(stored)
    assert manifest["files"]["postgres"]["sha256"] == stored["files"]["postgres"]["sha256"]

    (tmp_path / "x").mkdir()
    with pytest.raises(off.OffsiteError, match="can't decrypt"):
        off.fetch_set(t, off.read_identities(other), str(tmp_path / "x"), s3=s3)
    out = tmp_path / "restored"
    out.mkdir()
    got = off.fetch_set(t, off.read_identities(key), str(out), s3=s3)
    assert _read(got["postgres"]) == _read(files["postgres"])
    assert os.path.basename(got["oxigraph"]) == "oxigraph-20261108T033000Z.ttl.gz"
    # Least privilege: only what s3:PutObject, GetObject, ListBucket and DeleteObject allow.
    assert set(ops) <= {"PutObject", "HeadObject", "GetObject", "ListObjectsV2", "DeleteObjects",
                        "CreateMultipartUpload", "UploadPart", "CompleteMultipartUpload", "AbortMultipartUpload"}, ops


def test_a_tampered_object_is_refused(target, tmp_path):
    t, _ = target
    pub, key = _keypair(tmp_path)
    s3 = off.client(t)
    off.upload_set(t, [pub], "20261108T033000Z", _dump(tmp_path), str(tmp_path), s3)
    k = "site-a/20261108T033000Z/postgres-20261108T033000Z.sql.gz.age"
    blob = bytearray(s3.get_object(Bucket=t.bucket, Key=k)["Body"].read())
    blob[-10] ^= 1
    s3.put_object(Bucket=t.bucket, Key=k, Body=bytes(blob))
    (tmp_path / "out").mkdir()
    with pytest.raises(off.OffsiteError, match="SHA-256"):
        off.fetch_set(t, off.read_identities(key), str(tmp_path / "out"), s3=s3)


def test_retention_keeps_the_newest_good_copy(target, tmp_path):
    t, _ = target
    s3 = off.client(t)
    end = datetime(2026, 11, 8, 3, 30, tzinfo=timezone.utc)
    stamps = [_stamp(end - timedelta(days=i)) for i in range(40)]
    for s in stamps:  # cheap sets: manifest plus one object, as upload_set leaves them
        s3.put_object(Bucket=t.bucket, Key=f"{t.prefix}{s}/postgres-{s}.sql.gz.age", Body=b"age")
        s3.put_object(Bucket=t.bucket, Key=f"{t.prefix}{s}/{off.MANIFEST}", Body=b"{}")
    # An upload that died before its manifest, newer than every good set,
    # and an old one: the newest good copy is still the newest complete set.
    dead_new = _stamp(end + timedelta(hours=1))
    dead_old = _stamp(end - timedelta(days=100))
    for s in (dead_new, dead_old):
        s3.put_object(Bucket=t.bucket, Key=f"{t.prefix}{s}/postgres-{s}.sql.gz.age", Body=b"partial")
    # Another install's prefix in the same bucket is never touched.
    s3.put_object(Bucket=t.bucket, Key="site-b/20200101T000000Z/manifest.json", Body=b"{}")

    out = off.prune(t, 7, 4, s3)
    left = {s.stamp: s for s in off.list_sets(t, s3)}
    assert stamps[0] in left and left[stamps[0]].complete
    assert off.latest_good(t, s3).stamp == stamps[0]
    assert set(out["kept"]) == {s for s in left if left[s].complete}
    assert len(out["kept"]) == 7 + 3  # the 7 days are one ISO week (ending on a Sunday); weekly adds 3
    assert dead_new in left and dead_old not in left  # an in-progress upload is left alone
    assert s3.list_objects_v2(Bucket=t.bucket, Prefix="site-b/")["KeyCount"] == 1
    assert off.prune(t, 0, 0, s3)["kept"] == [stamps[0]]  # even keep-nothing keeps the newest good copy


def test_prune_with_no_good_copy_removes_nothing(target):
    t, _ = target
    s3 = off.client(t)
    s3.put_object(Bucket=t.bucket, Key=f"{t.prefix}20200101T000000Z/postgres-20200101T000000Z.sql.gz.age", Body=b"x")
    assert off.prune(t, 0, 0, s3) == {"kept": [], "removed": []}
    assert len(off.list_sets(t, s3)) == 1


# --- backup.py's off-site step: a failure keeps the local backup -------------------


def _backup_module():
    spec = importlib.util.spec_from_file_location("backup_script", os.path.join(REPO, "scripts", "backup.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_offsite_failure_is_reported_and_keeps_local_files(target, tmp_path):
    _, env = target
    pub, _ = _keypair(tmp_path)
    files = _dump(tmp_path)
    before = {k: _read(p) for k, p in files.items()}
    backup = _backup_module()
    bad = {**env, "BALER_BACKUP_AGE_RECIPIENTS": pub, "BALER_BACKUP_S3_SECRET_ACCESS_KEY": "wrong-secret-value",
           "BALER_BACKUP_S3_BUCKET": "no-such-bucket-" + uuid.uuid4().hex[:8]}
    report = backup.offsite_copy(off, str(tmp_path), "20261108T033000Z", files, bad)
    assert report["enabled"] and not report["ok"] and report["error"]
    assert "wrong-secret-value" not in json.dumps(report)
    assert {k: _read(p) for k, p in files.items()} == before
    assert not list(tmp_path.glob("baler-offsite-*")) and not list(tmp_path.glob("*.partial"))

    # The same failure is what /ops/status shows.
    from baler import ops_status

    now = datetime.now(timezone.utc).isoformat()
    state, detail = ops_status.backup_state({"local": {"ok": True, "files": {}}, "last_ok_at": now,
                                             "offsite": {**report, "last_ok_at": None}})
    assert state == "degraded" and "off-site copy failed" in detail and "local backup OK" in detail

    ok = backup.offsite_copy(off, str(tmp_path), "20261108T033000Z", files, {**env, "BALER_BACKUP_AGE_RECIPIENTS": pub})
    assert ok["ok"] and ok["kept"] == 1, ok


def test_missing_recipients_never_upload_plaintext(target, tmp_path):
    t, env = target
    backup = _backup_module()
    report = backup.offsite_copy(off, str(tmp_path), "20261108T033000Z", _dump(tmp_path), env)
    assert not report["ok"] and "unencrypted" in report["error"]
    assert off.list_sets(t) == []


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
