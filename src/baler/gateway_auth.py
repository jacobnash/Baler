"""
The one piece of auth code every backend needs, and no more: confirm a
request actually came through the gateway (which sets this header after
its own js_access role/site/rate-limit checks pass - see
gateway/njs/main.js) rather than hitting this container's port directly
from a sibling on the same compose network. Not a second trust boundary
or a place for real authz logic - the gateway is still the only place
that decides who can do what.
"""

import hmac
import os


def request_came_through_gateway(handler) -> bool:
    expected = os.environ.get("BALER_GATEWAY_SECRET")
    if not expected:
        # Unset means the check can't mean anything - fail closed rather
        # than silently accepting everything.
        return False
    got = handler.headers.get("X-Gateway-Secret", "")
    return hmac.compare_digest(got, expected)
