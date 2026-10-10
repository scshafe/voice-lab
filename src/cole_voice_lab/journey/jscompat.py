"""JavaScript value rules the ported journey modules need beyond ``jsonio``.

voice-journey's modules read loosely typed JSON and lean on JavaScript's coercions:
``undefined`` versus ``null`` (``?.``, ``??``, a key whose value is ``undefined`` is
dropped by ``JSON.stringify``), truthiness, ``Number(x)`` and ``String(x)``,
``Date.parse``, ``toFixed`` as a string, V8's ``Math.log2``/``Math.log10``, UTF-16
string order, ``localeCompare`` and Node's ``path`` module. The helpers here are those
rules, in one place, so the ports keep the JavaScript expressions one to one.
``MISSING`` (from ``jsonio``, whose ``JSON.stringify`` drops it) stands for
``undefined``; ``None`` is ``null``. JSON text itself, both ways, and number formatting
are ``journey/jsonio.py``'s.

Each rule was checked against Node 24 on the inputs the modules use (the tests run the
comparison where ``node`` is installed). Known limits, each outside what the pipeline's
manifests contain:

- :func:`date_parse` implements the ECMAScript date-time string format with V8's
  leniencies (lower-case ``t``/``z``, a space before the time, ``+hhmm`` offsets,
  day-of-month overflow up to 31, ``24:00``, any number of fraction digits); a date-time
  without an offset is local time, resolved as V8 does around a transition (the offset
  in effect before it, ICU's ``LOCAL_FORMER``). V8's legacy fallback parser
  (``"Jan 1 2020"``, ``"2020/1/1"``, leading whitespace) is not implemented: those give
  ``None`` (NaN) where V8 parses them as local time.
- :func:`locale_compare_key` is ICU root collation (``localeCompare`` without a locale)
  for ASCII, checked for all 128 ASCII characters: C0 controls other than tab to
  carriage return (and DEL) are ignorable, punctuation and symbols sort before digits
  before letters, letters compare case-blind first and lowercase first on a tie. A
  non-ASCII character sorts after every ASCII one, by code point, where ICU would
  interleave it. Recording ids, file names, session names and timestamps are ASCII.
- :func:`log2` and :func:`log10` are V8's (fdlibm's) algorithms, bit for bit on a
  platform without fused multiply-add contraction; the C library's differ from them in
  the last bit for some arguments. ``Math.sin``/``cos``/``atan2`` have no port here: the
  platform libm agrees with V8's to an ulp.
"""

from __future__ import annotations

import math
import os
import posixpath
import re
import struct
import time
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from cole_voice_lab.journey.jsonio import MISSING as MISSING
from cole_voice_lab.journey.jsonio import format_number

# ECMAScript WhiteSpace and LineTerminator code points: what String.prototype.trim, /\s/
# and Number() skip. Python's str.isspace differs (it has U+001C-U+001F and U+0085, it
# lacks U+FEFF).
WHITESPACE: Final = "\t\n\v\f\r                  　﻿"
_DECIMAL = re.compile(r"[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")
_RADIX = re.compile(r"0(?:[xX](?P<x>[0-9a-fA-F]+)|[oO](?P<o>[0-7]+)|[bB](?P<b>[01]+))")
_RADIX_BASE: Final = {"x": 16, "o": 8, "b": 2}
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_ISO_DATE = re.compile(
    r"(?P<year>[+-]\d{6}|\d{4})(?:-(?P<month>\d{2})(?:-(?P<day>\d{2}))?)?"
    r"(?:[Tt ](?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?:\.(?P<fraction>\d+))?)?)?"
    r"(?P<zone>[Zz]|(?P<sign>[+-])(?P<zh>\d{2}):?(?P<zm>\d{2}))?",
    re.ASCII,
)
_MAX_TIME_MS: Final = 8.64e15
_MS_PER_DAY: Final = 86_400_000


# ---- values -------------------------------------------------------------------------------


def get(value: Any, key: str) -> Any:
    """``value?.[key]``: ``MISSING`` when ``value`` is not an object or lacks ``key``."""

    if isinstance(value, Mapping):
        return value.get(key, MISSING)
    return MISSING


def prop(target: Any, key: str) -> Any:
    """``target.key``: throws on null and undefined, as JavaScript does; ``MISSING`` for
    a missing key or a primitive."""

    if nullish(target):
        kind = "null" if target is None else "undefined"
        raise TypeError(f"Cannot read properties of {kind} (reading '{key}')")
    return get(target, key)


def length(value: Any) -> Any:
    """``value?.length``: an array's length, a string's UTF-16 length, an object's
    ``length`` member; ``MISSING`` otherwise."""

    if isinstance(value, list | tuple):
        return len(value)
    if isinstance(value, str):
        return utf16_length(value)
    return get(value, "length")


def defined(fields: Mapping[str, Any] | None = None, /, **more: Any) -> dict[str, Any]:
    """An object literal as ``JSON.stringify`` sees it: keys whose value is ``MISSING``
    (``undefined``) are left out."""

    merged = {**(fields or {}), **more}
    return {key: value for key, value in merged.items() if value is not MISSING}


def nullish(value: Any) -> bool:
    return value is None or value is MISSING


def coalesce(*values: Any) -> Any:
    """``a ?? b ?? ...``: the first value that is neither null nor undefined, else the
    last one."""

    for value in values[:-1]:
        if not nullish(value):
            return value
    return values[-1]


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
    """``typeof value === "number"``."""

    return isinstance(value, int | float) and not isinstance(value, bool)


def is_finite(value: Any) -> bool:
    """``Number.isFinite``: a finite number, no coercion."""

    return is_number(value) and math.isfinite(to_number(value))


# ---- Number() and String() ----------------------------------------------------------------


def string_to_number(text: str) -> float:
    """``Number(text)`` for a string (StringToNumber): trimmed, empty is 0, decimal
    literals and ``Infinity``, ``0x``/``0o``/``0b`` literals; ``NaN`` otherwise."""

    stripped = text.strip(WHITESPACE)
    if stripped == "":
        return 0.0
    radix = _RADIX.fullmatch(stripped)
    if radix:
        prefix = stripped[1].lower()
        try:
            return float(int(radix[prefix], _RADIX_BASE[prefix]))
        except OverflowError:
            return math.inf
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
        try:
            return float(value)
        except OverflowError:
            return math.inf if value > 0 else -math.inf
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
        number = to_number(value)
        if number != number:
            return "NaN"
        if math.isinf(number):
            return "Infinity" if number > 0 else "-Infinity"
        return format_number(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return ",".join("" if nullish(item) else to_string(item) for item in value)
    return "[object Object]"


def to_fixed_string(value: float, digits: int) -> str:
    """``value.toFixed(digits)``: the exact binary value, halves away from zero."""

    if value != value:
        return "NaN"
    if abs(value) >= 1e21:
        return to_string(value)
    if value == 0:
        value = 0.0
    quantum = Decimal(1).scaleb(-digits)
    return format(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP), "f")


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


# ---- strings ------------------------------------------------------------------------------


def trim(text: str) -> str:
    """``String.prototype.trim``."""

    return text.strip(WHITESPACE)


def utf8(text: str) -> bytes:
    """Node's UTF-8 encoding of a string (``Buffer.from``, hashes, files): a lone
    surrogate becomes U+FFFD."""

    return _LONE_SURROGATE.sub("�", text).encode("utf-8")


def utf16_length(text: str) -> int:
    """``text.length``: UTF-16 code units."""

    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def slice_utf16(text: str, end: int) -> str:
    """``text.slice(0, end)`` counted in UTF-16 code units (may split a pair, as JS does)."""

    units = text.encode("utf-16-le", "surrogatepass")
    if len(units) <= end * 2:
        return text
    return units[: end * 2].decode("utf-16-le", "surrogatepass")


def utf16_key(text: str) -> bytes:
    """Sort key for JavaScript's default string order (UTF-16 code units): the order of
    ``Array.prototype.sort()`` without a comparator and of ``<`` on strings."""

    return text.encode("utf-16-be", "surrogatepass")


# ICU root collation (``localeCompare`` without a locale) of the printable ASCII
# characters and the whitespace controls, in primary order; letters fold case here and
# differ only at the tertiary level (lower before upper). Other controls are ignorable.
_ICU_ASCII_PRIMARY: Final = (
    "\t\n\x0b\x0c\r _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$0123456789abcdefghijklmnopqrstuvwxyz"
)
_ICU_WEIGHT: Final = {character: weight for weight, character in enumerate(_ICU_ASCII_PRIMARY)}


def locale_compare_key(text: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Sort key equal to ``String.prototype.localeCompare`` (ICU root collation) on
    ASCII strings: primary weights, then case (lower before upper); equal keys are
    exactly the strings ``localeCompare`` calls equal. A non-ASCII character sorts after
    every ASCII one, by code point (an approximation)."""

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


def locale_compare(left: str, right: str) -> int:
    """``Math.sign(left.localeCompare(right))``, as a comparator."""

    a, b = locale_compare_key(left), locale_compare_key(right)
    return (a > b) - (a < b)


# ---- Node's path module (POSIX) -----------------------------------------------------------


def _normalize(path: str) -> str:
    trailing = path.endswith("/")
    normal = posixpath.normpath(path)
    if normal.startswith("//"):
        normal = "/" + normal.lstrip("/")
    if trailing and normal != "/":
        normal += "/"
    return normal


def path_join(*parts: str) -> str:
    """``path.join``: empty parts dropped, the rest joined and normalized lexically (a
    trailing slash is kept, ``//`` collapses)."""

    joined = "/".join(part for part in parts if part)
    return _normalize(joined) if joined else "."


def path_resolve(path: str | os.PathLike[str]) -> str:
    """``path.resolve`` of one path: absolute and lexical (no symlinks)."""

    resolved = _normalize(posixpath.join(os.getcwd(), os.fspath(path)))
    return resolved.rstrip("/") or "/"


def path_basename(path: str) -> str:
    """``path.basename``: the last segment, trailing slashes ignored."""

    return posixpath.basename(path.rstrip("/"))


def path_extname(path: str) -> str:
    """``path.extname``."""

    start_dot = -1
    start_part = 0
    end = -1
    matched_slash = True
    pre_dot_state = 0
    for index in range(len(path) - 1, -1, -1):
        char = path[index]
        if char == "/":
            if not matched_slash:
                start_part = index + 1
                break
            continue
        if end == -1:
            matched_slash = False
            end = index + 1
        if char == ".":
            if start_dot == -1:
                start_dot = index
            elif pre_dot_state != 1:
                pre_dot_state = 1
        elif start_dot != -1:
            pre_dot_state = -1
    if (
        start_dot == -1
        or end == -1
        or pre_dot_state == 0
        or (pre_dot_state == 1 and start_dot == end - 1 and start_dot == start_part + 1)
    ):
        return ""
    return path[start_dot:end]


# ---- Date -----------------------------------------------------------------------------------


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


def _utc_offset_ms(utc_ms: int) -> int:
    """The local zone's offset from UTC at an instant (0 where the OS cannot say)."""

    try:
        return time.localtime(utc_ms // 1000).tm_gmtoff * 1000
    except (OverflowError, ValueError, OSError):
        return 0


def _local_to_utc_ms(local_ms: int) -> int:
    """V8's local-time interpretation (ICU's ``LOCAL_FORMER``): the offset in effect
    before a transition applies to a skipped time and to a repeated one."""

    before = _utc_offset_ms(local_ms - _MS_PER_DAY)
    after = _utc_offset_ms(local_ms + _MS_PER_DAY)
    before_valid = _utc_offset_ms(local_ms - before) == before
    after_valid = _utc_offset_ms(local_ms - after) == after
    return local_ms - (after if after_valid and not before_valid else before)


def date_parse(value: Any) -> float | None:
    """``Date.parse(String(value))`` in milliseconds; ``None`` where JavaScript gives NaN
    (see the module docstring for the formats)."""

    match = _ISO_DATE.fullmatch(value if isinstance(value, str) else to_string(value))
    if not match or match["year"] == "-000000":
        return None
    if match["sign"] is not None and match["hour"] is None:  # a date-only form takes no offset
        return None
    year = int(match["year"])
    month = int(match["month"] or 1)
    day = int(match["day"] or 1)
    hour = int(match["hour"] or 0)
    minute = int(match["minute"] or 0)
    second = int(match["second"] or 0)
    millisecond = int((match["fraction"] or "0")[:3].ljust(3, "0"))
    if not (1 <= month <= 12 and 1 <= day <= 31 and minute <= 59 and second <= 59):
        return None
    if hour > 24 or (hour == 24 and (minute or second or millisecond)):
        return None
    days = _days_from_civil(year, month, 1) + day - 1
    time_ms = days * _MS_PER_DAY + ((hour * 60 + minute) * 60 + second) * 1000 + millisecond
    if match["zone"] is None:
        if match["hour"] is not None:
            time_ms = _local_to_utc_ms(time_ms)
    elif match["sign"] is not None:
        zone_hour, zone_minute = int(match["zh"]), int(match["zm"])
        if zone_hour > 23 or zone_minute > 59:
            return None
        offset = (zone_hour * 60 + zone_minute) * 60_000
        time_ms += -offset if match["sign"] == "+" else offset
    if abs(time_ms) > _MAX_TIME_MS:
        return None
    return float(time_ms)


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


# ---- V8's Math.log2 and Math.log10 (fdlibm, src/base/ieee754.cc) --------------------------


def _double(bits: int) -> float:
    return float(struct.unpack("<d", struct.pack("<Q", bits))[0])


def _bits(value: float) -> int:
    return int(struct.unpack("<Q", struct.pack("<d", value))[0])


def _words(value: float) -> tuple[int, int]:
    bits = _bits(value)
    return bits >> 32, bits & 0xFFFFFFFF


def _from_words(high: int, low: int) -> float:
    return _double((high << 32) | low)


_TWO54: Final = 2.0**54
_LN2_HI: Final = _double(0x3FE62E42FEE00000)
_LN2_LO: Final = _double(0x3DEA39EF35793C76)
_IVLN2HI: Final = _double(0x3FF7154765200000)
_IVLN2LO: Final = _double(0x3DE705FC2EEFA200)
_IVLN10: Final = _double(0x3FDBCB7B1526E50E)
_LOG10_2HI: Final = _double(0x3FD34413509F6000)
_LOG10_2LO: Final = _double(0x3D59FEF311F12B36)
_LG1: Final = _double(0x3FE5555555555593)
_LG2: Final = _double(0x3FD999999997FA04)
_LG3: Final = _double(0x3FD2492494229359)
_LG4: Final = _double(0x3FCC71C51D8E78AF)
_LG5: Final = _double(0x3FC7466496CB03DE)
_LG6: Final = _double(0x3FC39A09D078C69F)
_LG7: Final = _double(0x3FC2F112DF3E5244)


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


def _fdlibm_log(x: float) -> float:
    """fdlibm ``__ieee754_log`` for a positive normal ``x`` (:func:`log10`'s domain)."""

    hx, lx = _words(x)
    k = (hx >> 20) - 1023
    hx &= 0x000FFFFF
    i = (hx + 0x95F64) & 0x100000
    x = _from_words(hx | (i ^ 0x3FF00000), lx)
    k += i >> 20
    f = x - 1.0
    dk = float(k)
    if (0x000FFFFF & (2 + hx)) < 3:
        if f == 0.0:
            return 0.0 if k == 0 else dk * _LN2_HI + dk * _LN2_LO
        r = f * f * (0.5 - 0.33333333333333333 * f)
        return f - r if k == 0 else dk * _LN2_HI - ((r - dk * _LN2_LO) - f)
    s = f / (2.0 + f)
    z = s * s
    w = z * z
    t1 = w * (_LG2 + w * (_LG4 + w * _LG6))
    t2 = z * (_LG1 + w * (_LG3 + w * (_LG5 + w * _LG7)))
    r = t2 + t1
    if (hx - 0x6147A) | (0x6B851 - hx) > 0:
        hfsq = 0.5 * f * f
        if k == 0:
            return f - (hfsq - s * (hfsq + r))
        return dk * _LN2_HI - ((hfsq - (s * (hfsq + r) + dk * _LN2_LO)) - f)
    if k == 0:
        return f - s * (f - r)
    return dk * _LN2_HI - ((s * (f - r) - dk * _LN2_LO) - f)


def log10(x: float) -> float:
    """``Math.log10``: V8's fdlibm ``e_log10.c`` over ``e_log.c``, bit for bit."""

    if x != x or x < 0:
        return math.nan
    if x == 0:
        return -math.inf
    if x == math.inf:
        return x
    hx, lx = _words(x)
    k = 0
    if hx < 0x00100000:
        k -= 54
        x *= _TWO54
        hx, lx = _words(x)
    if hx == 0x3FF00000 and lx == 0:
        return 0.0
    k += (hx >> 20) - 1023
    i = 1 if k < 0 else 0
    y = float(k + i)
    x = _from_words((hx & 0x000FFFFF) | ((0x3FF - i) << 20), lx)
    z = y * _LOG10_2LO + _IVLN10 * _fdlibm_log(x)
    return z + y * _LOG10_2HI
