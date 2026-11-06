"""
Haystack conformance matrix: each op × wire format × target × client, as a
pass/fail table. Baler (/api through the real gateway) is measured against
Haxall, the open-source reference server, with two independent clients:
openhay (ours) and phable (not ours).

Gates: every Baler cell must pass, and so must openhay against Haxall in
the formats Haxall accepts. Everything else is reported, not gated. The
table goes to $GITHUB_STEP_SUMMARY in CI and to $MATRIX_OUT if set.

Env:
  MATRIX_TARGETS   comma list, default "baler,haxall"
  HAXALL_API       e.g. http://localhost:8280/api/sys (+ HAXALL_SU_USERNAME/_PASSWORD)
  MATRIX_REQUIRED  "1" in the CI job: a missing target fails instead of skipping

Baler's pointWrite needs a Windrow site to confirm the BACnet write; this
test plays that site on MQTT (subscribes to the command topic and answers),
so the whole gateway -> haystack_api -> broker -> site path runs.
"""

import datetime as dt
import json
import os
import time
import uuid

import phable
import pytest

pytestmark = pytest.mark.integration

TARGETS = [t.strip() for t in os.environ.get("MATRIX_TARGETS", "baler,haxall").split(",") if t.strip()]
REQUIRED = os.environ.get("MATRIX_REQUIRED") == "1"

try:
    import openhay as oh
except ImportError:
    # The general integration job doesn't install the `haystack` extra; the
    # dedicated matrix job does, and there a missing openhay must fail.
    if REQUIRED:
        raise
    pytest.skip("openhay not installed (pip install -e '.[haystack]')", allow_module_level=True)
HAXALL_API = os.environ.get("HAXALL_API", "")
HAXALL_USER = os.environ.get("HAXALL_SU_USERNAME", "su")
HAXALL_PASS = os.environ.get("HAXALL_SU_PASSWORD", "")
OPS = ["about", "read", "readById", "nav", "hisWrite+Read", "watch", "pointWrite"]
NY = oh.haystack_tz("New_York")


def _skip_or_fail(why):
    if REQUIRED:
        pytest.fail(why)
    pytest.skip(why)


# ----------------------------------------------------------------- clients


class OpenhayClient:
    def __init__(self, base, user, pw, fmt):
        self.c = oh.Client(base, user=user, password=pw, format=fmt)

    def about(self):
        return self.c.about()["productName"]

    def read(self, flt):
        return self.c.read(flt)

    def read_by_id(self, ref):
        return self.c.read_ids([ref])[0]

    def nav(self):
        return self.c.nav()

    def his_write(self, ref, rows):
        self.c.his_write(ref, rows)

    def his_read(self, ref, day):
        return [r["val"].val for r in self.c.his_read(ref, day.isoformat()).rows]

    def watch(self, ref):
        wid, rows = self.c.watch_sub("matrix", [ref], 60)
        self.c.watch_poll(wid)
        self.c.watch_close(wid)
        return rows

    def point_write(self, ref, level, val):
        self.c.point_write(ref, level, None if val is None else oh.Number(val), who="matrix")

    def level(self, ref, level):
        v = self.c.point_write_array(ref).rows[level - 1].get("val")
        return v.val if isinstance(v, oh.Number) else v


class PhableClient:
    def __init__(self, base, user, pw, fmt):
        self.ctx = phable.open_haystack_client(base, user, pw, content_type=fmt)
        self.c = self.ctx.__enter__()

    @staticmethod
    def _ref(ref):
        return phable.Ref(ref.id)

    def about(self):
        return self.c.about()["productName"]

    def read(self, flt):
        return self.c.read_all(flt).rows

    def read_by_id(self, ref):
        return self.c.read_by_id(self._ref(ref))

    nav = None  # phable has no nav
    watch = None  # nor watches

    def his_write(self, ref, rows):
        self.c.his_write_by_id(self._ref(ref), [{"ts": ts, "val": phable.Number(v)} for ts, v in rows])

    def his_read(self, ref, day):
        return [r["val"].val for r in self.c.his_read_by_id(self._ref(ref), day).rows]

    def point_write(self, ref, level, val):
        self.c.point_write(self._ref(ref), level, None if val is None else phable.Number(val), who="matrix")

    def level(self, ref, level):
        v = self.c.point_write_array(self._ref(ref)).rows[level - 1].get("val")
        return getattr(v, "val", v)


CLIENTS = [("openhay", OpenhayClient, ["zinc", "json3", "hayson"]), ("phable", PhableClient, ["zinc", "json"])]


def _run_ops(client, marker, dat, sp, day):
    """One cell per op: 'pass', '—' (client lacks the op), or 'fail: why'."""
    cells = {}

    def cell(op, fn):
        if fn is None:
            cells[op] = "—"
            return
        try:
            fn()
            cells[op] = "pass"
        except Exception as exc:  # noqa: BLE001 - every failure becomes a cell
            cells[op] = f"fail: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}"

    def _read():
        assert len(client.read(f"{marker} and point")) >= 2

    def _by_id():
        rec = client.read_by_id(dat)
        assert rec and str(getattr(rec["id"], "id", getattr(rec["id"], "val", ""))) == dat.id

    def _his():
        t1, t2 = dt.datetime.combine(day, dt.time(10), NY), dt.datetime.combine(day, dt.time(11), NY)
        client.his_write(dat, [(t1, 70.25), (t2, 71.75)])
        vals = client.his_read(dat, day)
        assert 70.25 in vals and 71.75 in vals, vals

    def _watch():
        assert len(client.watch(sp)) == 1

    def _pw():
        client.point_write(sp, 8, 72.5)
        assert client.level(sp, 8) == 72.5
        client.point_write(sp, 8, None)

    cell("about", lambda: client.about())
    cell("read", _read)
    cell("readById", _by_id)
    cell("nav", client.nav and (lambda: client.nav()))
    cell("hisWrite+Read", _his)
    cell("watch", client.watch and _watch)
    cell("pointWrite", _pw)
    return cells


# ----------------------------------------------------------------- targets


def _haxall_target():
    if not HAXALL_API or not HAXALL_PASS:
        return None
    c = oh.Client(HAXALL_API, user=HAXALL_USER, password=HAXALL_PASS, format="hayson")

    def ev(expr):
        return c.call("eval", oh.Grid.from_rows([{"expr": expr}])).rows

    for lib in ("ph", "hx.point", "hx.conn"):  # dependency order matters
        try:
            ev(f'libAdd("{lib}")')
        except oh.HaystackError as exc:
            if "already" not in str(exc).lower():
                raise
    marker = "matrixProbe" + uuid.uuid4().hex[:6]
    ev(f'commit(diff(null, {{dis:"Matrix Site", site, {marker}, tz:"New_York"}}, {{add}}))')
    ev(f'commit(diff(null, {{dis:"Matrix AHU", equip, ahu, {marker}, siteRef: read({marker} and site)->id}}, {{add}}))')
    base = f'kind:"Number", unit:"°F", tz:"New_York", {marker}, equipRef: read({marker} and equip)->id, siteRef: read({marker} and site)->id'
    ev(f'commit(diff(null, {{dis:"Matrix DAT", point, his, sensor, temp, {base}}}, {{add}}))')
    ev(f'commit(diff(null, {{dis:"Matrix SP", point, writable, sp, temp, {base}}}, {{add}}))')
    pts = c.read(f"{marker} and point")
    dat = next(r["id"] for r in pts if "his" in r)
    sp = next(r["id"] for r in pts if "writable" in r)
    return {"name": "Haxall 4.0.6", "base": HAXALL_API, "user": HAXALL_USER, "pw": HAXALL_PASS,
            "marker": marker, "dat": dat, "sp": sp, "cleanup": lambda: None}


def _baler_target():
    import paho.mqtt.client as mqtt
    import requests
    from test_gateway_auth import BASE, _post_with_retry

    from baler.timeseries import connect

    org_name = "Matrix Test Org"
    email, pw = "admin@matrix-test.invalid", "correct-horse-battery-staple"

    def cleanup():
        conn = connect()
        sub = f"SELECT id FROM orgs WHERE name LIKE '{org_name}%'"
        conn.execute(f"DELETE FROM user_sites WHERE user_id IN (SELECT id FROM users WHERE org_id IN ({sub}))")
        conn.execute(f"DELETE FROM sites WHERE org_id IN ({sub})")
        conn.execute(f"DELETE FROM users WHERE org_id IN ({sub})")
        conn.execute(f"DELETE FROM orgs WHERE name LIKE '{org_name}%'")
        conn.close()

    try:
        requests.get(f"{BASE}/api/openapi.yaml", timeout=3)
    except requests.RequestException:
        return None
    cleanup()
    r = _post_with_retry(f"{BASE}/auth/orgs", json={"name": org_name, "admin_email": email, "admin_password": pw})
    assert r.status_code == 201, r.text
    token = _post_with_retry(f"{BASE}/auth/login", json={"email": email, "password": pw}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}

    run = uuid.uuid4().hex[:6]
    marker = "matrixProbe" + run
    prefix = f"fbf/matrix-{run}/ahu"  # mqtt_listener subscribes to fbf/#
    for label, tags in (("dat", {"sensor": True, "temp": True}), ("sp", {"sp": True, "temp": True, "writable": True})):
        topic = f"{prefix}/{label}"
        r = requests.post(f"{BASE}/ingest/tags", headers=h, json={"point": topic, "tags": {marker: True, **tags}})
        assert r.status_code in (200, 204), r.text
        r = requests.post(f"{BASE}/ingest/ingest", headers=h, json={"point": topic, "value": 60.0, "ts": time.time() - 30})
        assert r.status_code == 204, r.text

    # Play the Windrow site that owns these points: route tags on the equip,
    # and answer point_write commands as a controller would.
    site, inst = f"matrix-{run}", "fake-windrow"
    m = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    m.username_pw_set(os.environ.get("MQTT_USERNAME", "baler"), os.environ.get("MQTT_PASSWORD", ""))

    def on_message(client, userdata, msg):
        cmd = json.loads(msg.payload)
        client.publish(f"cmd/{site}/{inst}/result", json.dumps({
            "ok": True, "action": cmd["action"], "id": cmd.get("id"),
            "result": {"topic": cmd["topic"], "priority": cmd["priority"], "present_value": cmd["value"]},
        }), qos=1)

    m.on_message = on_message
    m.connect(os.environ.get("MQTT_HOST", "localhost"), 1883)
    m.subscribe(f"cmd/{site}/{inst}/point-write", qos=1)
    m.loop_start()
    m.publish(f"{prefix}/equip/tags", json.dumps({"point": "equip/tags", "value": json.dumps(
        {"tags": {"windrowSite": site, "windrowInstance": inst}, "points": ["dat", "sp"]}), "ts": time.time()}),
        qos=1, retain=True)

    reader = oh.Client(f"{BASE}/api/demo", bearer=token)
    sp = dat = None
    for _ in range(60):  # mqtt_listener links the equip asynchronously
        pts = reader.read(f"{marker} and point")
        sp = next((r for r in pts if "writable" in r and "equipRef" in r), None)
        dat = next((r for r in pts if "sensor" in r), None)
        if sp and dat:
            break
        time.sleep(1)
    assert sp and dat, f"Baler never linked the matrix points to their equip: {pts}"

    def stop():
        m.publish(f"{prefix}/equip/tags", b"", retain=True)
        m.loop_stop()
        cleanup()

    return {"name": "Baler /api", "base": f"{BASE}/api/demo", "user": email, "pw": pw,
            "marker": marker, "dat": dat["id"], "sp": sp["id"], "cleanup": stop}


def test_conformance_matrix():
    factories = {"baler": _baler_target, "haxall": _haxall_target}
    targets = []
    for name in TARGETS:
        t = factories[name]()
        if t is None:
            _skip_or_fail(f"matrix target {name!r} is not reachable/configured")
        targets.append(t)

    day = dt.date(2024, 1, 2)
    results = []  # (target, client, fmt, cells)
    try:
        for t in targets:
            for cname, cls, fmts in CLIENTS:
                for fmt in fmts:
                    try:
                        client = cls(t["base"], t["user"], t["pw"], fmt)
                        cells = _run_ops(client, t["marker"], t["dat"], t["sp"], day)
                    except Exception as exc:  # noqa: BLE001 - login/setup failure fills the row
                        cells = dict.fromkeys(OPS, f"fail: login: {type(exc).__name__}: {str(exc).splitlines()[0][:100]}")
                    results.append((t["name"], cname, fmt, cells))
    finally:
        for t in targets:
            t["cleanup"]()

    table = ["| target | client | format | " + " | ".join(OPS) + " |", "|" + "---|" * (3 + len(OPS))]
    for tname, cname, fmt, cells in results:
        icons = ["✅" if v == "pass" else ("—" if v == "—" else "❌") for v in (cells[o] for o in OPS)]
        table.append(f"| {tname} | {cname} | {fmt} | " + " | ".join(icons) + " |")
    failures = [f"- {t} / {c} / {f} / {op}: {v[6:]}" for t, c, f, cells in results for op, v in cells.items() if v.startswith("fail")]
    report = "## Haystack conformance matrix\n\n" + "\n".join(table) + "\n\n" + ("### Failures\n\n" + "\n".join(failures) if failures else "No failures.") + "\n"
    print(report)
    for path in (os.environ.get("GITHUB_STEP_SUMMARY"), os.environ.get("MATRIX_OUT")):
        if path:
            with open(path, "a") as f:
                f.write(report)

    gated = [
        (t, c, f, op, v) for t, c, f, cells in results for op, v in cells.items()
        if v.startswith("fail") and (t.startswith("Baler") or (c == "openhay" and f != "json3"))
    ]
    assert not gated, "gated cells failed:\n" + "\n".join(f"{t}/{c}/{f}/{op}: {v}" for t, c, f, op, v in gated)
