"""
Regression tests for the sandbox mechanism itself - a derivation's
fn_source is arbitrary Python accepted over HTTP, so these must fail
closed (rejected before execution), not just "happen to error". No
Postgres/Oxigraph needed - pure in-process logic.
"""

import pytest

from baler.sandbox import (
    SandboxError,
    compile_fn,
    run_test_cases,
    run_with_timeout,
)


def test_compile_fn_runs_a_legit_function():
    fn = compile_fn("def run(inputs, row):\n    return sum(inputs) if inputs else None")
    assert fn([1, 2, 3], {}) == 6


def test_compile_fn_rejects_missing_run():
    with pytest.raises(SandboxError, match="run"):
        compile_fn("def not_run(inputs, row):\n    return 1")


def test_compile_fn_rejects_syntax_error():
    with pytest.raises(SandboxError, match="syntax error"):
        compile_fn("def run(inputs, row:\n    return 1")


@pytest.mark.parametrize("source", [
    "import os\ndef run(inputs, row):\n    return 1",
    "def run(inputs, row):\n    import os\n    return 1",
    "def run(inputs, row):\n    open('/etc/passwd')\n    return 1",
    "def run(inputs, row):\n    eval('1')\n    return 1",
    "def run(inputs, row):\n    exec('x=1')\n    return 1",
    "def run(inputs, row):\n    __import__('os')\n    return 1",
    "def run(inputs, row):\n    getattr(inputs, 'x')\n    return 1",
])
def test_compile_fn_rejects_banned_constructs(source):
    with pytest.raises(SandboxError):
        compile_fn(source)


def test_compile_fn_rejects_dunder_attribute_escape_payload():
    """The canonical restricted-builtins escape chain: reach object's base
    class, walk its subclasses, find something dangerous. Must be rejected
    at validation time, not merely fail to find something useful."""
    payload = "def run(inputs, row):\n    return ().__class__.__base__.__subclasses__()"
    with pytest.raises(SandboxError, match="__"):
        compile_fn(payload)


def test_run_with_timeout_kills_an_infinite_loop():
    fn = compile_fn("def run(inputs, row):\n    while True:\n        pass")
    with pytest.raises(SandboxError, match="timeout"):
        run_with_timeout(fn, None, {}, timeout_seconds=0.5)


def test_run_with_timeout_returns_normally_when_fast():
    fn = compile_fn("def run(inputs, row):\n    return 42")
    assert run_with_timeout(fn, None, {}, timeout_seconds=5) == 42


def test_run_test_cases_reports_pass_and_fail():
    fn = compile_fn("def run(inputs, row):\n    vals = [v for s in inputs.values() for _, v in s[-1:]]\n    return sum(vals) / len(vals) if vals else None")
    test_cases = [
        {
            "inputs": {"a": [["2026-01-01T00:00:00Z", 70.0]], "b": [["2026-01-01T00:00:00Z", 74.0]]},
            "row": {},
            "expected": 72.0,
        },
        {
            "inputs": {"a": [["2026-01-01T00:00:00Z", 0.0]], "b": [["2026-01-01T00:00:00Z", 0.0]]},
            "row": {},
            "expected": 999.0,  # deliberately wrong
        },
    ]
    results = run_test_cases(fn, test_cases)
    assert results[0]["passed"] is True
    assert results[0]["actual"] == 72.0
    assert results[1]["passed"] is False
    assert results[1]["actual"] == 0.0


def test_run_test_cases_reports_compile_or_runtime_errors_as_failures_not_exceptions():
    fn = compile_fn("def run(inputs, row):\n    return 1 / 0")
    results = run_test_cases(fn, [{"inputs": {}, "row": {}, "expected": 1.0}])
    assert results[0]["passed"] is False
    assert results[0]["error"] is not None


def test_run_test_cases_parses_rollup_shaped_list_inputs():
    fn = compile_fn("def run(inputs, row):\n    vals = [s[-1][1] for s in inputs if s]\n    return sum(vals) if vals else None")
    test_cases = [{
        "inputs": [[["2026-01-01T00:00:00Z", 10.0]], [["2026-01-01T00:00:00Z", 15.0]]],
        "row": {},
        "expected": 25.0,
    }]
    results = run_test_cases(fn, test_cases)
    assert results[0]["passed"] is True
