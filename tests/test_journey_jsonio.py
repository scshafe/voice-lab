"""The JavaScript number and JSON rules the ported manifests rely on."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cole_voice_lab.journey import jsonio

NUMBERS = [
    0.0,
    -0.0,
    1.0,
    -1.0,
    0.1,
    0.5,
    1.5,
    2.5,
    100.0,
    123.456,
    -123.456,
    1e-7,
    1.5e-7,
    1e-6,
    0.000001234,
    1e20,
    1e21,
    1.5e21,
    1e16,
    12345678901234567890.0,
    3.141592653589793,
    5e-324,
    1.7976931348623157e308,
    0.30000000000000004,
    1e-5,
    2**53,
    0.001,
    1234.5e10,
]
FIXED = [
    (0.125, 2),
    (-0.125, 2),
    (1.005, 2),
    (2.5, 0),
    (-2.5, 0),
    (0.0005, 3),
    (1.23456, 3),
    (123.4567, 1),
    (1e-10, 3),
    (-0.0004, 3),
    (99.9995, 3),
]


def _node(script: str) -> str:
    return subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout


node_missing = subprocess.run(["which", "node"], capture_output=True).returncode != 0


def test_numbers_format_as_javascript_does() -> None:
    expected = {
        0.0: "0",
        1.0: "1",
        0.1: "0.1",
        100.0: "100",
        1e-7: "1e-7",
        1e-6: "0.000001",
        1e20: "100000000000000000000",
        1e21: "1e+21",
        1.5e21: "1.5e+21",
        -123.456: "-123.456",
        1e16: "10000000000000000",
    }
    for value, text in expected.items():
        assert jsonio.format_number(value) == text
    assert jsonio.format_number(-0.0) == "0"
    assert jsonio.format_number(float("nan")) == "null"
    assert jsonio.format_number(float("inf")) == "null"
    assert jsonio.format_number(7) == "7"
    assert jsonio.format_number(True) == "true"


@pytest.mark.skipif(node_missing, reason="node is not installed")
def test_numbers_and_to_fixed_match_node_exactly() -> None:
    script = (
        f"const n={json.dumps(NUMBERS)};const f={json.dumps(FIXED)};"
        "console.log(JSON.stringify({n:n.map(String),"
        "f:f.map(([v,p])=>Number(v.toFixed(p))).map(String)}))"
    )
    node = json.loads(_node(script))
    assert [jsonio.format_number(value) for value in NUMBERS] == node["n"]
    fixed = [jsonio.to_fixed(value, places) for value, places in FIXED]
    assert [jsonio.format_number(value) for value in fixed if value is not None] == node["f"]


@pytest.mark.skipif(node_missing, reason="node is not installed")
def test_stringify_matches_json_stringify_with_two_spaces() -> None:
    payload = {
        "a": [1, 2.0, {"b": None, "c": 'é\n"x" '}],
        "empty": [],
        "obj": {},
        "t": True,
        "nan": float("nan"),
        "nested": [[], [{}], [1e-7]],
    }
    script = (
        "const p={a:[1,2,{b:null,c:'é\\n\"x\"\\u2028'}],empty:[],obj:{},t:true,nan:NaN,"
        "nested:[[],[{}],[1e-7]]};process.stdout.write(JSON.stringify(p,null,2))"
    )
    assert jsonio.stringify(payload) == _node(script)


def test_to_fixed_and_js_round() -> None:
    assert jsonio.to_fixed(0.125, 2) == 0.13
    assert jsonio.to_fixed(-0.125, 2) == -0.13
    assert jsonio.to_fixed(None) is None
    assert jsonio.to_fixed(float("nan")) is None
    assert jsonio.to_fixed(True) is None
    assert jsonio.js_round(2.5) == 3.0
    assert jsonio.js_round(-2.5) == -2.0
    with pytest.raises(TypeError):
        jsonio.stringify({"x": object()})


def test_write_json_is_atomic_and_announces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "deep" / "m.json"
    jsonio.write_json(target, {"x": 1.0})
    assert target.read_text() == '{\n  "x": 1\n}\n'
    assert capsys.readouterr().out == f"{target}\n"
    assert jsonio.read_json(target) == {"x": 1}
    jsonio.write_json("-", [1])
    assert capsys.readouterr().out == "[\n  1\n]\n"
    jsonio.write_json(target, {}, announce=False)
    assert capsys.readouterr().out == ""
    assert sorted(path.name for path in target.parent.iterdir()) == ["m.json"]


def test_write_json_removes_its_temporary_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(source: str, destination: str) -> None:
        raise OSError("refused")

    monkeypatch.setattr(jsonio.os, "replace", refuse)
    with pytest.raises(OSError, match="refused"):
        jsonio.write_json(tmp_path / "m.json", {})
    assert list(tmp_path.iterdir()) == []


def test_iso_timestamp_is_to_iso_string() -> None:
    moment = datetime(2026, 10, 9, 7, 5, 3, 45678, tzinfo=UTC)
    assert jsonio.iso_timestamp(moment) == "2026-10-09T07:05:03.045Z"
    assert jsonio.iso_timestamp().endswith("Z")
