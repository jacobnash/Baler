"""Backup and restore-drill reporting (#276, #196) and the drill's scratch
guard, without docker: what /ops/status makes of the reports, how they're
written, the alert hook, and that the drill never treats a stack it didn't
create as scratch (an earlier drill once removed a non-scratch volume)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from baler import backup_status, ops_status

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("restore_drill_guard", os.path.join(REPO, "scripts", "restore_drill.py"))
assert _spec and _spec.loader
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


GOOD = {"ok": True, "local": {"ok": True, "files": {"postgres": {"bytes": 2_000_000}, "oxigraph": {"bytes": 500_000}}},
        "offsite": {"enabled": False}}


# --- /ops/status --------------------------------------------------------------------


def test_backup_card_states():
    assert ops_status.backup_state(None)[0] == "off"
    state, detail = ops_status.backup_state({**GOOD, "last_ok_at": _ago(hours=3)})
    assert state == "ok" and "2.5 MB" in detail and "no off-site copy" in detail
    assert ops_status.backup_state({**GOOD, "last_ok_at": _ago(hours=50)})[0] == "degraded"
    state, detail = ops_status.backup_state({"local": {"ok": False, "error": "pg_dump: connection refused"},
                                             "last_ok_at": _ago(days=2), "offsite": {"enabled": False}})
    assert state == "down" and "connection refused" in detail
    up = {**GOOD, "last_ok_at": _ago(hours=1), "offsite": {"enabled": True, "ok": True, "kept": 9}}
    assert ops_status.backup_state(up) == ("ok", ops_status.backup_state(up)[1]) and "9 kept" in ops_status.backup_state(up)[1]
    failed = {**GOOD, "last_ok_at": _ago(hours=1),
              "offsite": {"enabled": True, "ok": False, "error": "AccessDenied", "last_ok_at": _ago(days=1)}}
    state, detail = ops_status.backup_state(failed)
    assert state == "degraded" and "AccessDenied" in detail and "local backup OK" in detail


def test_drill_card_states():
    assert ops_status.drill_state(None)[0] == "off"
    ok = {"ok": True, "finished_at": _ago(days=1), "backup": "postgres-x.sql.gz", "source": "local", "checks": 15}
    assert ops_status.drill_state(ok)[0] == "ok"
    assert ops_status.drill_state({**ok, "finished_at": _ago(days=9)})[0] == "degraded"
    state, detail = ops_status.drill_state({**ok, "ok": False, "failed": ["point_history samples: dump 5, restored 4"]})
    assert state == "degraded" and "restored 4" in detail


def _status(building_extra):
    building = {"last_ingest_at": None, "points_seen_last_hour": 0, "open_faults": 0, "disabled_derivation_targets": 0,
                "org_count": 1, **building_extra}
    probes = {c["id"]: {"id": c["id"], "label": c["label"], "layer": c["layer"], "service": c["service"],
                        "state": "ok", "detail": None}
              for c in ops_status.COMPONENTS if not c["probe"].startswith("inferred_")}
    beats = {s: datetime.now(timezone.utc).isoformat() for s in ("fault_detector", "derivation_engine")}
    with patch("baler.ops_status._building_signals", return_value={**building, "heartbeats": beats,
                                                                    "last_ingest_at": datetime.now(timezone.utc).isoformat()}), \
            patch("baler.ops_status._run_probes", return_value=probes), \
            patch("baler.ops_status._probes.latest", return_value=None):
        return ops_status.collect_status()


def test_never_reported_is_off_and_not_a_problem():
    doc = _status({})
    cards = {c["id"]: c for c in doc["jobs"]}
    assert cards["backups"]["state"] == "off" and cards["restore_drill"]["state"] == "off"
    assert doc["summary"] == "ok" and not doc["attention"]


def test_an_offsite_failure_shows_and_needs_attention():
    doc = _status({"backup": {**GOOD, "last_ok_at": _ago(hours=1),
                              "offsite": {"enabled": True, "ok": False, "error": "endpoint unreachable"}}})
    card = next(c for c in doc["jobs"] if c["id"] == "backups")
    assert card["state"] == "degraded" and "endpoint unreachable" in card["detail"]
    assert doc["summary"] == "degraded" and card in doc["attention"]


# --- writing the reports ------------------------------------------------------------


def test_record_passes_the_detail_as_a_psql_variable():
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("baler.backup_status.subprocess.run", side_effect=fake_run):
        assert backup_status.record("backup", {"note": "it's \"quoted\"; DROP TABLE x"}, "/repo")
    cmd, kw = calls[0]
    assert cmd[:6] == ["docker", "compose", "exec", "-T", "postgres", "sh"]
    assert cmd[-2] == "backup" and json.loads(cmd[-1])["note"].endswith("DROP TABLE x")
    assert ":'detail'::jsonb" in kw["input"] and "DROP" not in kw["input"]  # the value never enters the SQL text
    assert kw["cwd"] == "/repo"


def test_record_failure_is_a_warning(capsys):
    with patch("baler.backup_status.subprocess.run",
               return_value=subprocess.CompletedProcess([], 1, "", "service \"postgres\" is not running")):
        assert backup_status.record("backup", {}, "/repo") is False
    assert "not running" in capsys.readouterr().out


def test_alert_hook_gets_the_report_but_not_the_bucket_credentials(tmp_path, monkeypatch):
    out = tmp_path / "alert.json"
    script = tmp_path / "alert.sh"
    script.write_text(f'#!/bin/sh\n{{ echo "$BALER_ALERT_EVENT"; env; cat; }} > {out}\n')
    script.chmod(0o755)
    monkeypatch.setenv("BALER_BACKUP_ALERT_COMMAND", str(script))
    monkeypatch.setenv("BALER_BACKUP_S3_SECRET_ACCESS_KEY", "do-not-leak")
    backup_status.alert("offsite_upload_failed", {"ok": False})
    text = out.read_text()
    assert text.startswith("offsite_upload_failed") and '"ok": false' in text and "do-not-leak" not in text


def test_no_alert_command_is_a_no_op(monkeypatch):
    monkeypatch.delenv("BALER_BACKUP_ALERT_COMMAND", raising=False)
    with patch("baler.backup_status.subprocess.run") as run:
        backup_status.alert("backup_failed", {})
    run.assert_not_called()


# --- the drill's scratch guard ----------------------------------------------------


@pytest.mark.parametrize("name", ["baler", "timberdoodle", "Bad Name", ""])
def test_drill_refuses_live_and_invalid_names(name):
    with pytest.raises(rd.NotScratch):
        rd.Scratch(name)


def test_drill_refuses_the_live_project_and_prefix(monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "site-a")
    with pytest.raises(rd.NotScratch, match="live compose project"):
        rd.Scratch("site-a")
    monkeypatch.setenv("BALER_VOLUME_PREFIX", "prod")
    with pytest.raises(rd.NotScratch, match="volume prefix"):
        rd.Scratch("prod")


def _docker(containers=(), project_vols=(), named_vols=(), labels=None):
    labels = labels or {}

    def fake(*args):
        if args[0] == "ps":
            return [f"{n}\t{labels.get(n, '')}" for n in containers]
        if args[:2] == ("volume", "ls"):
            if any(a.startswith("name=") for a in args):
                return list(named_vols)
            if any(a == f"label={rd.SCRATCH_LABEL}={rd.SCRATCH_VALUE}" for a in args):
                return [v for v in project_vols if labels.get(v) == rd.SCRATCH_VALUE]
            return list(project_vols)
        if args[:2] == ("volume", "inspect"):
            return [f"{v}\t{labels.get(v, '<no value>')}" for v in args[4:]]
        raise AssertionError(args)

    return fake


def test_drill_never_takes_down_a_stack_it_did_not_create():
    """`--project baler-prod` against an existing stack (the checkout's own
    name differs, so the live-name check alone wouldn't catch it): the
    guard refuses before compose or docker volume rm runs."""
    s = rd.Scratch("baler-prod")
    fake = _docker(containers=["baler-prod-postgres-1", "baler-prod-mqtt_listener-1"],
                   project_vols=["baler-prod_postgres-data"], named_vols=["baler-prod_postgres-data", "baler-prod_app-data"])
    with patch.object(rd, "_docker_lines", side_effect=fake), patch.object(rd.Scratch, "compose") as compose, \
            patch.object(rd.subprocess, "run") as run:
        with pytest.raises(rd.NotScratch, match="no restore drill created"):
            s.down()
        with pytest.raises(rd.NotScratch):
            s.up()
    compose.assert_not_called()
    run.assert_not_called()


def test_drill_refuses_an_unlabelled_volume_with_its_prefix():
    """A volume named like the scratch stack's but not made by the drill
    (e.g. `BALER_VOLUME_PREFIX=<name>` on some other stack) is refused too."""
    s = rd.Scratch("restore-drill-x")
    fake = _docker(named_vols=["restore-drill-x_postgres-data"])
    with patch.object(rd, "_docker_lines", side_effect=fake):
        assert s.foreign() == ["volume restore-drill-x_postgres-data"]


def test_drill_removes_only_its_own_labelled_volumes():
    s = rd.Scratch("restore-drill-y")
    vols = ["restore-drill-y_postgres-data", "restore-drill-y_oxigraph-data"]
    lab = {v: rd.SCRATCH_VALUE for v in vols} | {"restore-drill-y-postgres-1": rd.SCRATCH_VALUE}
    fake = _docker(containers=["restore-drill-y-postgres-1"], project_vols=vols, named_vols=vols, labels=lab)
    with patch.object(rd, "_docker_lines", side_effect=fake), patch.object(rd.Scratch, "compose") as compose, \
            patch.object(rd.subprocess, "run") as run:
        s.down()
    compose.assert_called_once_with("down", "--remove-orphans")
    assert run.call_args[0][0] == ["docker", "volume", "rm", *vols]
