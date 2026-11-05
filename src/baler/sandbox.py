"""
Runtime-submitted Python, sandboxed - a derivation's fn_source is arbitrary
code accepted over HTTP (derivation_api.py), not a developer's PR. This is
the whole mitigation surface for that: an AST denylist (rejects the
constructs that let restricted-builtins sandboxes get escaped, e.g.
`().__class__.__base__.__subclasses__()`), a builtins allowlist, and a
wall-clock timeout. None of this is a hard security boundary - only OS-level
isolation (subprocess-per-execution, gVisor, WASM) would be that, and that's
deliberately out of scope for the single-trusted-operator deployment this
targets. See derivation-api-openapi.yaml's security caveat.

Timeout is a plain daemon thread, not signal.alarm or ThreadPoolExecutor:
cur-mode derivations get evaluated from the MQTT callback thread, and
signal.alarm/setitimer only work on the interpreter's main thread - raises
ValueError anywhere else. ThreadPoolExecutor was tried first and rejected -
its worker threads aren't daemon threads, so a genuinely stuck submission
(e.g. a submitted infinite loop) blocks the executor's atexit shutdown
handler forever, hanging the whole process at exit, not just that one call.
A plain daemon=True thread doesn't have that problem.
"""

import ast
import builtins
import threading
from collections.abc import Callable
from datetime import datetime

from baler.schemas import TestCaseResult


class SandboxError(Exception):
    pass


_BANNED_CALL_NAMES = {
    "eval", "exec", "compile", "__import__",
    "getattr", "setattr", "delattr",
    "globals", "locals", "vars", "open",
}

_SAFE_BUILTIN_NAMES = [
    "abs", "min", "max", "sum", "len", "round", "sorted", "enumerate",
    "range", "zip", "map", "filter",
    "float", "int", "bool", "str", "list", "dict", "set", "tuple",
    "True", "False", "None",
]

SAFE_BUILTINS = {name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES if hasattr(builtins, name)}


def _validate_ast(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise SandboxError(f"import is not allowed (line {node.lineno})")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _BANNED_CALL_NAMES:
            raise SandboxError(f"call to {node.func.id!r} is not allowed (line {node.lineno})")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            raise SandboxError(f"access to {node.attr!r} is not allowed (line {node.lineno})")


def compile_fn(source: str) -> Callable:
    """Parses, validates, and exec()s fn_source in a restricted namespace,
    returning the `run` callable it defines. Never executes anything the
    validator hasn't cleared - validation happens on the parsed tree before
    compile()/exec() ever sees it."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SandboxError(f"syntax error: {exc}") from exc

    _validate_ast(tree)

    namespace = {"__builtins__": SAFE_BUILTINS}
    try:
        exec(compile(tree, "<derivation fn_source>", "exec"), namespace)  # noqa: S102 - this whole module exists to run arbitrary fn_source safely; _validate_ast already ran, see module docstring
    except SandboxError:
        raise
    except Exception as exc:
        raise SandboxError(f"error executing function source: {exc}") from exc

    fn = namespace.get("run")
    if not callable(fn):
        raise SandboxError("fn_source must define a callable named 'run'")
    return fn


# ponytail: no forcible kill (Python threads can't be), and no cap on how
# many timed-out submissions accumulate over the life of a long-running
# process - each leaves one daemon thread spinning forever, consuming a
# core but never blocking anything else (daemon=True keeps it out of
# process shutdown). Same accepted-risk shape as fault_detector.py's
# uncapped webhook-delivery threads. A subprocess-based executor would
# allow an actual kill - add it if repeated timeouts in practice make
# this matter; a process restart clears accumulated threads either way.
def run_with_timeout(fn: Callable, *args, timeout_seconds: float = 5, **kwargs):
    outcome: dict = {}

    def _target():
        try:
            outcome["value"] = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - fn is arbitrary sandboxed user code; any exception type must be captured and reported back to the caller, not just a predictable subset
            outcome["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise SandboxError(f"function exceeded {timeout_seconds}s timeout")
    if "error" in outcome:
        raise SandboxError(f"error running function: {outcome['error']}")
    return outcome.get("value")


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _parse_series(series: list) -> list[tuple[datetime, object]]:
    return [(_parse_ts(ts), value) for ts, value in series]


def _parse_test_inputs(inputs):
    """test_cases carry JSON-serializable timestamps (strings); real
    evaluation hands fn actual datetime objects via timeseries.read_range.
    Converting here keeps the test path faithful to the live path instead
    of silently testing a different input shape than production ever sees."""
    if isinstance(inputs, dict):
        return {name: _parse_series(series) for name, series in inputs.items()}
    return [_parse_series(series) for series in inputs]


def run_test_cases(fn: Callable, test_cases: list[dict]) -> list[TestCaseResult]:
    """Runs every declared test case through fn under the same timeout as
    live evaluation. Never raises - a case that errors is reported as a
    failure, not an exception, so callers can present every case's outcome
    at once instead of stopping at the first one."""
    results: list[TestCaseResult] = []
    for case in test_cases:
        expected = case["expected"]
        try:
            inputs = _parse_test_inputs(case["inputs"])
            actual = run_with_timeout(fn, inputs, case.get("row", {}))
        except (SandboxError, KeyError, TypeError, ValueError) as exc:
            results.append({"passed": False, "actual": None, "expected": expected, "error": str(exc)})
            continue
        results.append({"passed": actual == expected, "actual": actual, "expected": expected, "error": None})
    return results
