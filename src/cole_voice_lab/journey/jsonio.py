"""JSON and number semantics of the Node pipeline, so ported manifests match byte for byte.

voice-journey wrote every manifest as ``JSON.stringify(payload, null, 2) + "\\n"`` and
rounded with ``Number(value.toFixed(places))``. Python differs in three places that
matter for parity: ``json.dumps`` writes ``1.0`` where JavaScript writes ``1`` (and uses
exponents at other thresholds), it writes ``NaN`` where JavaScript writes ``null``, and
``round`` rounds half to even on the binary value where ``toFixed`` rounds half away
from zero. The helpers here are JavaScript's rules.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

JsonValue = Any


def to_fixed(value: float | int | None, places: int = 3) -> float | None:
    """``Number.isFinite(v) ? Number(v.toFixed(places)) : null``."""

    if value is None or isinstance(value, bool):
        return None
    if not math.isfinite(value):
        return None
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))


def js_round(value: float) -> float:
    """``Math.round``: the nearest integer, halves toward positive infinity."""

    return float(math.floor(value + 0.5))


def format_number(value: float | int) -> str:
    """``String(n)`` / JSON.stringify of one number; non-finite numbers are ``null``."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
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


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _dump(value: JsonValue, indent: str, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif isinstance(value, bool):
        out.append("true" if value else "false")
    elif isinstance(value, int | float):
        out.append(format_number(value))
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, Mapping):
        if not value:
            out.append("{}")
            return
        inner = indent + "  "
        out.append("{\n")
        first = True
        for key, child in value.items():
            if not first:
                out.append(",\n")
            first = False
            out.append(inner + _string(str(key)) + ": ")
            _dump(child, inner, out)
        out.append("\n" + indent + "}")
    elif isinstance(value, Sequence):
        if not value:
            out.append("[]")
            return
        inner = indent + "  "
        out.append("[\n")
        for index, child in enumerate(value):
            if index:
                out.append(",\n")
            out.append(inner)
            _dump(child, inner, out)
        out.append("\n" + indent + "]")
    else:
        raise TypeError(f"not JSON-serializable: {type(value).__name__}")


def stringify(value: JsonValue) -> str:
    """``JSON.stringify(value, null, 2)`` (keys whose value is undefined are the
    caller's to omit; ``None`` is ``null``)."""

    out: list[str] = []
    _dump(value, "", out)
    return "".join(out)


def manifest_text(payload: JsonValue) -> str:
    return stringify(payload) + "\n"


def read_json(path: Path | str) -> JsonValue:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path | str, payload: JsonValue, *, announce: bool = True) -> None:
    """Write a manifest as the Node pipeline did; ``-`` writes it to stdout. The file is
    replaced atomically (a temporary file in the same directory, then a rename)."""

    body = manifest_text(payload)
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


def iso_timestamp(moment: datetime | None = None) -> str:
    """``new Date().toISOString()``: UTC, milliseconds, ``Z``."""

    current = (moment or datetime.now(UTC)).astimezone(UTC)
    return current.strftime("%Y-%m-%dT%H:%M:%S.") + f"{current.microsecond // 1000:03d}Z"
