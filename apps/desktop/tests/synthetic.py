"""Synthetic manifests, audio and a running /api/v1 for the app's tests. Nothing real.

``data_root`` writes a corpus of 72 takes (2019 to 2024, three a quarter) through the
journey manifests the server reads (index, filter results, transcripts, lyric indicators,
voice features, song journeys) with the record shapes of the backend's own fixtures
(``tests/journey_browser_fixtures.py``). Ids are what the server derives from the file
names, so playback works for the takes that have audio: ``audio_root`` writes short sine
tones (AAC in MP4, made with PyAV) for some of them. ``big_root`` is the Corpus table's
scale case: about 7,000 index rows. ``serve`` runs the backend's ``ApiServer`` with the
journey routes over a data root, on 127.0.0.1, behind the backend's test token verifier.
"""

from __future__ import annotations

import io
import json
import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from home_host_fixtures import ISSUER, jwks, mint, policy
from journey_browser_fixtures import feature_row, journey_config, transcript, write_manifests

from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.journey_routes import JourneyRoutes
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.journey.browser import JourneyConfig, JourneyStore
from cole_voice_lab.journey.corpus import recording_id
from cole_voice_lab.journey.jsonio import write_json
from cole_voice_lab.paths import AssetLayout

Json = dict[str, Any]

YEARS = tuple(range(2019, 2025))
BUCKETS = (
    "clean_singing",
    "clean_singing",
    "clean_singing",
    "uncertain_manual_review",
    "noise_contaminated_singing",
    "music_contaminated_singing",
    "non_singing",
    "clean_singing",
)
CLUSTERS = {
    "lyric-cluster-001": "recurring_lyric_cluster_001",
    "lyric-cluster-002": "recurring_lyric_cluster_002",
}
TRANSCRIPT_TEXT = "a synthetic transcript line"
PLAYBACK_APPROVAL = "synthetic approval for the app's tests"


@dataclass(frozen=True)
class Take:
    index: int
    year: int
    month: int
    day: int
    filename: str
    recording_id: str
    captured_at: str
    bucket: str
    cluster: str | None


def takes() -> list[Take]:
    out = []
    index = 0
    for year in YEARS:
        for quarter in range(4):
            for k in range(3):
                month = quarter * 3 + 1 + k
                day = 5 + k
                filename = f"{year}{month:02d}{day:02d} 20{k}000-{index:03d}.m4a"
                cluster = (
                    "lyric-cluster-001"
                    if index % 4 == 0
                    else "lyric-cluster-002"
                    if index % 6 == 1
                    else None
                )
                out.append(
                    Take(
                        index=index,
                        year=year,
                        month=month,
                        day=day,
                        filename=filename,
                        recording_id=recording_id(filename),
                        captured_at=f"{year}-{month:02d}-{day:02d}T20:{k * 10:02d}:00Z",
                        bucket=BUCKETS[index % len(BUCKETS)],
                        cluster=cluster,
                    )
                )
                index += 1
    return out


def _index(all_takes: list[Take]) -> Json:
    return {
        "schemaVersion": "voice-journey.recording-index.v1",
        "totals": {"recordings": len(all_takes)},
        "recordings": [
            {
                "recordingId": t.recording_id,
                "filename": t.filename,
                "capturedAt": t.captured_at,
                "durationSeconds": 30 + t.index,
                "file": {"sizeBytes": 1_048_576 + 1000 * t.index},
            }
            for t in all_takes
        ],
    }


def _results(all_takes: list[Take]) -> Json:
    return {
        "schemaVersion": "voice-journey.full-corpus-filter-results.v1",
        "results": [
            {
                "recordingId": t.recording_id,
                "filename": t.filename,
                "capturedAt": t.captured_at,
                "year": str(t.year),
                "features": {"durationSeconds": 30 + t.index},
                "classification": {
                    "finalBucket": t.bucket,
                    "confidence": round(0.3 + (t.index % 7) / 10, 2),
                    "rationale": f"{t.bucket} rationale {t.index}",
                },
                "humanSpotCheck": {
                    "recommended": t.bucket == "uncertain_manual_review",
                    "status": "reviewed" if t.index % 11 == 5 else "not_reviewed",
                    "reviewedBy": None,
                    "notes": None,
                },
            }
            for t in all_takes
        ],
    }


def _transcripts(all_takes: list[Take]) -> Json:
    results = []
    for t in all_takes:
        if t.index % 3 == 0:
            results.append(transcript(t.recording_id, "completed", "en", 20 + t.index, True))
        elif t.index % 5 == 1:
            results.append(transcript(t.recording_id, "failed", None, None, False))
    return {"schemaVersion": "voice-journey.local-stt-transcripts.v1", "results": results}


def _lyrics(all_takes: list[Take]) -> Json:
    results = []
    for t in all_takes:
        marked = t.cluster is not None
        results.append(
            {
                "recordingId": t.recording_id,
                "capturedAt": t.captured_at,
                "lyricMatch": {
                    "marked": marked,
                    "status": "marked_indicator" if marked else "not_marked",
                    "method": "cross_recording_similarity" if marked else "none",
                    "confidence": 0.91 if marked else 0.35,
                    "matchedCluster": t.cluster,
                    "matchedClusterLabel": CLUSTERS[t.cluster] if t.cluster else None,
                    "signals": {
                        "wordlessVocalise": {
                            "marked": t.index % 9 == 2,
                            "confidence": 0.8 if t.index % 9 == 2 else 0,
                            "detectedLanguage": "en",
                        }
                    },
                },
            }
        )
    return {
        "schemaVersion": "voice-journey.local-lyric-indicators.v1",
        "totals": {"recordings": len(all_takes)},
        "clusters": [
            {
                "clusterId": cluster,
                "label": label,
                "method": "cross_recording_shingle_similarity.v1",
                "recordingIds": [t.recording_id for t in all_takes if t.cluster == cluster],
            }
            for cluster, label in CLUSTERS.items()
        ],
        "results": results,
    }


def _features(all_takes: list[Take]) -> Json:
    results = []
    for t in all_takes:
        step = t.year - YEARS[0]
        f0 = {
            "p05": 100 + 2 * step,
            "p25": 150 + 6 * step,
            "p50": 200 + 10 * step + (t.index % 3),
            "p75": 260 + 12 * step,
            "p95": 300 + 15 * step,
        }
        flagged = t.index % 12 == 7
        row = feature_row(
            t.recording_id,
            t.captured_at,
            f0,
            vibRate=round(4.0 + 0.2 * step + (t.index % 3) / 10, 2),
            vibExtent=60 - 3 * step + (t.index % 3),
            cpps=round(10 + 0.3 * step, 2),
            hnr=round(18 - 0.5 * step, 2),
            sustain=round(2 + 0.2 * step, 2),
            rejectedOutlierShare=0.2 if flagged else 0.01,
        )
        row["filename"] = t.filename
        row["year"] = str(t.year)
        row["features"]["dynamics"]["voicedRmsDbP50"] = -25 + (t.index % 5)
        results.append(row)
    return {
        "schemaVersion": "voice-journey.local-voice-features.v1",
        "generatedAt": "2026-07-31T00:00:00.000Z",
        "updatedAt": "2026-07-31T00:00:00.000Z",
        "results": results,
    }


def _journeys(all_takes: list[Take]) -> Json:
    note_core = [
        {
            "id": t.recording_id,
            "year": str(t.year),
            "quarter": f"{t.year}-Q{(t.month - 1) // 3 + 1}",
            "centErrorMedian": round(22 - 0.5 * (t.year - YEARS[0]) + (t.index % 3) / 10, 2),
        }
        for t in all_takes
    ]
    journeys = []
    for n, cluster in enumerate(CLUSTERS):
        members = [t for t in all_takes if t.cluster == cluster]
        per_year = {
            str(year): {
                "n": sum(1 for t in members if t.year == year),
                "noteCoreCentError": round(21 - 0.4 * (year - YEARS[0]) - n, 2),
            }
            for year in YEARS
            if any(t.year == year for t in members)
        }
        journeys.append(
            {
                "clusterId": cluster,
                "label": CLUSTERS[cluster],
                "takesUsed": len(members),
                "firstYear": str(YEARS[0]),
                "lastYear": str(YEARS[-1]),
                "perYear": per_year,
                "slopesPerYear": {"noteCoreCentError": -0.4 - n / 10, "cpps": 0.3 - n / 2},
            }
        )
    return {
        "schemaVersion": "voice-journey.song-journeys.v1",
        "totals": {"reliableTakes": 66, "takesWithNoteCore": 72, "clustersEligible": 2},
        "method": {"bootstrap": {"resamples": 1000, "seed": 2026}},
        "noteCore": {"takes": note_core},
        "journeys": journeys,
        "improvementIndex": [
            {
                "key": "noteCoreCentError",
                "label": "note-core tuning error (cents)",
                "medianSlopePerYear": -0.45,
                "ci95": [-0.8, -0.1],
                "clustersUsed": 2,
                "verdict": "improving",
            },
            {
                "key": "cpps",
                "label": "voice clarity (CPPS dB)",
                "medianSlopePerYear": 0.05,
                "ci95": [-0.2, 0.3],
                "clustersUsed": 2,
                "verdict": "flat",
            },
        ],
    }


def tone_m4a(hz: float = 440.0, seconds: float = 0.5, rate: int = 44100) -> bytes:
    """A sine tone as AAC in MP4 (what a Voice Memo is), made with PyAV."""
    import av
    import numpy as np

    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="mp4") as container:
        stream = container.add_stream("aac", rate=rate, layout="mono")
        frames = int(seconds * rate)
        t = np.arange(frames, dtype=np.float32) / rate
        samples = (0.5 * np.sin(2 * math.pi * hz * t)).astype(np.float32)
        for start in range(0, frames, 1024):
            chunk = samples[start : start + 1024].reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="mono")
            frame.sample_rate = rate
            frame.pts = start
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue()


def data_root(root: Path) -> Path:
    """The synthetic journey data root under ``root`` (its ``data/``)."""
    all_takes = takes()
    data = root / "data"
    write_manifests(
        data,
        index=_index(all_takes),
        results=_results(all_takes),
        transcripts=_transcripts(all_takes),
        lyrics=_lyrics(all_takes),
        features=_features(all_takes),
        journeys=_journeys(all_takes),
    )
    texts = data / "artifacts" / "stt-transcripts"
    texts.mkdir(parents=True, exist_ok=True)
    details = data / "artifacts" / "voice-features"
    details.mkdir(parents=True, exist_ok=True)
    features = {row["recordingId"]: row for row in _features(all_takes)["results"]}
    for t in all_takes:
        if t.index % 3 == 0:
            (texts / f"{t.recording_id}.txt").write_text(TRANSCRIPT_TEXT, encoding="utf-8")
        if t.index % 2 == 0:
            detail = {
                "methodId": "synthetic",
                "summary": features[t.recording_id]["features"],
                "detail": {
                    "f0ContourVoiced50ms": [
                        [round(i * 0.05, 2), round(200 + 20 * math.sin(i / 5), 1)]
                        for i in range(60)
                    ]
                },
            }
            write_json(details / f"{t.recording_id}.json", detail, announce=False)
    return data


def audio_root(root: Path, *, count: int = 8) -> Path:
    """Tones for the first ``count`` takes, as the corpus root the server reads."""
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    tone = tone_m4a()
    for t in takes()[:count]:
        (corpus / t.filename).write_bytes(tone)
    # Every take of the referee's clusters plays too.
    for t in takes():
        if t.cluster is not None:
            (corpus / t.filename).write_bytes(tone)
    return corpus


def big_root(root: Path, rows: int = 7000) -> Path:
    """An index and filter results of ``rows`` takes (the Corpus table's scale case)."""
    data = root / "big"
    recordings, results = [], []
    for i in range(rows):
        year = 2019 + i % 7
        filename = f"{year}0101 {i:06d}-big.m4a"
        identifier = recording_id(filename)
        captured = f"{year}-01-01T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z"
        recordings.append(
            {
                "recordingId": identifier,
                "filename": filename,
                "capturedAt": captured,
                "durationSeconds": 10 + i % 300,
                "file": {"sizeBytes": 100_000 + i},
            }
        )
        results.append(
            {
                "recordingId": identifier,
                "filename": filename,
                "capturedAt": captured,
                "year": str(year),
                "features": {"durationSeconds": 10 + i % 300},
                "classification": {
                    "finalBucket": BUCKETS[i % len(BUCKETS)],
                    "confidence": 0.5,
                    "rationale": "bulk",
                },
                "humanSpotCheck": {"recommended": False, "status": "not_reviewed"},
            }
        )
    write_manifests(
        data,
        index={
            "schemaVersion": "voice-journey.recording-index.v1",
            "totals": {"recordings": rows},
            "recordings": recordings,
        },
        results={
            "schemaVersion": "voice-journey.full-corpus-filter-results.v1",
            "results": results,
        },
    )
    return data


def config_for(
    data: Path, *, corpus: Path | None = None, playback: bool = True, **changes: Any
) -> JourneyConfig:
    """The journey configuration of a synthetic data root, with every gate open by default."""
    base = journey_config(
        data,
        transcript_dir=data / "artifacts" / "stt-transcripts",
        feature_dir=data / "artifacts" / "voice-features",
        playback_approval=PLAYBACK_APPROVAL if playback else None,
        queue_size=6,
        **({"corpus_root": corpus} if corpus is not None else {}),
    )
    return replace(base, **changes)


@dataclass
class Served:
    server: ApiServer
    store: JourneyStore | None
    url: str


@contextmanager
def serve(work: Path, config: JourneyConfig | None) -> Iterator[Served]:
    """``ApiServer`` with the journey routes over ``config`` on 127.0.0.1 (``None``: the
    server runs without them)."""
    work.mkdir(parents=True, exist_ok=True)
    jwks_path = work / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    lab = work / "lab"
    layout = AssetLayout.validate(lab) if lab.exists() else AssetLayout.initialize(lab)
    store = JourneyStore(config, log=lambda message: None) if config is not None else None
    server = ApiServer(
        ("127.0.0.1", 0),
        staging=IntakeStaging(layout, min_free_bytes=0),
        verifier=TokenVerifier(policy(jwks_path)),
        meta=ApiMeta(server_build="synthetic-build", minimum_client_version="0.1.0"),
        log=False,
        routes=(JourneyRoutes(store),),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Served(server, store, f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()


__all__ = [
    "ISSUER",
    "PLAYBACK_APPROVAL",
    "TRANSCRIPT_TEXT",
    "Served",
    "Take",
    "audio_root",
    "big_root",
    "config_for",
    "data_root",
    "mint",
    "serve",
    "takes",
    "tone_m4a",
]
