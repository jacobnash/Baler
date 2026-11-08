"""
Times the read paths the Equipment, Alarms and Ops pages wait on (Baler#54,
#106, #107) through the gateway, the way the issues measured them: the
whole request, body included. Prints a Markdown table.

  python scripts/bench_reads.py --email operator@perf.test --password ...
  python scripts/bench_reads.py --runs 1 ...     # a cold pass, right after a restart

The first pass of each route is reported separately from the warm ones, so
running it straight after `docker compose restart` shows the cold cost.
"""

import argparse
import statistics
import time

import requests

ROUTES = [
    ("readAll(equip) /ingest/his", "/ingest/his", {"expr": "readAll(equip).hisRead(today)"}),
    ("readAll(equip) /ingest/his?compact=1", "/ingest/his", {"expr": "readAll(equip).hisRead(today)", "compact": "1"}),
    ("/api/read?filter=point", "/api/read", {"filter": "point"}),
    ("/api/read?filter=equip", "/api/read", {"filter": "equip"}),
    # the Equipment page's other two reads (ui/app.js)
    ("/api/read?filter=Site or Building or Floor or Zone", "/api/read", {"filter": "Site or Building or Floor or Zone"}),
    ("/api/read?filter=point and equipRef==@<an AHU>", "/api/read", {"filter": "point and equipRef==@{equip}"}),
    ("/fault/faults", "/fault/faults", {}),
    ("/ops/status", "/ops/status", {}),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--runs", type=int, default=6, help="passes per route; the first is reported as cold")
    ap.add_argument("--equip", default="urn:equip:fbf~2Fperf-north~2Fahu-1", help="the equip (Haystack ref id) the per-equip point read asks about")
    ap.add_argument("--cx-project", help="also time this Cx project's report (?format=html)")
    ap.add_argument("--gzip", action="store_true", help="ask for compressed bodies, as a browser does (default: identity, as curl does)")
    args = ap.parse_args()
    s = requests.Session()
    s.headers["Accept-Encoding"] = "gzip" if args.gzip else "identity"
    for _ in range(60):  # straight after a restart the gateway can 502 until auth_api is up
        r = s.post(f"{args.url}/auth/login", json={"email": args.email, "password": args.password}, timeout=30)
        if r.status_code not in (502, 503, 504):
            break
        time.sleep(1)
    r.raise_for_status()
    s.headers["Authorization"] = "Bearer " + r.json()["token"]
    routes = [(n, path, {k: v.format(equip=args.equip) for k, v in q.items()}) for n, path, q in ROUTES]
    if args.cx_project:
        routes.append(("/cx/projects/:id/report?format=html", f"/cx/projects/{args.cx_project}/report", {"format": "html"}))
    times: dict[str, list[float]] = {name: [] for name, *_ in routes}
    sizes: dict[str, int] = {}
    for _ in range(args.runs):
        for name, path, params in routes:
            t0 = time.perf_counter()
            r = s.get(args.url + path, params=params, timeout=120)
            body = r.content
            times[name].append(time.perf_counter() - t0)
            sizes[name] = r.raw.tell() or len(body)  # bytes on the wire (compressed, with --gzip)
            if r.status_code != 200:
                print(f"{name}: HTTP {r.status_code} {body[:200]!r}")
    print("| Route | First | Warm median | Warm max | Size |\n|---|---|---|---|---|")
    for name, ts in times.items():
        warm = ts[1:] or ts
        print(f"| {name} | {ts[0]:.2f} s | {statistics.median(warm):.2f} s | {max(warm):.2f} s | {sizes[name] / 1024:,.0f} KB |")


if __name__ == "__main__":
    main()
