"""
scripts/gen_function_index.py (#93): the docs-site function index. No
network: manifests are local files, and the https path is exercised with
urlopen monkeypatched.
"""

import hashlib
import importlib.util
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gen_function_index", ROOT / "scripts" / "gen_function_index.py")
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

SAMPLE_MANIFEST = (ROOT / "templates" / "function-project" / "baler.toml").read_text()


def _index(tmp_path, *entries: str) -> Path:
    p = tmp_path / "function-index.toml"
    p.write_text("".join(f"[[package]]\n{e}\n" for e in entries))
    return p


def test_the_repo_index_builds_and_lists_the_sample_package(tmp_path):
    out = tmp_path / "functions.mdx"
    assert gen.main(["--out", str(out)]) == 0
    page = out.read_text()
    assert page.startswith("---\ntitle: Function index\n")
    assert "## baler-sample" in page
    assert "his_stats(client, point_id, range='today')" in page
    assert 'pip install "git+https://github.com/jacobnash/Baler@main#subdirectory=templates/function-project"' in page
    assert "**Version** 0.1.0" in page and "**Author** Baler maintainers" in page


def test_a_malformed_manifest_fails_the_build(tmp_path, capsys):
    (tmp_path / "bad.toml").write_text(SAMPLE_MANIFEST.replace('version = "0.1.0"', 'version = "latest"'))
    out = tmp_path / "functions.mdx"
    assert gen.main(["--index", str(_index(tmp_path, 'manifest = "bad.toml"')), "--out", str(out)]) == 1
    assert "package.version: 'latest' is not valid" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("entry, message", [
    ('manifest = "missing.toml"', "can't read manifest"),
    ('manifest = "http://example.com/baler.toml"', "must be https://"),
    ('url = "x"', "needs manifest ="),
    ('manifest = "ok.toml"\nsha256 = "abc"', "sha256 must be 64"),
    ('manifest = "ok.toml"\nsha256 = "' + "0" * 64 + '"', "changed since it was published"),
    ('manifest = "ok.toml"\nowner = "me"', "unknown key(s) owner"),
])
def test_bad_index_entries_fail_the_build(tmp_path, entry, message):
    (tmp_path / "ok.toml").write_text(SAMPLE_MANIFEST)
    with pytest.raises(gen.IndexError_, match=__import__("re").escape(message)):
        gen.load_index(_index(tmp_path, entry))


def test_duplicate_packages_fail_the_build(tmp_path):
    (tmp_path / "a.toml").write_text(SAMPLE_MANIFEST)
    (tmp_path / "b.toml").write_text(SAMPLE_MANIFEST)
    with pytest.raises(gen.IndexError_, match="already listed"):
        gen.load_index(_index(tmp_path, 'manifest = "a.toml"', 'manifest = "b.toml"'))


def test_a_matching_sha256_passes(tmp_path):
    (tmp_path / "ok.toml").write_text(SAMPLE_MANIFEST)
    digest = hashlib.sha256((tmp_path / "ok.toml").read_bytes()).hexdigest()
    (m,) = gen.load_index(_index(tmp_path, f'manifest = "ok.toml"\nsha256 = "{digest}"'))
    assert m.name == "baler-sample"


def test_https_manifests_are_fetched(tmp_path, monkeypatch):
    seen = []

    def fake_urlopen(url, timeout):
        seen.append(url)
        return io.BytesIO(SAMPLE_MANIFEST.encode())

    monkeypatch.setattr(gen.urllib.request, "urlopen", fake_urlopen)
    (m,) = gen.load_index(_index(tmp_path, 'manifest = "https://example.com/v1/baler.toml"'))
    assert seen == ["https://example.com/v1/baler.toml"]
    assert m.source == "https://example.com/v1/baler.toml"


def test_an_unreachable_https_manifest_fails_the_build(tmp_path, monkeypatch):
    def fake_urlopen(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(gen.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(gen.IndexError_, match="can't fetch manifest: connection refused"):
        gen.load_index(_index(tmp_path, 'manifest = "https://example.com/baler.toml"'))


def test_manifest_text_cannot_inject_mdx(tmp_path):
    """Descriptions, authors and summaries are untrusted; MDX would run
    {expressions} and render JSX if they got through unescaped."""
    evil = SAMPLE_MANIFEST.replace(
        'author = "Baler maintainers"', 'author = "<img src=x onerror=alert(1)> {process.exit(1)}"'
    ).replace(
        'summary = "Points matching', 'summary = "[click](javascript:alert(1)) *bold* | pipe # Points matching'
    )
    (tmp_path / "evil.toml").write_text(evil)
    page = gen.render(gen.load_index(_index(tmp_path, 'manifest = "evil.toml"')))
    for raw in ("<img", "{process", "[click]", "*bold*", "| pipe"):
        assert raw not in page
    assert "&#60;img src=x onerror=alert(1)&#62; &#123;process.exit(1)&#125;" in page


def test_text_escaping_keeps_plain_prose_readable():
    assert gen.text("Peak demand, in kW (15-min).") == "Peak demand, in kW (15-min)."
    assert gen.text("multi\n  line") == "multi line"
    assert gen.text("a<b") == "a&#60;b"


def test_an_empty_index_renders_a_placeholder(tmp_path):
    page = gen.render(gen.load_index(_index(tmp_path)))
    assert "No packages are published yet." in page
