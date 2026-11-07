"""Connectors -> Modbus register editor sends Windrow's point forms (Baler#128).

A plain holding/uint16 row must stay a bare register number (so old
connectors, and older Windrows, round-trip unchanged); anything else
becomes a point object carrying only the fields that differ from Windrow's
defaults. Runs ui/connectors.js's modbusSpec under node."""

import json
import pathlib
import shutil
import subprocess

import pytest

CONNECTORS_JS = (pathlib.Path(__file__).resolve().parent.parent / "ui" / "connectors.js").read_text()

ROW = {"register": 4, "function": "holding", "type": "uint16", "word_order": "big", "byte_order": "",
       "scale": 1, "offset": 0, "unit": "", "writable": False}


def _spec(**fields):
    start = CONNECTORS_JS.index("const MB_FUNCTIONS")
    end = CONNECTORS_JS.index("function addRegisterRow")
    script = CONNECTORS_JS[start:end] + f"console.log(JSON.stringify(modbusSpec({json.dumps({**ROW, **fields})})));"
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def test_plain_row_stays_a_bare_register_with_unit_in_the_units_map():
    assert _spec() == {"spec": 4, "unit": None}
    assert _spec(unit="°F") == {"spec": 4, "unit": "°F"}
    assert _spec(word_order="little") == {"spec": 4, "unit": None}  # word order is 32-bit only


def test_typed_scaled_writable_row_is_an_object_with_its_unit():
    assert _spec(type="float32", word_order="little", scale=1.8, offset=32, unit="°F", writable=True) == {
        "spec": {"register": 4, "type": "float32", "word_order": "little", "scale": 1.8, "offset": 32,
                 "unit": "°F", "writable": True},
        "unit": None,
    }
    assert _spec(byte_order="little") == {"spec": {"register": 4, "byte_order": "little"}, "unit": None}


def test_bit_functions_drop_register_only_options():
    assert _spec(function="coil", type="int16", scale=2, writable=True) == {
        "spec": {"register": 4, "function": "coil", "writable": True}, "unit": None}
    assert _spec(function="discrete", writable=True, byte_order="little") == {
        "spec": {"register": 4, "function": "discrete"}, "unit": None}
    assert _spec(function="input", writable=True, type="int16") == {
        "spec": {"register": 4, "function": "input", "type": "int16"}, "unit": None}
