"""
Unit tests for http_handler_base.py's shared HTTP-handler plumbing
(PLAN.md Stage 2.1) - no live server, no services. BaseHTTPRequestHandler
normally does all its work in __init__ (reads and dispatches the
request immediately), so these bypass __init__ via __new__ and set just
the attributes each method under test actually reads - the standard way
to unit-test a BaseHTTPRequestHandler subclass's methods in isolation.
"""

import io
import json
from email.message import Message
from unittest.mock import MagicMock

import pytest

from baler import http_handler_base
from baler.http_handler_base import BaseAPIHandler, respond_error, respond_json


def _make_handler(headers: dict | None = None, body: bytes = b"") -> BaseAPIHandler:
    handler = BaseAPIHandler.__new__(BaseAPIHandler)
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    handler.headers = msg
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    return handler


def test_respond_json_writes_status_headers_and_body():
    handler = _make_handler()

    respond_json(handler, 201, {"id": "abc"})

    handler.send_response.assert_called_once_with(201)
    handler.send_header.assert_any_call("Content-Type", "application/json")
    handler.wfile.seek(0)
    assert json.loads(handler.wfile.read()) == {"id": "abc"}


def test_respond_json_passes_through_a_default_serializer():
    """derivation_api.py needs json.dumps's own default= hook (traces can
    carry datetimes) - every other caller leaves this None/unchanged."""
    import datetime

    handler = _make_handler()

    respond_json(handler, 200, {"ts": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)}, default=str)

    handler.wfile.seek(0)
    assert json.loads(handler.wfile.read()) == {"ts": "2026-01-01 00:00:00+00:00"}


def test_respond_error_wraps_message_in_error_key():
    handler = _make_handler()

    respond_error(handler, 400, "bad request")

    handler.send_response.assert_called_once_with(400)
    handler.wfile.seek(0)
    assert json.loads(handler.wfile.read()) == {"error": "bad request"}


def test_read_json_body_parses_content_length_and_body():
    body = json.dumps({"a": 1}).encode()
    handler = _make_handler(headers={"Content-Length": str(len(body))}, body=body)

    assert handler.read_json_body() == {"a": 1}


def test_read_json_body_returns_empty_dict_when_no_content_length():
    handler = _make_handler()

    assert handler.read_json_body() == {}


def test_handle_traced_calls_fn_with_span_on_success():
    handler = _make_handler()
    handler.TRACER = http_handler_base_tracer_stub()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()

    seen = []
    handler.handle_traced("test.op", lambda span: seen.append(span))

    assert len(seen) == 1  # fn was called once, with a span object


def test_handle_traced_responds_400_for_declared_error_types():
    handler = _make_handler()
    handler.TRACER = http_handler_base_tracer_stub()

    def raises_key_error(span):
        raise KeyError("missing_field")

    handler.handle_traced("test.op", raises_key_error)

    handler.send_response.assert_called_once_with(400)
    handler.wfile.seek(0)
    assert "missing_field" in json.loads(handler.wfile.read())["error"]


def test_handle_traced_lets_undeclared_exceptions_propagate():
    handler = _make_handler()
    handler.TRACER = http_handler_base_tracer_stub()

    def raises_runtime_error(span):
        raise RuntimeError("not a declared ERROR_TYPE")

    with pytest.raises(RuntimeError):
        handler.handle_traced("test.op", raises_runtime_error)


def test_handle_traced_respects_subclass_extended_error_types():
    class CustomError(Exception):
        pass

    handler = _make_handler()
    handler.TRACER = http_handler_base_tracer_stub()
    handler.ERROR_TYPES = (*BaseAPIHandler.ERROR_TYPES, CustomError)

    def raises_custom(span):
        raise CustomError("sandbox-style error")

    handler.handle_traced("test.op", raises_custom)

    handler.send_response.assert_called_once_with(400)


def test_serve_openapi_spec_serves_file_contents(tmp_path):
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("openapi: 3.0.0\n")
    handler = _make_handler()
    handler.TRACER = http_handler_base_tracer_stub()

    handler.serve_openapi_spec("test.get_openapi_spec", str(spec_path))

    handler.send_response.assert_called_once_with(200)
    handler.send_header.assert_any_call("Content-Type", "application/yaml")
    handler.wfile.seek(0)
    assert handler.wfile.read() == b"openapi: 3.0.0\n"


def test_do_options_uses_subclass_allowed_methods_and_headers():
    handler = _make_handler()
    handler.ALLOWED_METHODS = "GET, POST, DELETE, OPTIONS"
    handler.ALLOWED_HEADERS = "Content-Type, Authorization"

    handler.do_OPTIONS()

    handler.send_response.assert_called_once_with(204)
    handler.send_header.assert_any_call("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    handler.send_header.assert_any_call("Access-Control-Allow-Headers", "Content-Type, Authorization")


def test_end_headers_adds_cors_header_before_delegating(monkeypatch):
    handler = _make_handler()
    calls = []
    handler.send_header = lambda k, v: calls.append((k, v))
    monkeypatch.setattr(http_handler_base.BaseHTTPRequestHandler, "end_headers", lambda self: calls.append(("super_called", None)))

    BaseAPIHandler.end_headers(handler)

    assert calls == [("Access-Control-Allow-Origin", "*"), ("super_called", None)]


def test_log_message_is_silent(capsys):
    handler = _make_handler()

    handler.log_message("%s - something happened", "GET /x")

    assert capsys.readouterr().out == ""


def http_handler_base_tracer_stub():
    """A minimal stand-in for tracing.get_tracer(__name__)'s
    start_as_current_span context manager - real OTel spans work fine
    too, but this keeps these tests dependency-free."""

    class _Span:
        def set_attribute(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Tracer:
        def start_as_current_span(self, name):
            return _Span()

    return _Tracer()
