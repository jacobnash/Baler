"""A scheduled worker: logs his_stats for one point every interval.

    BALER_SAMPLE_POINT=urn:point:... python -m baler_sample.worker

Runs in the project's own container (compose.baler-sample.yml, profile
baler-sample-worker), never inside Baler. Replace the body of tick() with
your own job, e.g. computing something and writing it back with
client.his_write (needs an operator API key).
"""

import os
import sys
import time

import baler_fn
from baler_sample.functions import his_stats


def tick(client, point_id):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {point_id}: {his_stats(client, point_id, 'today')}", flush=True)


def main():
    point_id = os.environ.get("BALER_SAMPLE_POINT", "").strip()
    if not point_id:
        sys.exit("BALER_SAMPLE_POINT is not set: the point id the worker summarizes")
    interval = float(os.environ.get("BALER_SAMPLE_INTERVAL_SECONDS") or 3600)
    client = baler_fn.connect()
    while True:
        try:
            tick(client, point_id)
        except Exception as exc:  # noqa: BLE001 - a failed run is logged; the next one still happens
            print(f"tick failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
