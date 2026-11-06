"""Edge-node broker accounts (Baler#36): the ACLs each account gets, the
Postgres side with a fake control channel, and - integration - real
isolation on the compose stack's mosquitto (dynamic-security plugin)."""

import os
import threading
import time
import uuid

import pytest

from baler import broker_accounts as ba


class FakeBroker:
    def __init__(self, fail: str | None = None):
        self.batches: list[list[dict]] = []
        self.fail = fail

    def run(self, commands, tolerate_missing=False):
        self.batches.append(commands)
        if self.fail:
            raise ba.BrokerError(self.fail)
        return [{"command": c["command"]} for c in commands]


def _allowed(acls, acltype, topic):
    """Evaluate acls the way the plugin does: highest priority first, first match wins."""
    def match(pattern, t):
        pp, tt = pattern.split("/"), t.split("/")
        for i, p in enumerate(pp):
            if p == "#":
                return True
            if i >= len(tt) or (p != "+" and p != tt[i]):
                return False
        return len(pp) == len(tt)
    for a in sorted((a for a in acls if a["acltype"] == acltype), key=lambda a: -a["priority"]):
        if match(a["topic"], topic):
            return a["allow"]
    return False


def test_acls_publish_only_under_own_prefixes_and_own_command_channel():
    acls = ba.acls_for("pi1", ["fbf/hospital-a", "fbf/annex/"])
    pub = lambda t: _allowed(acls, "publishClientSend", t)
    assert pub("fbf/hospital-a/ahu-1/sat")
    assert pub("fbf/hospital-a")
    assert pub("fbf/annex/x")
    assert pub("cmd/pi1/reply")
    assert not pub("fbf/hospital-b/x")
    assert not pub("fbf/hospital-ab/x")  # a prefix is a whole topic level, not a string prefix
    assert not pub("cmd/pi2/x")
    assert not pub("$CONTROL/dynamic-security/v1")
    sub = lambda t: _allowed(acls, "subscribePattern", t)
    assert sub("cmd/pi1/#")
    assert not sub("fbf/#")
    assert not sub("#")
    assert not sub("cmd/pi2/#")
    # explicit denies, outranked by the allows (see acls_for's docstring)
    denies = [a for a in acls if not a["allow"]]
    assert {a["topic"] for a in denies} == {"#"}
    assert max(a["priority"] for a in denies) < min(a["priority"] for a in acls if a["allow"])


@pytest.mark.parametrize("name", ["ab", "Pi1", "-pi", "pi 1", "pi/1", "pi+1", "a" * 64, "baler", "admin"])
def test_bad_or_reserved_usernames_are_refused_before_touching_anything(name):
    broker = FakeBroker()
    with pytest.raises(ValueError):
        ba.create_account(None, broker, "org", "site", name)
    assert broker.batches == []


def test_connect_info_from_env(monkeypatch):
    monkeypatch.setenv("BALER_MQTT_PUBLIC_HOST", "mqtt.example.com")
    monkeypatch.setenv("BALER_MQTT_PUBLIC_TLS_PORT", "8883")
    monkeypatch.delenv("BALER_MQTT_PUBLIC_PORT", raising=False)
    assert ba.connect_info() == {"host": "mqtt.example.com", "port": 1883, "tls_port": 8883}


# --- Postgres (integration) --------------------------------------------------


@pytest.fixture
def org_site():
    from baler import auth
    from baler.timeseries import connect

    conn = connect()
    auth.ensure_schema(conn)
    ba.ensure_schema(conn)
    tag = uuid.uuid4().hex[:8]
    org_id = f"org-bt-{tag}"
    conn.execute("INSERT INTO orgs (id, name, created_at) VALUES (%s, %s, now())", (org_id, f"Broker Test Org {tag}"))
    site = auth.create_site(conn, org_id, f"Site {tag}", [f"fbf/bt-{tag}"])
    empty = auth.create_site(conn, org_id, f"Empty {tag}", [])
    yield conn, org_id, site, empty, tag
    conn.execute("DELETE FROM broker_accounts WHERE org_id = %s", (org_id,))
    conn.execute("DELETE FROM sites WHERE org_id = %s", (org_id,))
    conn.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
    conn.close()


@pytest.mark.integration
def test_create_rotate_revoke_round_trip(org_site):
    conn, org_id, site, _, tag = org_site
    broker = FakeBroker()
    acct, password = ba.create_account(conn, broker, org_id, site["id"], f"pi-{tag}", "roof AHUs")
    assert len(password) >= 24 and acct["username"] == f"pi-{tag}" and acct["rotated_at"] is None
    cmds = [c["command"] for c in broker.batches[-1]]
    assert cmds == ["createRole", "createClient"]
    assert broker.batches[-1][1]["password"] == password
    assert password not in str(ba.list_accounts(conn, org_id))  # never stored
    assert [a["id"] for a in ba.list_accounts(conn, org_id)] == [acct["id"]]

    with pytest.raises(ValueError, match="taken"):
        ba.create_account(conn, broker, org_id, site["id"], f"pi-{tag}")

    acct2, pw2 = ba.rotate_password(conn, broker, org_id, acct["id"])
    assert pw2 != password and acct2["rotated_at"] is not None
    assert broker.batches[-1] == [{"command": "setClientPassword", "username": f"pi-{tag}", "password": pw2}]

    with pytest.raises(LookupError):
        ba.rotate_password(conn, broker, "another-org", acct["id"])  # not yours
    ba.revoke(conn, broker, org_id, acct["id"])
    assert ba.list_accounts(conn, org_id) == []
    assert [c["command"] for c in broker.batches[-1]] == ["deleteClient", "deleteRole"]
    with pytest.raises(LookupError):
        ba.revoke(conn, broker, org_id, acct["id"])


@pytest.mark.integration
def test_site_without_prefix_or_unknown_site_is_refused(org_site):
    conn, org_id, _, empty, tag = org_site
    with pytest.raises(ValueError, match="topic prefix"):
        ba.create_account(conn, FakeBroker(), org_id, empty["id"], f"pe-{tag}")
    with pytest.raises(LookupError):
        ba.create_account(conn, FakeBroker(), org_id, "no-such-site", f"pe-{tag}")


@pytest.mark.integration
def test_broker_failure_leaves_no_row_and_no_change(org_site):
    conn, org_id, site, _, tag = org_site
    with pytest.raises(ba.BrokerError):
        ba.create_account(conn, FakeBroker(fail="down"), org_id, site["id"], f"pf-{tag}")
    assert ba.list_accounts(conn, org_id) == []
    acct, _ = ba.create_account(conn, FakeBroker(), org_id, site["id"], f"pf-{tag}")
    with pytest.raises(ba.BrokerError):
        ba.revoke(conn, FakeBroker(fail="down"), org_id, acct["id"])
    assert [a["id"] for a in ba.list_accounts(conn, org_id)] == [acct["id"]]
    with pytest.raises(ba.BrokerError):
        ba.rotate_password(conn, FakeBroker(fail="down"), org_id, acct["id"])
    assert ba.get_account(conn, org_id, acct["id"])["rotated_at"] is None


@pytest.mark.integration
def test_reapply_site_rebuilds_each_accounts_role(org_site):
    from baler import auth

    conn, org_id, site, _, tag = org_site
    broker = FakeBroker()
    ba.create_account(conn, broker, org_id, site["id"], f"pr-{tag}")
    auth.set_site_prefixes(conn, org_id, site["id"], [f"fbf/new-{tag}"])
    assert ba.reapply_site(conn, broker, org_id, site["id"]) == 1
    batch = broker.batches[-1]
    assert [c["command"] for c in batch] == ["deleteRole", "createRole", "addClientRole"]
    topics = {a["topic"] for a in batch[1]["acls"] if a["allow"]}
    assert f"fbf/new-{tag}/#" in topics and f"fbf/bt-{tag}/#" not in topics


# --- real broker (integration) -------------------------------------------------


def _mqtt(user, password):
    import paho.mqtt.client as mqtt

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"t-{uuid.uuid4().hex[:8]}")
    c.username_pw_set(user, password)
    return c


@pytest.mark.integration
def test_real_broker_enforces_site_isolation():
    host = os.environ.get("MQTT_HOST", "localhost")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    broker = ba.DynsecClient(host=host, port=port)
    tag = uuid.uuid4().hex[:8]
    user, prefix = f"it-{tag}", f"fbf/it-{tag}"
    password = "pw-" + uuid.uuid4().hex
    broker.run(ba._role_commands(user, [prefix]) + [
        {"command": "createClient", "username": user, "password": password, "roles": [{"rolename": ba.role_name(user)}]}])
    try:
        got: list[str] = []
        backend = _mqtt(broker.username, broker.password)
        backend.on_message = lambda c, u, m: got.append(m.topic)
        subscribed = threading.Event()
        backend.on_subscribe = lambda *a, **k: subscribed.set()
        backend.connect(host, port)
        backend.loop_start()
        backend.subscribe(f"fbf/it-{tag}/#")
        backend.subscribe(f"fbf/other-{tag}/#")
        assert subscribed.wait(5)
        time.sleep(0.3)

        node = _mqtt(user, password)
        node_got: list[str] = []
        node.on_message = lambda c, u, m: node_got.append(m.topic)
        node_subacks: list = []
        node.on_subscribe = lambda c, u, mid, codes, p=None: node_subacks.extend(codes)
        assert node.connect(host, port) == 0
        node.loop_start()
        node.subscribe([(f"cmd/{user}/#", 0), ("fbf/#", 0)])
        node.publish(f"{prefix}/ahu/sat", "1", qos=1).wait_for_publish(5)
        node.publish(f"fbf/other-{tag}/ahu/sat", "1", qos=1).wait_for_publish(5)
        time.sleep(0.5)
        backend.publish(f"cmd/{user}/write", "x", qos=1).wait_for_publish(5)
        backend.publish(f"fbf/it-{tag}/not-for-node", "x", qos=1).wait_for_publish(5)
        time.sleep(1.0)
        assert f"{prefix}/ahu/sat" in got
        assert f"fbf/other-{tag}/ahu/sat" not in got  # the other site's topic never arrived
        assert node_got == [f"cmd/{user}/write"]  # its command channel only
        assert [getattr(c, "value", c) for c in node_subacks][1] >= 0x80  # fbf/# refused

        # rotate: old password refused
        broker.run([{"command": "setClientPassword", "username": user, "password": password + "2"}])
        old = _mqtt(user, password)
        refused = threading.Event()
        old.on_connect = lambda c, u, f, rc, p=None: rc != 0 and refused.set()
        old.connect(host, port)
        old.loop_start()
        assert refused.wait(5)
        old.loop_stop()
        node.loop_stop()
        backend.loop_stop()
        for c in (node, backend, old):
            c.disconnect()
    finally:
        broker.run([{"command": "deleteClient", "username": user},
                    {"command": "deleteRole", "rolename": ba.role_name(user)}], tolerate_missing=True)
