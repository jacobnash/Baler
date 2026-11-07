"""
BACnet controller alarms from Windrow (Baler#168, the Baler half of #164).

A Windrow connection with `forward_alarms` publishes a controller's
intrinsic-reporting alarms, retained, on

    {topic_prefix}/{label}/alarm               an alarm on a polled point
    {topic_prefix}/alarms/{type}-{instance}    one on an object nobody polls

as the usual {point, value, ts} envelope with the event as `value` (Windrow's
openapi.yaml, AlarmEvent). They are alarms, not readings: mqtt_listener skips
them (is_alarm), and fault_detector raises and clears them as faults here,
so they show on the Alarms page, in GET /faults and through webhooks like
any rule's fault.

- One fault per object: rule_id `bacnet:alarm` and the point's own URI
  (`{prefix}/{label}`; the `alarms/...` topic itself for an unpolled object).
  The `bacnet:` namespace keeps fault_detector's orphan sweep, which closes
  faults whose rule was deleted, away from them.
- A notification to a non-normal state, or a startup summary entry not in
  `normal`, opens the fault, or updates its detail and severity if it's
  already open (high-limit -> low-limit). A notification back to `normal`,
  or a summary entry in `normal`, resolves it. Retained replays are
  idempotent.
- Severity: a `fault` state is critical; notify_type `event` is info;
  otherwise BACnet priority 0-63 (the standard's life-safety and critical
  band) is critical, and the rest are warning.

Acknowledging a fault in Baler is Baler's own ack; it isn't sent to the
controller (AcknowledgeAlarm isn't in Windrow yet).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from baler import faults
from baler.ingest import topic_to_point_uri

RULE_ID = "bacnet:alarm"


def is_alarm(topic: str, value) -> bool:
    """A Windrow alarm message, by topic shape and payload. Both: a point
    someone named `alarm` publishes a plain value, never an event dict."""
    if not isinstance(value, dict) or "object" not in value:
        return False
    if "to_state" not in value and "event_state" not in value:
        return False
    return topic.endswith("/alarm") or "/alarms/" in topic


def alarm_point_uri(topic: str) -> str:
    if topic.endswith("/alarm"):
        return str(topic_to_point_uri(topic[: -len("/alarm")]))
    return str(topic_to_point_uri(topic))


def alarm_state(event: dict) -> str | None:
    """The state the object is in now: a notification's to_state, or a
    summary's event_state."""
    return event.get("to_state") or event.get("event_state")


def severity(event: dict) -> str:
    if alarm_state(event) == "fault":
        return "critical"
    if event.get("notify_type") == "event":
        return "info"
    priority = event.get("priority")
    if isinstance(priority, int) and not isinstance(priority, bool) and priority <= 63:
        return "critical"
    return "warning"


def _when(event: dict, ts) -> datetime:
    for candidate in (event.get("ts"), ts):
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return datetime.fromtimestamp(candidate, tz=timezone.utc)
    return datetime.now(timezone.utc)


def handle_alarm(conn, topic: str, event: dict, ts=None, fire: Callable[..., None] | None = None) -> str | None:
    """Open, update or resolve the fault for this alarm message. Returns
    'opened', 'updated', 'resolved' or None (nothing changed). `fire(event,
    rule_id, point_uri, status, detail)` is called for an opening or a
    resolution, the way fault_detector fires its webhooks."""
    point_uri = alarm_point_uri(topic)
    state = alarm_state(event)
    when = _when(event, ts)
    detail = {k: v for k, v in event.items() if v is not None}
    if state is None:
        return None
    if state == "normal":
        fault_id = faults.close_fault(conn, RULE_ID, point_uri, when)
        if fault_id is None:
            return None
        if fire:
            fire("fault.resolved", RULE_ID, point_uri, "resolved", detail)
        return "resolved"
    fault_id = faults.open_fault(conn, RULE_ID, point_uri, when, detail=detail, severity=severity(event))
    if fault_id is not None:
        if fire:
            fire("fault.opened", RULE_ID, point_uri, "open", detail)
        return "opened"
    # already open: keep it current (a summary replay, or another off-normal state)
    if faults.update_open_fault(conn, RULE_ID, point_uri, detail, severity(event)):
        return "updated"
    return None
