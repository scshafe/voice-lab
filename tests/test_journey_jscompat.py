"""The JavaScript value rules the journey ports rely on (jscompat), checked against
Node where it is installed and against fixed values everywhere."""

from __future__ import annotations

import functools
import json
import math
import os
import random
import shutil
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import pytest

from cole_voice_lab.journey import jscompat as js
from cole_voice_lab.journey import jsonio

NODE = shutil.which("node")
node_missing = NODE is None

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


@pytest.fixture
def chicago(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _node_env(script: str, **env: str) -> str:
    assert NODE is not None
    return subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **env},
    ).stdout


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
    assert jsonio.stringify_compact(texts) == node["stringify"]


@pytest.mark.usefixtures("chicago")
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
    assert js.date_parse("+010000-01-01T00:00:00") == 253402322400000.0
    assert js.iso_from_ms(253402300800000.0) == "+010000-01-01T00:00:00.000Z"
    assert js.iso_from_ms(-62198755200000.0) == "-000001-01-01T00:00:00.000Z"
    assert js.utc_year(-1) == 1969
    with pytest.raises(ValueError, match="Invalid time value"):
        js.iso_from_ms(math.nan)
    assert js.now_ms() > 1.7e12


def test_values_and_coercions() -> None:
    assert repr(js.MISSING) == "undefined"
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
    assert not js.is_finite(10**400)
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


def test_log2_special_values() -> None:
    assert js.log2(0.0) == -math.inf
    assert math.isnan(js.log2(-1.0))
    assert js.log2(math.inf) == math.inf
    assert js.log2(1.0) == 0.0
    assert js.log2(5e-324) == -1074.0
    assert js.log2(8.0) == 3.0
    assert js.log2(1375.0) == 10.425215903299385


# V8's Math.log10 (Node 24), as hex: where the C library's log10 differs in the last bit,
# and every branch of fdlibm's log and log10.
LOG10_VECTORS = [
    ("0x1.2b92ebf2bb5cdp-1", "-0x1.dcb4678844f28p-3"),
    ("0x1.b7773b028db5fp-1", "-0x1.0fbfb456a00dap-4"),
    ("0x1.9f656de75fb0fp-2", "-0x1.913dd4b614431p-2"),
    ("0x1.416eebe085123p-1", "-0x1.9e10150ddfdddp-3"),
    ("0x1.4d90c33dd0ef0p-1", "-0x1.7d1c28448454bp-3"),
    ("0x1.6ef2e6fe3bc5bp-1", "-0x1.2845d5275b927p-3"),
    ("0x1.a30c3eae09e0fp-1", "-0x1.6460c8d9cc336p-4"),
    ("0x1.65ce3dba482e7p-1", "-0x1.3eb7298d9ca6fp-3"),
    ("0x1.0000000000000p-1", "-0x1.34413509f79ffp-2"),
    ("0x1.0000000000001p-1", "-0x1.34413509f79fdp-2"),
    ("0x1.fffffffffffffp-1", "-0x1.bcb7b1526e50ep-55"),
    ("0x1.0000000000001p+0", "0x1.bcb7b1526e50dp-54"),
    ("0x1.fffffffffffffp+0", "0x1.34413509f79fep-2"),
    ("0x1.0000000000001p+1", "0x1.34413509f7a01p-2"),
    ("0x1.6a09e667f3bcdp-1", "-0x1.34413509f79fep-3"),
    ("0x1.6a09e667f3bcdp+0", "0x1.34413509f79ffp-3"),
    ("0x0.0000000000001p-1022", "-0x1.434e6420f4374p+8"),
    ("0x1.0000000000000p-1022", "-0x1.33a7146f72a42p+8"),
    ("0x1.7e43c8800759cp+996", "0x1.2c00000000000p+8"),
    ("0x1.47ae147ae147cp-7", "-0x1.0000000000000p+1"),
    ("0x1.4000000000000p+0", "0x1.8cf183886480dp-4"),
    ("0x1.8000000000000p-1", "-0x1.ffbfc2bbc7802p-4"),
    ("0x1.3333333333333p-1", "-0x1.c658a32216008p-3"),
    ("0x1.999999999999ap+0", "0x1.a209a84fbcff8p-3"),
    ("0x1.d65c995d9999ap+0", "0x1.0e885900280eep-2"),
    ("0x1.56b3841b9999ap-1", "-0x1.6513dcf17e8b1p-3"),
    ("0x1.05d4a6f233334p+0", "0x1.407d3f74abd63p-7"),
    ("0x1.4b23378666666p+0", "0x1.c9cdc0bef9d52p-4"),
    ("0x1.47a4c83600000p+0", "0x1.b6ef52a70aa91p-4"),
    ("0x1.3d171ede66667p-1", "-0x1.aa298233889dbp-3"),
    ("0x1.6a7c2a30ccccdp-1", "-0x1.3328a9417969bp-3"),
    ("0x1.0000000000000p+0", "0x0.0p+0"),
    ("0x1.0000000000000p+1", "0x1.34413509f79ffp-2"),
]


def test_log10_is_v8s_fdlibm_log10_bit_for_bit() -> None:
    for argument, expected in LOG10_VECTORS:
        assert js.log10(float.fromhex(argument)).hex() == float.fromhex(expected).hex()
    assert math.isnan(js.log10(math.nan))
    assert math.isnan(js.log10(-1.0))
    assert js.log10(0.0) == -math.inf
    assert js.log10(math.inf) == math.inf


def _node_input(script: str, payload: Any) -> Any:
    """Run ``script`` with ``input`` (the JSON payload) and return what it prints."""

    source = f"const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n{script}"
    completed = subprocess.run(
        ["node", "-e", source],
        input=json.dumps(payload),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


@pytest.mark.skipif(node_missing, reason="node is not installed")
def test_log10_matches_node_on_frame_rms_values() -> None:
    rng = random.Random(7)
    values = [math.sqrt(rng.randrange(1, 2**40) / 2**30 / 1024) for _ in range(20000)]
    node = _node_input("console.log(JSON.stringify(input.map(Math.log10)))", values)
    assert [js.log10(value) for value in values] == node


def test_locale_compare_is_icu_root_collation_for_ascii() -> None:
    order = "\t _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$0123456789aAbBzZ"
    chars = sorted(order, key=lambda c: (ord(c) * 7919) % 101)
    assert "".join(sorted(chars, key=functools.cmp_to_key(js.locale_compare))) == order
    assert js.locale_compare("ab", "Aa") == 1
    assert js.locale_compare("a-b", "ab") == -1
    assert js.locale_compare("a", "A") == -1
    assert js.locale_compare("a\x01b", "ab") == 0
    assert js.locale_compare("é", "é") == 0


@pytest.mark.skipif(node_missing, reason="node is not installed")
def test_locale_compare_matches_node_for_ascii_filenames() -> None:
    rng = random.Random(11)
    alphabet = "aAbBzZ09 -_.()'~$"
    names = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 5))) for _ in range(300)]
    node = _node_input(
        "console.log(JSON.stringify(input.sort((a, b) => a.localeCompare(b))))", names
    )
    assert sorted(names, key=functools.cmp_to_key(js.locale_compare)) == node


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4", 4.0),
        (" 4\n", 4.0),
        ("", 0.0),
        ("4.0", 4.0),
        ("1e3", 1000.0),
        ("+.5", 0.5),
        ("0x10", 16.0),
        ("0B11", 3.0),
        ("0o17", 15.0),
        ("0x", math.nan),
        ("0x 1", math.nan),
        ("-0x10", math.nan),
        ("1_000", math.nan),
        ("inf", math.nan),
        ("Infinity", math.inf),
        ("-Infinity", -math.inf),
        ("four", math.nan),
        (None, 0.0),
        (True, 1.0),
        (False, 0.0),
        (js.MISSING, math.nan),
        ([], 0.0),
    ],
)
def test_to_number(text: Any, expected: float) -> None:
    result = js.to_number(text)
    assert result == expected or (math.isnan(result) and math.isnan(expected))


def test_to_string() -> None:
    values = [js.MISSING, None, True, False, 0.5, 1.0, math.nan, math.inf, -math.inf]
    assert [js.to_string(v) for v in values] == [
        "undefined",
        "null",
        "true",
        "false",
        "0.5",
        "1",
        "NaN",
        "Infinity",
        "-Infinity",
    ]
    assert js.to_string(["a", None, 2]) == "a,,2"
    assert js.to_string({"a": 1}) == "[object Object]"
    assert js.to_string("text") == "text"
    assert repr(js.MISSING) == "undefined"


def test_path_join_is_node_path_join() -> None:
    join = js.path_join
    assert join("/corpus", "a b.m4a") == "/corpus/a b.m4a"
    assert join("corpus/", "/x/../y.m4a") == "corpus/y.m4a"
    assert join("//corpus", "y") == "/corpus/y"
    assert join("", "") == "."


def test_js_number_follows_number_of_a_string() -> None:
    cases = {
        "": 0.0,
        " \t7\n": 7.0,
        "+5": 5.0,
        ".5e1": 5.0,
        "12.": 12.0,
        "0b11": 3.0,
        "0O17": 15.0,
        "0x1f": 31.0,
        "Infinity": math.inf,
        "+Infinity": math.inf,
        "-Infinity": -math.inf,
    }
    for text, expected in cases.items():
        assert js.string_to_number(text) == expected
    for text in ["inf", "nan", "1_0", "-0x10", "0b2", "abc", "1e"]:
        assert math.isnan(js.string_to_number(text))


def test_js_truthiness_and_length() -> None:
    assert not js.truthy(math.nan)
    assert not js.truthy("")
    assert js.truthy({})
    assert js.truthy([])
    assert js.length("a\U0001f600") == 3
    assert js.length({}) is js.MISSING


def test_js_join_appends_every_part() -> None:
    assert js.path_join("/corpus/", "/abs/name.m4a") == "/corpus/abs/name.m4a"
    assert js.path_join("", "x.json") == "x.json"
    assert js.path_join("./a/../b", "c") == "b/c"
    assert js.path_join("", "") == "."


def test_string_to_number_reads_index_limits() -> None:
    assert js.string_to_number(" 0x1F\n") == 31
    assert js.string_to_number("0o17") == 15
    assert js.string_to_number("0B101") == 5
    assert js.string_to_number("1e2") == 100
    assert js.string_to_number("5.") == 5
    assert js.string_to_number(".5") == 0.5
    assert js.string_to_number("-Infinity") == -math.inf
    assert js.string_to_number("\u00a0\ufeff") == 0
    assert js.string_to_number("0x" + "f" * 300) == math.inf
    for text in ("1_0", "inf", "nan", "-0x1", "0x", "1e", "e5", "12px"):
        assert math.isnan(js.string_to_number(text))


def test_locale_compare_key_is_icu_root_collation_for_ascii() -> None:
    names = ["aB", "Ab", "ab", "AB", "a", "A", "b", "_x", "-x", "1", "x:y", "x~y", "x$y"]
    ordered = sorted(names, key=js.locale_compare_key)
    assert ordered == ["_x", "-x", "1", "a", "A", "ab", "aB", "Ab", "AB", "b"] + [
        "x:y",
        "x~y",
        "x$y",
    ]
    assert js.locale_compare_key("a\x01b") == js.locale_compare_key("ab")
    assert js.locale_compare_key("z") < js.locale_compare_key("\u00e9")


def test_js_path_helpers_follow_node() -> None:
    cases = {
        "a/..wav": ".wav",
        "..m4a": ".m4a",
        "sid/": "",
        "sid/.hidden": "",
        "x.": ".",
        "a.b.M4A": ".M4A",
        "/x/y.tar.gz": ".gz",
        "a/b/.": "",
        "a/b/..": "",
        "...": ".",
        "a/.b.c": ".c",
        "": "",
    }
    assert {path: js.path_extname(path) for path in cases} == cases
    assert js.path_join("/r", "sid", "/etc/x") == "/r/sid/etc/x"
    assert js.path_join("/r", "sid", "") == "/r/sid"
    assert js.path_join("/r", "sid", "../../x") == "/x"
    assert js.path_join("/r", "sid", "x/") == "/r/sid/x/"
    assert js.path_join("", "") == "."
    assert js.path_join("//a", "b") == "/a/b"
    assert js.path_resolve("//a/b/") == "/a/b"
    assert js.path_resolve("/") == "/"
    assert js.path_resolve("rel") == os.path.join(os.getcwd(), "rel")


def test_values_follow_javascript() -> None:
    undefined = js.MISSING
    assert repr(undefined) == "undefined"
    assert [js.to_string(v) for v in [undefined, None, True, False, 2026.0, math.nan]] == [
        "undefined",
        "null",
        "true",
        "false",
        "2026",
        "NaN",
    ]
    assert js.to_string(-math.inf) == "-Infinity"
    assert js.to_string(math.inf) == "Infinity"
    assert js.to_string([1, None, "a", [2, 3]]) == "1,,a,2,3"
    assert js.to_string({}) == "[object Object]"
    assert js.to_number(undefined) != js.to_number(undefined)
    assert js.to_number(None) == 0
    assert js.to_number(True) == 1
    assert js.to_number(False) == 0
    assert js.to_number(10**400) == math.inf
    assert js.to_number([" 7 "]) == 7
    assert math.isnan(js.to_number({}))
    assert jsonio.object_key_order(["b", "2019", "999", "a", "4294967295", "01"]) == [
        "999",
        "2019",
        "b",
        "a",
        "4294967295",
        "01",
    ]
    assert js.path_join("", "") == "."
    assert js.path_join("//dir/../x", "id.json") == "/x/id.json"
    assert js.path_join("store", "/abs.json") == "store/abs.json"
    assert js.length("abc") == 3
    assert js.truthy([]) and js.truthy({}) and not js.truthy(math.nan)
    assert not js.truthy(0) and not js.truthy("") and js.truthy("0")
    with pytest.raises(TypeError, match="reading 'x'"):
        js.prop(undefined, "x")


def test_date_parse_iso_forms() -> None:
    parse = js.date_parse
    assert parse("2019-02-10T00:00:00Z") == 1549756800000
    assert parse("2019-02-10T00:00:00.1234Z") == 1549756800123
    assert parse("+275760-09-13T00:00:00.001Z") is None
    assert parse("-000000-01-01") is None
    assert parse("2019-02-10T00:00:00+24:00") is None
    assert parse("2019-02-10T24:00:01Z") is None
    assert parse("2019-13-01") is None
    assert parse("2019/02/10") is None
    assert parse("2019-02-10T00:00:00-01:00") == 1549760400000
    assert js._utc_offset_ms(10**20) == 0


JS_DATES = [
    "2019-02-10T00:00:00Z",
    "2019-02-10T00:00:00.123456Z",
    "2019-02-10t00:00:00z",
    "2019-02-10T00:00Z",
    "2019-02-10T00Z",
    "2019-02-10",
    "2019-02",
    "2019",
    "2019Z",
    "2019T10:00Z",
    "2019-02-10Z",
    "2019-02-10T00:00:00",
    "2019-02-10T10:00:00.5",
    "2019-02-10 00:00:00",
    "2019-02-10 00:00:00Z",
    "2019-02-10T00:00:00+01:00",
    "2019-02-10T00:00:00+0100",
    "2019-02-10T00:00:00+23:59",
    "2019-02-10T00:00:00+24:00",
    "2019-02-10T00:00:00+01:60",
    "2019-02-10T10:00+01",
    "2019-02-10T24:00:00Z",
    "2019-02-10T24:00:00.001Z",
    "2019-02-10T25:00:00Z",
    "2019-02-10T00:60:00Z",
    "2019-02-10T00:00:60Z",
    "+002019-02-10T00:00:00Z",
    "-000001-01-01T00:00:00Z",
    "-000000-01-01T00:00:00Z",
    "+275760-09-13T00:00:00Z",
    "+275760-09-13T00:00:00.001Z",
    "2019-02-30T00:00:00Z",
    "2019-04-31",
    "2019-02-32",
    "2019-00-10",
    "2019-02-00",
    "2019-13-01",
    "20190210",
    "2019-02-10T00:00:00.Z",
    "2019-02-10T00:00:00Z ",
    "1900-01-01T00:00:00",
    "2019-03-10T02:30:00",
    "2019-11-03T01:30:00",
    "+010000-01-01T00:00:00",
    "0000-06-01T12:00:00",
    "1800-06-01T12:00:00",
    "-000001-06-01T00:00:00",
    "2019-02-10+01:00",
    "garbage",
    "",
]
JS_NUMBERS = [
    "",
    "  ",
    "12",
    " 12 ",
    "+5",
    "-5",
    "1e3",
    ".5",
    "5.",
    "0x1F",
    "0o17",
    "0b101",
    "-0x10",
    "Infinity",
    "-Infinity",
    "infinity",
    "1_000",
    "nan",
    "1e400",
    "0x",
    " 12﻿",
    "\x1c12",
    "07",
    "1.5e-3",
    "e5",
    "--1",
]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.usefixtures("chicago")
def test_date_parse_and_number_match_node_in_chicago() -> None:
    script = (
        f"const d={json.dumps(JS_DATES)};const n={json.dumps(JS_NUMBERS)};"
        "console.log(JSON.stringify({d:d.map(x=>{const v=Date.parse(x);"
        "return Number.isNaN(v)?null:v}),n:n.map(x=>String(Number(x)))}))"
    )
    node = json.loads(_node_env(script, TZ="America/Chicago"))
    assert [js.date_parse(text) for text in JS_DATES] == node["d"]
    assert [js.to_string(js.string_to_number(t)) for t in JS_NUMBERS] == node["n"]


PATHS = [["/a", "/b"], ["a/", "b/"], ["", "x"], ["//a//b", "../c"], ["a", "", ".."], ["./x"]]
BASENAMES = ["a/b/", "/", "", "x.m4a", "a//b//", ".", "..", "/a/b/c.txt"]


@pytest.mark.skipif(node_missing, reason="node is not installed")
def test_path_join_and_basename_match_node() -> None:
    node = _node_input(
        "const p=require('path');"
        "console.log(JSON.stringify([input.j.map((a)=>p.join(...a)),input.b.map((x)=>p.basename(x))]))",
        {"j": PATHS, "b": BASENAMES},
    )
    assert [
        [js.path_join(*parts) for parts in PATHS],
        [js.path_basename(b) for b in BASENAMES],
    ] == (node)
    assert js.path_basename("a/b/") == "b" and js.path_basename(".") == "."


def test_utf8_and_defined_follow_node() -> None:
    assert js.utf8("a\ud800b\U0001f600") == b"a\xef\xbf\xbdb\xf0\x9f\x98\x80"
    assert js.defined({"a": js.MISSING, "b": None}, c=js.MISSING, d=0) == {"b": None, "d": 0}
    assert js.coalesce(js.MISSING, None, 3) == 3
    assert js.coalesce(js.MISSING, None) is None
    assert js.length({"length": 2}) == 2 and js.length(None) is js.MISSING
