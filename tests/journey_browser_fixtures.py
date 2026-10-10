"""Synthetic manifests of corpus-browser.test.mjs, and a running API with the journey routes.

The manifests are the JS test's fixtures, built in memory and written into a temporary
data root (``<root>/manifests``) only by the tests; no generated manifest is committed.
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from home_host_fixtures import jwks, mint, policy

from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.journey_routes import JourneyRoutes
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.journey.browser import JourneyConfig, JourneyStore
from cole_voice_lab.journey.jsonio import write_json
from cole_voice_lab.journey.paths import resolve_paths
from cole_voice_lab.paths import AssetLayout

Json = dict[str, Any]


def feature_row(
    recording_id: str, captured_at: str, f0: Mapping[str, float], **overrides: Any
) -> Json:
    status = overrides.get("status", "completed")
    return {
        "recordingId": recording_id,
        "filename": f"{recording_id}.m4a",
        "capturedAt": captured_at,
        "year": captured_at[:4],
        "status": status,
        "features": None
        if status == "failed"
        else {
            "durationSeconds": overrides.get("durationSeconds", 60),
            "voicing": {
                "voicedShare": 0.7,
                "frameCount": 6000,
                "rejectedOutlierShare": overrides.get("rejectedOutlierShare", 0.01),
                "rejectedGlobalOutlierShare": overrides.get("rejectedGlobalOutlierShare", 0),
            },
            "pitch": {
                "f0Hz": {key: f0.get(key) for key in ("p05", "p25", "p50", "p75", "p95")},
                "rangeSemitonesP05P95": 11,
                "madSemitones": 1.1,
            },
            "tuning": {"offsetCents": 0, "medianAbsCentError": 21, "within25CentsShare": 0.55},
            "vibrato": {
                "sustainedSegmentCount": 3,
                "vibratoSegmentCount": 1,
                "meanRateHz": overrides.get("vibRate", 4.1),
                "meanExtentCents": overrides.get("vibExtent", 45),
                "vibratoTimeShare": 0.3,
            },
            "quality": {
                "jitterLocal": 0.01,
                "shimmerLocal": 0.07,
                "meanHnrDb": overrides.get("hnr", 15),
                "cpps": overrides.get("cpps", 11),
            },
            "spectral": {
                "centroidHzMean": 1400,
                "centroidHzSd": 300,
                "rolloffHzMean": 2800,
                "singerFormantRatio": 0.04,
            },
            "dynamics": {
                "voicedRmsDbP10": -32,
                "voicedRmsDbP50": -25,
                "voicedRmsDbP90": -19,
                "dynamicSpreadDb": 13,
            },
            "phrasing": {
                "longestSustainedSeconds": overrides.get("sustain", 2.8),
                "meanVoicedSegmentSeconds": 1.7,
                "pauseRatePerMinute": 9,
            },
        },
    }


def fake_features() -> Json:
    results = []
    for i in range(12):
        month = f"{(i % 6) + 1:02d}"
        results.append(
            feature_row(
                f"y19-{i}",
                f"2019-{month}-10T00:00:00Z",
                {"p05": 90, "p25": 110, "p50": 150, "p75": 190, "p95": 260},
            )
        )
        results.append(
            feature_row(
                f"y21-{i}",
                f"2021-{month}-10T00:00:00Z",
                {"p05": 100, "p25": 150, "p50": 310, "p75": 350, "p95": 390},
            )
        )
    results.append(
        feature_row(
            "flagged",
            "2021-02-11T00:00:00Z",
            {"p05": 80, "p25": 120, "p50": 200, "p75": 500, "p95": 900},
            rejectedOutlierShare=0.2,
        )
    )
    results.append(feature_row("failed", "2021-03-11T00:00:00Z", {}, status="failed"))
    return {
        "schemaVersion": "voice-journey.local-voice-features.v1",
        "generatedAt": "2026-07-31T00:00:00.000Z",
        "updatedAt": "2026-07-31T00:00:00.000Z",
        "results": results,
    }


def recording(recording_id: str, filename: str, captured_at: str, size_bytes: int) -> Json:
    return {
        "recordingId": recording_id,
        "filename": filename,
        "capturedAt": captured_at,
        "durationSeconds": None,
        "file": {"sizeBytes": size_bytes},
    }


def fake_index() -> Json:
    return {
        "schemaVersion": "voice-journey.recording-index.v1",
        "totals": {"recordings": 7},
        "recordings": [
            recording("a", "20190101 000000-A.m4a", "2019-01-01T00:00:00Z", 1000),
            recording("b", "20200101 000000-B.m4a", "2020-01-01T00:00:00Z", 2000),
            recording("c", "20200201 000000-C.m4a", "2020-02-01T00:00:00Z", 3000),
            recording("d", "20210101 000000-D.m4a", "2021-01-01T00:00:00Z", 4000),
            recording("e", "20220101 000000-E.m4a", "2022-01-01T00:00:00Z", 5000),
            recording("f", "20230101 000000-F.m4a", "2023-01-01T00:00:00Z", 6000),
            recording("g", "20240101 000000-G.m4a", "2024-01-01T00:00:00Z", 7000),
        ],
    }


def result(
    recording_id: str,
    filename: str,
    year: str,
    duration_seconds: float,
    bucket: str,
    confidence: float,
    spot_check_status: str,
) -> Json:
    return {
        "recordingId": recording_id,
        "filename": filename,
        "capturedAt": f"{year}-01-01T00:00:00Z",
        "year": year,
        "features": {"durationSeconds": duration_seconds},
        "classification": {
            "finalBucket": bucket,
            "confidence": confidence,
            "rationale": f"{bucket} rationale",
        },
        "humanSpotCheck": {
            "recommended": bucket == "uncertain_manual_review",
            "status": spot_check_status,
            "reviewedBy": None,
            "notes": None,
        },
    }


def fake_results() -> Json:
    return {
        "schemaVersion": "voice-journey.full-corpus-filter-results.v1",
        "results": [
            result("a", "20190101 000000-A.m4a", "2019", 10, "clean_singing", 0.91, "not_reviewed"),
            result(
                "b",
                "20200101 000000-B.m4a",
                "2020",
                20,
                "uncertain_manual_review",
                0.42,
                "not_reviewed",
            ),
            result("c", "20200201 000000-C.m4a", "2020", 30, "non_singing", 0.8, "reviewed"),
            result(
                "d",
                "20210101 000000-D.m4a",
                "2021",
                40,
                "uncertain_manual_review",
                0.31,
                "not_reviewed",
            ),
            result(
                "e",
                "20220101 000000-E.m4a",
                "2022",
                50,
                "noise_contaminated_singing",
                0.72,
                "not_reviewed",
            ),
            result(
                "f",
                "20230101 000000-F.m4a",
                "2023",
                60,
                "music_contaminated_singing",
                0.64,
                "not_reviewed",
            ),
            result("g", "20240101 000000-G.m4a", "2024", 70, "clean_singing", 0.88, "not_reviewed"),
        ],
    }


def fake_state() -> Json:
    return {
        "schemaVersion": "voice-journey.corpus-browser-state.v1",
        "updatedAt": "2026-07-16T00:00:00.000Z",
        "verdicts": [
            {
                "recordingId": "b",
                "verdict": "clean_singing",
                "note": "fixture",
                "reviewedBy": "tester",
                "reviewedAt": "2026-07-16T00:00:00.000Z",
                "source": "corpus-browser",
            }
        ],
    }


def transcript(
    recording_id: str, status: str, language: str | None, words: int | None, has_text: bool
) -> Json:
    return {
        "recordingId": recording_id,
        "status": status,
        "language": {"detected": language},
        "counts": {
            "words": words,
            "segments": None if words is None else max(1, -(-words // 10)),
            "characters": None if words is None else words * 5,
        },
        "derivedFeatures": {"hasTranscribedWords": None if words is None else words > 0},
        "localArtifacts": {
            "transcriptTextPath": f"local-artifacts/stt-transcripts/{recording_id}.txt",
            "localOnly": True,
            "textIncludedInManifest": False,
        }
        if has_text
        else None,
    }


def fake_transcripts() -> Json:
    return {
        "schemaVersion": "voice-journey.local-stt-transcripts.v1",
        "results": [
            transcript("a", "completed", "en", 42, True),
            transcript("b", "completed", "en", 0, True),
            transcript("d", "failed", None, None, False),
        ],
    }


def lyric(
    recording_id: str,
    marked: bool,
    cluster_id: str | None,
    wordless: bool,
    captured_at: str | None = None,
) -> Json:
    return {
        "recordingId": recording_id,
        "capturedAt": captured_at,
        "lyricMatch": {
            "marked": marked,
            "status": "marked_indicator" if marked else "not_marked",
            "method": "cross_recording_similarity" if marked else "none",
            "confidence": 0.91 if marked else 0.35,
            "matchedCluster": cluster_id,
            "matchedClusterLabel": "recurring_lyric_cluster_001" if cluster_id else None,
            "signals": {
                "wordlessVocalise": {
                    "marked": wordless,
                    "confidence": 0.8 if wordless else 0,
                    "detectedLanguage": "en",
                }
            },
        },
    }


def fake_lyrics() -> Json:
    return {
        "schemaVersion": "voice-journey.local-lyric-indicators.v1",
        "totals": {"recordings": 3},
        "clusters": [
            {
                "clusterId": "lyric-cluster-001",
                "label": "recurring_lyric_cluster_001",
                "method": "cross_recording_shingle_similarity.v1",
                "recordingIds": ["a", "b"],
            }
        ],
        "results": [
            lyric("a", True, "lyric-cluster-001", False, "2019-01-01T00:00:00Z"),
            lyric("b", True, None, True, "2020-01-01T00:00:00Z"),
            lyric("c", False, None, False, "2020-02-01T00:00:00Z"),
        ],
    }


def referee_lyrics() -> Json:
    return {
        "schemaVersion": "voice-journey.local-lyric-indicators.v1",
        "clusters": [
            {
                "clusterId": "cluster-song-1",
                "label": "recurring_lyric_cluster_S1",
                "recordingIds": [
                    "y19-0",
                    "y19-1",
                    "y19-2",
                    "y21-0",
                    "y21-1",
                    "y21-2",
                    "flagged",
                    "failed",
                ],
            },
            {
                "clusterId": "cluster-song-2",
                "label": "recurring_lyric_cluster_S2",
                "recordingIds": ["y19-3", "y21-3"],
            },
        ],
        "results": [],
    }


# ---- a server --------------------------------------------------------------------------

MANIFEST_NAMES = {
    "index": "recording-index.json",
    "results": "full-corpus-filter-results.json",
    "transcripts": "local-stt-transcripts.json",
    "lyrics": "local-lyric-indicators.json",
    "features": "local-voice-features.json",
    "journeys": "song-journeys.json",
}


def write_manifests(data_root: Path, **manifests: Any) -> None:
    for key, payload in manifests.items():
        write_json(data_root / "manifests" / MANIFEST_NAMES[key], payload, announce=False)


def journey_config(data_root: Path, **changes: Any) -> JourneyConfig:
    paths = resolve_paths({"VOICE_JOURNEY_DATA": str(data_root)})
    config = JourneyConfig.from_paths(paths)
    fields = {
        "transcript_dir": None,
        "feature_dir": None,
        "queue_size": 5,
        **changes,
    }
    return replace(config, **fields)


@dataclass
class Running:
    server: ApiServer
    store: JourneyStore | None
    data_root: Path

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def call(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = "default",
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = dict(headers or {})
        if token is not None:
            sent["Authorization"] = f"Bearer {mint() if token == 'default' else token}"
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        data = response.read()
        answer = {name.lower(): value for name, value in response.getheaders()}
        connection.close()
        return response.status, answer, data

    def json(self, method: str, path: str, payload: Any = None, **kwargs: Any) -> tuple[int, Any]:
        body = None if payload is None else json.dumps(payload).encode()
        status, _, data = self.call(method, path, body, **kwargs)
        return status, json.loads(data) if data else None

    def get(self, path: str) -> Any:
        status, payload = self.json("GET", path)
        assert status == 200, (status, payload)
        return payload


@contextmanager
def running(
    tmp_path: Path, config: JourneyConfig | None = None, *, enabled: bool = True
) -> Iterator[Running]:
    """An ``ApiServer`` with the journey routes over ``config`` (or disabled)."""

    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    lab = tmp_path / "lab"
    layout = AssetLayout.validate(lab) if lab.exists() else AssetLayout.initialize(lab)
    store = JourneyStore(config, log=lambda message: None) if enabled and config else None
    server = ApiServer(
        ("127.0.0.1", 0),
        staging=IntakeStaging(layout, min_free_bytes=0),
        verifier=TokenVerifier(policy(jwks_path)),
        meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
        log=False,
        routes=(JourneyRoutes(store),),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Running(server, store, tmp_path / "data")
    finally:
        server.shutdown()
        server.server_close()
