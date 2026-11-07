"""
baler_fn: write Baler functions in Python and run them in a notebook.

    import baler_fn
    client = baler_fn.connect()           # BALER_URL / BALER_API_KEY / BALER_PROJECT
    baler_fn.list()                       # functions installed packages provide

    @baler_fn.baler_function(tags=["energy"])
    def peak(client, point_id, range="yesterday"):
        "Highest sample of a point over a range."
        ...

Docs: docs-site/pages/code/baler-fn.mdx. Baler never runs this code; it
runs wherever the notebook (or your own container) runs.
"""

from typing import Any

from baler_fn.manifest import (
    ENTRY_POINT_GROUP,
    FunctionSpec,
    Manifest,
    ManifestError,
    load_manifest,
    parse_manifest,
)
from baler_fn.registry import (
    FunctionInfo,
    FunctionList,
    baler_function,
    describe,
    is_baler_function,
    list_functions,
)

# `baler_fn.list()` reads naturally in a notebook; list_functions is the
# same thing under a name that doesn't shadow the builtin.
list = list_functions

__all__ = [
    "ENTRY_POINT_GROUP", "BalerClient", "ConfigError", "FunctionInfo", "FunctionList", "FunctionSpec", "Manifest",
    "ManifestError", "baler_function", "connect", "describe", "is_baler_function", "list", "list_functions",
    "load_manifest", "parse_manifest",
]


def __getattr__(name: str) -> Any:
    # The client pulls in requests; the manifest parser (used by the docs
    # build's index generator) must import without it.
    if name in ("connect", "BalerClient", "ConfigError"):
        from baler_fn import client

        return getattr(client, name)
    raise AttributeError(f"module 'baler_fn' has no attribute {name!r}")
