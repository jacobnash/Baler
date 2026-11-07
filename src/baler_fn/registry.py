"""
@baler_function and function discovery (#91).

There is deliberately no registry object: a function package declares its
functions as Python entry points in the `baler.functions` group, and
list_functions() reads them from the installed distributions every time it
is called. Installing or uninstalling a package (pip) is the only way the
list changes, the same as SkySpark's Code app showing what its installed
pods provide. The decorator only labels a function; it returns the very
same function object, so help(fn), inspect and IDEs behave as normal.
"""

from __future__ import annotations

import builtins
import html
import importlib.metadata as md
import inspect
import types
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeVar, overload

from baler_fn.manifest import ENTRY_POINT_GROUP

F = TypeVar("F", bound=Callable[..., Any])
_ATTR = "__baler_function__"


@dataclass(frozen=True)
class FunctionInfo:
    """What list_functions() and describe() report for one function."""

    name: str
    signature: str
    doc: str
    tags: tuple[str, ...]
    module: str
    package: str | None = None
    version: str | None = None
    entry_point: str | None = None
    func: Callable[..., Any] | None = field(default=None, repr=False, compare=False)

    @property
    def summary(self) -> str:
        """First line of the docstring."""
        return self.doc.strip().splitlines()[0] if self.doc.strip() else ""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.func is None:
            raise TypeError(f"{self.name} has no function attached")
        return self.func(*args, **kwargs)


@overload
def baler_function(fn: F) -> F: ...
@overload
def baler_function(*, name: str | None = None, tags: Iterable[str] = ()) -> Callable[[F], F]: ...


def baler_function(fn: Any = None, *, name: str | None = None, tags: Iterable[str] = ()) -> Any:
    """Marks a function as a Baler function. Use bare (`@baler_function`)
    or with options (`@baler_function(tags=["energy"])`). `name` defaults
    to the function's own name. The function itself is returned unchanged
    apart from one attribute, so calling it and help() work as before."""
    tag_tuple = tuple(tags)
    if isinstance(tags, str) or not all(isinstance(t, str) for t in tag_tuple):
        raise TypeError("tags must be a list of strings, e.g. tags=[\"energy\"]")

    def mark(f: F) -> F:
        if not callable(f):
            raise TypeError(f"@baler_function needs a function, got {type(f).__name__}")
        setattr(f, _ATTR, {"name": name or f.__name__, "tags": tag_tuple})
        return f

    if fn is not None:
        return mark(fn)
    return mark


def is_baler_function(obj: Any) -> bool:
    return callable(obj) and isinstance(getattr(obj, _ATTR, None), dict)


def describe(fn: Callable[..., Any], *, package: str | None = None, version: str | None = None,
             entry_point: str | None = None) -> FunctionInfo:
    """FunctionInfo for any callable; decorated ones carry their name/tags."""
    meta = getattr(fn, _ATTR, None) or {}
    try:
        signature = str(inspect.signature(fn))
    except (TypeError, ValueError):
        signature = "(...)"
    return FunctionInfo(
        name=str(meta.get("name") or getattr(fn, "__name__", repr(fn))),
        signature=signature,
        doc=inspect.getdoc(fn) or "",
        tags=tuple(meta.get("tags", ())),
        module=getattr(fn, "__module__", "") or "",
        package=package,
        version=version,
        entry_point=entry_point,
        func=fn,
    )


class FunctionList(builtins.list):
    """A list of FunctionInfo that prints as a table (text in a terminal,
    HTML in Jupyter). `.errors` holds entry points that failed to load, so
    one broken package doesn't hide every other package's functions."""

    def __init__(self, items: Iterable[FunctionInfo] = (), errors: Iterable[str] = ()):
        super().__init__(items)
        self.errors: builtins.list[str] = builtins.list(errors)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            for info in self:
                if info.name == key:
                    return info
            raise KeyError(key)
        return super().__getitem__(key)

    def names(self) -> builtins.list[str]:
        return [f.name for f in self]

    def _rows(self) -> builtins.list[tuple[str, str, str, str, str]]:
        return [(f.name + f.signature, f.package or "", f.version or "", ", ".join(f.tags), f.summary) for f in self]

    def __str__(self) -> str:
        if not self:
            out = f"no functions installed (entry-point group {ENTRY_POINT_GROUP!r})"
        else:
            head = ("function", "package", "version", "tags", "summary")
            rows = [head, *self._rows()]
            widths = [max(len(r[i]) for r in rows) for i in range(5)]
            out = "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)
        if self.errors:
            out += "\n\nfailed to load:\n" + "\n".join(f"  {e}" for e in self.errors)
        return out

    __repr__ = __str__

    def _repr_html_(self) -> str:
        head = "".join(f"<th>{h}</th>" for h in ("function", "package", "version", "tags", "summary"))
        body = "".join(
            "<tr>" + "".join(f"<td><code>{html.escape(c)}</code></td>" if i == 0 else f"<td>{html.escape(c)}</td>"
                             for i, c in enumerate(r)) + "</tr>"
            for r in self._rows()
        )
        out = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        if not self:
            out = f"<p>{html.escape(str(FunctionList()))}</p>"
        if self.errors:
            out += "<p>Failed to load:</p><ul>" + "".join(f"<li>{html.escape(e)}</li>" for e in self.errors) + "</ul>"
        return out

    def to_frame(self) -> Any:
        """pandas DataFrame of the list (needs pandas)."""
        import pandas as pd

        return pd.DataFrame(
            [{"name": f.name, "signature": f.signature, "package": f.package, "version": f.version,
              "tags": builtins.list(f.tags), "summary": f.summary, "module": f.module} for f in self]
        )


def _functions_in(obj: Any) -> builtins.list[Callable[..., Any]]:
    if isinstance(obj, types.ModuleType):
        return [v for v in vars(obj).values() if is_baler_function(v) and getattr(v, "__module__", None) == obj.__name__]
    if is_baler_function(obj):
        return [obj]
    raise TypeError(f"{obj!r} is not decorated with @baler_function")


def list_functions(*, tag: str | None = None, group: str = ENTRY_POINT_GROUP) -> FunctionList:
    """Every function installed packages declare in the `baler.functions`
    entry-point group. An entry point names one decorated function
    (`pkg.mod:fn`) or a module (`pkg.mod`), meaning every decorated
    function defined in it. `tag` keeps only functions with that tag."""
    found: builtins.list[FunctionInfo] = []
    errors: builtins.list[str] = []
    seen: set[tuple[str, str]] = set()
    for ep in sorted(md.entry_points(group=group), key=lambda e: (e.dist.name if e.dist else "", e.name)):
        dist = ep.dist
        label = f"{ep.name} = {ep.value}" + (f" ({dist.name} {dist.version})" if dist else "")
        try:
            fns = _functions_in(ep.load())
        except Exception as exc:  # noqa: BLE001 - one broken package must not hide the rest; reported in .errors
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        for fn in fns:
            info = describe(fn, package=dist.name if dist else None, version=dist.version if dist else None,
                            entry_point=f"{ep.name} = {ep.value}")
            key = (info.module, info.name)
            if key in seen:
                continue
            seen.add(key)
            if tag is None or tag in info.tags:
                found.append(info)
    if errors:
        warnings.warn(f"{len(errors)} Baler function entry point(s) failed to load - see .errors", stacklevel=2)
    return FunctionList(found, errors)
