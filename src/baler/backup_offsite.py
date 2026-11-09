"""
Encrypted off-site copies of scripts/backup.py's backups (#276).

Off by default: nothing here runs unless BALER_BACKUP_S3_BUCKET is set.
When it is, each successful backup is encrypted on this host and uploaded
to an S3-compatible bucket (AWS S3, Backblaze B2, Wasabi, Cloudflare R2,
MinIO, Garage, ...), then old copies there are pruned by the same
daily/weekly policy as local backups. scripts/restore.py and
scripts/restore_drill.py fetch and decrypt a set back with --from-offsite.

Encryption is age (https://age-encryption.org/v1) through pyrage, rage's
Python binding:

- Public-key: the host only needs the *recipient* (public) key to encrypt.
  The identity (private) key that decrypts is not needed to back up, is
  never uploaded, and can live off the host entirely (a password manager,
  a printed copy); only a restore, or a drill that reads the off-site copy,
  needs it.
- Streaming and authenticated: the payload is ChaCha20-Poly1305 in 64 KiB
  chunks (age's STREAM construction), so a multi-GB dump is encrypted in
  constant memory, and any bit flipped or chunk cut off or reordered fails
  decryption instead of yielding a quietly wrong dump.
- A standard format with independent implementations: if Baler's code is
  gone, `age -d -i key.txt postgres-<ts>.sql.gz.age | gunzip` (the age or
  rage CLI, packaged in Debian/Ubuntu/Homebrew) still recovers a backup.

Layout in the bucket, one "set" per backup run:

    <prefix><stamp>/postgres-<stamp>.sql.gz.age
    <prefix><stamp>/oxigraph-<stamp>.ttl.gz.age
    <prefix><stamp>/manifest.json      written last: a set is complete
                                       ("good") only once it exists

manifest.json is plaintext and holds no data: the file names, each
ciphertext's size and SHA-256 (checked before decrypting), and the
recipient public keys, so whoever restores knows which key they need.

The credentials need only s3:PutObject, s3:GetObject, s3:ListBucket and
s3:DeleteObject on that one bucket (and prefix); see backups.mdx. They are
read from the environment, never printed: Target's repr hides them, and
every error message that leaves this module goes through redact().
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_STAMP = re.compile(r"(\d{8}T\d{6}Z)")
MANIFEST = "manifest.json"
KINDS = {"postgres": ".sql.gz", "oxigraph": ".ttl.gz"}
DEFAULT_KEEP_DAILY = 7
DEFAULT_KEEP_WEEKLY = 4


class OffsiteError(RuntimeError):
    """A misconfiguration or a failed transfer; the message is safe to log."""


# --- configuration ------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One S3-compatible bucket (and key prefix) for one install."""

    bucket: str
    prefix: str = "baler/"
    endpoint: str | None = None  # None: AWS S3
    region: str | None = None
    addressing: str = "auto"  # auto (path when an endpoint is set), path, virtual
    access_key_id: str | None = field(default=None, repr=False)
    secret_access_key: str | None = field(default=None, repr=False)

    def describe(self) -> str:
        """For logs and reports: where, never with what credentials."""
        where = f"s3://{self.bucket}/{self.prefix}"
        return where + (f" at {self.endpoint}" if self.endpoint else "")

    def secrets(self) -> list[str]:
        return [s for s in (self.access_key_id, self.secret_access_key) if s]


def _env(env: Mapping[str, str], name: str, default: str = "") -> str:
    return (env.get(name) or default).strip()


def target_from_env(env: Mapping[str, str] = os.environ) -> Target | None:
    """The install's target, or None when off-site copies are off (no
    BALER_BACKUP_S3_BUCKET). Without BALER_BACKUP_S3_ACCESS_KEY_ID/_SECRET_
    ACCESS_KEY, boto3's own chain applies (AWS_* variables, an instance or
    task role, ~/.aws/credentials)."""
    bucket = _env(env, "BALER_BACKUP_S3_BUCKET")
    if not bucket:
        return None
    prefix = _env(env, "BALER_BACKUP_S3_PREFIX", "baler/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    addressing = _env(env, "BALER_BACKUP_S3_ADDRESSING", "auto")
    if addressing not in ("auto", "path", "virtual"):
        raise OffsiteError(f"BALER_BACKUP_S3_ADDRESSING must be auto, path or virtual, not {addressing!r}")
    key_id, secret = _env(env, "BALER_BACKUP_S3_ACCESS_KEY_ID"), _env(env, "BALER_BACKUP_S3_SECRET_ACCESS_KEY")
    if bool(key_id) != bool(secret):
        raise OffsiteError("set both BALER_BACKUP_S3_ACCESS_KEY_ID and BALER_BACKUP_S3_SECRET_ACCESS_KEY, or neither")
    return Target(bucket=bucket, prefix=prefix, endpoint=_env(env, "BALER_BACKUP_S3_ENDPOINT") or None,
                  region=_env(env, "BALER_BACKUP_S3_REGION") or None, addressing=addressing,
                  access_key_id=key_id or None, secret_access_key=secret or None)


def retention_from_env(env: Mapping[str, str] = os.environ) -> tuple[int, int]:
    """(daily, weekly) copies to keep; local and off-site share the policy."""
    try:
        daily = int(_env(env, "BALER_BACKUP_KEEP_DAILY", str(DEFAULT_KEEP_DAILY)))
        weekly = int(_env(env, "BALER_BACKUP_KEEP_WEEKLY", str(DEFAULT_KEEP_WEEKLY)))
    except ValueError as exc:
        raise OffsiteError(f"BALER_BACKUP_KEEP_DAILY/_WEEKLY must be whole numbers: {exc}") from None
    if daily < 0 or weekly < 0:
        raise OffsiteError("BALER_BACKUP_KEEP_DAILY/_WEEKLY can't be negative")
    return daily, weekly


def _key_lines(text: str) -> list[str]:
    """age's file format for recipients and identities: one per line, `#`
    comments and blank lines ignored (age-keygen's output reads as is)."""
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def recipients_from_env(env: Mapping[str, str] = os.environ) -> list[str]:
    """age recipient (public) keys, `age1...`: BALER_BACKUP_AGE_RECIPIENTS
    (space- or comma-separated) plus the lines of
    BALER_BACKUP_AGE_RECIPIENTS_FILE. More than one means any of the
    matching identities can decrypt (e.g. the host's key and a recovery
    key kept offline)."""
    keys = [k for k in re.split(r"[\s,]+", _env(env, "BALER_BACKUP_AGE_RECIPIENTS")) if k]
    path = _env(env, "BALER_BACKUP_AGE_RECIPIENTS_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                keys += _key_lines(f.read())
        except OSError as exc:
            raise OffsiteError(f"can't read BALER_BACKUP_AGE_RECIPIENTS_FILE: {exc.strerror} ({path})") from None
    bad = [k for k in keys if not k.startswith("age1")]
    if bad:
        raise OffsiteError(f"not age recipient keys (they start with age1): {', '.join(k[:12] + '...' for k in bad)}")
    if not keys:
        raise OffsiteError("BALER_BACKUP_S3_BUCKET is set but no BALER_BACKUP_AGE_RECIPIENTS(_FILE): "
                           "refusing to upload a backup unencrypted")
    return keys


def identity_file_from_env(env: Mapping[str, str] = os.environ) -> str | None:
    return _env(env, "BALER_BACKUP_AGE_IDENTITY_FILE") or None


def read_identities(path: str) -> list[str]:
    """The AGE-SECRET-KEY-1... lines of an age identity file."""
    try:
        with open(path, encoding="utf-8") as f:
            keys = [k for k in _key_lines(f.read()) if k.startswith("AGE-SECRET-KEY-")]
    except OSError as exc:
        raise OffsiteError(f"can't read the age identity file: {exc.strerror} ({path})") from None
    if not keys:
        raise OffsiteError(f"no AGE-SECRET-KEY-... line in {path}")
    return keys


def redact(text: str, target: Target | None) -> str:
    """`text` with the target's credentials blanked, for anything logged."""
    for s in target.secrets() if target else []:
        text = text.replace(s, "<redacted>")
    return text


# --- encryption ---------------------------------------------------------------


def _pyrage():
    try:
        import pyrage
    except ImportError:
        raise OffsiteError("off-site backups need pyrage and boto3: pip install -e '.[offsite]'") from None
    return pyrage


def encrypt_file(src: str, dst: str, recipients: Iterable[str]) -> None:
    pyrage = _pyrage()
    try:
        rcpts = [pyrage.x25519.Recipient.from_str(r) for r in recipients]
    except Exception as exc:  # noqa: BLE001 - pyrage raises its own types
        raise OffsiteError(f"bad age recipient key: {exc}") from None
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        pyrage.encrypt_io(fin, fout, rcpts)


def decrypt_file(src: str, dst: str, identities: Iterable[str]) -> None:
    pyrage = _pyrage()
    try:
        ids = [pyrage.x25519.Identity.from_str(i) for i in identities]
    except Exception:  # noqa: BLE001 - never echo a secret key back
        raise OffsiteError("the age identity file has a malformed AGE-SECRET-KEY line") from None
    try:
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            pyrage.decrypt_io(fin, fout, ids)
    except pyrage.DecryptError as exc:
        raise OffsiteError(f"can't decrypt {os.path.basename(src)} with this identity (wrong key, or the object "
                           f"is damaged): {exc}") from None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# --- the bucket ---------------------------------------------------------------


def client(target: Target):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise OffsiteError("off-site backups need pyrage and boto3: pip install -e '.[offsite]'") from None
    style = target.addressing if target.addressing != "auto" else ("path" if target.endpoint else "virtual")
    return boto3.session.Session().client(
        "s3", endpoint_url=target.endpoint, region_name=target.region,
        aws_access_key_id=target.access_key_id, aws_secret_access_key=target.secret_access_key,
        config=Config(s3={"addressing_style": style}, retries={"mode": "standard", "max_attempts": 5},
                      connect_timeout=15, read_timeout=120,
                      # Some S3-compatible servers reject the newer default
                      # CRC checksums; only send them where required.
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"))


def _error(target: Target, what: str, exc: BaseException) -> OffsiteError:
    return OffsiteError(redact(f"{what} ({target.describe()}): {type(exc).__name__}: {exc}", target))


@dataclass
class RemoteSet:
    stamp: str
    keys: list[str]

    @property
    def at(self) -> datetime:
        return parse_stamp(self.stamp)

    @property
    def complete(self) -> bool:
        return any(k.endswith("/" + MANIFEST) for k in self.keys)


def parse_stamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=timezone.utc)


def list_sets(target: Target, s3=None) -> list[RemoteSet]:
    """Every set under the prefix, oldest first."""
    s3 = s3 or client(target)
    sets: dict[str, RemoteSet] = {}
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=target.bucket, Prefix=target.prefix):
            for obj in page.get("Contents", []):
                rest = obj["Key"][len(target.prefix):]
                stamp, sep, _ = rest.partition("/")
                if sep and _STAMP.fullmatch(stamp):
                    sets.setdefault(stamp, RemoteSet(stamp, [])).keys.append(obj["Key"])
    except Exception as exc:  # noqa: BLE001 - boto3/botocore raise many types
        raise _error(target, "listing the bucket failed", exc) from None
    return [sets[s] for s in sorted(sets)]


def latest_good(target: Target, s3=None) -> RemoteSet | None:
    good = [s for s in list_sets(target, s3) if s.complete]
    return good[-1] if good else None


def upload_set(target: Target, recipients: list[str], stamp: str, files: Mapping[str, str], work_dir: str,
               s3=None) -> dict[str, Any]:
    """Encrypt each local backup file (kind -> path) into work_dir, upload
    it, check the stored size, then write the manifest. The local files are
    only read. Returns the manifest."""
    s3 = s3 or client(target)
    base = f"{target.prefix}{stamp}/"
    manifest: dict[str, Any] = {"format": "baler-offsite-backup/1", "stamp": stamp,
                                "encryption": "age (age-encryption.org/v1), X25519 recipients",
                                "recipients": list(recipients), "files": {}}
    for kind, path in files.items():
        name = os.path.basename(path) + ".age"
        tmp = os.path.join(work_dir, name + ".partial")
        try:
            encrypt_file(path, tmp, recipients)
            size, digest = os.path.getsize(tmp), sha256_file(tmp)
            try:
                s3.upload_file(tmp, target.bucket, base + name,
                               ExtraArgs={"ContentType": "application/octet-stream", "Metadata": {"sha256": digest}})
                head = s3.head_object(Bucket=target.bucket, Key=base + name)
            except Exception as exc:  # noqa: BLE001
                raise _error(target, f"uploading {name} failed", exc) from None
            if head.get("ContentLength") != size:
                raise OffsiteError(f"{base + name}: stored {head.get('ContentLength')} bytes, uploaded {size}")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        manifest["files"][kind] = {"name": name, "bytes": size, "sha256": digest,
                                   "plain_bytes": os.path.getsize(path)}
    manifest["uploaded_at"] = datetime.now(timezone.utc).isoformat()
    try:
        s3.put_object(Bucket=target.bucket, Key=base + MANIFEST, Body=json.dumps(manifest, indent=2).encode(),
                      ContentType="application/json")
    except Exception as exc:  # noqa: BLE001
        raise _error(target, "writing the manifest failed", exc) from None
    return manifest


def fetch_set(target: Target, identities: list[str], dest_dir: str, stamp: str | None = None,
              s3=None) -> dict[str, str]:
    """Download a complete set (the newest, or `stamp`), check each object
    against the manifest, and decrypt it into dest_dir. Returns kind -> the
    decrypted .gz path, as scripts/backup.py names them."""
    s3 = s3 or client(target)
    if stamp in (None, "latest"):
        found = latest_good(target, s3)
        if found is None:
            raise OffsiteError(f"no complete backup set in {target.describe()}")
        stamp = found.stamp
    base = f"{target.prefix}{stamp}/"
    try:
        manifest = json.loads(s3.get_object(Bucket=target.bucket, Key=base + MANIFEST)["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise _error(target, f"reading {base + MANIFEST} failed (is set {stamp} complete?)", exc) from None
    out = {}
    for kind, meta in manifest["files"].items():
        name = meta["name"]
        enc = os.path.join(dest_dir, name)
        try:
            s3.download_file(target.bucket, base + name, enc)
        except Exception as exc:  # noqa: BLE001
            raise _error(target, f"downloading {name} failed", exc) from None
        if sha256_file(enc) != meta["sha256"]:
            raise OffsiteError(f"{base + name} doesn't match its manifest's SHA-256: damaged or replaced")
        plain = os.path.join(dest_dir, name.removesuffix(".age"))
        decrypt_file(enc, plain, identities)
        os.remove(enc)
        out[kind] = plain
    return out


# --- retention ----------------------------------------------------------------


def select_keep(stamps: Iterable[datetime], daily: int, weekly: int) -> set[datetime]:
    """Which backups a daily/weekly policy keeps: the newest of each of the
    `daily` most recent UTC days that have one, the newest of each of the
    `weekly` most recent ISO weeks that have one, and always the newest of
    all, whatever the numbers say."""
    ordered = sorted(set(stamps), reverse=True)
    keep: set[datetime] = set(ordered[:1])
    days: set = set()
    weeks: set = set()
    for ts in ordered:
        day, week = ts.date(), ts.isocalendar()[:2]
        if day not in days and len(days) < daily:
            days.add(day)
            keep.add(ts)
        if week not in weeks and len(weeks) < weekly:
            weeks.add(week)
            keep.add(ts)
    return keep


def prune(target: Target, daily: int, weekly: int, s3=None) -> dict[str, Any]:
    """Apply the policy to the complete sets in the bucket. Incomplete sets
    (an upload that died before its manifest) older than the newest
    complete one are removed too; nothing is removed while there is no
    complete set at all."""
    s3 = s3 or client(target)
    sets = list_sets(target, s3)
    good = [s for s in sets if s.complete]
    if not good:
        return {"kept": [], "removed": []}
    newest = good[-1]
    keep = select_keep([s.at for s in good], daily, weekly)
    doomed = [s for s in sets if (s.complete and s.at not in keep) or (not s.complete and s.at < newest.at)]
    assert newest not in doomed  # select_keep always keeps the newest; never delete the newest good copy
    for s in doomed:
        try:
            # 1000 keys per DeleteObjects call is S3's limit; a set has 3.
            resp = s3.delete_objects(Bucket=target.bucket, Delete={"Objects": [{"Key": k} for k in s.keys],
                                                                    "Quiet": True})
        except Exception as exc:  # noqa: BLE001
            raise _error(target, f"removing set {s.stamp} failed", exc) from None
        if resp.get("Errors"):
            raise OffsiteError(f"removing set {s.stamp}: {resp['Errors'][0].get('Message', resp['Errors'][0])}")
    return {"kept": [s.stamp for s in good if s.at in keep], "removed": [s.stamp for s in doomed]}


# --- local files --------------------------------------------------------------


def local_sets(out_dir: str) -> dict[str, dict[str, str]]:
    """stamp -> kind -> path of scripts/backup.py's files in out_dir."""
    out: dict[str, dict[str, str]] = {}
    try:
        names = os.listdir(out_dir)
    except FileNotFoundError:
        return out
    for name in names:
        for kind, ext in KINDS.items():
            m = re.fullmatch(rf"{kind}-(\d{{8}}T\d{{6}}Z){re.escape(ext)}", name)
            if m:
                out.setdefault(m.group(1), {})[kind] = os.path.join(out_dir, name)
    return out


def prune_local(out_dir: str, daily: int, weekly: int) -> dict[str, Any]:
    """The same policy over local sets; a set counts as good when its
    Postgres dump is there (backup.py writes it first)."""
    sets = local_sets(out_dir)
    good = {s: files for s, files in sets.items() if "postgres" in files}
    if not good:
        return {"kept": [], "removed": []}
    keep = select_keep([parse_stamp(s) for s in good], daily, weekly)
    newest = max(good)
    removed = []
    for stamp, files in sorted(sets.items()):
        if stamp == newest or (stamp in good and parse_stamp(stamp) in keep) or (stamp not in good and stamp > newest):
            continue
        for path in files.values():
            os.remove(path)
        removed.append(stamp)
    return {"kept": sorted(s for s in good if parse_stamp(s) in keep), "removed": removed}


class WorkDir:
    """A private temporary directory (0700) for downloads and decryption,
    removed afterwards; under `parent` when given (it needs room for one
    backup set, twice over while decrypting)."""

    def __init__(self, parent: str | None = None, prefix: str = "baler-offsite-"):
        self.path = tempfile.mkdtemp(prefix=prefix, dir=parent)

    def __enter__(self) -> str:
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
