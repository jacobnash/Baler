"""No commissioning extension (commissioning_api.EXTENSIONS) may answer a
request the built-in route table already answers, or one another extension
answers. Extensions are matched first, so an overlap silently shadows the
older route: #217's construction schedules once took GET
/projects/{id}/schedules from #212's override tests."""

import re

import pytest

from baler import commissioning_api
from baler.commissioning import db, runner

SAMPLE = "x1"


def _sample_path(rx: re.Pattern) -> str:
    path = re.sub(r"\(\[\^/\]\+\)", SAMPLE, rx.pattern)
    assert rx.fullmatch(path), f"can't build a sample path for {rx.pattern!r}"
    return path


def _routes():
    for ext in commissioning_api.EXTENSIONS:
        for method, rx, name, _fn in ext.ROUTES:
            yield ext.__name__, method, rx, name


@pytest.fixture
def builtin_route(monkeypatch):
    repo = db.MemoryRepo()
    handler = commissioning_api.make_handler(repo, worker=runner.RunWorker(repo, lambda: None))
    monkeypatch.setattr(commissioning_api, "EXTENSIONS", ())
    h = object.__new__(handler)
    return lambda method, path: h._route(method, path)


@pytest.mark.parametrize("ext,method,rx,name", list(_routes()), ids=lambda v: getattr(v, "pattern", v))
def test_no_extension_shadows_a_builtin_route(builtin_route, ext, method, rx, name):
    path = _sample_path(rx)
    assert builtin_route(method, path) is None, f"{ext}.{name} ({method} {rx.pattern}) shadows a built-in route"


def test_no_two_extensions_answer_the_same_request():
    routes = list(_routes())
    for ext, method, rx, name in routes:
        path = _sample_path(rx)
        others = [f"{e}.{n}" for e, m, r, n in routes if m == method and r.fullmatch(path) and (e, n) != (ext, name)]
        assert not others, f"{method} {path}: {ext}.{name} and {others}"
