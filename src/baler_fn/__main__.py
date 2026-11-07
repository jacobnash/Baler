"""
python -m baler_fn list                     installed functions (entry points)
python -m baler_fn check [baler.toml]       manifest vs. the installed package
python -m baler_fn functions-toml NAME      [[functions]] entries for a package

`check` and `functions-toml` import the package, so run them where the
package is installed (your notebook or your project's container), never
on a shared Baler host.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import sys

from baler_fn.manifest import ENTRY_POINT_GROUP, Manifest, ManifestError, load_manifest
from baler_fn.registry import FunctionInfo, list_functions


def _installed(dist_name: str) -> list[FunctionInfo]:
    return [f for f in list_functions() if f.package and _norm(f.package) == _norm(dist_name)]


def _norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def check(manifest: Manifest) -> list[str]:
    """Differences between a manifest and its installed distribution."""
    problems = []
    try:
        dist = md.distribution(manifest.name)
    except md.PackageNotFoundError:
        return [f"{manifest.name} is not installed here - pip install -e . first"]
    if dist.version != manifest.version:
        problems.append(f"version: baler.toml says {manifest.version}, installed package is {dist.version}")
    eps = {ep.name: ep.value for ep in dist.entry_points if ep.group == ENTRY_POINT_GROUP}
    if eps != manifest.entry_points:
        problems.append(f"entry points: baler.toml has {manifest.entry_points}, pyproject.toml installed {eps}")
    real = {f.name: f for f in _installed(manifest.name)}
    declared = {f.name: f for f in manifest.functions}
    for name in sorted(set(real) - set(declared)):
        problems.append(f"function {name} is exported but missing from [[functions]]")
    for name in sorted(set(declared) - set(real)):
        problems.append(f"function {name} is in [[functions]] but not exported by the package")
    for name in sorted(set(real) & set(declared)):
        r, d = real[name], declared[name]
        if r.signature != d.signature:
            problems.append(f"function {name}: signature is {r.signature}, baler.toml says {d.signature}")
        if r.summary != d.summary:
            problems.append(f"function {name}: docstring summary is {r.summary!r}, baler.toml says {d.summary!r}")
        if tuple(r.tags) != tuple(d.tags):
            problems.append(f"function {name}: tags are {list(r.tags)}, baler.toml says {list(d.tags)}")
    return problems


def functions_toml(dist_name: str) -> str:
    out = []
    for f in _installed(dist_name):
        out.append("[[functions]]")
        out.append(f"name = {json.dumps(f.name)}")
        out.append(f"signature = {json.dumps(f.signature)}")
        out.append(f"summary = {json.dumps(f.summary)}")
        if f.tags:
            out.append(f"tags = {json.dumps(list(f.tags))}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m baler_fn", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="installed functions")
    c = sub.add_parser("check", help="compare baler.toml with the installed package")
    c.add_argument("manifest", nargs="?", default="baler.toml")
    t = sub.add_parser("functions-toml", help="print [[functions]] entries for an installed package")
    t.add_argument("package", help="pip distribution name, e.g. baler-sample")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        print(list_functions())
        return 0
    if args.cmd == "functions-toml":
        text = functions_toml(args.package)
        if not text:
            print(f"no functions found for {args.package} - is it installed, with "
                  f"[project.entry-points.\"{ENTRY_POINT_GROUP}\"] in pyproject.toml?", file=sys.stderr)
            return 1
        print(text, end="")
        return 0
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(exc, file=sys.stderr)
        return 1
    problems = check(manifest)
    for p in problems:
        print(f"{args.manifest}: {p}", file=sys.stderr)
    if not problems:
        n = len(manifest.functions)
        print(f"{args.manifest}: ok ({manifest.name} {manifest.version}, {n} function{'' if n == 1 else 's'})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
