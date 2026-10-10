"""The JavaScript value rules the journey ports rely on (jscompat), checked against
Node where it is installed and against fixed values everywhere."""

from __future__ import annotations

import json
import math
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab.journey import jscompat as js

node_missing = shutil.which("node") is None

DATES = [
    "2020-01-01T00:00:00Z",
    "2020-01-01",
    "2020-01",
    "2020",
    "2020T00:00Z",
    "2020-01-01T00:00Z",
    "2020-01-01T00:00:00.5Z",
    "2020-01-01T00:00:00.123456Z",
    "2020-01-01 00:00:00Z",
    "2020-02-31T00:00:00Z",
    "2020-13-01",
    "2020-00-01",
    "2020-01-32",
    "2020-01-00",
    "2020-01-01T24:00:00Z",
    "2020-01-01T24:00:01Z",
    "2020-01-01T25:00:00Z",
    "2020-01-01T00:60:00Z",
    "2020-01-01T00:00:60Z",
    "+002020-01-01T00:00:00Z",
    "-000001-01-01T00:00:00Z",
    "-000000-01-01T00:00:00Z",
    "0000-01-01T00:00:00Z",
    "+275760-09-13T00:00:00Z",
    "+275760-09-13T00:00:00.001Z",
    "2020-01-01T00:00:00+05:30",
    "2020-01-01T00:00:00+0530",
    "2020-01-01T00:00:00-23:59",
    "2020-01-01T00:00:00+24:00",
    "2020-01-01T00:00:00+05:60",
    "2020-01-01Z",
    "2020-01-01+05:00",
    "2020-01-01t00:00:00z",
    "20200101",
    "",
    "garbage",
    " 2020-01-01T00:00:00Z",
]
NUMBER_STRINGS = [
    "",
    "  ",
    "12",
    " 12 ",
    "\u00a012\ufeff",
    "1e3",
    ".5",
    "5.",
    "-.5e-2",
    "+7",
    "0x1F",
    "0b101",
    "0o17",
    "-0x10",
    "0x",
    "Infinity",
    "-Infinity",
    "inf",
    "nan",
    "1_000",
    "12abc",
    "١٢",
]


def _node(script: str, payload: Any) -> Any:
    result = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(payload),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(node_missing, reason="node is not installed")
def test_log2_matches_v8_bit_for_bit() -> None:
    rnd = random.Random(7)
    values = [float(n) for n in range(1, 3000)]
    values += [rnd.uniform(1e-3, 1e4) for _ in range(3000)] + [5e-324, 2.2250738585072014e-308]
    expected = _node(
        "const v=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "console.log(JSON.stringify(v.map((x)=>Math.log2(x))))",
        values,
    )
    assert [js.log2(value) for value in values] == expected


@pytest.mark.skipif(node_missing, reason="node is not installed")
def test_dates_numbers_and_strings_match_node() -> None:
    rnd = random.Random(3)
    fixed = [rnd.uniform(-50, 50) for _ in range(300)] + [0.05, 1.005, 2.5, -2.5, -0.04, 1e21]
    ms = [rnd.uniform(-1e14, 4e12) for _ in range(200)] + [0, -1, 8.64e15, -8.64e15, 1.9]
    alphabet = [chr(code) for code in range(128)] + ["é", "😀"]
    texts = ["".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 4))) for _ in range(600)]
    script = (
        "const p=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "const d=(x)=>{const t=Date.parse(x);return Number.isNaN(t)?null:t;};"
        "console.log(JSON.stringify({"
        "dates:p.dates.map(d),"
        "numbers:p.numbers.map((s)=>String(Number(s))),"
        "fixed:p.fixed.map((x)=>[x.toFixed(1),x.toFixed(2)]),"
        "iso:p.ms.map((m)=>[new Date(m).toISOString(),new Date(m).getUTCFullYear()]),"
        "compare:p.texts.slice(1).map((t,i)=>Math.sign(p.texts[i].localeCompare(t))),"
        "trim:p.texts.map((t)=>t.trim().slice(0,2)),"
        "stringify:JSON.stringify(p.texts)"
        "}))"
    )
    payload = {"dates": DATES, "numbers": NUMBER_STRINGS, "fixed": fixed, "ms": ms, "texts": texts}
    node = _node(script, payload)
    assert [js.date_parse(text) for text in DATES] == node["dates"]
    assert [js.to_string(js.string_to_number(text)) for text in NUMBER_STRINGS] == node["numbers"]
    assert [[js.to_fixed_string(x, 1), js.to_fixed_string(x, 2)] for x in fixed] == node["fixed"]
    assert [[js.iso_from_ms(m), js.utc_year(m)] for m in ms] == node["iso"]
    ascii_pairs = [
        (index, left, right)
        for index, (left, right) in enumerate(zip(texts, texts[1:], strict=False))
        if left.isascii() and right.isascii()
    ]
    for index, left, right in ascii_pairs:
        left_key, right_key = js.locale_compare_key(left), js.locale_compare_key(right)
        assert (left_key > right_key) - (left_key < right_key) == node["compare"][index]
    assert [js.slice_utf16(js.trim(text), 2) for text in texts] == node["trim"]
    assert js.stringify_compact(texts) == node["stringify"]


def test_dates_without_node() -> None:
    assert js.date_parse("2020-01-01T00:00:00Z") == 1577836800000.0
    assert js.date_parse("2020-02-31T00:00:00Z") == 1583107200000.0
    assert js.date_parse("2020-01-01T00:00:00+0530") == 1577817000000.0
    assert js.date_parse("2020-01-01T24:00:01Z") is None
    assert js.date_parse("2020-01-01+05:00") is None
    assert js.date_parse("2020-01-01T00:00:00+24:00") is None
    assert js.date_parse("-000000-01-01T00:00:00Z") is None
    assert js.date_parse("+275760-09-13T00:00:00.001Z") is None
    assert js.date_parse(None) is None
    local = js.date_parse("2020-07-01T12:00:00")
    assert local is not None
    assert abs(local - 1593604800000.0) <= 14 * 3_600_000
    assert js.date_parse("+010000-01-01T00:00:00") == 253402300800000.0
    assert js.iso_from_ms(253402300800000.0) == "+010000-01-01T00:00:00.000Z"
    assert js.iso_from_ms(-62198755200000.0) == "-000001-01-01T00:00:00.000Z"
    assert js.utc_year(-1) == 1969
    with pytest.raises(ValueError, match="Invalid time value"):
        js.iso_from_ms(math.nan)
    assert js.now_ms() > 1.7e12


def test_values_and_coercions() -> None:
    assert repr(js.MISSING) == "MISSING"
    assert not js.MISSING
    assert js.get({"a": None}, "a") is None
    assert js.get({"a": 1}, "b") is js.MISSING
    assert js.get([1], "a") is js.MISSING
    assert js.coalesce(js.MISSING, 2) == 2
    assert js.coalesce(0, 2) == 0
    truthy = [True, 1, -0.5, "x", [], {}]
    falsy = [js.MISSING, None, False, 0, 0.0, math.nan, ""]
    assert all(js.truthy(value) for value in truthy)
    assert not any(js.truthy(value) for value in falsy)
    assert js.is_finite(3) and not js.is_finite(True) and not js.is_finite(math.inf)
    assert not js.is_finite("3")
    numbers = {
        js.MISSING: math.nan,
        None: 0.0,
        False: 0.0,
        True: 1.0,
        "0x1G": math.nan,
    }
    for value, expected in numbers.items():
        result = js.to_number(value)
        assert result == expected or (math.isnan(result) and math.isnan(expected))
    assert js.to_number(7) == 7.0
    assert js.to_number([]) == 0.0
    assert js.to_number([" 5 "]) == 5.0
    assert js.to_number([1, 2]) != js.to_number([1, 2])
    assert math.isnan(js.to_number({"a": 1}))
    strings = {
        "undefined": js.MISSING,
        "null": None,
        "true": True,
        "false": False,
        "NaN": math.nan,
        "Infinity": math.inf,
        "-Infinity": -math.inf,
        "1.5": 1.5,
        "2": 2.0,
        ",1,,a": [None, 1, js.MISSING, "a"],
        "[object Object]": {"a": 1},
    }
    for expected, value in strings.items():
        assert js.to_string(value) == expected
    assert math.isnan(js.js_max(1, math.nan, 3))
    assert js.js_max() == -math.inf
    assert js.js_min() == math.inf
    assert math.isnan(js.js_min(1, math.nan))
    assert js.js_min(3, 1, 2) == 1
    assert js.to_fixed_string(math.nan, 1) == "NaN"
    assert js.to_fixed_string(-0.0, 1) == "0.0"
    assert js.to_fixed_string(1e21, 1) == "1e+21"
    assert js.to_fixed_string(-0.04, 1) == "-0.0"
    assert js.utf16_key("😀") < js.utf16_key("\uffff")
    assert js.slice_utf16("abc", 5) == "abc"
    assert js.locale_compare_key("é") > js.locale_compare_key("z")
    assert js.locale_compare_key("a\x01b") == js.locale_compare_key("ab")


def test_json_rules(tmp_path: Path) -> None:
    payload = {
        "b": True,
        "n": None,
        "f": 1.0,
        "s": "é\ud83d",
        "skip": js.MISSING,
        "l": [js.MISSING, 2],
        "t": (False,),
    }
    assert js.stringify_compact(payload) == (
        '{"b":true,"n":null,"f":1,"s":"é\\ud83d","l":[null,2],"t":[false]}'
    )
    with pytest.raises(TypeError, match="not JSON-serializable"):
        js.stringify_compact({1, 2})
    path = tmp_path / "x.json"
    path.write_text('{"b": 1, "10": 2, "2": 3, "a": 4, "b": 5, "4294967295": 6, "01": 7}')
    assert list(js.read_json(path).items()) == [
        ("2", 3),
        ("10", 2),
        ("b", 5),
        ("a", 4),
        ("4294967295", 6),
        ("01", 7),
    ]
    assert list(js.parse_json('{"z": 1, "y": 2}')) == ["z", "y"]
    with pytest.raises(ValueError, match="Unexpected token"):
        js.parse_json("[NaN]")


def test_log2_special_values() -> None:
    assert js.log2(0.0) == -math.inf
    assert math.isnan(js.log2(-1.0))
    assert js.log2(math.inf) == math.inf
    assert js.log2(1.0) == 0.0
    assert js.log2(5e-324) == -1074.0
    assert js.log2(8.0) == 3.0
    assert js.log2(1375.0) == 10.425215903299385
