"""Devices -> Create connection keeps what Windrow learned (Baler#82).

The BACnet learn picker posted only object_identifier and label, so
Windrow never saw a point's units (or description) and a learned
zone_temp read "no unit recorded" in the tree."""

import pathlib
import re

DEVICES_JS = (pathlib.Path(__file__).resolve().parent.parent / "ui" / "devices.js").read_text()


def test_learned_units_and_description_reach_create_connection():
    picker = DEVICES_JS[DEVICES_JS.index("for (const point of points)"):DEVICES_JS.index("learnList.appendChild")]
    assert re.search(r"picked\.units = point\.units", picker)
    assert re.search(r"picked\.description = point\.description", picker)
    assert "JSON.stringify(picked)" in picker
