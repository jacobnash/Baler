"""
baler.toml: the manifest a function package ships at its repository root
(#92), and the only thing the docs-site function index reads (#93).

The index build never imports or runs package code: everything it shows
(signatures included) comes from this file, so `python -m baler_fn check`
exists for the author to confirm, in their own environment, that the
manifest still matches what the installed package really exports.

Strings from a manifest end up on a public web page, so the parser is
strict about shape (see _RULES) and the index generator escapes on top.
"""

from __future__ import annotations

import datetime as dt
import keyword
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 only
    import tomli as tomllib

MANIFEST_VERSION = 1
ENTRY_POINT_GROUP = "baler.functions"

_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:[.+-]?[A-Za-z0-9]+)*$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_HTTPS = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[A-Za-z0-9._~%/+@:=-]*)?$")
_GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_SUBDIR = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_EP_TARGET = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?::[A-Za-z_][A-Za-z0-9_]*)?$")
_FORBIDDEN_TEXT = re.compile(r"[`\x00-\x08\x0b-\x1f\x7f]")


class ManifestError(ValueError):
    """A baler.toml that can't be used. The message names the file, the
    field and what was expected, so it can be shown to the author as-is."""


@dataclass(frozen=True)
class FunctionSpec:
    """One `[[functions]]` entry: what the index shows for a function."""

    name: str
    signature: str
    summary: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    description: str
    author: str
    repository: str
    entry_points: dict[str, str]
    functions: tuple[FunctionSpec, ...]
    tags: tuple[str, ...] = ()
    license: str | None = None
    docs: str | None = None
    date: dt.date | None = None
    ref: str | None = None
    subdirectory: str | None = None
    source: str = field(default="baler.toml", compare=False)

    @property
    def git_ref(self) -> str:
        """The tag the install line pins: `ref`, else `v<version>`."""
        return self.ref or f"v{self.version}"

    @property
    def install_line(self) -> str:
        url = f"git+{self.repository}@{self.git_ref}"
        if self.subdirectory:
            url += f"#subdirectory={self.subdirectory}"
        return f'pip install "{url}"'


def load_manifest(path: str | Path) -> Manifest:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"{p}: can't read manifest ({exc.strerror or exc})") from exc
    return parse_manifest(text, source=str(p))


def parse_manifest(text: str, source: str = "baler.toml") -> Manifest:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{source}: not valid TOML: {exc}") from exc
    return _Parser(source).manifest(data)


class _Parser:
    def __init__(self, source: str):
        self.source = source

    def fail(self, where: str, why: str) -> ManifestError:
        return ManifestError(f"{self.source}: {where}: {why}")

    def table(self, data: dict, key: str, where: str) -> dict:
        value = data.get(key)
        if not isinstance(value, dict):
            raise self.fail(where, "missing table" if value is None else f"must be a table, got {type(value).__name__}")
        return value

    def text(self, data: dict, key: str, where: str, *, required: bool = True, max_len: int = 300,
             pattern: re.Pattern | None = None, hint: str = "") -> str | None:
        value = data.get(key)
        if value is None:
            if required:
                raise self.fail(f"{where}.{key}", "required" + (f" ({hint})" if hint else ""))
            return None
        if not isinstance(value, str):
            raise self.fail(f"{where}.{key}", f"must be a string, got {type(value).__name__}")
        value = value.strip()
        if not value:
            raise self.fail(f"{where}.{key}", "must not be empty")
        if len(value) > max_len:
            raise self.fail(f"{where}.{key}", f"longer than {max_len} characters")
        if _FORBIDDEN_TEXT.search(value):
            raise self.fail(f"{where}.{key}", "must not contain backticks or control characters")
        if pattern is not None and not pattern.match(value):
            raise self.fail(f"{where}.{key}", f"{value!r} is not valid" + (f" ({hint})" if hint else ""))
        return value

    def tags(self, data: dict, where: str) -> tuple[str, ...]:
        value = data.get("tags", [])
        if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
            raise self.fail(f"{where}.tags", "must be a list of strings")
        if len(value) > 20:
            raise self.fail(f"{where}.tags", "at most 20 tags")
        for t in value:
            if not _TAG.match(t):
                raise self.fail(f"{where}.tags", f"{t!r} is not valid (lowercase letters, digits and '-', e.g. \"energy\")")
        return tuple(value)

    def manifest(self, data: dict) -> Manifest:
        known = {"manifest_version", "package", "entry-points", "functions"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise self.fail("top level", f"unknown key(s) {', '.join(unknown)} (expected {', '.join(sorted(known))})")
        version = data.get("manifest_version")
        if version != MANIFEST_VERSION:
            raise self.fail("manifest_version", f"must be {MANIFEST_VERSION}, got {version!r}")

        pkg = self.table(data, "package", "[package]")
        w = "package"
        date = pkg.get("date")
        if date is not None and (not isinstance(date, dt.date) or isinstance(date, dt.datetime)):
            raise self.fail(f"{w}.date", "must be a TOML date like 2026-11-07 (no quotes)")
        allowed = {"name", "version", "description", "author", "repository", "license", "docs", "ref",
                   "subdirectory", "tags", "date"}
        extra = sorted(set(pkg) - allowed)
        if extra:
            raise self.fail("[package]", f"unknown key(s) {', '.join(extra)}")
        manifest_kwargs: dict[str, Any] = {
            "name": self.text(pkg, "name", w, pattern=_NAME, max_len=100, hint="the pip distribution name, e.g. \"baler-sample\""),
            "version": self.text(pkg, "version", w, pattern=_VERSION, max_len=50, hint="e.g. \"0.1.0\""),
            "description": self.text(pkg, "description", w, max_len=500),
            "author": self.text(pkg, "author", w, max_len=200),
            "repository": self.text(pkg, "repository", w, pattern=_HTTPS, max_len=300,
                                   hint="an https:// git URL pip can clone"),
            "license": self.text(pkg, "license", w, required=False, max_len=100),
            "docs": self.text(pkg, "docs", w, required=False, pattern=_HTTPS, max_len=300, hint="an https:// URL"),
            "ref": self.text(pkg, "ref", w, required=False, pattern=_GIT_REF, max_len=100, hint="a git tag or branch"),
            "subdirectory": self.text(pkg, "subdirectory", w, required=False, pattern=_SUBDIR, max_len=200,
                                     hint="a relative path inside the repository"),
            "tags": self.tags(pkg, w),
            "date": date,
        }

        eps = self.table(data, "entry-points", "[entry-points]")
        group = eps.get(ENTRY_POINT_GROUP)
        if not isinstance(group, dict) or not group:
            raise self.fail(f'[entry-points."{ENTRY_POINT_GROUP}"]',
                            "required, with at least one name = \"module:function\" entry")
        entry_points: dict[str, str] = {}
        for ep_name, target in group.items():
            where = f'entry-points."{ENTRY_POINT_GROUP}".{ep_name}'
            if not ep_name.isidentifier() or keyword.iskeyword(ep_name):
                raise self.fail(where, "entry point names must be Python identifiers")
            if not isinstance(target, str) or not _EP_TARGET.match(target):
                raise self.fail(where, f"{target!r} must look like \"package.module:function\" or \"package.module\"")
            entry_points[ep_name] = target

        raw_functions = data.get("functions")
        if not isinstance(raw_functions, list) or not raw_functions:
            raise self.fail("[[functions]]", "at least one [[functions]] entry is required "
                            "(python -m baler_fn functions-toml prints them for you)")
        functions = []
        seen: set[str] = set()
        for i, fn in enumerate(raw_functions):
            where = f"functions[{i}]"
            if not isinstance(fn, dict):
                raise self.fail(where, "must be a table")
            extra = sorted(set(fn) - {"name", "signature", "summary", "tags"})
            if extra:
                raise self.fail(where, f"unknown key(s) {', '.join(extra)}")
            name = self.text(fn, "name", where, max_len=100)
            assert name is not None
            if not name.isidentifier() or keyword.iskeyword(name):
                raise self.fail(f"{where}.name", f"{name!r} is not a Python identifier")
            if name in seen:
                raise self.fail(f"{where}.name", f"duplicate function {name!r}")
            seen.add(name)
            signature = self.text(fn, "signature", where, max_len=500, hint="e.g. \"(client, point_id, range='today')\"")
            assert signature is not None
            if not (signature.startswith("(") and (signature.endswith(")") or ") -> " in signature)):
                raise self.fail(f"{where}.signature", f"{signature!r} must look like \"(a, b=1) -> str\"")
            summary = self.text(fn, "summary", where, max_len=300)
            assert summary is not None
            functions.append(FunctionSpec(name, signature, summary, self.tags(fn, where)))

        return Manifest(entry_points=entry_points, functions=tuple(functions), source=self.source, **manifest_kwargs)
