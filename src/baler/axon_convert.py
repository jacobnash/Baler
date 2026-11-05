"""
Axon -> Python converter, v1 scope per todo/skyspark-haxall-migration-gaps.md
item 6: the structural/expression subset only (arithmetic, comparisons,
conditionals, local `def`s via `do...end`, lambdas). A call to anything
outside MAPPED_BUILTINS - hisRead, readAll, Folio filters, the rest of
Axon's standard library - has no 1:1 Baler equivalent and is
deliberately not guessed at: it becomes a flagged stub for a human to
finish, not a silent wrong answer.

Hand-rolled recursive-descent parser over Axon's own small grammar
(confirmed against haxall/haxall's real Tokenizer.fan/Parser.fan - ~40KB,
not the obstacle) rather than vendoring that Fantom parser: no Fantom
toolchain exists in this repo's CI, and there's no in-repo Axon corpus to
validate a vendored parser against anyway (see the todo item's own
findings) - a native Python port is the smaller, self-contained bet for a
v1 whose real cost driver is the builtin-function mapping work, not the
grammar. Only validated against the hand-authored examples in
tests/test_axon_convert.py, not real customer rule bodies - there is no
such corpus available to test against.

KNOWN GAP - null semantics: operators here map straight to Python's, but
verified live against a real Haxall instance (axon shell v4.0.6,
`docker run --entrypoint /haxall/bin/axon windrow-haxall:latest '<expr>'`),
Axon's own null handling is inconsistent by operator family and this
converter does not reproduce any of it:
  - Arithmetic (+ - * /) SILENTLY PROPAGATES null: `1 + null` -> `null`,
    no error. The generated Python `1 + None` raises TypeError instead.
  - Ordering (< > <= >=) treats null as sorting below every number:
    `null < 1` -> true, `1 < null` -> false. Python's `<` on None raises
    TypeError instead. (`==`/`!=` happen to already match - Python's
    equality on None doesn't raise.)
  - Boolean logic (and/or/not) does the OPPOSITE of arithmetic: it ERRORS
    on a null operand (`not null`, `true and null` both raise a NullErr
    in real Axon), except where short-circuiting skips evaluating it
    (`true or null` -> true). Python's `and`/`or`/`not` treat None as
    falsy instead of erroring - a silent behavior change, not a crash,
    for exactly the case Axon itself treats as a hard error.
Fixing this properly means a runtime null-aware layer for every operator,
which is bigger than v1's scope - noted here so it's a known, verified
gap instead of a surprise a migrated rule discovers in production.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

# The only Axon builtins with an exact 1:1 Python equivalent handled today.
# Everything else becomes a NotImplementedError stub - see _Call codegen.
MAPPED_BUILTINS = {"abs": "abs"}


class AxonSyntaxError(SyntaxError):
    pass


# --- tokenizer ---------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      (?P<NUMBER>\d+\.\d+|\d+)
    | (?P<STRING>"(?:[^"\\]|\\.)*")
    | (?P<ARROW>=>)
    | (?P<OP>==|!=|<=|>=|[-+*/(),:<>])
    | (?P<NAME>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<SKIP>[ \t\r\n]+)
    """,
    re.VERBOSE,
)

KEYWORDS = {"if", "else", "and", "or", "not", "true", "false", "null", "do", "end"}


@dataclass
class Token:
    kind: str
    value: str
    pos: int


def tokenize(source: str) -> list[Token]:
    tokens = []
    i = 0
    while i < len(source):
        m = _TOKEN_RE.match(source, i)
        if m is None:
            raise AxonSyntaxError(f"unexpected character {source[i]!r} at {i}")
        i = m.end()
        kind = m.lastgroup
        if kind is None:
            # every alternative in _TOKEN_RE is a named group, so a match
            # always has a group name - guard for the type checker / safety.
            raise AxonSyntaxError(f"unnamed token match at {m.start()}")
        if kind == "SKIP":
            continue
        value = m.group()
        if kind == "NAME" and value in KEYWORDS:
            kind = value
        tokens.append(Token(kind, value, m.start()))
    tokens.append(Token("EOF", "", len(source)))
    return tokens


# --- Axon AST (pre-codegen) --------------------------------------------


@dataclass
class Num:
    value: float


@dataclass
class Str:
    value: str


@dataclass
class BoolLit:
    value: bool


@dataclass
class NullLit:
    pass


@dataclass
class Name:
    id: str


@dataclass
class BinOp:
    op: str
    left: object
    right: object


@dataclass
class UnaryOp:
    op: str
    operand: object


@dataclass
class BoolOp:
    op: str  # "and" / "or"
    left: object
    right: object


@dataclass
class If:
    test: object
    body: object
    orelse: object


@dataclass
class Call:
    func: str
    args: list = field(default_factory=list)
    src_text: str = ""


@dataclass
class Do:
    bindings: list  # list[(name, expr)]
    result: object


@dataclass
class Lambda:
    params: list[str]
    body: object  # expr or Do


# --- parser --------------------------------------------------------------


class Parser:
    def __init__(self, source: str):
        self.source = source
        self.tokens = tokenize(source)
        self.i = 0

    def _peek(self) -> Token:
        return self.tokens[self.i]

    def _peek2(self) -> Token:
        return self.tokens[self.i + 1] if self.i + 1 < len(self.tokens) else self.tokens[-1]

    def _advance(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def _expect(self, kind: str, value: str | None = None) -> Token:
        tok = self._peek()
        if tok.kind != kind or (value is not None and tok.value != value):
            raise AxonSyntaxError(f"expected {value or kind!r}, got {tok.value!r} at {tok.pos}")
        return self._advance()

    def parse_program(self):
        if self._peek().kind == "OP" and self._peek().value == "(" and self._is_lambda_ahead():
            node = self._parse_lambda()
        elif self._peek().kind == "do":
            node = self._parse_do()
        else:
            node = self._parse_expr()
        self._expect("EOF")
        return node

    def _is_lambda_ahead(self) -> bool:
        # "(" ... ")" "=>" - scan forward past the matching paren for ARROW,
        # without consuming, so a parenthesized expression like "(1 + 2)"
        # isn't mistaken for a zero/one-arg lambda.
        depth = 0
        j = self.i
        while j < len(self.tokens):
            tok = self.tokens[j]
            if tok.kind == "OP" and tok.value == "(":
                depth += 1
            elif tok.kind == "OP" and tok.value == ")":
                depth -= 1
                if depth == 0:
                    nxt = self.tokens[j + 1] if j + 1 < len(self.tokens) else self.tokens[-1]
                    return nxt.kind == "ARROW"
            j += 1
        return False

    def _parse_lambda(self) -> Lambda:
        self._expect("OP", "(")
        params = []
        if not (self._peek().kind == "OP" and self._peek().value == ")"):
            params.append(self._expect("NAME").value)
            while self._peek().kind == "OP" and self._peek().value == ",":
                self._advance()
                params.append(self._expect("NAME").value)
        self._expect("OP", ")")
        self._expect("ARROW")
        body = self._parse_do() if self._peek().kind == "do" else self._parse_expr()
        return Lambda(params, body)

    def _parse_do(self) -> Do:
        self._expect("do")
        bindings = []
        while self._peek().kind == "NAME" and self._peek2().kind == "OP" and self._peek2().value == ":":
            name = self._advance().value
            self._advance()  # ':'
            bindings.append((name, self._parse_expr()))
        result = self._parse_expr()
        self._expect("end")
        return Do(bindings, result)

    # expr := ifExpr
    def _parse_expr(self):
        if self._peek().kind == "if":
            return self._parse_if()
        return self._parse_or()

    def _parse_if(self) -> If:
        self._expect("if")
        self._expect("OP", "(")
        test = self._parse_expr()
        self._expect("OP", ")")
        body = self._parse_expr()
        self._expect("else")
        orelse = self._parse_expr()
        return If(test, body, orelse)

    def _parse_or(self):
        node = self._parse_and()
        while self._peek().kind == "or":
            self._advance()
            node = BoolOp("or", node, self._parse_and())
        return node

    def _parse_and(self):
        node = self._parse_not()
        while self._peek().kind == "and":
            self._advance()
            node = BoolOp("and", node, self._parse_not())
        return node

    def _parse_not(self):
        if self._peek().kind == "not":
            self._advance()
            return UnaryOp("not", self._parse_not())
        return self._parse_comparison()

    _COMPARE_OPS = frozenset({"==", "!=", "<", ">", "<=", ">="})

    def _parse_comparison(self):
        node = self._parse_add()
        if self._peek().kind == "OP" and self._peek().value in self._COMPARE_OPS:
            op = self._advance().value
            node = BinOp(op, node, self._parse_add())
        return node

    def _parse_add(self):
        node = self._parse_mul()
        while self._peek().kind == "OP" and self._peek().value in ("+", "-"):
            op = self._advance().value
            node = BinOp(op, node, self._parse_mul())
        return node

    def _parse_mul(self):
        node = self._parse_unary()
        while self._peek().kind == "OP" and self._peek().value in ("*", "/"):
            op = self._advance().value
            node = BinOp(op, node, self._parse_unary())
        return node

    def _parse_unary(self):
        if self._peek().kind == "OP" and self._peek().value == "-":
            self._advance()
            return UnaryOp("-", self._parse_unary())
        return self._parse_postfix()

    def _parse_postfix(self):
        node = self._parse_primary()
        while self._peek().kind == "OP" and self._peek().value == "(":
            if not isinstance(node, Name):
                raise AxonSyntaxError(f"only a name can be called, got {node!r}")
            start = self._call_start_pos
            self._advance()  # '('
            args = []
            if not (self._peek().kind == "OP" and self._peek().value == ")"):
                args.append(self._parse_expr())
                while self._peek().kind == "OP" and self._peek().value == ",":
                    self._advance()
                    args.append(self._parse_expr())
            end_tok = self._expect("OP", ")")
            end = end_tok.pos + 1
            node = Call(node.id, args, self.source[start:end])
        return node

    def _parse_primary(self):
        tok = self._peek()
        self._call_start_pos = tok.pos
        if tok.kind == "NUMBER":
            self._advance()
            return Num(float(tok.value) if "." in tok.value else int(tok.value))
        if tok.kind == "STRING":
            self._advance()
            return Str(ast.literal_eval(tok.value))
        if tok.kind == "true":
            self._advance()
            return BoolLit(True)
        if tok.kind == "false":
            self._advance()
            return BoolLit(False)
        if tok.kind == "null":
            self._advance()
            return NullLit()
        if tok.kind == "NAME":
            self._advance()
            return Name(tok.value)
        if tok.kind == "OP" and tok.value == "(":
            self._advance()
            node = self._parse_expr()
            self._expect("OP", ")")
            return node
        raise AxonSyntaxError(f"unexpected token {tok.value!r} at {tok.pos}")


def parse(source: str):
    return Parser(source).parse_program()


# --- codegen: our AST -> Python ast -------------------------------------

_BINOP_CLS = {"+": ast.Add, "-": ast.Sub, "*": ast.Mult, "/": ast.Div}
_CMPOP_CLS = {"==": ast.Eq, "!=": ast.NotEq, "<": ast.Lt, ">": ast.Gt, "<=": ast.LtE, ">=": ast.GtE}


def _expr(node) -> ast.expr:
    if isinstance(node, Num):
        return ast.Constant(node.value)
    if isinstance(node, Str):
        return ast.Constant(node.value)
    if isinstance(node, BoolLit):
        return ast.Constant(node.value)
    if isinstance(node, NullLit):
        return ast.Constant(None)
    if isinstance(node, Name):
        return ast.Name(node.id, ctx=ast.Load())
    if isinstance(node, UnaryOp):
        if node.op == "not":
            return ast.UnaryOp(ast.Not(), _expr(node.operand))
        return ast.UnaryOp(ast.USub(), _expr(node.operand))
    if isinstance(node, BoolOp):
        cls = ast.And if node.op == "and" else ast.Or
        return ast.BoolOp(cls(), [_expr(node.left), _expr(node.right)])
    if isinstance(node, BinOp):
        if node.op in _CMPOP_CLS:
            return ast.Compare(_expr(node.left), [_CMPOP_CLS[node.op]()], [_expr(node.right)])
        return ast.BinOp(_expr(node.left), _BINOP_CLS[node.op](), _expr(node.right))
    if isinstance(node, If):
        return ast.IfExp(_expr(node.test), _expr(node.body), _expr(node.orelse))
    if isinstance(node, Call):
        if node.func in MAPPED_BUILTINS:
            py_name = MAPPED_BUILTINS[node.func]
            return ast.Call(ast.Name(py_name, ctx=ast.Load()), [_expr(a) for a in node.args], [])
        # Unmapped builtin (hisRead, readAll, a Folio filter, ...) - no 1:1
        # Baler equivalent, flagged for a human rather than guessed.
        # _add_stub_comments() turns this into a real "# AXON: ..." comment
        # line above the raise, once the whole module is unparsed to text.
        return ast.Call(ast.Name("__axon_stub__", ctx=ast.Load()), [ast.Constant(node.src_text)], [])
    if isinstance(node, Do):
        raise AxonSyntaxError("do...end is only valid as a function body, not a nested expression")
    raise AxonSyntaxError(f"don't know how to convert {node!r}")


def _body_statements(node) -> list[ast.stmt]:
    if isinstance(node, Do):
        stmts: list[ast.stmt] = [
            ast.Assign([ast.Name(name, ctx=ast.Store())], _expr(value)) for name, value in node.bindings
        ]
        stmts.append(ast.Return(_expr(node.result)))
        return stmts
    return [ast.Return(_expr(node))]


_STUB_HELPER = (
    "def __axon_stub__(call):\n"
    "    raise NotImplementedError(f\"AXON: {call}\")\n\n\n"
)
_STUB_CALL_RE = re.compile(
    r"""__axon_stub__\((?P<call>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\)"""
)


def _add_stub_comments(src: str) -> str:
    """Python's ast can't hold comments, so this is a text pass over the
    unparsed module: every line that calls __axon_stub__ (wherever it sits
    in a return/assignment/nested expression) gets a "# AXON: <original
    call>" comment line inserted directly above it."""
    out_lines = []
    for line in src.split("\n"):
        m = _STUB_CALL_RE.search(line)
        if m:
            indent = line[: len(line) - len(line.lstrip())]
            out_lines.append(f"{indent}# AXON: {ast.literal_eval(m['call'])}")
        out_lines.append(line)
    return "\n".join(out_lines)


def convert(source: str, func_name: str = "axon_fn") -> str:
    """Converts one Axon expression or lambda into a standalone Python
    module defining `func_name`. Raises AxonSyntaxError on anything outside
    the v1 grammar (see module docstring) - never silently drops syntax it
    doesn't understand."""
    program = parse(source)
    if isinstance(program, Lambda):
        params = [ast.arg(p) for p in program.params]
        body = _body_statements(program.body)
    else:
        params = []
        body = _body_statements(program)

    func_def = ast.FunctionDef(
        name=func_name,
        args=ast.arguments(
            posonlyargs=[], args=params, vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]
        ),
        body=body,
        decorator_list=[],
        returns=None,
        type_comment=None,
        type_params=[],
    )
    module = ast.Module([func_def], type_ignores=[])
    ast.fix_missing_locations(module)
    src = _add_stub_comments(ast.unparse(module))
    if "__axon_stub__" in src:
        src = _STUB_HELPER + src
    return src
