"""Same-song journeys and the improvement index (port of voice-journey ``journeys.mjs``).

Reads the features and lyric manifests plus the local feature detail store (stored f0
contours, never audio), segments each take's contour into note cores, scores note-core
tuning against the take's own inferred tuning offset, builds a journey per lyric
cluster (Theil-Sen slopes per year) and aggregates them into an improvement index with
seeded bootstrap confidence intervals. The output manifest
(``voice-journey.song-journeys.v1``) holds only numeric aggregates and generic cluster
labels.

Parity with the JavaScript module:

- The bootstrap's generator is ``mulberry32`` in exact 32-bit arithmetic: the same seed
  draws the same resamples, so the same slopes give the same intervals.
- Manifests are written with :func:`jsonio.write_json`; every rounding is
  ``Number(v.toFixed(p))`` (:func:`jsonio.to_fixed`); sums are left folds in the
  JavaScript order (never ``sum()``, which compensates since Python 3.12); medians pick
  an element, as the JavaScript did.
- JavaScript value rules are kept where a manifest can carry any JSON (the shared ones
  are ``journey/jscompat.py``'s): truthiness, ``??``, ``Number(...)``, ``String(...)``,
  ``+`` concatenating strings, ``Map`` keys (``1`` and ``"1"`` differ), keys whose value
  is ``undefined`` being omitted, the ordering of integer-like keys in ``perYear`` and its
  prototype-key failure.
- ``Date.parse`` is :func:`jscompat.date_parse` (the ECMAScript date-time format with
  V8's tolerances; a time without an offset is local time). V8's legacy fallback formats
  (``2019/02/10``, ``Feb 10 2019``, leading whitespace) give null here where V8 parses
  them as local time.
- ``Math.log2`` is V8's fdlibm port (:func:`jscompat.log2`), bit for bit.

Where Python can differ: ``Math.sin``, ``Math.cos`` and ``Math.atan2`` are V8's fdlibm
ports, Python's are the platform libm. They agree to 1 ulp (2 ulp on a cents value); an
output differs only if an intermediate lies within those ulps of a rounding boundary (1 to
4 decimals). ``capturedAt`` is sliced by code point, not UTF-16 unit, and a non-string
``capturedAt`` fails (V8 would slice an array). Defaults for the paths are resolved when
the options are built, not when the module is imported. The output file is replaced
atomically (Node wrote it in place).
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from cole_voice_lab.journey import jscompat as js
from cole_voice_lab.journey import jsonio
from cole_voice_lab.journey.paths import resolve_paths

MANIFEST_SCHEMA_VERSION: Final = "voice-journey.song-journeys.v1"
RELIABLE_REJECTION_MAX: Final = 0.10

# Note segmentation over the stored 50 ms voiced-only f0 contour: points are grouped
# into one note while consecutive samples stay within the cents jump and time gap
# thresholds; runs shorter than minPoints are discarded as transitions/melisma. Tuning
# is scored on note MEDIANS only, against a per-take offset inferred from the notes.
NOTE_SEGMENTATION: Final[Mapping[str, float | int]] = {
    "contourStepSeconds": 0.05,
    "maxGapSeconds": 0.12,
    "maxJumpCents": 80,
    "minPoints": 4,
    "minNotesForTuning": 5,
    "inTuneCents": 25,
}
SLOPES: Final[Mapping[str, float | int]] = {"minTakes": 6, "minSpanYears": 1.5, "minPairYears": 0.5}
BOOTSTRAP: Final[Mapping[str, float | int]] = {"resamples": 1000, "level": 0.95}

DIMENSIONS: Final[tuple[Mapping[str, str], ...]] = (
    {
        "key": "noteCoreCentError",
        "label": "note-core tuning error (cents)",
        "direction": "down_good",
    },
    {"key": "inTuneShare", "label": "note time within ±25 cents", "direction": "up_good"},
    {"key": "rangeSemitones", "label": "range used (semitones p05–p95)", "direction": "neutral"},
    {"key": "cpps", "label": "voice clarity (CPPS dB)", "direction": "up_good"},
    {"key": "longestSustain", "label": "longest sustained note (s)", "direction": "up_good"},
    {
        "key": "vibratoDistanceFrom5p5",
        "label": "vibrato rate distance from 5.5 Hz",
        "direction": "down_good",
    },
)

HELP = """Voice Journey same-song journey analysis.

Usage:
  voice-journey-journeys analyze --dry-run [--features PATH] [--lyrics PATH]
  voice-journey-journeys analyze [--features PATH] [--lyrics PATH] [--feature-dir PATH] \
[--out PATH] [--analyzed-at ISO] [--seed N]

Reads the committed features + lyric manifests plus the gitignored local
feature detail store (stored f0 contours — no audio bytes are read). Segments
each take's contour into note cores, scores note-core tuning against a
per-take inferred tuning offset, builds per-cluster song journeys, and
aggregates a same-song improvement index (Theil–Sen slopes per cluster,
seeded bootstrap confidence intervals across clusters). Writes only numeric
aggregates and generic cluster labels to the repo-safe output manifest.
Host-executed because the feature detail store is gitignored; dry-run reads
only committed manifests.
"""

ReadDetail = Callable[[Any], Any]


class JourneysError(Exception):
    """A refused command line or command."""


_UINT32: Final = 2**32
_MS_PER_YEAR: Final = 31557600000
_OBJECT_PROTOTYPE_KEYS: Final = frozenset(
    {
        "constructor",
        "__defineGetter__",
        "__defineSetter__",
        "hasOwnProperty",
        "__lookupGetter__",
        "__lookupSetter__",
        "isPrototypeOf",
        "propertyIsEnumerable",
        "toString",
        "valueOf",
        "__proto__",
        "toLocaleString",
    }
)


# --- JavaScript value rules -------------------------------------------------------


def _array(value: Any, what: str) -> list[Any]:
    """``(value ?? [])`` used as an array."""

    if js.nullish(value):
        return []
    if isinstance(value, list):
        return value
    raise TypeError(f"{what} is not an array")


def _add(left: Any, right: Any) -> Any:
    """``left + right``: concatenation when either side is (or becomes) a string."""

    left = js.to_string(left) if isinstance(left, list | dict) else left
    right = js.to_string(right) if isinstance(right, list | dict) else right
    if isinstance(left, str) or isinstance(right, str):
        return js.to_string(left) + js.to_string(right)
    return js.to_number(left) + js.to_number(right)


def _sub(left: Any, right: Any) -> float:
    """``left - right``."""

    return js.to_number(left) - js.to_number(right)


def _div(left: float, right: float) -> float:
    """``left / right`` with IEEE division by zero."""

    if right != 0:
        return left / right
    if left == 0 or math.isnan(left):
        return math.nan
    return math.copysign(math.inf, left) * math.copysign(1.0, right)


def _map_key(value: Any) -> tuple[Any, ...]:
    """A ``Map`` key under SameValueZero: ``1`` and ``"1"`` and ``true`` differ."""

    if value is js.MISSING:
        return ("undefined",)
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, int | float):
        return ("number", js.to_number(value))
    if isinstance(value, str):
        return ("string", value)
    return ("object", id(value))


def _slice(value: Any, start: int, end: int) -> str:
    if not isinstance(value, str):
        raise TypeError("capturedAt.slice is not a function")
    return value[start:end]


# --- numeric helpers --------------------------------------------------------------


def _round(value: Any, places: int = 3) -> float | None:
    """``Number.isFinite(v) ? Number(v.toFixed(places)) : null``."""

    return jsonio.to_fixed(value, places) if js.is_finite(value) else None


def _weight(value: Any) -> float:
    """A note duration in arithmetic: null counts as 0."""

    return 0.0 if value is None else float(value)


def cents_of(hz: float) -> float:
    return 1200 * js.log2(float(hz) / 440)


def median_of(values: Sequence[Any]) -> Any:
    ordered = sorted(value for value in values if js.is_finite(value))
    if not ordered:
        return None
    return ordered[(len(ordered) - 1) // 2]


def quantile_of(values: Sequence[Any], p: float) -> Any:
    ordered = sorted(value for value in values if js.is_finite(value))
    if not ordered:
        return None
    return ordered[math.floor((len(ordered) - 1) * p)]


def _to_uint32(value: float | int) -> int:
    """``value >>> 0``."""

    number = js.to_number(value)
    if not math.isfinite(number):
        return 0
    return int(number) % _UINT32


def _imul(left: int, right: int) -> int:
    return (left * right) % _UINT32


def mulberry32(seed: float | int) -> Callable[[], float]:
    """The bootstrap's generator, bit for bit. Every value is kept as its unsigned
    32-bit pattern: ``^``, ``|`` and ``Math.imul`` only see the low 32 bits, and the
    one double addition (two int32 values) is exact, so reducing it mod 2**32 is
    ``ToInt32`` of the JavaScript sum."""

    state = _to_uint32(seed)

    def random() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) % _UINT32
        t = state
        t = _imul(t ^ (t >> 15), t | 1)
        t ^= (t + _imul(t ^ (t >> 7), t | 61)) % _UINT32
        return ((t ^ (t >> 14)) % _UINT32) / 4294967296

    return random


# --- the analysis -----------------------------------------------------------------


def segment_notes(
    contour: Any, config: Mapping[str, float | int] = NOTE_SEGMENTATION
) -> list[dict[str, Any]]:
    """Split a ``[[t, hz], ...]`` contour into note cores."""

    if js.nullish(contour) or isinstance(contour, str):
        points: list[Any] = []
    elif isinstance(contour, list):
        points = contour
    else:
        raise TypeError("contour is not iterable")
    runs: list[list[tuple[Any, float]]] = []
    current: list[tuple[Any, float]] = []
    for point in points:
        if not isinstance(point, list) or len(point) < 2 or not js.is_finite(point[1]):
            continue
        if point[1] <= 0:
            continue
        sample = (point[0], cents_of(point[1]))
        if not current:
            current.append(sample)
            continue
        previous = current[-1]
        gap = _sub(sample[0], previous[0])
        jump = abs(sample[1] - previous[1])
        if gap <= config["maxGapSeconds"] and jump <= config["maxJumpCents"]:
            current.append(sample)
        else:
            if len(current) >= config["minPoints"]:
                runs.append(current)
            current = [sample]
    if len(current) >= config["minPoints"]:
        runs.append(current)
    return [
        {
            "startSeconds": _round(run[0][0], 2),
            "durationSeconds": _round(
                _sub(run[-1][0], run[0][0]) + config["contourStepSeconds"], 2
            ),
            "medianCents": median_of([cents for _, cents in run]),
            "points": len(run),
        }
        for run in runs
    ]


def tuning_offset_for(notes: Sequence[Mapping[str, Any]]) -> float:
    sum_sin = 0.0
    sum_cos = 0.0
    for note in notes:
        angle = math.fmod(math.fmod(note["medianCents"], 100) + 100, 100) * (2 * math.pi / 100)
        sum_sin += math.sin(angle) * _weight(note["durationSeconds"])
        sum_cos += math.cos(angle) * _weight(note["durationSeconds"])
    return math.atan2(sum_sin, sum_cos) * (100 / (2 * math.pi))


def note_core_tuning(
    notes: Sequence[Mapping[str, Any]], config: Mapping[str, float | int] = NOTE_SEGMENTATION
) -> dict[str, Any]:
    """Score note medians against the take's own duration-weighted tuning offset."""

    if len(notes) < config["minNotesForTuning"]:
        note_time = 0.0
        for note in notes:
            note_time += _weight(note["durationSeconds"])
        return {
            "notes": len(notes),
            "noteTimeSeconds": _round(note_time, 2),
            "tuningOffsetCents": None,
            "centErrorMedian": None,
            "inTuneShare": None,
            "meanNoteSeconds": None,
            "longestNoteSeconds": None,
        }
    offset = tuning_offset_for(notes)
    weighted: list[tuple[float, float]] = []
    for note in notes:
        deviation = math.fmod(math.fmod(note["medianCents"] - offset + 50, 100) + 100, 100) - 50
        weighted.append((abs(deviation), _weight(note["durationSeconds"])))
    weighted.sort(key=lambda entry: entry[0])
    total_weight = 0.0
    for _, weight in weighted:
        total_weight += weight
    cumulative = 0.0
    weighted_median = weighted[-1][0]
    for absolute, weight in weighted:
        cumulative += weight
        if cumulative >= total_weight / 2:
            weighted_median = absolute
            break
    in_tune_weight = 0.0
    for absolute, weight in weighted:
        if absolute <= config["inTuneCents"]:
            in_tune_weight += weight
    longest = max(_weight(note["durationSeconds"]) for note in notes)
    return {
        "notes": len(notes),
        "noteTimeSeconds": _round(total_weight, 2),
        "tuningOffsetCents": _round(offset, 1),
        "centErrorMedian": _round(weighted_median, 1),
        "inTuneShare": _round(_div(in_tune_weight, total_weight), 3),
        "meanNoteSeconds": _round(total_weight / len(notes), 2),
        "longestNoteSeconds": _round(longest, 2),
    }


def theil_sen_slope(
    points: Sequence[tuple[float, Any]], min_pair_years: float = SLOPES["minPairYears"]
) -> Any:
    """The median of pairwise slopes, over pairs at least ``min_pair_years`` apart."""

    slopes: list[float] = []
    for i, (x_i, y_i) in enumerate(points):
        for x_j, y_j in points[i + 1 :]:
            dx = x_j - x_i
            if abs(dx) < min_pair_years:
                continue
            slopes.append(_sub(y_j, y_i) / dx)
    return median_of(slopes) if slopes else None


def bootstrap_ci(
    values: Sequence[Any],
    seed: float | int,
    resamples: int = int(BOOTSTRAP["resamples"]),
    level: float = BOOTSTRAP["level"],
) -> list[float | None] | None:
    """A seeded percentile interval of the median, resampling ``values``."""

    if len(values) < 2:
        return None
    random = mulberry32(seed)
    medians: list[Any] = []
    for _ in range(resamples):
        sample = [values[math.floor(random() * len(values))] for _ in range(len(values))]
        medians.append(median_of(sample))
    alpha = (1 - level) / 2
    return [_round(quantile_of(medians, alpha), 3), _round(quantile_of(medians, 1 - alpha), 3)]


def verdict_for(direction: str, ci: Sequence[Any] | None) -> str:
    if not ci:
        return "insufficient_data"
    low, high = ci
    if direction == "down_good":
        if high < 0:
            return "improving"
        if low > 0:
            return "declining"
        return "flat"
    if direction == "up_good":
        if low > 0:
            return "improving"
        if high < 0:
            return "declining"
        return "flat"
    if low > 0:
        return "trending_up"
    if high < 0:
        return "trending_down"
    return "flat"


def year_fraction_of(captured_at: Any) -> float | None:
    parsed = js.date_parse(js.to_string(captured_at))
    return None if parsed is None else parsed / _MS_PER_YEAR + 1970


def reliable_feature_takes(features_manifest: Any) -> list[Any]:
    """Completed, dated takes whose f0 frame rejection is at most 10%."""

    reliable = []
    for row in _array(js.prop(features_manifest, "results"), "results"):
        if js.prop(row, "status") != "completed" or not js.truthy(js.prop(row, "features")):
            continue
        if not js.truthy(js.prop(row, "capturedAt")):
            continue
        features = js.prop(row, "features")
        voicing = js.coalesce(js.prop(features, "voicing"), {})
        rejected = _add(
            js.coalesce(js.prop(voicing, "rejectedOutlierShare"), 0),
            js.coalesce(js.prop(voicing, "rejectedGlobalOutlierShare"), 0),
        )
        if js.to_number(rejected) <= RELIABLE_REJECTION_MAX:
            reliable.append(row)
    return reliable


def take_metrics_for(row: Any, note_core: Mapping[str, Any] | None) -> dict[str, Any]:
    features = js.prop(row, "features")
    vibrato_rate = js.coalesce(js.get(js.prop(features, "vibrato"), "meanRateHz"), None)
    return {
        "noteCoreCentError": None if note_core is None else note_core["centErrorMedian"],
        "inTuneShare": None if note_core is None else note_core["inTuneShare"],
        "rangeSemitones": js.coalesce(
            js.get(js.prop(features, "pitch"), "rangeSemitonesP05P95"), None
        ),
        "cpps": js.coalesce(js.get(js.prop(features, "quality"), "cpps"), None),
        "longestSustain": js.coalesce(
            js.get(js.prop(features, "phrasing"), "longestSustainedSeconds"), None
        ),
        "vibratoDistanceFrom5p5": None
        if vibrato_rate is None
        else _round(abs(js.to_number(vibrato_rate) - 5.5), 2),
    }


def eligible_clusters(
    lyrics_manifest: Any, takes_by_id: Mapping[tuple[Any, ...], Mapping[str, Any]]
) -> list[tuple[Any, list[Mapping[str, Any]]]]:
    """Clusters with at least 6 reliable takes spanning at least 1.5 years."""

    eligible = []
    for cluster in _array(js.prop(lyrics_manifest, "clusters"), "clusters"):
        ids = _array(js.prop(cluster, "recordingIds"), "recordingIds")
        takes = [take for take in (takes_by_id.get(_map_key(i)) for i in ids) if take is not None]
        fractions = [take["yearFraction"] for take in takes if take["yearFraction"] is not None]
        span = max(fractions) - min(fractions) if fractions else 0
        if len(takes) >= SLOPES["minTakes"] and span >= SLOPES["minSpanYears"]:
            eligible.append((cluster, takes))
    return eligible


@dataclass
class JourneysOptions:
    """The command line's options; path defaults come from :func:`resolve_paths`."""

    features: str = field(
        default_factory=lambda: str(resolve_paths().manifest("local-voice-features.json"))
    )
    lyrics: str = field(
        default_factory=lambda: str(resolve_paths().manifest("local-lyric-indicators.json"))
    )
    feature_dir: str = field(
        default_factory=lambda: str(resolve_paths().artifact("voice-features"))
    )
    out: str = field(default_factory=lambda: str(resolve_paths().manifest("song-journeys.json")))
    seed: float | int = 2026
    analyzed_at: str | None = None
    command: str = "analyze"
    dry_run: bool = False
    help: bool = False


def journeys_method(options: JourneysOptions) -> dict[str, Any]:
    return {
        "noteSegmentation": {**NOTE_SEGMENTATION, "id": "contour_note_cores.v1"},
        "tuning": {
            "id": "duration_weighted_circular_offset_mod_100.v1",
            "scoredOn": "note medians only (transitions and melisma excluded by the "
            "stability criterion)",
        },
        "slopes": {"id": "theil_sen_per_cluster.v1", **SLOPES},
        "bootstrap": {"id": "cluster_resample_median.v1", **BOOTSTRAP, "seed": options.seed},
        "reliability": "takes with total f0 frame rejection <= "
        + js.to_string(RELIABLE_REJECTION_MAX),
        "clusterVerificationNote": "clusters come from transcript-shingle similarity; acoustic "
        "verification/splitting is a planned upgrade and would need its own gated audio pass",
    }


def journeys_read_scope() -> dict[str, bool]:
    return {
        "committedManifests": True,
        "localFeatureDetailContours": True,
        "audioBytes": False,
        "transcriptText": False,
        "lyricText": False,
        "upload": False,
        "corpusMutation": False,
    }


def build_journeys_dry_run(
    features_manifest: Any, lyrics_manifest: Any, options: JourneysOptions
) -> dict[str, Any]:
    reliable = reliable_feature_takes(features_manifest)
    takes_by_id = {
        _map_key(js.prop(row, "recordingId")): {
            "yearFraction": year_fraction_of(js.prop(row, "capturedAt"))
        }
        for row in reliable
    }
    eligible = eligible_clusters(lyrics_manifest, takes_by_id)
    return {
        "dryRun": True,
        "operation": "same-song-journey-analysis",
        "architecture": {
            "slug": "kickoff-corpus-flow",
            "reads": [
                "voice-feature-manifest",
                "lyric-indicator-manifest",
                "local-voice-feature-store",
            ],
            "writes": ["song-journeys-manifest"],
        },
        "featuresPath": options.features,
        "lyricsPath": options.lyrics,
        "localFeatureDir": options.feature_dir,
        "outputPath": options.out,
        "scope": {
            "reliableTakes": len(reliable),
            "clustersTotal": js.coalesce(js.length(js.get(lyrics_manifest, "clusters")), 0),
            "clustersEligible": len(eligible),
            "eligibilityRule": f">= {js.to_string(SLOPES['minTakes'])} reliable takes spanning "
            f">= {js.to_string(SLOPES['minSpanYears'])} years",
        },
        "method": journeys_method(options),
        "readScope": journeys_read_scope(),
    }


def _detail_reader(feature_dir: str) -> Callable[[Any], Any]:
    def read(recording_id: Any) -> Any:
        try:
            return jsonio.read_json(js.path_join(feature_dir, f"{js.to_string(recording_id)}.json"))
        except FileNotFoundError:
            return None

    return read


def _per_year_rollup(takes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    per_year: dict[str, list[Mapping[str, Any]]] = {}
    for take in takes:
        key = js.to_string(take["year"])
        if key in _OBJECT_PROTOTYPE_KEYS:
            raise TypeError("perYear[take.year].push is not a function")
        per_year.setdefault(key, []).append(take)
    rollup: dict[str, Any] = {}
    for year in jsonio.object_key_order(sorted(per_year, key=js.utf16_key)):
        group = per_year[year]

        def median(key: str, group: Sequence[Mapping[str, Any]] = group) -> Any:
            return median_of([take["metrics"][key] for take in group])

        rollup[year] = {
            "n": len(group),
            "noteCoreCentError": _round(median("noteCoreCentError"), 1),
            "inTuneShare": _round(median("inTuneShare"), 3),
            "rangeSemitones": _round(median("rangeSemitones"), 2),
            "cpps": _round(median("cpps"), 2),
            "longestSustain": _round(median("longestSustain"), 2),
        }
    return rollup


def _journey(cluster: Any, takes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    slopes: dict[str, Any] = {}
    for dimension in DIMENSIONS:
        points = [
            (take["yearFraction"], take["metrics"][dimension["key"]])
            for take in takes
            if take["yearFraction"] is not None and take["metrics"][dimension["key"]] is not None
        ]
        slopes[dimension["key"]] = (
            _round(theil_sen_slope(points), 4) if len(points) >= SLOPES["minTakes"] else None
        )
    fractions = [take["yearFraction"] for take in takes if take["yearFraction"] is not None]
    cluster_id = js.prop(cluster, "clusterId")
    return js.defined(
        {
            "clusterId": cluster_id,
            "label": js.coalesce(js.prop(cluster, "label"), cluster_id),
            "takesUsed": len(takes),
            "firstYear": js.to_string(math.floor(min(fractions))),
            "lastYear": js.to_string(math.floor(max(fractions))),
            "perYear": _per_year_rollup(takes),
            "slopesPerYear": slopes,
        }
    )


def analyze_journeys(
    features_manifest: Any,
    lyrics_manifest: Any,
    options: JourneysOptions,
    read_detail: ReadDetail | None = None,
) -> dict[str, Any]:
    """Build the journeys manifest, write it to ``options.out`` and return it.

    ``read_detail(recordingId)`` returns a take's feature detail record or ``None``; by
    default it reads ``<feature_dir>/<recordingId>.json``. A row without a recordingId
    is passed as ``None`` to a custom reader."""

    default_reader = _detail_reader(options.feature_dir)
    reliable = reliable_feature_takes(features_manifest)
    note_core_takes: list[dict[str, Any]] = []
    takes_by_id: dict[tuple[Any, ...], dict[str, Any]] = {}
    missing_details = 0
    for row in reliable:
        recording_id = js.prop(row, "recordingId")
        if read_detail is None:
            detail = default_reader(recording_id)
        else:
            detail = read_detail(None if recording_id is js.MISSING else recording_id)
        contour = js.get(js.get(detail, "detail"), "f0ContourVoiced50ms")
        note_core: dict[str, Any] | None = None
        if js.truthy(contour):
            note_core = note_core_tuning(segment_notes(contour))
        else:
            missing_details += 1
        captured_at = js.prop(row, "capturedAt")
        year = js.prop(row, "year")
        if js.nullish(year):
            year = _slice(captured_at, 0, 4) if js.truthy(captured_at) else "unknown"
        quarter = None
        if js.truthy(captured_at):
            month_third = js.string_to_number(_slice(captured_at, 5, 7)) / 3
            ceiling = math.ceil(month_third) if math.isfinite(month_third) else month_third
            quarter = f"{_slice(captured_at, 0, 4)}-Q{js.to_string(ceiling)}"
        take = {
            "id": recording_id,
            "year": year,
            "yearFraction": year_fraction_of(captured_at),
            "quarter": quarter,
            "noteCore": note_core,
            "metrics": take_metrics_for(row, note_core),
        }
        takes_by_id[_map_key(recording_id)] = take
        if note_core is not None:
            note_core_takes.append(
                js.defined(
                    {
                        "id": take["id"],
                        "year": take["year"],
                        "quarter": take["quarter"],
                        **note_core,
                    }
                )
            )

    journeys = [
        _journey(cluster, takes)
        for cluster, takes in eligible_clusters(lyrics_manifest, takes_by_id)
    ]

    improvement_index = []
    for index, dimension in enumerate(DIMENSIONS):
        cluster_slopes = [
            journey["slopesPerYear"][dimension["key"]]
            for journey in journeys
            if journey["slopesPerYear"][dimension["key"]] is not None
        ]
        ci = bootstrap_ci(cluster_slopes, options.seed + index)
        improvement_index.append(
            {
                "key": dimension["key"],
                "label": dimension["label"],
                "direction": dimension["direction"],
                "medianSlopePerYear": _round(median_of(cluster_slopes), 4),
                "ci95": ci,
                "clustersUsed": len(cluster_slopes),
                "verdict": verdict_for(dimension["direction"], ci),
            }
        )

    analyzed_at = options.analyzed_at if options.analyzed_at is not None else jsonio.iso_timestamp()
    results = js.get(features_manifest, "results")
    clusters = js.get(lyrics_manifest, "clusters")
    manifest = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "generatedAt": analyzed_at,
        "updatedAt": analyzed_at,
        "sources": {
            "features": {
                "schemaVersion": js.coalesce(js.get(features_manifest, "schemaVersion"), None),
                "generatedAt": js.coalesce(js.get(features_manifest, "generatedAt"), None),
                "updatedAt": js.coalesce(js.get(features_manifest, "updatedAt"), None),
                "recordingCount": js.coalesce(js.length(results), None),
            },
            "lyrics": {
                "schemaVersion": js.coalesce(js.get(lyrics_manifest, "schemaVersion"), None),
                "generatedAt": js.coalesce(js.get(lyrics_manifest, "generatedAt"), None),
                "clusterCount": js.coalesce(js.length(clusters), None),
            },
        },
        "method": journeys_method(options),
        "readScope": journeys_read_scope(),
        "repoSafe": {
            "containsContourArrays": False,
            "containsAudioBytes": False,
            "containsTranscriptText": False,
            "containsLyricText": False,
            "genericClusterLabelsOnly": True,
        },
        "totals": {
            "reliableTakes": len(reliable),
            "takesWithNoteCore": len(note_core_takes),
            "missingDetailFiles": missing_details,
            "clustersEligible": len(journeys),
        },
        "noteCore": {"takes": note_core_takes},
        "journeys": journeys,
        "improvementIndex": improvement_index,
    }
    _write(options.out, manifest)
    return manifest


def _write(out: str, payload: Any) -> None:
    """``writeJson``: ``-`` is stdout; otherwise the file, then its path as given."""

    if out == "-":
        jsonio.write_json("-", payload)
        return
    jsonio.write_json(out, payload, announce=False)
    sys.stdout.write(f"{out}\n")


# --- command line -----------------------------------------------------------------


def _require_value(args: list[str], flag: str) -> str:
    value = args.pop(0) if args else ""
    if not value or value.startswith("--"):
        raise JourneysError(f"{flag} requires a value")
    return value


def _non_negative_integer(value: str, flag: str) -> float | int:
    parsed = js.string_to_number(value)
    if not math.isfinite(parsed) or not parsed.is_integer() or parsed < 0:
        raise JourneysError(f"{flag} must be a non-negative integer")
    return int(parsed) if parsed < 2**53 else parsed


def parse_args(argv: Sequence[str]) -> JourneysOptions:
    first = argv[0] if argv else ""
    has_command = bool(first) and not first.startswith("--") and first != "help"
    options = JourneysOptions(command=first if has_command else "analyze")
    args = list(argv[1:] if has_command else argv)
    while args:
        flag = args.pop(0)
        if flag == "--analyzed-at":
            options.analyzed_at = _require_value(args, flag)
        elif flag == "--dry-run":
            options.dry_run = True
        elif flag == "--feature-dir":
            options.feature_dir = _require_value(args, flag)
        elif flag == "--features":
            options.features = _require_value(args, flag)
        elif flag == "--lyrics":
            options.lyrics = _require_value(args, flag)
        elif flag == "--out":
            options.out = _require_value(args, flag)
        elif flag == "--seed":
            options.seed = _non_negative_integer(_require_value(args, flag), flag)
        elif flag in ("--help", "help"):
            options.help = True
        else:
            raise JourneysError(f"unsupported argument: {flag}")
    return options


def run(argv: Sequence[str]) -> int:
    options = parse_args(argv)
    if options.help:
        sys.stdout.write(HELP)
        return 0
    if options.command == "analyze":
        features_manifest = jsonio.read_json(options.features)
        lyrics_manifest = jsonio.read_json(options.lyrics)
        if options.dry_run:
            _write("-", build_journeys_dry_run(features_manifest, lyrics_manifest, options))
            return 0
        analyze_journeys(features_manifest, lyrics_manifest, options)
        return 0
    raise JourneysError(f"unsupported command: {options.command}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except Exception as error:
        sys.stderr.write(f"error: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
