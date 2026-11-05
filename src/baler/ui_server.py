"""
Serves ui/ with Cache-Control: no-store on every response. A bare
`python -m http.server` leaves JS/CSS caching to browser heuristics -
confirmed live: after editing app.js, a browser tab kept running the old
cached copy (silently, no error) until a hard refresh. This UI is a
handful of small local files with no perf reason to ever be cached, so
just turn caching off instead of asking every future editor to remember
to hard-refresh.
"""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet by default, matching every other process in this repo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8004)
    parser.add_argument("--directory", default="ui")
    args = parser.parse_args()

    handler = partial(NoCacheHandler, directory=args.directory)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"UI listening on {args.host}:{args.port} (serving {args.directory}, no-store)")
    server.serve_forever()


if __name__ == "__main__":
    main()
