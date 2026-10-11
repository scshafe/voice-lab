"""The corpus's display formats: voice-journey's ``web/src/utils/fmt.js``, ported.

Values come from the server's rows as JSON; a missing value formats as ``""`` (the SPA's
``null``/``undefined`` rule). Numbers round as JavaScript's ``Math.round`` and ``toFixed``
do for the values the corpus holds (half away from zero for positive values).
"""

from __future__ import annotations

import math
import re
from typing import Any, Final

from cole_voice_lab.journey import jscompat as js

_CAPTURED_TAIL = re.compile(r":\d\d(\.\d+)?Z?$")

#: The bucket's tone (``bucketTone`` in fmt.js), as a SuiChip tone (SuiTheme.toneBase).
BUCKET_TONES: Final = {
    "clean_singing": "ok",
    "uncertain_manual_review": "warn",
    "noise_contaminated_singing": "orange",
    "music_contaminated_singing": "purple",
    "non_singing": "danger",
    "unclassified": "info",
}


def _missing(value: Any) -> bool:
    return value is None or value is js.MISSING


def _round_half_up(value: float) -> int:
    """``Math.round``: halves go up."""
    return math.floor(value + 0.5)


def _fixed(value: float, digits: int) -> str:
    """``Number.prototype.toFixed`` for the corpus's magnitudes."""
    scale = 10**digits
    return f"{_round_half_up(value * scale) / scale:.{digits}f}"


def fmt_captured(value: Any) -> str:
    """``2019-01-05T20:10:00Z`` -> ``2019-01-05 20:10``."""
    if _missing(value) or not js.truthy(value):
        return ""
    return _CAPTURED_TAIL.sub("", str(value).replace("T", " ", 1))


def fmt_seconds(value: Any) -> str:
    if _missing(value):
        return ""
    return f"{_round_half_up(float(value))}s"


def fmt_bytes(value: Any) -> str:
    if _missing(value):
        return ""
    return f"{_fixed(float(value) / 1048576, 2)} MB"


def fmt_confidence(value: Any) -> str:
    if _missing(value):
        return ""
    return _fixed(float(value), 2)


def bucket_tone(bucket: Any) -> str:
    return BUCKET_TONES.get(str(bucket), "info")


def labelize(value: Any) -> str:
    """``foo_bar`` -> ``foo bar`` (``String(value ?? "").replaceAll("_", " ")``)."""
    if _missing(value):
        return ""
    return js.to_string(value).replace("_", " ")


def text(value: Any) -> str:
    """``String(value)`` for display; ``""`` for a missing value."""
    if _missing(value):
        return ""
    return js.to_string(value)
