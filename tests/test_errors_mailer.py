"""baler.errors (process starts / unhandled exception counts) and
baler.mailer, without a database or mail server (#277)."""

import smtplib
import threading

import pytest

from baler import errors, mailer


@pytest.fixture
def captured(monkeypatch):
    writes = []
    monkeypatch.setattr(errors, "_write", lambda sql, params: writes.append((sql, params)))
    monkeypatch.setattr(errors, "_service", None)
    return writes


def _raise(exc):
    try:
        raise exc
    except Exception as e:  # noqa: BLE001
        return e


def test_describe_keeps_type_message_and_location_only():
    info = errors.describe(_raise(ValueError("x" * 2000)))
    assert info["type"] == "ValueError"
    assert len(info["message"]) <= errors.MAX_MESSAGE + 3
    assert info["frames"] and set(info["frames"][0]) == {"file", "line", "function"}
    assert "/" not in info["frames"][0]["file"]


def test_record_counts_against_the_installed_service(captured, monkeypatch):
    errors.record(_raise(RuntimeError("boom")), wait=True)
    assert captured == []  # no service named: nothing to count against
    monkeypatch.setattr(errors, "_service", "baler-ops-api")
    errors.record(_raise(RuntimeError("boom")), wait=True)
    sql, params = captured[0]
    assert "errors = service_runtime.errors + 1" in sql
    assert params[0] == "baler-ops-api" and params[2] == "RuntimeError: boom"


def test_record_posts_to_the_tracking_url(captured, monkeypatch):
    sent = []
    monkeypatch.setenv("BALER_ERROR_TRACKING_URL", "https://errors.example/in")
    monkeypatch.setattr(errors, "_service", "baler-fault-detector")
    import baler.webhooks as wh

    monkeypatch.setattr(wh, "deliver", lambda url, secret, payload, **kw: sent.append((url, payload, kw)) or (True, None))
    errors.record(_raise(KeyError("k")), wait=True)
    url, payload, kw = sent[0]
    assert url == "https://errors.example/in" and payload["service"] == "baler-fault-detector"
    assert payload["type"] == "KeyError" and kw["allow_private"] is False


def test_install_records_start_and_chains_thread_hook(captured, monkeypatch):
    monkeypatch.setattr(errors, "_installed", False)
    monkeypatch.delenv("BALER_ERRORS_DISABLED", raising=False)
    prev_sys, prev_thread = errors.sys.excepthook, threading.excepthook
    seen = []
    monkeypatch.setattr(threading, "excepthook", lambda args: seen.append(type(args.exc_value).__name__))
    monkeypatch.setattr(errors.sys, "excepthook", prev_sys)
    try:
        errors.install("baler-test")
        for t in threading.enumerate():
            if t.name == "errors-record":
                t.join(5)
        assert "service_runtime.starts + 1" in captured[0][0]

        def bad():
            raise ZeroDivisionError("x")

        t = threading.Thread(target=bad)
        t.start()
        t.join()
        for t in threading.enumerate():
            if t.name == "errors-record":
                t.join(5)
        assert seen == ["ZeroDivisionError"]
        assert any("errors = service_runtime.errors + 1" in sql for sql, _ in captured)
    finally:
        threading.excepthook = prev_thread
        errors.sys.excepthook = prev_sys


def test_summary_without_the_table_is_empty():
    class Conn:
        def execute(self, *a):
            raise RuntimeError("relation service_runtime does not exist")

    assert errors.summary(Conn()) == [] and errors.recent_starts(Conn()) == {}


# --------------------------------------------------------------------- mailer


def test_mailer_not_configured(monkeypatch):
    monkeypatch.delenv("BALER_SMTP_HOST", raising=False)
    assert not mailer.configured()
    with pytest.raises(mailer.MailError, match="not configured"):
        mailer.send(["a@example.org"], "s", "b")


def test_mailer_refuses_header_injection():
    with pytest.raises(mailer.MailError):
        mailer.build(["a@example.org"], "hi\r\nBcc: x@example.org", "b", sender="baler@example.org")


def test_mailer_starttls_login_send(monkeypatch):
    monkeypatch.setenv("BALER_SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("BALER_SMTP_FROM", "baler@example.org")
    monkeypatch.setenv("BALER_SMTP_USER", "u")
    monkeypatch.setenv("BALER_SMTP_PASSWORD", "p")
    monkeypatch.delenv("BALER_SMTP_TLS", raising=False)
    monkeypatch.delenv("BALER_SMTP_PORT", raising=False)
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            calls.append(("starttls",))

        def login(self, u, p):
            calls.append(("login", u))

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg["Subject"]))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    mailer.send(["ops@example.org"], "[x] FIRING", "body")
    assert calls == [("connect", "smtp.example.org", 587), ("starttls",), ("login", "u"),
                     ("send", "ops@example.org", "[x] FIRING")]

    class Refusing(FakeSMTP):
        def send_message(self, msg):
            raise smtplib.SMTPRecipientsRefused({})

    monkeypatch.setattr(smtplib, "SMTP", Refusing)
    with pytest.raises(mailer.MailError):
        mailer.send(["ops@example.org"], "s", "b")
