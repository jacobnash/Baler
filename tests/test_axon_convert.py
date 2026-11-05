"""Broad coverage for axon_convert.py's v1 grammar subset - see its module
docstring for scope. No real Axon corpus exists to test against (see
todo/skyspark-haxall-migration-gaps.md item 6), so every case here is
hand-authored: precedence/associativity of every operator, every grammar
production (arithmetic, comparisons, booleans, conditionals, do-blocks,
lambdas), the mapped/unmapped builtin split, and the parser's error
behavior on malformed input."""

import pytest

from baler.axon_convert import AxonSyntaxError, convert


def _run(source: str, *args):
    ns = {}
    exec(convert(source), ns)
    return ns["axon_fn"](*args)


# --- arithmetic: precedence, associativity, unary minus -------------------

ARITHMETIC_CASES = [
    ("1 + 2", 3),
    ("2 - 5", -3),
    ("3 * 4", 12),
    ("10 / 4", 2.5),
    ("7 / 2", 3.5),
    ("2 + 3 * 4", 14),
    ("(2 + 3) * 4", 20),
    ("10 - 2 - 3", 5),
    ("2 * 3 * 4", 24),
    ("-5 + 3", -2),
    ("2 + -3", -1),
    ("-2 * -3", 6),
    ("1.5 + 2.5", 4.0),
    ("10 / 2 / 5", 1.0),
    ("-(5 + 3)", -8),
    ("2 * (3 + 4) - 1", 13),
]


@pytest.mark.parametrize("source, expected", ARITHMETIC_CASES)
def test_arithmetic(source, expected):
    assert _run(source) == expected


# --- comparisons ------------------------------------------------------

COMPARISON_CASES = [
    ("1 == 1", True),
    ("1 != 1", False),
    ("3 < 5", True),
    ("5 < 3", False),
    ("5 > 3", True),
    ("3 >= 3", True),
    ("3 <= 2", False),
    ('"a" == "a"', True),
    ('"a" != "b"', True),
    ("1 + 1 == 2", True),
    ("2 * 3 > 5", True),
    ("2 * 3 <= 5", False),
]


@pytest.mark.parametrize("source, expected", COMPARISON_CASES)
def test_comparisons(source, expected):
    assert _run(source) is expected


# --- boolean logic: and/or/not precedence ------------------------------

BOOLEAN_CASES = [
    ("true and false", False),
    ("true or false", True),
    ("not true", False),
    ("not false", True),
    ("true and true and false", False),
    ("false or false or true", True),
    ("not true and false", False),
    ("true and not false", True),
    ("1 < 2 and 3 < 4", True),
    ("1 < 2 or 3 > 4", True),
    ("not (1 == 1)", False),
    ("not not true", True),
]


@pytest.mark.parametrize("source, expected", BOOLEAN_CASES)
def test_boolean_logic(source, expected):
    assert _run(source) is expected


# --- conditionals: if/else, nesting ------------------------------------

CONDITIONAL_CASES = [
    ("if (true) 1 else 2", 1),
    ("if (false) 1 else 2", 2),
    ('if (1 < 2) "yes" else "no"', "yes"),
    ('if (1 > 2) "yes" else "no"', "no"),
    ("if (true) if (false) 1 else 2 else 3", 2),
    ("if (false) if (true) 1 else 2 else 3", 3),
    ("if (5 > 3) 5 + 1 else 5 - 1", 6),
    ('if (5 > 3) if (1 > 2) "a" else "b" else "c"', "b"),
    ("if (true) if (true) 1 else 2 else 3", 1),
    ("if (1 == 1) true else false", True),
]


@pytest.mark.parametrize("source, expected", CONDITIONAL_CASES)
def test_conditionals(source, expected):
    assert _run(source) == expected


# --- do...end blocks: local defs, sequential bindings ------------------

DO_BLOCK_CASES = [
    ("do\n  x: 5\n  x + 1\nend", (), 6),
    ("do\n  x: 2\n  y: 3\n  x * y\nend", (), 6),
    ("do\n  x: 1\n  y: x + 1\n  z: y + 1\n  z\nend", (), 3),
    ("(a) => do\n  x: a * 2\n  x + 1\nend", (5,), 11),
    ("(a, b) => do\n  sum: a + b\n  avg: sum / 2\n  avg\nend", (4, 6), 5.0),
    ('(a) => do\n  doubled: a * 2\n  if (doubled > 10) "big" else "small"\nend', (3,), "small"),
    ('(a) => do\n  doubled: a * 2\n  if (doubled > 10) "big" else "small"\nend', (10,), "big"),
    ('do\n  greeting: "hello"\n  name: "world"\n  greeting\nend', (), "hello"),
]


@pytest.mark.parametrize("source, args, expected", DO_BLOCK_CASES)
def test_do_blocks_and_local_defs(source, args, expected):
    assert _run(source, *args) == expected


# --- lambdas: arity, bare-expression bodies -----------------------------

LAMBDA_CASES = [
    ("() => 42", (), 42),
    ("(a) => a * a", (4,), 16),
    ("(a, b, c) => a + b + c", (1, 2, 3), 6),
    ("(x) => not x", (True,), False),
    ("(x) => not x", (False,), True),
    ("(x, y) => x and y", (True, True), True),
    ("(x, y) => x and y", (True, False), False),
    ("(x, y) => x and y", (False, True), False),
    ("(x, y) => x and y", (False, False), False),
    ("(a, b) => a - b", (10, 3), 7),
]


@pytest.mark.parametrize("source, args, expected", LAMBDA_CASES)
def test_lambdas(source, args, expected):
    assert _run(source, *args) == expected


# --- mapped builtins: the one function with a real 1:1 translation ------

MAPPED_BUILTIN_CASES = [
    ("(x) => abs(x)", (-7,), 7),
    ("(x) => abs(x)", (7,), 7),
    ("(x) => abs(x)", (0,), 0),
]


@pytest.mark.parametrize("source, args, expected", MAPPED_BUILTIN_CASES)
def test_mapped_builtin(source, args, expected):
    assert _run(source, *args) == expected


def test_mapped_builtin_in_bare_expression():
    assert _run("abs(-3) + 1") == 4


# --- unmapped builtins: flagged stub, not a silent guess ----------------

UNMAPPED_BUILTIN_NAMES = [
    "hisRead",
    "readAll",
    "folioQuery",
    "curVal",
    "hisWrite",
    "trapVal",
    "toRadix",
    "dateTime",
    "weatherCond",
    "parseNumber",
]


@pytest.mark.parametrize("name", UNMAPPED_BUILTIN_NAMES)
def test_unmapped_builtin_emits_flagged_stub(name):
    source = convert(f"(pt) => {name}(pt)")
    assert f"# AXON: {name}(pt)" in source
    ns = {}
    exec(source, ns)
    with pytest.raises(NotImplementedError, match=name):
        ns["axon_fn"]("some-point")


def test_unmapped_call_in_arithmetic_raises():
    with pytest.raises(NotImplementedError, match="hisRead"):
        _run("(x) => 1 + hisRead(x)", 5)


def test_unmapped_call_in_do_binding_raises():
    with pytest.raises(NotImplementedError, match="hisRead"):
        _run("(pt) => do\n  v: hisRead(pt)\n  v\nend", "p1")


def test_unmapped_call_in_untaken_if_branch_does_not_raise():
    # ast.IfExp only evaluates the taken branch - the untaken hisRead()
    # must never run, same as Python's own conditional expression.
    assert _run("(x) => if (false) hisRead(x) else 1", "p1") == 1


def test_unmapped_call_in_taken_if_branch_raises():
    with pytest.raises(NotImplementedError, match="hisRead"):
        _run("(x) => if (true) hisRead(x) else 1", "p1")


def test_unmapped_call_as_argument_to_mapped_builtin_raises():
    with pytest.raises(NotImplementedError, match="hisRead"):
        _run("(pt) => abs(hisRead(pt))", "p1")


def test_first_unmapped_call_evaluates_before_second():
    # Python evaluates a BinOp's left operand first - hisRead(a) must raise
    # before readAll(b) is ever reached.
    with pytest.raises(NotImplementedError, match="hisRead"):
        _run("(a, b) => hisRead(a) + readAll(b)", "p1", "p2")


def test_unmapped_call_preserves_multiple_original_arguments():
    source = convert("(a, b) => pointWrite(a, b)")
    assert "# AXON: pointWrite(a, b)" in source
    ns = {}
    exec(source, ns)
    with pytest.raises(NotImplementedError, match=r"pointWrite\(a, b\)"):
        ns["axon_fn"]("p1", 42)


# --- string literals: escapes -------------------------------------------

STRING_CASES = [
    ('"hello"', "hello"),
    ('"line1\\nline2"', "line1\nline2"),
    ('"quote: \\"hi\\""', 'quote: "hi"'),
    ('"back\\\\slash"', "back\\slash"),
    ('"tab\\there"', "tab\there"),
]


@pytest.mark.parametrize("source, expected", STRING_CASES)
def test_string_literals(source, expected):
    assert _run(source) == expected


# --- null ----------------------------------------------------------------

def test_null_literal():
    assert _run("null") is None


def test_null_equality_true():
    assert _run("null == null") is True


@pytest.mark.parametrize(
    "arg, expected",
    [(None, "empty"), (5, "has value")],
)
def test_null_check_in_conditional(arg, expected):
    source = '(x) => if (x == null) "empty" else "has value"'
    assert _run(source, arg) == expected


# --- syntax errors: the parser must reject these, not guess -------------

SYNTAX_ERROR_CASES = [
    "(a) => a +",
    "if (true) 1",
    "do\n  x: 1\nend",
    "(a b) => a",
    "1 +",
    "1 + + 2",
    "(1, 2)",
    '"unterminated',
    "@#$%",
    "",
    "(a, b",
    "do\n  : 1\nend",
    "1 < 2 < 3",
    "if 1 else 2",
]


@pytest.mark.parametrize("source", SYNTAX_ERROR_CASES)
def test_rejects_malformed_input(source):
    with pytest.raises(AxonSyntaxError):
        convert(source)
