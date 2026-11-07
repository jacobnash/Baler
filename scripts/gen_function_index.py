"""
Generates the docs-site function index (#93, the Eggbox equivalent) from
docs-site/function-index.toml, a list of baler.toml manifest locations.
Publishing a package is a PR that adds one [[package]] entry there.

    python3 scripts/gen_function_index.py            # docs-site's prebuild/predev run this

Writes docs-site/pages/code/functions.mdx (generated, git-ignored). Any
malformed index entry, unreachable or malformed manifest, sha256 mismatch
or duplicate package fails with exit status 1, which fails `npm run build`.

Static by design: no registry service, and package code is never imported
or run here - every field shown comes from the manifest. Manifest text is
untrusted (a URL's content can change after review, unless pinned with
sha256), so everything is escaped before it reaches MDX, which would
otherwise evaluate {expressions} and JSX at build time.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from baler_fn.manifest import (
    Manifest,
    ManifestError,
    parse_manifest,
    tomllib,
)

DEFAULT_INDEX = ROOT / "docs-site" / "function-index.toml"
DEFAULT_OUT = ROOT / "docs-site" / "pages" / "code" / "functions.mdx"
MAX_MANIFEST_BYTES = 256 * 1024
_SAFE = re.compile(r"[A-Za-z0-9 .,:;?!()'\"/=+-]")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class IndexError_(RuntimeError):
    """The index or one of its manifests can't be used; fails the build."""


def text(value: str) -> str:
    """Untrusted text -> MDX that renders as exactly that text. Anything
    outside a small safe set becomes a numeric character reference, so no
    { } < > or markdown syntax survives to be interpreted."""
    value = " ".join(value.split())
    return "".join(c if _SAFE.match(c) else f"&#{ord(c)};" for c in value)


def fetch(location: str, base: Path) -> tuple[bytes, str]:
    if location.startswith("https://"):
        try:
            with urllib.request.urlopen(location, timeout=30) as resp:
                data = resp.read(MAX_MANIFEST_BYTES + 1)
        except OSError as exc:
            raise IndexError_(f"{location}: can't fetch manifest: {exc}") from exc
        source = location
    elif "://" in location:
        raise IndexError_(f"{location}: manifest URLs must be https://")
    else:
        path = (base / location).resolve()
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise IndexError_(f"{location}: can't read manifest ({exc.strerror or exc})") from exc
        source = location
    if len(data) > MAX_MANIFEST_BYTES:
        raise IndexError_(f"{location}: manifest larger than {MAX_MANIFEST_BYTES} bytes")
    return data, source


def load_index(index_path: Path) -> list[Manifest]:
    try:
        index = tomllib.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise IndexError_(f"{index_path}: {exc}") from exc
    entries = index.get("package", [])
    if not isinstance(entries, list):
        raise IndexError_(f"{index_path}: [[package]] entries expected")
    manifests: list[Manifest] = []
    seen: dict[str, str] = {}
    for i, entry in enumerate(entries):
        where = f"{index_path.name}: package[{i}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("manifest"), str):
            raise IndexError_(f"{where}: needs manifest = \"<https URL or path to baler.toml>\"")
        extra = sorted(set(entry) - {"manifest", "sha256"})
        if extra:
            raise IndexError_(f"{where}: unknown key(s) {', '.join(extra)}")
        data, source = fetch(entry["manifest"], index_path.parent)
        want = entry.get("sha256")
        if want is not None:
            if not isinstance(want, str) or not _SHA256.match(want):
                raise IndexError_(f"{where}: sha256 must be 64 lowercase hex characters")
            got = hashlib.sha256(data).hexdigest()
            if got != want:
                raise IndexError_(f"{where}: {source} changed since it was published (sha256 {got}, index says {want})")
        try:
            manifest = parse_manifest(data.decode("utf-8"), source=source)
        except UnicodeDecodeError as exc:
            raise IndexError_(f"{source}: not UTF-8") from exc
        except ManifestError as exc:
            raise IndexError_(str(exc)) from exc
        if manifest.name in seen:
            raise IndexError_(f"{where}: package {manifest.name} is already listed by {seen[manifest.name]}")
        seen[manifest.name] = source
        manifests.append(manifest)
    return sorted(manifests, key=lambda m: m.name.lower())


def render(manifests: list[Manifest]) -> str:
    out = [
        "---",
        "title: Function index",
        "description: Published Baler function packages, generated from docs-site/function-index.toml.",
        "---",
        "",
        "{/* Generated by scripts/gen_function_index.py from docs-site/function-index.toml. Do not edit. */}",
        "",
        "Function packages people have published for [Baler Notebooks](/code/notebooks). Install one in",
        "your notebook with the `pip install` line shown, then find its functions with `baler_fn.list()`.",
        "To add yours, see [Publishing a package](/code/publishing).",
        "",
        "Packages run in **your** notebook or container, with your API key. Read a package's source before",
        "you install it, and never install packages on the shared Baler host.",
        "",
    ]
    if not manifests:
        out += ["No packages are published yet.", ""]
        return "\n".join(out)
    out += ["| Package | Version | Tags | Description |", "|---|---|---|---|"]
    for m in manifests:
        out.append(f"| [{m.name}](#{m.name.lower()}) | {text(m.version)} | {text(', '.join(m.tags))} | "
                   f"{text(m.description)} |")
    out.append("")
    for m in manifests:
        facts = [f"**Version** {text(m.version)}", f"**Author** {text(m.author)}"]
        if m.date:
            facts.append(f"**Date** {m.date.isoformat()}")
        if m.license:
            facts.append(f"**License** {text(m.license)}")
        facts.append(f"[Source]({m.repository})")
        if m.docs:
            facts.append(f"[Docs]({m.docs})")
        out += [f"## {m.name}", "", " · ".join(facts), "", text(m.description), ""]
        if m.tags:
            out += ["Tags: " + ", ".join(f"`{t}`" for t in m.tags), ""]
        out += ["```bash", m.install_line, "```", "", "### Functions", ""]
        for f in m.functions:
            out += [f"#### `{f.name}`", "", "```python", f"{f.name}{f.signature}", "```", "", text(f.summary), ""]
            if f.tags:
                out += ["Tags: " + ", ".join(f"`{t}`" for t in f.tags), ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    try:
        manifests = load_index(args.index)
    except IndexError_ as exc:
        print(f"function index: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(manifests), encoding="utf-8")
    print(f"function index: {len(manifests)} package(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
