"""
Start-up warm-up: run a service's typical first reads before it starts
listening, so its healthcheck - which the deploy waits on before moving to
the next service, and which the gateway's traffic follows - only passes
once the first real request no longer pays for cold caches (Baler#107,
#150, #172). On nash-srv the first reads after a deploy took 3-10x their
warm time: the image build just before evicts the page cache, so Postgres
pages, Oxigraph's store and the process's own lazily built tables all
start cold.

Best effort and bounded: a warm-up that fails or runs long never stops the
service from starting (the thread is a daemon, left to finish alone).
"""

from __future__ import annotations

import gc
import threading
import time

DEFAULT_TIMEOUT = 30.0


def run(name: str, fn, timeout: float = DEFAULT_TIMEOUT) -> None:
    """fn() on a daemon thread, waited on for at most `timeout` seconds;
    what it took (or why it didn't) is printed, nothing is raised."""
    outcome: dict[str, str] = {}

    def target():
        try:
            fn()
            outcome["ok"] = "done"
        except Exception as exc:  # noqa: BLE001 - warm-up is best effort
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    t0 = time.monotonic()
    thread = threading.Thread(target=target, name=f"warmup-{name}", daemon=True)
    thread.start()
    thread.join(timeout)
    took = time.monotonic() - t0
    if "error" in outcome:
        print(f"warm-up {name}: failed after {took:.1f}s ({outcome['error']})", flush=True)
    elif thread.is_alive():
        print(f"warm-up {name}: still running after {timeout:g}s, starting anyway", flush=True)
    else:
        print(f"warm-up {name}: {took:.1f}s", flush=True)
    freeze_heap()


def freeze_heap() -> None:
    """Move everything alive now - imported modules, the vendored Brick
    vocabulary, the lookup tables warm-up just built: hundreds of thousands
    of objects that live as long as the process - out of the cyclic
    garbage collector's view (gc.freeze). Otherwise every full collection
    walks them all again, and a large read allocates enough to trigger
    several: freezing them took a 10k-point /api/read from 1.78 s to
    1.07 s and its encode from 0.79 s to 0.57 s (Baler#244). Garbage first
    (gc.collect), so only live objects are frozen."""
    gc.collect()
    gc.freeze()
