"""
Standalone fake Haystack server for trying `haystack_puller` without a
real Haxall/SkySpark instance - see haystack-puller.mdx. Seeded with a
small already-tagged building (one AHU, one VAV, a handful of points,
plus one deliberately-untaggable point) instead of test fixtures, and run
as a long-lived process instead of inside a test. Same SCRAM-over-HTTP
implementation `tests/test_haystack_client.py` verifies against a real
client - see `baler.fake_haystack_server`.

    python -m scripts.mock_haystack_server

Then, in another terminal, against the printed URL/user/password:

    python -m baler.haystack_puller \\
      --haystack-url http://127.0.0.1:<port> \\
      --haystack-user tduser --haystack-password tdpass \\
      --once
"""

import time

from baler.fake_haystack_server import TEST_PASSWORD, TEST_USER, make_fake_haxall_server

EQUIP_ROWS = [
    {"id": "r:ahu-1 AHU-1", "dis": "AHU-1", "ahu": "m:"},
    {"id": "r:vav-1 VAV-1", "dis": "VAV-1", "vav": "m:"},
]

POINT_ROWS = [
    {"id": "r:p-zone-temp Zone Temp", "dis": "Zone Temp", "zone": "m:", "air": "m:", "temp": "m:", "sensor": "m:", "equipRef": "r:vav-1"},
    {"id": "r:p-fan-cmd Fan Cmd", "dis": "Fan Cmd", "fan": "m:", "cmd": "m:", "equipRef": "r:ahu-1"},
    {"id": "r:p-mystery Mystery Point", "dis": "Mystery Point", "totallyUnknownTag": "m:", "equipRef": "r:ahu-1"},
]

HIS_ROWS = {
    "@p-zone-temp": {"rows": [{"ts": f"t:{ts} UTC", "val": "n:71.5"} for ts in ("2026-08-25T00:00:00Z", "2026-08-25T01:00:00Z")]},
    "@p-fan-cmd": {"rows": [{"ts": "t:2026-08-25T00:00:00Z UTC", "val": "m:"}]},
    "@p-mystery": {"rows": []},
}


def main() -> None:
    server, _ = make_fake_haxall_server(
        {
            "about": {"rows": [{"productName": "MockHaxall", "version": "n:4.0"}]},
            "read": {"equip": {"rows": EQUIP_ROWS}, "point": {"rows": POINT_ROWS}},
            "hisRead": HIS_ROWS,
        }
    )
    port = server.server_address[1]
    # flush=True: stdout is fully block-buffered when not attached to a
    # TTY (e.g. piped, or run in the background) - without it this line
    # can sit invisible in the buffer indefinitely instead of reaching
    # whoever's waiting to read the port.
    print(f"mock Haystack server on http://127.0.0.1:{port}", flush=True)
    print(f"  --haystack-user {TEST_USER} --haystack-password {TEST_PASSWORD}", flush=True)
    print("Ctrl-C to stop", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
