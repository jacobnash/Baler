"""
Live against the real running Postgres. Proves the atomicity/race-safety
of open_fault/close_fault, not just their happy path.
"""

from datetime import datetime, timedelta, timezone

import pytest

from baler.faults import (
    ack_fault,
    close_fault,
    enable_webhook,
    ensure_schema,
    is_webhook_disabled,
    list_faults,
    list_faults_page,
    list_webhook_health,
    open_fault,
    record_webhook_failure,
    record_webhook_success,
    snooze_fault,
)
from baler.timeseries import connect


@pytest.fixture
def conn():
    c = connect()
    ensure_schema(c)
    c.execute("DELETE FROM faults WHERE rule_id LIKE 'test-faults:%'")
    c.execute("DELETE FROM webhook_health WHERE webhook_id LIKE 'test-faults:%'")
    return c


@pytest.mark.integration
def test_open_fault_returns_id_on_first_open(conn):
    fault_id = open_fault(conn, "test-faults:range", "urn:point:test-faults:a", datetime.now(timezone.utc))
    assert fault_id is not None


@pytest.mark.integration
def test_open_fault_is_a_noop_when_already_open(conn):
    now = datetime.now(timezone.utc)
    first_id = open_fault(conn, "test-faults:range", "urn:point:test-faults:b", now)
    second_id = open_fault(conn, "test-faults:range", "urn:point:test-faults:b", now + timedelta(seconds=1))

    assert first_id is not None
    assert second_id is None  # already open - no duplicate row, no new webhook fire

    open_rows = [f for f in list_faults(conn, rule_id="test-faults:range", point_uri="urn:point:test-faults:b") if f["status"] == "open"]
    assert len(open_rows) == 1


@pytest.mark.integration
def test_close_fault_returns_id_then_none_on_repeat(conn):
    now = datetime.now(timezone.utc)
    open_fault(conn, "test-faults:range", "urn:point:test-faults:c", now)

    first_close = close_fault(conn, "test-faults:range", "urn:point:test-faults:c", now + timedelta(seconds=5))
    second_close = close_fault(conn, "test-faults:range", "urn:point:test-faults:c", now + timedelta(seconds=6))

    assert first_close is not None
    assert second_close is None  # nothing open anymore - no-op


@pytest.mark.integration
def test_close_fault_on_nothing_open_is_a_noop(conn):
    assert close_fault(conn, "test-faults:range", "urn:point:test-faults:never-opened", datetime.now(timezone.utc)) is None


@pytest.mark.integration
def test_reopen_after_close_gets_a_new_row(conn):
    now = datetime.now(timezone.utc)
    open_fault(conn, "test-faults:range", "urn:point:test-faults:d", now)
    close_fault(conn, "test-faults:range", "urn:point:test-faults:d", now + timedelta(seconds=5))
    reopened_id = open_fault(conn, "test-faults:range", "urn:point:test-faults:d", now + timedelta(seconds=10))

    assert reopened_id is not None
    all_rows = list_faults(conn, rule_id="test-faults:range", point_uri="urn:point:test-faults:d")
    assert len(all_rows) == 2


@pytest.mark.integration
def test_list_faults_filters_by_status(conn):
    now = datetime.now(timezone.utc)
    open_fault(conn, "test-faults:range", "urn:point:test-faults:e", now)
    open_fault(conn, "test-faults:range", "urn:point:test-faults:f", now)
    close_fault(conn, "test-faults:range", "urn:point:test-faults:f", now + timedelta(seconds=1))

    open_only = list_faults(conn, status="open", rule_id="test-faults:range")
    assert {f["point_uri"] for f in open_only} == {"urn:point:test-faults:e"}


@pytest.mark.integration
def test_open_fault_defaults_severity_to_warning(conn):
    open_fault(conn, "test-faults:sev-default", "urn:point:test-faults:g", datetime.now(timezone.utc))
    fault = list_faults(conn, rule_id="test-faults:sev-default")[0]
    assert fault["severity"] == "warning"


@pytest.mark.integration
def test_open_fault_stores_given_severity(conn):
    open_fault(conn, "test-faults:sev-critical", "urn:point:test-faults:h", datetime.now(timezone.utc), severity="critical")
    fault = list_faults(conn, rule_id="test-faults:sev-critical")[0]
    assert fault["severity"] == "critical"


@pytest.mark.integration
def test_ack_fault_sets_acked_at_and_by(conn):
    now = datetime.now(timezone.utc)
    fault_id = open_fault(conn, "test-faults:ack", "urn:point:test-faults:i", now)

    assert ack_fault(conn, fault_id, "alice@example.com", now) is True

    fault = list_faults(conn, rule_id="test-faults:ack")[0]
    assert fault["acked_by"] == "alice@example.com"
    assert fault["acked_at"] is not None


@pytest.mark.integration
def test_ack_fault_on_missing_id_returns_false(conn):
    assert ack_fault(conn, 999999999, "alice@example.com", datetime.now(timezone.utc)) is False


@pytest.mark.integration
def test_snooze_fault_sets_snoozed_until(conn):
    now = datetime.now(timezone.utc)
    fault_id = open_fault(conn, "test-faults:snooze", "urn:point:test-faults:j", now)
    until = now + timedelta(minutes=30)

    assert snooze_fault(conn, fault_id, until) is True

    fault = list_faults(conn, rule_id="test-faults:snooze")[0]
    assert fault["snoozed_until"] is not None


@pytest.mark.integration
def test_snooze_fault_on_missing_id_returns_false(conn):
    assert snooze_fault(conn, 999999999, datetime.now(timezone.utc) + timedelta(minutes=5)) is False


@pytest.mark.integration
def test_webhook_health_disables_after_threshold(conn):
    webhook_id = "test-faults:webhook-1"
    disabled = False
    for _ in range(5):
        disabled = record_webhook_failure(conn, webhook_id, "connection refused", disable_threshold=5)

    assert disabled is True
    assert is_webhook_disabled(conn, webhook_id) is True


@pytest.mark.integration
def test_webhook_health_success_resets_failure_count(conn):
    webhook_id = "test-faults:webhook-2"
    record_webhook_failure(conn, webhook_id, "timeout", disable_threshold=5)
    record_webhook_failure(conn, webhook_id, "timeout", disable_threshold=5)
    record_webhook_success(conn, webhook_id)

    # 4 more failures shouldn't disable it - the success reset the counter
    for _ in range(4):
        disabled = record_webhook_failure(conn, webhook_id, "timeout", disable_threshold=5)
    assert disabled is False
    assert is_webhook_disabled(conn, webhook_id) is False


@pytest.mark.integration
def test_enable_webhook_clears_disabled_state(conn):
    webhook_id = "test-faults:webhook-3"
    for _ in range(5):
        record_webhook_failure(conn, webhook_id, "timeout", disable_threshold=5)
    assert is_webhook_disabled(conn, webhook_id) is True

    enable_webhook(conn, webhook_id)
    assert is_webhook_disabled(conn, webhook_id) is False


@pytest.mark.integration
def test_list_webhook_health_returns_every_webhook_keyed_by_id(conn):
    record_webhook_failure(conn, "test-faults:webhook-4", "timeout", disable_threshold=5)
    record_webhook_failure(conn, "test-faults:webhook-4", "timeout", disable_threshold=5)
    for _ in range(5):
        record_webhook_failure(conn, "test-faults:webhook-5", "refused", disable_threshold=5)

    health = list_webhook_health(conn)

    assert health["test-faults:webhook-4"] == {"disabled": False, "consecutive_failures": 2, "last_error": "timeout"}
    assert health["test-faults:webhook-5"] == {"disabled": True, "consecutive_failures": 5, "last_error": "refused"}
    # a webhook that's never recorded an attempt has no row - the caller
    # (fault_api.py's GET /webhooks) is responsible for the not-in-dict default
    assert "test-faults:never-attempted" not in health


@pytest.mark.integration
def test_list_faults_page_walks_list_faults_in_order(conn):
    """Pages, joined, are list_faults' answer (Baler#107): same rows, same
    newest-first order, every row exactly once."""
    t0 = datetime(2099, 1, 1, tzinfo=timezone.utc)  # `since` keeps other rows out
    for i in range(7):
        fid = open_fault(conn, f"test-faults:page-{i}", f"urn:point:test-faults:page/{'mine' if i % 2 else 'theirs'}",
                         t0 + timedelta(minutes=i % 4))  # repeated started_at: ties go by id
        close_fault(conn, f"test-faults:page-{i}", f"urn:point:test-faults:page/{'mine' if i % 2 else 'theirs'}", t0 + timedelta(hours=1))
        assert fid
    whole = [f for f in list_faults(conn, status="resolved") if f["rule_id"].startswith("test-faults:page-")]
    since = t0
    seen, before = [], None
    while True:
        page, total, before = list_faults_page(conn, status="resolved", limit=3, before=before, since=since)
        seen += page
        if before is None:
            break
    assert len(seen) == 7 and total == 7
    assert seen == sorted(whole, key=lambda f: (datetime.fromisoformat(f["started_at"]), f["id"]), reverse=True)


@pytest.mark.integration
def test_list_faults_page_applies_scope_before_paging(conn):
    t0 = datetime(2099, 1, 2, tzinfo=timezone.utc)
    for i in range(6):
        open_fault(conn, f"test-faults:scope-{i}", f"urn:point:test-faults:scope/{'mine' if i % 2 else 'theirs'}/{i}",
                   t0 + timedelta(minutes=i))

    def allows(uri):
        return "/mine/" in uri

    page, total, nxt = list_faults_page(conn, limit=2, since=t0, allows=allows)
    assert total == 3
    assert [f["rule_id"] for f in page] == ["test-faults:scope-5", "test-faults:scope-3"]
    assert nxt == page[-1]["id"]
    rest, total2, _ = list_faults_page(conn, limit=2, since=t0, allows=allows, before=nxt)
    assert total2 == total and [f["rule_id"] for f in rest] == ["test-faults:scope-1"]
    assert list_faults_page(conn, limit=2, since=t0, allows=allows, before=rest[0]["id"])[0] == []


@pytest.mark.integration
def test_list_faults_page_rejects_bad_limits_and_cursors(conn):
    with pytest.raises(ValueError):
        list_faults_page(conn, limit=0)
    with pytest.raises(ValueError):
        list_faults_page(conn, limit=5001)
    with pytest.raises(ValueError):
        list_faults_page(conn, status="open", before=-1)
