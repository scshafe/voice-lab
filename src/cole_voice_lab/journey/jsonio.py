"""JSON and number semantics of the Node pipeline, so ported manifests match byte for byte.

voice-journey wrote every manifest as ``JSON.stringify(payload, null, 2) + "\\n"`` and
rounded with ``Number(value.toFixed(places))``. Python differs in three places that
matter for parity: ``json.dumps`` writes ``1.0`` where JavaScript writes ``1`` (and uses
exponents at other thresholds), it writes ``NaN`` where JavaScript writes ``null``, and
``round`` rounds half to even on the binary value where ``toFixed`` rounds half away
from zero. The helpers here are JavaScript's rules, in both directions:

- out: :func:`stringify` (``JSON.stringify(v, null, 2)``), :func:`stringify_compact`
  (``JSON.stringify(v)``), :func:`manifest_text` and :func:`write_json`; a member whose
  value is :data:`MISSING` (``undefined``) is left out, as an array element it is
  ``null``; a lone surrogate is escaped (``\\udxxx``), as well-formed ``JSON.stringify``
  does;
- in: :func:`parse_json` / :func:`read_json` (``JSON.parse`` of ``readFile(p, "utf8")``):
  ``NaN``/``Infinity`` are refused, object keys take JavaScript's own-key order
  (:func:`object_key_order`), an integer beyond ``2**53`` becomes the double JavaScript
  reads, and invalid UTF-8 is replaced by U+FFFD as Node decodes it;
- numbers: :func:`format_number` (``String(n)``), :func:`to_fixed`
  (``Number(v.toFixed(p))``), :func:`js_round` (``Math.round``), and
  :func:`iso_timestamp` (``toISOString()``).

Every other JavaScript value rule (``Number()``, ``String()``, truthiness, ``Date.parse``,
``localeCompare``, V8's ``Math``, Node's ``path``) is in ``journey/jscompat.py``.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final

JsonValue = Any

_MAX_SAFE_INTEGER: Final = 2**53
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_UINT32: Final = 2**32


class _Missing:
    """JavaScript's ``undefined``: absent, as opposed to ``null`` (``None``)."""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "undefined"

    def __bool__(self) -> bool:
        return False


MISSING: Final[Any] = _Missing()


# ---- numbers ----------------------------------------------------------------------------


def to_fixed(value: float | int | None, places: int = 3) -> float | None:
    """``Number.isFinite(v) ? Number(v.toFixed(places)) : null``."""

    if value is None or isinstance(value, bool):
        return None
    if not math.isfinite(value):
        return None
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))


def js_round(value: float) -> float:
    """``Math.round``: the nearest integer, halves toward positive infinity (V8's
    ``ceil`` then step back, which is exact where ``floor(x + 0.5)`` is not)."""

    if not math.isfinite(value):
        return value
    result = float(math.ceil(value))
    if result - 0.5 > value:
        result -= 1.0
    return math.copysign(0.0, value) if result == 0 else result


def _as_double(value: int) -> float:
    """The double JavaScript holds for an integer (``inf`` beyond the double range)."""

    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def format_number(value: float | int) -> str:
    """``String(n)`` / JSON.stringify of one number; non-finite numbers are ``null``."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) <= _MAX_SAFE_INTEGER:
            return str(value)
        value = _as_double(value)
    if not math.isfinite(value):
        return "null"
    if value == 0:
        return "0"
    text = repr(abs(value))
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).lstrip("0")
    point = len(whole) + exponent  # position of the decimal point within digits+zeros
    if whole == "0":
        stripped = fraction.lstrip("0")
        point = exponent - (len(fraction) - len(stripped))
        digits = stripped
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    n = point
    sign = "-" if value < 0 else ""
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        e_text = f"+{e}" if e >= 0 else str(e)
        body = (digits[0] + ("." + digits[1:] if k > 1 else "")) + "e" + e_text
    return sign + body


def iso_timestamp(moment: datetime | None = None) -> str:
    """``new Date().toISOString()``: UTC, milliseconds, ``Z``."""

    current = (moment or datetime.now(UTC)).astimezone(UTC)
    return current.strftime("%Y-%m-%dT%H:%M:%S.") + f"{current.microsecond // 1000:03d}Z"


# ---- JSON.stringify -----------------------------------------------------------------------


def quote(value: str) -> str:
    """A JSON string literal as ``JSON.stringify`` writes it (lone surrogates escaped)."""

    text = json.dumps(value, ensure_ascii=False)
    return _LONE_SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def _dump(value: JsonValue, indent: str | None, out: list[str]) -> None:
    if value is None or value is MISSING:
        out.append("null")
    elif isinstance(value, bool):
        out.append("true" if value else "false")
    elif isinstance(value, int | float):
        out.append(format_number(value))
    elif isinstance(value, str):
        out.append(quote(value))
    elif isinstance(value, Mapping):
        members = [(key, child) for key, child in value.items() if child is not MISSING]
        if not members:
            out.append("{}")
            return
        inner = None if indent is None else indent + "  "
        out.append("{" if inner is None else "{\n")
        for index, (key, child) in enumerate(members):
            if index:
                out.append("," if inner is None else ",\n")
            out.append(quote(str(key)) + ":" if inner is None else inner + quote(str(key)) + ": ")
            _dump(child, inner, out)
        out.append("}" if inner is None else "\n" + str(indent) + "}")
    elif isinstance(value, Sequence):
        if not value:
            out.append("[]")
            return
        inner = None if indent is None else indent + "  "
        out.append("[" if inner is None else "[\n")
        for index, child in enumerate(value):
            if index:
                out.append("," if inner is None else ",\n")
            if inner is not None:
                out.append(inner)
            _dump(child, inner, out)
        out.append("]" if inner is None else "\n" + str(indent) + "]")
    else:
        raise TypeError(f"not JSON-serializable: {type(value).__name__}")


def stringify(value: JsonValue) -> str:
    """``JSON.stringify(value, null, 2)`` (``None`` is ``null``; a member whose value is
    ``MISSING`` is left out)."""

    out: list[str] = []
    _dump(value, "", out)
    return "".join(out)


def stringify_compact(value: JsonValue) -> str:
    """``JSON.stringify(value)`` (no whitespace at all; ``MISSING`` as in :func:`stringify`)."""

    out: list[str] = []
    _dump(value, None, out)
    return "".join(out)


def manifest_text(payload: JsonValue) -> str:
    return stringify(payload) + "\n"


def write_json(
    path: Path | str, payload: JsonValue, *, announce: bool = True, compact: bool = False
) -> None:
    """Write a manifest as the Node pipeline did (``compact``: ``JSON.stringify(p)``);
    ``-`` writes it to stdout. The file is replaced atomically (a temporary file in the
    same directory, then a rename)."""

    body = (stringify_compact(payload) + "\n") if compact else manifest_text(payload)
    if str(path) == "-":
        sys.stdout.write(body)
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    if announce:
        sys.stdout.write(f"{target}\n")


# ---- JSON.parse ---------------------------------------------------------------------------


def _array_index(key: str) -> int | None:
    if key == "0" or (key[:1] in "123456789" and key.isdigit() and key.isascii()):
        index = int(key)
        if index < _UINT32 - 1:
            return index
    return None


def object_key_order(keys: Iterable[str]) -> list[str]:
    """A plain object's own-key order: array-index keys ascending, then the other keys
    in insertion order."""

    listed = list(keys)
    indexed = sorted((index, key) for key in listed if (index := _array_index(key)) is not None)
    if not indexed:
        return listed
    numeric = {key for _, key in indexed}
    return [key for _, key in indexed] + [key for key in listed if key not in numeric]


def js_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object with JavaScript's property order (a duplicate key keeps its first
    position and its last value)."""

    merged = dict(pairs)
    return {key: merged[key] for key in object_key_order(merged)}


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"Unexpected token {name}, {name} is not valid JSON")


def _parse_int(text: str) -> int | float:
    if text == "-0":
        return -0.0
    value = int(text)
    return value if abs(value) <= _MAX_SAFE_INTEGER else _as_double(value)


def parse_json(text: str) -> JsonValue:
    """``JSON.parse``: no ``NaN``/``Infinity`` literals, JavaScript's object key order,
    integers as JavaScript reads them (exact ones stay ``int``)."""

    return json.loads(
        text, object_pairs_hook=js_object, parse_constant=_refuse_constant, parse_int=_parse_int
    )


def read_text(path: Path | str) -> str:
    """``readFile(path, "utf8")``: invalid UTF-8 becomes U+FFFD, line ends are kept."""

    with open(path, encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def read_json(path: Path | str) -> JsonValue:
    """``JSON.parse(await readFile(path, "utf8"))``."""

    return parse_json(read_text(path))
