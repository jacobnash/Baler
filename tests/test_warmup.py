"""baler.warmup: best effort and bounded (no services needed)."""

import threading
import time

from baler import warmup


def test_warmup_runs_and_reports(capsys):
    ran = []
    warmup.run("x", lambda: ran.append(1))
    assert ran == [1] and "warm-up x:" in capsys.readouterr().out


def test_a_failing_warmup_never_raises(capsys):
    def boom():
        raise RuntimeError("store down")

    warmup.run("x", boom)
    out = capsys.readouterr().out
    assert "failed" in out and "store down" in out


def test_a_slow_warmup_is_left_running_after_the_timeout(capsys):
    release = threading.Event()
    t0 = time.monotonic()
    warmup.run("slow", lambda: release.wait(5), timeout=0.1)
    assert time.monotonic() - t0 < 1
    assert "still running" in capsys.readouterr().out
    release.set()
