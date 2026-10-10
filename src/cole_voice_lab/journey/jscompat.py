"""JavaScript value rules the ported journey modules need beyond ``jsonio``.

voice-journey's modules read loosely typed JSON and lean on JavaScript's coercions:
``undefined`` versus ``null`` (``?.``, ``??``, a key whose value is ``undefined`` is
dropped by ``JSON.stringify``), truthiness, ``Number(x)`` and ``String(x)``,
``Date.parse``, ``toFixed`` as a string, ``Math.log2`` and UTF-16 string order. The
helpers here are those rules, so the ports can keep the JavaScript expressions one to
one. ``MISSING`` stands for ``undefined``; ``None`` is ``null``.

Known limits, each outside what the pipeline's manifests contain: ``date_parse``
implements the ECMAScript date-time string format with V8's leniencies (lower-case
``t``/``z``, a space before the time, ``+hhmm`` offsets, day-of-month overflow up to 31,
any number of fraction digits) but not V8's legacy fallback parser (``"Jan 1 2020"``,
``"2020-1-1"``), which it reports as ``None`` (NaN). ``log2`` is V8's (fdlibm's)
algorithm, bit for bit on a platform without fused multiply-add contraction.
"""

from __future__ import annotations

import json
import math
import re
import struct
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final

from cole_voice_lab.journey.jsonio import format_number


class _Missing:
    """JavaScript's ``undefined``: absent, as opposed to ``null``."""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _Missing()

# ECMAScript WhiteSpace and LineTerminator code points (String.prototype.trim, Number()).
_JS_SPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_DECIMAL = re.compile(r"[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|Infinity)", re.ASCII)
_RADIX = {"x": 16, "X": 16, "o": 8, "O": 8, "b": 2, "B": 2}
_RADIX_LITERAL = re.compile(r"0([xXoObB])([0-9a-fA-F]+)")
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_ISO_DATE = re.compile(
    r"([+-]\d{6}|\d{4})(?:-(\d{2})(?:-(\d{2}))?)?"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?"
    r"(?:([Zz])|([+-])(\d{2}):?(\d{2}))?",
    re.ASCII,
)
_MAX_TIME_MS = 8.64e15
_MS_PER_DAY = 86_400_000


def get(value: Any, key: str) -> Any:
    """``value?.[key]``: ``MISSING`` when ``value`` is not an object or lacks ``key``."""

    if isinstance(value, Mapping):
        return value.get(key, MISSING)
    return MISSING


def nullish(value: Any) -> bool:
    return value is None or value is MISSING


def coalesce(value: Any, default: Any) -> Any:
    """``value ?? default``."""

    return default if nullish(value) else value


def truthy(value: Any) -> bool:
    if nullish(value) or value is False:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, int | float):
        return value == value and value != 0
    if isinstance(value, str):
        return value != ""
    return True


def is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def is_finite(value: Any) -> bool:
    """``Number.isFinite``: a finite number, no coercion."""

    return is_number(value) and math.isfinite(value)


def string_to_number(text: str) -> float:
    """``Number(text)`` for a string (StringToNumber)."""

    stripped = text.strip(_JS_SPACE)
    if stripped == "":
        return 0.0
    radix = _RADIX_LITERAL.fullmatch(stripped)
    if radix:
        base = _RADIX[radix.group(1)]
        try:
            return float(int(radix.group(2), base))
        except ValueError:
            return math.nan
    if _DECIMAL.fullmatch(stripped):
        return float(stripped.replace("Infinity", "inf"))
    return math.nan


def to_number(value: Any) -> float:
    """``Number(value)`` for a JSON value (or ``MISSING``)."""

    if value is MISSING:
        return math.nan
    if value is None or value is False:
        return 0.0
    if value is True:
        return 1.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return string_to_number(value)
    if isinstance(value, list | tuple):
        return string_to_number(to_string(value))
    return math.nan


def to_string(value: Any) -> str:
    """``String(value)`` (template-literal interpolation) for a JSON value."""

    if value is MISSING:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return "NaN" if value != value else _number_string(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return ",".join("" if nullish(item) else to_string(item) for item in value)
    return "[object Object]"


def _number_string(value: float | int) -> str:
    if isinstance(value, float) and math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return format_number(value)


def js_max(*values: float) -> float:
    """``Math.max``: NaN wins, no arguments is ``-Infinity``."""

    result = -math.inf
    for value in values:
        if value != value:
            return math.nan
        if value > result:
            result = value
    return result


def js_min(*values: float) -> float:
    """``Math.min``: NaN wins, no arguments is ``Infinity``."""

    result = math.inf
    for value in values:
        if value != value:
            return math.nan
        if value < result:
            result = value
    return result


def to_fixed_string(value: float, digits: int) -> str:
    """``value.toFixed(digits)``: the exact binary value, halves away from zero."""

    if value != value:
        return "NaN"
    if abs(value) >= 1e21:
        return _number_string(value)
    if value == 0:
        value = 0.0
    quantum = Decimal(1).scaleb(-digits)
    text = format(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP), "f")
    return text


def utf16_key(text: str) -> bytes:
    """Sort key for JavaScript's default string order (UTF-16 code units)."""

    return text.encode("utf-16-be", "surrogatepass")


# ICU root collation (``localeCompare`` without a locale) of the printable ASCII
# characters and the whitespace controls, in primary order; letters fold case here and
# differ only at the tertiary level (lower before upper). Other controls are ignorable.
_ICU_ASCII_PRIMARY = (
    "\t\n\x0b\x0c\r _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$0123456789abcdefghijklmnopqrstuvwxyz"
)
_ICU_WEIGHT = {character: weight for weight, character in enumerate(_ICU_ASCII_PRIMARY)}


def locale_compare_key(text: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Sort key equal to ``String.prototype.localeCompare`` (ICU root collation) on
    ASCII strings: primary weights, then case (lower before upper). A non-ASCII
    character sorts after every ASCII one, by code point (an approximation)."""

    primary: list[int] = []
    tertiary: list[int] = []
    for character in text:
        folded = character.lower() if character.isascii() else character
        weight = _ICU_WEIGHT.get(folded)
        if weight is None:
            if character.isascii():
                continue
            weight = len(_ICU_ASCII_PRIMARY) + ord(character)
        primary.append(weight)
        tertiary.append(1 if character.isascii() and character.isupper() else 0)
    return tuple(primary), tuple(tertiary)


def trim(text: str) -> str:
    """``String.prototype.trim``."""

    return text.strip(_JS_SPACE)


def slice_utf16(text: str, end: int) -> str:
    """``text.slice(0, end)`` counted in UTF-16 code units (may split a pair, as JS does)."""

    units = text.encode("utf-16-le", "surrogatepass")
    if len(units) <= end * 2:
        return text
    return units[: end * 2].decode("utf-16-le", "surrogatepass")


def stringify_compact(value: Any) -> str:
    """``JSON.stringify(value)`` with no indentation (``MISSING`` members are dropped)."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return format_number(value)
    if isinstance(value, str):
        text = json.dumps(value, ensure_ascii=False)
        return _LONE_SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)
    if isinstance(value, Mapping):
        members = (
            stringify_compact(str(key)) + ":" + stringify_compact(child)
            for key, child in value.items()
            if child is not MISSING
        )
        return "{" + ",".join(members) + "}"
    if isinstance(value, list | tuple):
        return (
            "["
            + ",".join("null" if item is MISSING else stringify_compact(item) for item in value)
            + "]"
        )
    raise TypeError(f"not JSON-serializable: {type(value).__name__}")


def _array_index(key: str) -> int | None:
    if key == "0" or (key[:1] in "123456789" and key.isdigit() and key.isascii()):
        index = int(key)
        if index < 2**32 - 1:
            return index
    return None


def js_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object with JavaScript's property order: array-index keys ascending first,
    then the other keys in insertion order (a duplicate keeps its first position)."""

    merged = dict(pairs)
    indexed = [(index, key) for key in merged if (index := _array_index(key)) is not None]
    if not indexed:
        return merged
    ordered = {key: merged[key] for _, key in sorted(indexed)}
    ordered.update((key, child) for key, child in merged.items() if key not in ordered)
    return ordered


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"Unexpected token in JSON: {name}")


def parse_json(text: str) -> Any:
    """``JSON.parse``: no NaN or Infinity literals, JavaScript object key order."""

    return json.loads(text, object_pairs_hook=js_object, parse_constant=_refuse_constant)


def read_json(path: Path | str) -> Any:
    return parse_json(Path(path).read_text(encoding="utf-8"))


def _days_from_civil(year: int, month: int, day: int) -> int:
    year -= month <= 2
    era = year // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _civil_from_days(days: int) -> tuple[int, int, int]:
    days += 719468
    era = days // 146097
    doe = days - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + (3 if mp < 10 else -9)
    return yoe + era * 400 + (month <= 2), month, day


def _local_to_utc_ms(year: int, month: int, day: int, ms_of_day: int) -> int:
    utc = _days_from_civil(year, month, 1) * _MS_PER_DAY + (day - 1) * _MS_PER_DAY + ms_of_day
    if not 1 <= year <= 9998:
        return utc
    naive = datetime(year, month, 1) + timedelta(days=day - 1, milliseconds=ms_of_day)
    return round(time.mktime(naive.timetuple()) * 1000) + naive.microsecond // 1000


def date_parse(value: Any) -> float | None:
    """``Date.parse(String(value))`` in milliseconds; ``None`` where JavaScript gives NaN."""

    match = _ISO_DATE.fullmatch(value if isinstance(value, str) else to_string(value))
    if not match:
        return None
    year_text, month_text, day_text, hour_text, minute_text, second_text, fraction = match.group(
        1, 2, 3, 4, 5, 6, 7
    )
    zulu, sign, offset_hours, offset_minutes = match.group(8, 9, 10, 11)
    if year_text == "-000000" or (sign and hour_text is None):
        return None
    year = int(year_text)
    month = int(month_text or 1)
    day = int(day_text or 1)
    hour = int(hour_text or 0)
    minute = int(minute_text or 0)
    second = int(second_text or 0)
    millis = int((fraction or "0")[:3].ljust(3, "0"))
    if not (1 <= month <= 12 and 1 <= day <= 31 and minute <= 59 and second <= 59):
        return None
    if hour > 24 or (hour == 24 and (minute or second or millis)):
        return None
    ms_of_day = ((hour * 60 + minute) * 60 + second) * 1000 + millis
    if sign:
        if int(offset_hours) > 23 or int(offset_minutes) > 59:
            return None
        offset = (int(offset_hours) * 60 + int(offset_minutes)) * 60_000
        utc = _days_from_civil(year, month, 1) * _MS_PER_DAY + (day - 1) * _MS_PER_DAY
        result = utc + ms_of_day - (offset if sign == "+" else -offset)
    elif zulu or hour_text is None:
        result = _days_from_civil(year, month, 1) * _MS_PER_DAY + (day - 1) * _MS_PER_DAY
        result += ms_of_day
    else:
        result = _local_to_utc_ms(year, month, day, ms_of_day)
    if abs(result) > _MAX_TIME_MS:
        return None
    return float(result)


def _time_value(ms: float) -> int:
    if not math.isfinite(ms) or abs(ms) > _MAX_TIME_MS:
        raise ValueError("Invalid time value")
    return math.trunc(ms) + 0


def utc_year(ms: float) -> int:
    """``new Date(ms).getUTCFullYear()``."""

    return _civil_from_days(_time_value(ms) // _MS_PER_DAY)[0]


def iso_from_ms(ms: float) -> str:
    """``new Date(ms).toISOString()``."""

    value = _time_value(ms)
    days, ms_of_day = divmod(value, _MS_PER_DAY)
    year, month, day = _civil_from_days(days)
    seconds, millis = divmod(ms_of_day, 1000)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    if 0 <= year <= 9999:
        year_text = f"{year:04d}"
    else:
        year_text = ("+" if year > 0 else "-") + f"{abs(year):06d}"
    return f"{year_text}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}.{millis:03d}Z"


def now_ms() -> int:
    """``Date.now()``."""

    return time.time_ns() // 1_000_000


def _double(bits: int) -> float:
    return float(struct.unpack("<d", struct.pack("<Q", bits))[0])


def _bits(value: float) -> int:
    return int(struct.unpack("<Q", struct.pack("<d", value))[0])


_TWO54 = 2.0**54
_IVLN2HI = _double(0x3FF7154765200000)
_IVLN2LO = _double(0x3DE705FC2EEFA200)
_LG1 = _double(0x3FE5555555555593)
_LG2 = _double(0x3FD999999997FA04)
_LG3 = _double(0x3FD2492494229359)
_LG4 = _double(0x3FCC71C51D8E78AF)
_LG5 = _double(0x3FC7466496CB03DE)
_LG6 = _double(0x3FC39A09D078C69F)
_LG7 = _double(0x3FC2F112DF3E5244)


def _k_log1p(f: float) -> float:
    s = f / (2.0 + f)
    z = s * s
    w = z * z
    t1 = w * (_LG2 + w * (_LG4 + w * _LG6))
    t2 = z * (_LG1 + w * (_LG3 + w * (_LG5 + w * _LG7)))
    r = t2 + t1
    hfsq = 0.5 * f * f
    return s * (hfsq + r)


def log2(x: float) -> float:
    """``Math.log2``: V8's port of FreeBSD's ``e_log2.c``, so results match bit for bit
    (the C library's ``log2`` differs from it in the last bit for some arguments)."""

    bits = _bits(x)
    hx = bits >> 32
    if hx >= 0x80000000:
        hx -= 1 << 32
    lx = bits & 0xFFFFFFFF
    k = 0
    if hx < 0x00100000:
        if ((hx & 0x7FFFFFFF) | lx) == 0:
            return -math.inf
        if hx < 0:
            return math.nan
        k -= 54
        x *= _TWO54
        hx = _bits(x) >> 32
    if hx >= 0x7FF00000:
        return x + x
    if hx == 0x3FF00000 and lx == 0:
        return 0.0
    k += (hx >> 20) - 1023
    hx &= 0x000FFFFF
    i = (hx + 0x95F64) & 0x100000
    x = _double(((hx | (i ^ 0x3FF00000)) << 32) | (_bits(x) & 0xFFFFFFFF))
    k += i >> 20
    y = float(k)
    f = x - 1.0
    hfsq = 0.5 * f * f
    r = _k_log1p(f)
    hi = _double(_bits(f - hfsq) & 0xFFFFFFFF00000000)
    lo = (f - hi) - hfsq + r
    val_hi = hi * _IVLN2HI
    val_lo = (lo + hi) * _IVLN2LO + lo * _IVLN2HI
    w = y + val_hi
    val_lo += (y - w) + val_hi
    val_hi = w
    return val_lo + val_hi
