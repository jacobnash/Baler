"""
baler_fn (#91, #92): the decorator, entry-point discovery, the baler.toml
parser, connect(), and the HaystackClient additions it relies on (bearer
token auth, Hayson decoding, hisWrite). No services: discovery runs
against a real dist-info written to tmp_path, and HTTP against a loopback
server in this process.
"""

import datetime as dt
import importlib
import json
import shutil
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import baler_fn
from baler.haystack_client import (
    HaystackAuthError,
    HaystackClient,
    HaystackError,
    _decode_haystack_scalar,
)
from baler_fn.__main__ import check, functions_toml
from baler_fn.__main__ import main as cli_main

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "templates" / "function-project"


# ------------------------------------------------------------- decorator


def test_decorator_returns_the_same_function_and_help_still_works():
    def peak(client, point_id, range="yesterday"):
        """Highest sample of a point.

        Longer text.
        """
        return 1

    decorated = baler_fn.baler_function(peak)
    assert decorated is peak
    assert peak(None, "p") == 1
    info = baler_fn.describe(peak)
    assert info.name == "peak"
    assert info.signature == "(client, point_id, range='yesterday')"
    assert info.summary == "Highest sample of a point."
    assert "Longer text." in info.doc
    assert info.tags == ()
    assert baler_fn.is_baler_function(peak)


def test_decorator_with_options_records_name_and_tags():
    @baler_fn.baler_function(name="kw_peak", tags=["energy", "demand"])
    def peak(client):
        """Peak kW."""

    assert peak.__name__ == "peak"
    info = baler_fn.describe(peak)
    assert (info.name, info.tags) == ("kw_peak", ("energy", "demand"))


def test_decorator_rejects_a_string_for_tags():
    with pytest.raises(TypeError, match="list of strings"):
        baler_fn.baler_function(tags="energy")


def test_undecorated_functions_are_not_baler_functions():
    def plain():
        pass

    assert not baler_fn.is_baler_function(plain)


# ------------------------------------------------- entry-point discovery


def _install_fake_dist(root: Path, dist: str, version: str, module: str, source: str, entry_points: dict[str, str]):
    """A real installed distribution as importlib.metadata sees one: a
    module plus a .dist-info with METADATA and entry_points.txt."""
    (root / f"{module}.py").write_text(textwrap.dedent(source))
    info = root / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n")
    eps = "\n".join(f"{k} = {v}" for k, v in entry_points.items())
    (info / "entry_points.txt").write_text(f"[baler.functions]\n{eps}\n")


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for name in [m for m in sys.modules if m.startswith("fakefns")]:
        del sys.modules[name]
    importlib.invalidate_caches()


FAKE_MODULE = '''
from baler_fn import baler_function

@baler_function(tags=["energy"])
def peak_demand(client, meter_id, range="yesterday"):
    """Highest demand of a meter over a range."""
    return ("peak", meter_id, range)

@baler_function
def runtime_hours(client, equip_id):
    """Hours an equipment ran."""

def helper():
    "not a Baler function"
'''


def test_list_finds_functions_from_entry_points_with_signature_and_docstring(site):
    _install_fake_dist(site, "fake-fns", "1.2.3", "fakefns_a", FAKE_MODULE,
                       {"peak_demand": "fakefns_a:peak_demand", "runtime_hours": "fakefns_a:runtime_hours"})
    fns = baler_fn.list()
    assert {"peak_demand", "runtime_hours"} <= set(fns.names())
    peak = fns["peak_demand"]
    assert peak.signature == "(client, meter_id, range='yesterday')"
    assert peak.summary == "Highest demand of a meter over a range."
    assert (peak.package, peak.version, peak.tags) == ("fake-fns", "1.2.3", ("energy",))
    assert peak.entry_point == "peak_demand = fakefns_a:peak_demand"
    assert peak(None, "m1") == ("peak", "m1", "yesterday")
    assert fns.errors == []
    assert "peak_demand(client, meter_id, range='yesterday')" in str(fns)
    assert "<table>" in fns._repr_html_()


def test_a_module_entry_point_lists_every_decorated_function_in_it(site):
    _install_fake_dist(site, "fake-mod", "0.1", "fakefns_b", FAKE_MODULE, {"all": "fakefns_b"})
    names = [f.name for f in baler_fn.list() if f.package == "fake-mod"]
    assert sorted(names) == ["peak_demand", "runtime_hours"]


def test_list_filters_by_tag(site):
    _install_fake_dist(site, "fake-tag", "0.1", "fakefns_c", FAKE_MODULE, {"all": "fakefns_c"})
    assert [f.name for f in baler_fn.list(tag="energy") if f.package == "fake-tag"] == ["peak_demand"]


def test_a_broken_entry_point_is_reported_without_hiding_the_others(site):
    _install_fake_dist(site, "fake-good", "0.1", "fakefns_d", FAKE_MODULE, {"peak_demand": "fakefns_d:peak_demand"})
    _install_fake_dist(site, "fake-bad", "0.1", "fakefns_e", "raise RuntimeError('boom')\n", {"x": "fakefns_e:x"})
    _install_fake_dist(site, "fake-plain", "0.1", "fakefns_f", FAKE_MODULE, {"helper": "fakefns_f:helper"})
    with pytest.warns(UserWarning, match="failed to load"):
        fns = baler_fn.list()
    assert "peak_demand" in fns.names()
    assert any("fake-bad" in e and "boom" in e for e in fns.errors)
    assert any("fake-plain" in e and "not decorated" in e for e in fns.errors)
    assert "failed to load" in str(fns)


def test_html_repr_escapes_docstrings(site):
    _install_fake_dist(site, "fake-html", "0.1", "fakefns_g", '''
from baler_fn import baler_function

@baler_function
def sneaky(client):
    """<script>alert(1)</script>"""
''', {"sneaky": "fakefns_g:sneaky"})
    html = baler_fn.list()._repr_html_()
    assert "<script>" not in html and "&lt;script&gt;" in html


# -------------------------------------------------------------- manifest


def test_the_sample_manifest_parses():
    m = baler_fn.load_manifest(SAMPLE / "baler.toml")
    assert m.name == "baler-sample"
    assert [f.name for f in m.functions] == ["point_table", "his_stats", "hourly_profile"]
    assert m.install_line == 'pip install "git+https://github.com/jacobnash/Baler@main#subdirectory=templates/function-project"'
    assert m.date == dt.date(2026, 11, 7)


VALID = '''
manifest_version = 1
[package]
name = "my-fns"
version = "1.0.0"
description = "Useful things"
author = "Me"
repository = "https://github.com/me/my-fns"
[entry-points."baler.functions"]
peak = "my_fns.energy:peak"
[[functions]]
name = "peak"
signature = "(client, meter_id)"
summary = "Peak demand."
'''


def test_a_minimal_manifest_defaults_the_install_ref_to_v_version():
    m = baler_fn.parse_manifest(VALID)
    assert m.install_line == 'pip install "git+https://github.com/me/my-fns@v1.0.0"'
    assert m.entry_points == {"peak": "my_fns.energy:peak"}


@pytest.mark.parametrize("change, message", [
    (("manifest_version = 1", "manifest_version = 2"), "manifest_version: must be 1"),
    (('name = "my-fns"\n', ""), "package.name: required"),
    (('version = "1.0.0"', 'version = "one"'), "package.version: 'one' is not valid"),
    (('repository = "https://github.com/me/my-fns"', 'repository = "http://github.com/me/my-fns"'),
     "package.repository"),
    (('description = "Useful things"', 'description = "a `rm -rf` b"'), "backticks"),
    (('author = "Me"', 'author = "Me"\ntags = ["Has Space"]'), "package.tags: 'Has Space' is not valid"),
    (('author = "Me"', 'author = "Me"\nhomepage = "x"'), "unknown key(s) homepage"),
    (('peak = "my_fns.energy:peak"', 'peak = "my_fns energy"'), "must look like"),
    (('signature = "(client, meter_id)"', 'signature = "client, meter_id"'), "functions[0].signature"),
    (('name = "peak"\nsignature', 'name = "not valid"\nsignature'), "not a Python identifier"),
    (("[package]", "[pkg]"), "unknown key(s) pkg"),
    (("[[functions]]", "[[function]]"), "unknown key(s) function"),
    (('name = "my-fns"', 'name = "my-fns"\ndate = "2026-11-07"'), "must be a TOML date"),
    (("manifest_version = 1", "manifest_version = = 1"), "not valid TOML"),
])
def test_invalid_manifests_fail_with_a_message_naming_the_field(change, message):
    old, new = change
    assert old in VALID
    with pytest.raises(baler_fn.ManifestError, match=__import__("re").escape(message)):
        baler_fn.parse_manifest(VALID.replace(old, new, 1), source="my/baler.toml")


def test_manifest_errors_name_the_file():
    with pytest.raises(baler_fn.ManifestError, match=r"^my/baler.toml: "):
        baler_fn.parse_manifest("manifest_version = 3", source="my/baler.toml")


def test_load_manifest_reports_a_missing_file(tmp_path):
    with pytest.raises(baler_fn.ManifestError, match="can't read manifest"):
        baler_fn.load_manifest(tmp_path / "nope.toml")


def test_manifest_without_functions_points_at_the_helper_command():
    text = VALID.split("[[functions]]")[0]
    with pytest.raises(baler_fn.ManifestError, match="functions-toml"):
        baler_fn.parse_manifest(text)


# ------------------------------------------- the sample package installed


@pytest.fixture
def sample_installed(site):
    """The template package as pip would install it: its module on the
    path plus dist-info built from its own pyproject.toml."""
    tomllib = baler_fn.manifest.tomllib
    project = tomllib.loads((SAMPLE / "pyproject.toml").read_text())["project"]
    shutil.copytree(SAMPLE / "src" / "baler_sample", site / "baler_sample")
    info = site / f"baler_sample-{project['version']}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {project['name']}\nVersion: {project['version']}\n")
    eps = project["entry-points"]["baler.functions"]
    (info / "entry_points.txt").write_text("[baler.functions]\n" + "".join(f"{k} = {v}\n" for k, v in eps.items()))
    yield site
    for name in [m for m in sys.modules if m.startswith("baler_sample")]:
        del sys.modules[name]


def test_sample_package_functions_appear_in_list(sample_installed):
    pytest.importorskip("pandas")
    fns = baler_fn.list()
    assert fns["his_stats"].signature == "(client, point_id, range='today')"
    assert fns["his_stats"].package == "baler-sample"


def test_sample_manifest_matches_the_installed_sample_package(sample_installed):
    """`python -m baler_fn check` on the template: the manifest the index
    shows must describe what the package really exports."""
    pytest.importorskip("pandas")
    assert check(baler_fn.load_manifest(SAMPLE / "baler.toml")) == []
    assert functions_toml("baler-sample").startswith("[[functions]]\nname = ")


def test_check_reports_drift(sample_installed, capsys):
    pytest.importorskip("pandas")
    text = (SAMPLE / "baler.toml").read_text().replace('version = "0.1.0"', 'version = "0.2.0"')
    text = text.replace("(client, point_id, range='today')", "(client, point_id)", 1)
    path = sample_installed / "baler.toml"
    path.write_text(text)
    assert cli_main(["check", str(path)]) == 1
    err = capsys.readouterr().err
    assert "version: baler.toml says 0.2.0, installed package is 0.1.0" in err
    assert "function his_stats: signature is (client, point_id, range='today')" in err


def test_sample_functions_run_against_a_fake_client(sample_installed):
    pd = pytest.importorskip("pandas")
    from baler_sample.functions import his_stats, hourly_profile, point_table

    class FakeClient:
        def read(self, filter, limit=None):
            return [{"id": "p1", "dis": "Temp", "kind": "Number", "unit": "°F", "curVal": 70.0, "extra": 1}]

        def his_read_df(self, point_id, range="today"):
            idx = pd.to_datetime(["2026-11-07T10:00Z", "2026-11-07T10:30Z", "2026-11-07T11:00Z"], utc=True)
            return pd.DataFrame({"val": [1.0, 3.0, 5.0]}, index=pd.Index(idx, name="ts"))

    c = FakeClient()
    assert list(point_table(c).columns) == ["id", "dis", "kind", "unit", "curVal"]
    stats = his_stats(c, "p1")
    assert (stats["count"], stats["mean"], stats["min"], stats["max"]) == (3, 3.0, 1.0, 5.0)
    prof = hourly_profile(c, "p1", tz="UTC")
    assert prof.loc[10, "mean"] == 2.0 and prof.loc[11, "max"] == 5.0


# --------------------------------------------- connect() and the client


class _FakeBaler:
    """Loopback Haystack server: checks the bearer token, answers about /
    read / hisRead in Hayson like Baler does, records hisWrite bodies."""

    def __init__(self, token="key-123"):
        self.token = token
        self.writes: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self._send(401, {})
                    return False
                return True

            def do_GET(self):
                if not self._authorized():
                    return
                op = self.path.split("?")[0].rsplit("/", 1)[-1]
                rows = {
                    "about": [{"productName": "Baler", "productUri": {"_kind": "uri", "val": "https://x"}}],
                    "read": [{"id": {"_kind": "ref", "val": "urn:p1", "dis": "P1"}, "point": {"_kind": "marker"},
                              "curVal": {"_kind": "number", "val": 70.5, "unit": "°F"}}],
                    "hisRead": [{"ts": {"_kind": "dateTime", "val": "2026-11-07T10:00:00Z", "tz": "UTC"}, "val": 1.5},
                                {"ts": {"_kind": "dateTime", "val": "2026-11-07T11:00:00Z", "tz": "UTC"}, "val": 2.5}],
                }[op]
                self._send(200, {"_kind": "grid", "meta": {"ver": "3.0"}, "cols": [], "rows": rows})

            def do_POST(self):
                if not self._authorized():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.writes.append({"path": self.path, "body": body})
                meta = {"ver": "3.0", "err": "m:", "dis": "s:not writable"} if "bad" in body["meta"]["id"] else {"ver": "3.0"}
                self._send(200, {"meta": meta, "cols": [{"name": "empty"}], "rows": []})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake_baler():
    srv = _FakeBaler()
    yield srv
    srv.server.shutdown()


def test_connect_reads_the_environment(monkeypatch, fake_baler):
    monkeypatch.setenv("BALER_URL", fake_baler.url + "/")
    monkeypatch.setenv("BALER_API_KEY", "key-123")
    monkeypatch.delenv("BALER_PROJECT", raising=False)
    client = baler_fn.connect()
    assert client.base_url == f"{fake_baler.url}/api/demo"
    assert "key-123" not in repr(client)
    assert client.about()["productName"] == "Baler"
    assert client.read("point") == [{"id": "urn:p1", "point": True, "curVal": 70.5}]
    assert client.his_read("urn:p1") == [
        {"ts": dt.datetime(2026, 11, 7, 10, tzinfo=dt.timezone.utc).timestamp(), "val": 1.5},
        {"ts": dt.datetime(2026, 11, 7, 11, tzinfo=dt.timezone.utc).timestamp(), "val": 2.5},
    ]


def test_connect_names_the_missing_variable(monkeypatch):
    monkeypatch.delenv("BALER_URL", raising=False)
    monkeypatch.delenv("BALER_API_KEY", raising=False)
    with pytest.raises(baler_fn.ConfigError, match="BALER_URL"):
        baler_fn.connect()
    with pytest.raises(baler_fn.ConfigError, match="BALER_API_KEY"):
        baler_fn.connect(url="http://x")


def test_a_rejected_api_key_raises_instead_of_attempting_scram(fake_baler):
    client = baler_fn.connect(url=fake_baler.url, api_key="wrong", project="p")
    with pytest.raises(HaystackAuthError, match="bearer token rejected"):
        client.about()


def test_his_read_df_is_indexed_by_utc_timestamp(fake_baler):
    pd = pytest.importorskip("pandas")
    df = baler_fn.connect(url=fake_baler.url, api_key="key-123").his_read_df("urn:p1", "today")
    assert list(df.columns) == ["val"]
    assert df.index[0] == pd.Timestamp("2026-11-07T10:00Z")
    assert str(df.index.tz) == "UTC"


def test_his_write_sends_a_haystack_3_json_grid(fake_baler):
    client = baler_fn.connect(url=fake_baler.url, api_key="key-123")
    ts = dt.datetime(2026, 11, 7, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=-6)))
    assert client.his_write("urn:p1", [(ts, 42), (ts + dt.timedelta(minutes=5), 43.5)]) == 2
    (write,) = fake_baler.writes
    assert write["path"] == "/api/demo/hisWrite"
    assert write["body"] == {
        "meta": {"ver": "3.0", "id": "r:urn:p1"},
        "cols": [{"name": "ts"}, {"name": "val"}],
        "rows": [{"ts": "t:2026-11-07T16:00:00Z UTC", "val": "n:42.0"},
                 {"ts": "t:2026-11-07T16:05:00Z UTC", "val": "n:43.5"}],
    }


def test_his_write_accepts_a_pandas_series(fake_baler):
    pd = pytest.importorskip("pandas")
    s = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-11-07T10:00Z", "2026-11-07T11:00Z"], utc=True))
    assert baler_fn.connect(url=fake_baler.url, api_key="key-123").his_write("@urn:p1", s) == 2
    assert fake_baler.writes[0]["body"]["rows"][1] == {"ts": "t:2026-11-07T11:00:00Z UTC", "val": "n:2.0"}


def test_his_write_raises_on_an_error_grid(fake_baler):
    client = baler_fn.connect(url=fake_baler.url, api_key="key-123")
    with pytest.raises(HaystackError, match="not writable"):
        client.his_write("urn:bad", [(dt.datetime.now(dt.timezone.utc), 1)])


def test_his_write_encodes_nan_and_infinity_the_haystack_way(fake_baler):
    ts = dt.datetime(2026, 11, 7, 10, tzinfo=dt.timezone.utc)
    baler_fn.connect(url=fake_baler.url, api_key="key-123").his_write(
        "urn:p1", [(ts, float("nan")), (ts, float("inf")), (ts, float("-inf"))])
    assert [r["val"] for r in fake_baler.writes[0]["body"]["rows"]] == ["n:NaN", "n:INF", "n:-INF"]


def test_his_write_refuses_naive_timestamps():
    client = HaystackClient("http://unused", token="t")
    with pytest.raises(ValueError, match="timezone"):
        naive = dt.datetime(2026, 11, 7, tzinfo=dt.timezone.utc).replace(tzinfo=None)
        client.his_write("p", [(naive, 1.0)])


def test_hayson_scalars_decode_like_the_classic_encoding():
    assert _decode_haystack_scalar({"_kind": "marker"}) is True
    assert _decode_haystack_scalar({"_kind": "number", "val": 71.5, "unit": "°F"}) == 71.5
    assert _decode_haystack_scalar({"_kind": "ref", "val": "abc", "dis": "AHU"}) == "abc"
    assert _decode_haystack_scalar({"_kind": "uri", "val": "https://x"}) == "https://x"
    expected = dt.datetime(2026, 8, 24, tzinfo=dt.timezone.utc).timestamp()
    assert _decode_haystack_scalar({"_kind": "dateTime", "val": "2026-08-24T00:00:00Z", "tz": "UTC"}) == expected
    assert _decode_haystack_scalar({"_kind": "coord", "lat": 1, "lng": 2}) == {"_kind": "coord", "lat": 1, "lng": 2}


def test_phable_shares_url_and_key(fake_baler):
    phable = pytest.importorskip("phable")
    ph = baler_fn.connect(url=fake_baler.url, api_key="key-123").phable()
    assert isinstance(ph, phable.HaystackClient)
    assert ph.uri == f"{fake_baler.url}/api/demo"


def test_importing_baler_fn_does_not_import_requests():
    """The docs build's index generator imports baler_fn.manifest on a
    machine that may not have requests."""
    import subprocess

    code = "import sys, baler_fn; baler_fn.parse_manifest; print('requests' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
