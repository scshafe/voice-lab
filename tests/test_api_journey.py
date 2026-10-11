"""corpus-browser.test.mjs's HTTP cases and paths.test.mjs's two server cases, translated
(design §5.3), against voice-lab's API server with the journey routes: the ``/api/v1``
resources, the SPA's page routes (308 to the web view), auth on every route, voice-journey's
old paths being gone (phase 4), and the cases that hold the server's remaining branches.
Every request carries a bearer token unless a case says otherwise."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest
from home_host_fixtures import ISSUER, SUBJECT, jwks, mint
from journey_browser_fixtures import (
    fake_features,
    fake_index,
    fake_lyrics,
    fake_results,
    fake_transcripts,
    journey_config,
    recording,
    referee_lyrics,
    result,
    running,
    write_manifests,
)

from cole_voice_lab.api import server as server_module
from cole_voice_lab.api.contracts import REFUSAL_CODES
from cole_voice_lab.api.journey_contracts import (
    GoalAbandoned,
    GoalCreated,
    JourneyDocument,
    JourneyFeatureDetail,
    JourneyRecording,
    JourneyRecordings,
    JourneyReviewQueue,
    JourneySummary,
    JourneyTranscript,
    RefereeNext,
    RefereeResults,
    RefereeVerdictAnswer,
    ReviewVerdictAnswer,
    WatchStatus,
)
from cole_voice_lab.journey.browser import WATCH_STATE_VERSION
from cole_voice_lab.journey.corpus import recording_id
from cole_voice_lab.journey.jsonio import read_json
from cole_voice_lab.paths import AssetLayout


def _data(tmp_path: Path, **manifests: Any) -> Path:
    data = tmp_path / "data"
    write_manifests(
        data,
        **{"index": fake_index(), "results": fake_results(), **manifests},
    )
    return data


# ---- corpus-browser.test.mjs ----------------------------------------------------------


def test_page_routes_redirect_to_the_view_instead_of_the_spa(tmp_path: Path) -> None:
    """The JS served a "not built" notice without web/dist; the PC never serves the SPA
    (D-035), so every page route and asset is a 308 to /view/, without identity. They stay
    after the old API paths went: the cutover's bookmark redirect (D-032) forwards old
    bookmarks here until runbook I removes it."""

    with running(tmp_path, journey_config(_data(tmp_path))) as api:
        for route in ("/", "/journey", "/referee", "/practice", "/report", "/assets/app.js"):
            status, headers, body = api.call("GET", route, token=None)
            assert (status, headers["location"], body) == (308, "/view/", b"")
        assert api.call("HEAD", "/journey", token=None)[0] == 308
        assert api.json("POST", "/journey", token=None)[0] == 404


def test_transcript_route_serves_only_indexed_local_text(tmp_path: Path) -> None:
    transcripts = tmp_path / "stt-transcripts"
    transcripts.mkdir()
    (transcripts / "a.txt").write_text("fixture transcript text", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside transcript dir", encoding="utf-8")
    data = _data(tmp_path, transcripts=fake_transcripts(), lyrics=fake_lyrics())
    with running(tmp_path, journey_config(data, transcript_dir=transcripts)) as api:
        rows = api.call("GET", "/api/v1/recordings")[2].decode()
        assert "fixture transcript text" not in rows
        row = next(r for r in json.loads(rows)["rows"] if r["recordingId"] == "a")
        assert row["transcriptWordCount"] == 42
        text = JourneyTranscript.model_validate_json(
            api.call("GET", "/api/v1/recordings/a/transcript")[2]
        )
        assert text.text == "fixture transcript text"
        assert api.json("GET", "/api/v1/recordings/b/transcript")[0] == 404
        for climb in ("not-indexed", "..%2Fsecret", "..%2F..%2Fsecret.txt"):
            status, payload = api.json("GET", f"/api/v1/recordings/{climb}/transcript")
            assert (status, payload["error"]) == (404, "recording_not_found"), climb


def _one_recording(tmp_path: Path) -> tuple[Path, str]:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "20190101 000000-A.m4a").write_text("fixture-audio", encoding="utf-8")
    recording_key = "b74e7f7075c282e2"
    assert recording_id("20190101 000000-A.m4a") == recording_key
    index = {
        **fake_index(),
        "recordings": [
            recording(recording_key, "20190101 000000-A.m4a", "2019-01-01T00:00:00Z", 13)
        ],
    }
    results = {
        **fake_results(),
        "results": [
            result(
                recording_key,
                "20190101 000000-A.m4a",
                "2019",
                10,
                "clean_singing",
                0.9,
                "not_reviewed",
            )
        ],
    }
    write_manifests(tmp_path / "data", index=index, results=results)
    return corpus, recording_key


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through any mode")
def test_playback_maps_corpus_root_permission_denials_to_a_distinct_503(
    tmp_path: Path,
) -> None:
    corpus, recording_key = _one_recording(tmp_path)
    config = journey_config(
        tmp_path / "data", corpus_root=corpus, playback_approval="fixture approval"
    )
    with running(tmp_path, config) as api:
        corpus.chmod(0)
        try:
            status, payload = api.json("GET", f"/api/v1/recordings/{recording_key}/audio")
            assert status == 503 and payload["error"] == "corpus_access_denied"
        finally:
            corpus.chmod(0o755)


def test_serve_state_reload_swaps_fresh_manifests_into_the_running_server(
    tmp_path: Path,
) -> None:
    data = _data(tmp_path)
    with running(tmp_path, journey_config(data)) as api:
        assert len(api.get("/api/v1/recordings")["rows"]) == 7
        grown_index, grown_results = fake_index(), fake_results()
        grown_index["recordings"].append(
            recording("h", "20250101 000000-H.m4a", "2025-01-01T00:00:00Z", 8000)
        )
        grown_results["results"].append(
            result("h", "20250101 000000-H.m4a", "2025", 80, "clean_singing", 0.9, "not_reviewed")
        )
        write_manifests(data, index=grown_index, results=grown_results)
        after = api.get("/api/v1/recordings")
        assert len(after["rows"]) == 8
        assert any(row["recordingId"] == "h" for row in after["rows"])
        assert api.get("/api/v1/recordings/h")["row"]["recordingId"] == "h"


def _goal_features() -> dict[str, Any]:
    return {
        "results": [
            {
                "recordingId": f"t{day}",
                "capturedAt": f"2999-01-0{day}T00:00:00Z",
                "status": "completed",
                "features": {"vibrato": {"meanRateHz": rate}},
            }
            for day, rate in ((1, 5.6), (2, 5.8), (3, 5.7))
        ]
    }


def _serve_arguments(tmp_path: Path, *extra: str) -> list[str]:
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    lab = tmp_path / "lab"
    if not lab.exists():
        AssetLayout.initialize(lab)
    return [
        "serve",
        "--asset-root",
        str(lab),
        "--jwks",
        str(jwks_path),
        "--issuer",
        ISSUER,
        "--audience",
        "voice-lab-desktop",
        "--allowed-subject",
        SUBJECT,
        "--server-build",
        "test-build",
        "--port",
        "0",
        "--once",
        *extra,
    ]


def _started(stderr: str) -> tuple[str, dict[str, Any]]:
    lines = stderr.splitlines()
    endpoint = next(line for line in lines if line.startswith("api serving on "))
    summary = next(line for line in lines if line.startswith("api journey "))
    return endpoint.removeprefix("api serving on "), json.loads(
        summary.removeprefix("api journey ")
    )


def test_run_once_starts_and_closes_a_manifest_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VOICE_JOURNEY_DATA", str(_data(tmp_path)))
    monkeypatch.delenv("VOICE_JOURNEY_CONTAINER", raising=False)
    code = server_module.main(_serve_arguments(tmp_path, "--journey", "--host", "127.0.0.1"))
    assert code == 0
    endpoint, started = _started(capsys.readouterr().err)
    assert started["rows"] == 7 and started["playback"]["enabled"] is False
    host, _, port = endpoint.rpartition(":")
    assert host == "127.0.0.1" and port.isdigit()


def test_feature_detail_gates_on_feature_dir_and_serves_local_detail(tmp_path: Path) -> None:
    feature_dir = tmp_path / "voice-features"
    feature_dir.mkdir()
    (feature_dir / "a.json").write_text(
        json.dumps(
            {
                "recordingId": "a",
                "methodId": "voice-journey.feature-extract.v1",
                "summary": {"pitch": {"f0Hz": {"p50": 220}, "rangeSemitonesP05P95": 10}},
                "detail": {
                    "f0ContourVoiced50ms": [[0, 220.1], [0.05, 221.0]],
                    "vibratoSegments": [],
                },
            }
        ),
        encoding="utf-8",
    )
    data = _data(tmp_path)
    with running(tmp_path, journey_config(data)) as gated:
        status, payload = gated.json("GET", "/api/v1/recordings/a/features")
        assert status == 403 and payload["error"] == "feature_dir_not_configured"
        summary = JourneySummary.model_validate_json(gated.call("GET", "/api/v1/summary")[2])
        assert summary.feature_detail_enabled is False
    with running(tmp_path, journey_config(data, feature_dir=feature_dir)) as api:
        detail = JourneyFeatureDetail.model_validate_json(
            api.call("GET", "/api/v1/recordings/a/features")[2]
        )
        assert detail.filename == "20190101 000000-A.m4a" and detail.local_only is True
        assert isinstance(detail.detail, dict)
        assert len(detail.detail["f0ContourVoiced50ms"]) == 2
        status, payload = api.json("GET", "/api/v1/recordings/b/features")
        assert (status, payload["error"]) == (404, "feature_detail_missing")
        status, payload = api.json("GET", "/api/v1/recordings/zz/features")
        assert (status, payload["error"]) == (404, "recording_not_found")
        summary = JourneySummary.model_validate_json(api.call("GET", "/api/v1/summary")[2])
        assert summary.feature_detail_enabled is True


def _journeys() -> dict[str, Any]:
    return {
        "totals": {"clustersEligible": 1},
        "noteCore": {
            "takes": [
                {
                    "id": "a",
                    "year": "2019",
                    "quarter": "2019-Q1",
                    "centErrorMedian": 18.2,
                    "inTuneShare": 0.64,
                }
            ]
        },
        "journeys": [
            {
                "clusterId": "lyric-cluster-001",
                "label": "recurring_lyric_cluster_001",
                "takesUsed": 6,
                "firstYear": "2019",
                "lastYear": "2021",
                "perYear": {},
                "slopesPerYear": {"noteCoreCentError": -1.2, "cpps": 0.3},
            }
        ],
        "improvementIndex": [
            {
                "key": "noteCoreCentError",
                "label": "note-core tuning error (cents)",
                "direction": "down_good",
                "medianSlopePerYear": -1.2,
                "ci95": [-2.0, -0.4],
                "clustersUsed": 1,
                "verdict": "improving",
            }
        ],
    }


def _referee(tmp_path: Path, **changes: Any) -> Any:
    data = _data(tmp_path, lyrics=referee_lyrics(), features=fake_features())
    return journey_config(
        data,
        state_dir=tmp_path / "state",
        referee_seed=7,
        referee_pairs_per_cluster=4,
        **changes,
    )


# ---- paths.test.mjs (its two server cases) --------------------------------------------


def test_server_on_an_empty_data_root_starts_and_refuses_a_wide_bind_outside_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "empty-data"
    data.mkdir()
    monkeypatch.setenv("VOICE_JOURNEY_DATA", str(data))
    for name in ("VOICE_JOURNEY_CONTAINER", "VOICE_JOURNEY_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    assert server_module.main(_serve_arguments(tmp_path, "--journey")) == 0
    _, started = _started(capsys.readouterr().err)
    assert started["rows"] == 0
    assert started["statePath"] == str(data / "artifacts" / "corpus-browser-state.json")
    assert started["transcriptText"]["enabled"] is True
    wide = _serve_arguments(tmp_path, "--journey", "--host", "0.0.0.0")
    assert server_module.main(wide) == 1
    assert "loopback only" in capsys.readouterr().err
    monkeypatch.setenv("VOICE_JOURNEY_CONTAINER", "1")
    assert server_module.main(wide) == 0
    endpoint, _ = _started(capsys.readouterr().err)
    assert endpoint.startswith("0.0.0.0:")


def test_healthz_answers_without_identity(tmp_path: Path) -> None:
    with running(tmp_path, journey_config(_data(tmp_path))) as api:
        status, payload = api.json("GET", "/healthz", token=None)
        assert (status, payload) == (200, {"status": "ok"})


# ---- auth on every route ----------------------------------------------------------------

API_PATHS = [
    ("GET", "/api/v1/recordings"),
    ("GET", "/api/v1/recordings/a"),
    ("GET", "/api/v1/recordings/a/audio"),
    ("GET", "/api/v1/recordings/a/transcript"),
    ("GET", "/api/v1/recordings/a/features"),
    ("GET", "/api/v1/review/queue"),
    ("POST", "/api/v1/review/verdicts"),
    ("GET", "/api/v1/summary"),
    ("GET", "/api/v1/trends"),
    ("GET", "/api/v1/journeys"),
    ("GET", "/api/v1/coach"),
    ("GET", "/api/v1/goals"),
    ("POST", "/api/v1/goals"),
    ("POST", "/api/v1/goals/x/abandon"),
    ("GET", "/api/v1/referee/next"),
    ("POST", "/api/v1/referee/verdicts"),
    ("GET", "/api/v1/referee/results"),
    ("GET", "/api/v1/watch"),
]
#: voice-journey's own paths, served as aliases during phase 3 and removed in phase 4.
OLD_PATHS = [
    ("GET", "/api/rows"),
    ("GET", "/api/row/a"),
    ("GET", "/api/audio/a"),
    ("GET", "/api/transcript/a"),
    ("GET", "/api/feature-detail/a"),
    ("GET", "/api/review-queue"),
    ("POST", "/api/verdicts"),
    ("GET", "/api/summary"),
    ("GET", "/api/trends"),
    ("GET", "/api/journeys"),
    ("GET", "/api/coach"),
    ("GET", "/api/goals"),
    ("POST", "/api/goals"),
    ("POST", "/api/goals/abandon"),
    ("GET", "/api/referee/next"),
    ("POST", "/api/referee/verdicts"),
    ("GET", "/api/referee/results"),
    ("GET", "/api/watch/status"),
    ("POST", "/api/intake/sessions"),
    ("GET", "/api/intake/sessions/s-20261003-101500"),
    ("PUT", "/api/intake/sessions/s-20261003-101500/files/01-room-silence.wav"),
    ("POST", "/api/intake/sessions/s-20261003-101500/finalize"),
]


def test_every_journey_route_needs_a_verified_bearer(tmp_path: Path) -> None:
    config = journey_config(_data(tmp_path), state_dir=tmp_path / "state")
    expired = mint({"exp": 1})
    stranger = mint({"sub": "someone-else"})
    with running(tmp_path, config) as api:
        for method, path in API_PATHS:
            for token in (None, expired, stranger, "not.a.jwt"):
                status, payload = api.json(method, path, {"recordingId": "b"}, token=token)
                assert (status, payload["error"]) == (401, "unauthorized"), (method, path)
            forwarded = {"X-Forwarded-User": SUBJECT, "X-Forwarded-Email": "cole@example.invalid"}
            assert api.json(method, path, headers=forwarded, token=None)[0] == 401
    assert not config.state_dir.exists()


def test_voice_journeys_old_paths_are_gone(tmp_path: Path) -> None:
    """Phase 4 removed the phase-3 aliases and the compatibility intake: with a valid bearer
    each old path is the server's 404; without one it is still 401 (the bearer check comes
    before any route), and nothing is written."""

    config = journey_config(_data(tmp_path), state_dir=tmp_path / "state")
    with running(tmp_path, config) as api:
        for method, path in OLD_PATHS:
            status, payload = api.json(method, path, {"recordingId": "b"})
            assert (status, payload["error"]) == (404, "not_found"), (method, path)
            assert api.json(method, path, token=None)[0] == 401, (method, path)
    assert not config.state_dir.exists()


def test_without_journey_the_routes_answer_503(tmp_path: Path) -> None:
    with running(tmp_path, None, enabled=False) as api:
        for method, path in API_PATHS:
            status, payload = api.json(method, path)
            assert (status, payload["error"]) == (503, "journey_unavailable"), (method, path)
        assert api.call("GET", "/referee", token=None)[0] == 308
        assert api.json("GET", "/api/v1/meta")[0] == 200
        assert api.json("GET", "/api/v1/nowhere")[1]["error"] == "not_found"
        assert api.json("GET", "/nowhere", token=None)[1]["error"] == "not_found"


# ---- /api/v1 --------------------------------------------------------------------------


def test_v1_recordings_are_paged_projected_and_typed(tmp_path: Path) -> None:
    with running(tmp_path, journey_config(_data(tmp_path))) as api:
        status, _, body = api.call("GET", "/api/v1/recordings?bucket=uncertain_manual_review")
        assert status == 200
        page = JourneyRecordings.model_validate_json(body)
        assert page.page.total_filtered == 2 and page.page.limit == 100
        assert [row["recordingId"] for row in page.rows] == ["b", "d"]
        assert page.totals.rows == 2 and page.totals.years == {"2020": 1, "2021": 1}
        lean = JourneyRecordings.model_validate_json(
            api.call("GET", "/api/v1/recordings?limit=2&offset=1&fields=list")[2]
        )
        assert [row["recordingId"] for row in lean.rows] == ["b", "c"]
        assert "rationale" not in lean.rows[0]
        status, payload = api.json("GET", "/api/v1/recordings?fields=all")
        assert (status, payload["error"]) == (400, "unsupported_fields_projection")
        one = JourneyRecording.model_validate_json(api.call("GET", "/api/v1/recordings/d")[2])
        assert one.row["rationale"] == "uncertain_manual_review rationale"
        for path in ("/api/v1/recordings/zz", "/api/v1/recordings/%ff", "/api/v1/recordings/%zz"):
            status, payload = api.json("GET", path)
            assert (status, payload["error"]) == (404, "recording_not_found"), path
        assert api.json("POST", "/api/v1/recordings")[1]["error"] == "method_not_allowed"
        assert api.json("GET", "/api/v1/recordings/a/other")[1]["error"] == "not_found"


def test_v1_audio_honours_range_and_refuses_with_stable_codes(tmp_path: Path) -> None:
    corpus, recording_key = _one_recording(tmp_path)
    path = f"/api/v1/recordings/{recording_key}/audio"
    with running(tmp_path, journey_config(tmp_path / "data", corpus_root=corpus)) as gated:
        status, payload = gated.json("GET", path)
        assert (status, payload["error"]) == (403, "playback_not_approved")
    config = journey_config(tmp_path / "data", corpus_root=corpus, playback_approval="approved")
    with running(tmp_path, config) as api:
        status, headers, body = api.call("GET", path)
        assert (status, body, headers["accept-ranges"]) == (200, b"fixture-audio", "bytes")
        assert headers["content-type"] == "audio/mp4"
        status, headers, body = api.call("GET", path, headers={"Range": "bytes=8-12"})
        assert (status, body) == (206, b"audio")
        assert headers["content-range"] == "bytes 8-12/13"
        status, _, body = api.call("GET", path, headers={"Range": "bytes=20-30"})
        assert (status, body) == (200, b"fixture-audio")
        status, _, body = api.call("GET", path, headers={"Range": "bytes=-2"})
        assert (status, body) == (206, b"fix")
        (corpus / "20190101 000000-A.m4a").unlink()
        assert api.json("GET", path)[1]["error"] == "audio_not_found"
        assert api.json("GET", "/api/v1/recordings/zz/audio")[1]["error"] == "recording_not_found"


def test_v1_audio_refuses_a_row_that_does_not_name_its_file(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "other.m4a").write_bytes(b"x")
    index = {"recordings": [recording("a", "other.m4a", "2019-01-01T00:00:00Z", 1)]}
    write_manifests(tmp_path / "data", index=index, results={"results": []})
    config = journey_config(tmp_path / "data", corpus_root=corpus, playback_approval="approved")
    with running(tmp_path, config) as api:
        status, payload = api.json("GET", "/api/v1/recordings/a/audio")
        assert (status, payload["error"]) == (409, "seam_handle_mismatch")


def test_v1_transcript_and_features_are_gated_and_typed(tmp_path: Path) -> None:
    transcripts, features = tmp_path / "stt", tmp_path / "features"
    transcripts.mkdir()
    features.mkdir()
    (transcripts / "a.txt").write_text("words", encoding="utf-8")
    (features / "a.json").write_text('{"methodId": "m", "summary": {"x": 1}}', encoding="utf-8")
    data = _data(tmp_path, transcripts=fake_transcripts())
    with running(tmp_path, journey_config(data)) as gated:
        for part, code in (
            ("transcript", "transcript_dir_not_configured"),
            ("features", "feature_dir_not_configured"),
        ):
            status, payload = gated.json("GET", f"/api/v1/recordings/a/{part}")
            assert (status, payload["error"]) == (403, code)
    config = journey_config(data, transcript_dir=transcripts, feature_dir=features)
    with running(tmp_path, config) as api:
        text = JourneyTranscript.model_validate_json(
            api.call("GET", "/api/v1/recordings/a/transcript")[2]
        )
        assert (text.text, text.word_count, text.language) == ("words", 42, "en")
        detail = JourneyFeatureDetail.model_validate_json(
            api.call("GET", "/api/v1/recordings/a/features")[2]
        )
        assert (detail.method_id, detail.summary, detail.detail) == ("m", {"x": 1}, None)
        for path, status, code in (
            ("/api/v1/recordings/b/transcript", 404, "transcript_text_missing"),
            ("/api/v1/recordings/d/transcript", 404, "transcript_not_available"),
            ("/api/v1/recordings/b/features", 404, "feature_detail_missing"),
        ):
            answer = api.json("GET", path)
            assert (answer[0], answer[1]["error"]) == (status, code), path
        assert all(code in REFUSAL_CODES for code in ("transcript_text_missing",))


def test_v1_review_queue_and_verdicts(tmp_path: Path) -> None:
    data = _data(tmp_path)
    config = journey_config(data, state_dir=tmp_path / "state")
    manifests = {path: path.read_bytes() for path in (data / "manifests").iterdir()}
    with running(tmp_path, config) as api:
        queue = JourneyReviewQueue.model_validate_json(api.call("GET", "/api/v1/review/queue")[2])
        assert len(queue.rows) == 5
        assert [row["reviewQueueOrder"] for row in queue.rows] == [1, 2, 3, 4, 5]
        body = {
            "recording_id": "d",
            "verdict": "non_singing",
            "reviewed_at": "2026-10-09T00:00:00Z",
        }
        status, _, raw = api.call("POST", "/api/v1/review/verdicts", json.dumps(body).encode())
        assert status == 200
        answer = ReviewVerdictAnswer.model_validate_json(raw)
        assert answer.verdict.reviewed_by == "operator" and answer.totals.reviewed == 2
        assert read_json(config.state)["verdicts"][0]["reviewedAt"] == "2026-10-09T00:00:00Z"
        reviewed = api.get("/api/v1/recordings?spotCheckStatus=reviewed")
        assert any(row["recordingId"] == "d" for row in reviewed["rows"])
        unknown = {"recording_id": "zz", "verdict": "non_singing"}
        assert api.json("POST", "/api/v1/review/verdicts", unknown)[1]["error"] == (
            "recording_not_found"
        )
        for bad in ({"recording_id": "d", "verdict": "nope"}, {"recordingId": "d"}):
            status, payload = api.json("POST", "/api/v1/review/verdicts", bad)
            assert (status, payload["error"]) == (422, "invalid_request")
        big = b'{"recording_id": "' + b"x" * (65 * 1024) + b'"}'
        status, _, raw = api.call("POST", "/api/v1/review/verdicts", big)
        assert (status, json.loads(raw)["error"]) == (413, "body_too_large")
    # A verdict is local state only: the manifests the pipeline wrote are untouched.
    assert {path: path.read_bytes() for path in (data / "manifests").iterdir()} == manifests


def test_v1_summary_documents_and_watch(tmp_path: Path) -> None:
    config = journey_config(
        _data(tmp_path, features=fake_features(), journeys=_journeys()),
        state_dir=tmp_path / "state",
    )
    with running(tmp_path, config) as api:
        summary = JourneySummary.model_validate_json(api.call("GET", "/api/v1/summary")[2])
        assert summary.review_queue.selected_count == 5 and summary.playback_enabled is False
        assert summary.filter_options.years[0] == "2019"
        documents = {}
        for kind in ("trends", "journeys", "coach", "goals"):
            document = JourneyDocument.model_validate_json(api.call("GET", f"/api/v1/{kind}")[2])
            assert (document.kind, document.available) == (kind, True)
            documents[kind] = document.document
        assert len(documents["trends"]["quarters"]) >= 2
        assert documents["trends"]["readScope"]["featureArrays"] is False
        assert documents["journeys"]["improvementIndex"][0]["verdict"] == "improving"
        coach = documents["coach"]
        assert coach["dueSongs"] == [] and isinstance(coach["frontier"], list)
        assert coach["readScope"]["transcriptText"] is False
        assert coach["readScope"]["audioBytes"] is False
        assert coach["params"]["dueAfterDays"] == 90
        watch = WatchStatus.model_validate_json(api.call("GET", "/api/v1/watch")[2])
        assert (watch.enabled, watch.reason) == (False, "watch_not_enabled")
        config.state_dir.mkdir(exist_ok=True)
        config.watch_state.write_text(json.dumps({"version": WATCH_STATE_VERSION}), "utf-8")
        watch = WatchStatus.model_validate_json(api.call("GET", "/api/v1/watch")[2])
        assert watch.enabled is True and watch.state is not None
        assert watch.state["source"] == "job_runner_state_file"
    with running(tmp_path / "bare", journey_config(_data(tmp_path / "bare"))) as bare:
        for kind in ("trends", "journeys"):
            document = JourneyDocument.model_validate_json(bare.call("GET", f"/api/v1/{kind}")[2])
            assert document.available is False


def test_v1_goals_create_list_and_abandon(tmp_path: Path) -> None:
    config = journey_config(_data(tmp_path, features=_goal_features()), state_dir=tmp_path / "s")
    with running(tmp_path, config) as api:
        request = {
            "metric": "vibratoRateHz",
            "direction": "at_least",
            "target": 5.5,
            "takes_required": 3,
        }
        status, _, raw = api.call("POST", "/api/v1/goals", json.dumps(request).encode())
        assert status == 201
        goal = GoalCreated.model_validate_json(raw).goal
        assert goal["takesRequired"] == 3 and goal["title"].startswith("vibrato rate")
        listed = JourneyDocument.model_validate_json(api.call("GET", "/api/v1/goals")[2])
        assert listed.document["goals"][0]["derivedStatus"] == "achieved"
        assert listed.document["goals"][0]["progress"]["takesCounted"] == 3
        assert listed.document["goals"][0]["progress"]["runningMedian"] == 5.7
        status, _, raw = api.call("POST", f"/api/v1/goals/{goal['goalId']}/abandon")
        assert status == 200
        assert GoalAbandoned.model_validate_json(raw).goal_id == goal["goalId"]
        after = JourneyDocument.model_validate_json(api.call("GET", "/api/v1/goals")[2])
        assert after.document["goals"][0]["derivedStatus"] == "abandoned"
        assert after.document["goals"][0]["progress"] is None
        for path in ("/api/v1/goals/missing/abandon", "/api/v1/goals/%zz/abandon"):
            assert api.json("POST", path)[1]["error"] == "goal_not_found"
        status, payload = api.json("POST", "/api/v1/goals", {**request, "takes_required": 2})
        assert (status, payload["error"]) == (422, "invalid_request")
        assert api.json("GET", "/api/v1/goals/x/abandon")[1]["error"] == "method_not_allowed"
        status, payload = api.json("POST", "/api/v1/goals", {**request, "title": " "})
        assert status == 201


def test_v1_goal_refused_by_the_goal_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cole_voice_lab.journey import browser
    from cole_voice_lab.journey.goals import GoalError

    def refuse(body: Any) -> Any:
        raise GoalError("invalid_target: expected a finite number")

    monkeypatch.setattr(browser, "validate_goal_spec", refuse)
    with running(tmp_path, journey_config(_data(tmp_path))) as api:
        request = {"metric": "cpps", "direction": "at_most", "target": 1}
        status, payload = api.json("POST", "/api/v1/goals", request)
        assert (status, payload["error"]) == (400, "invalid_goal")


def test_v1_referee_needs_the_playback_approval(tmp_path: Path) -> None:
    with running(tmp_path, _referee(tmp_path)) as gated:
        for method, path in (("GET", "/api/v1/referee/next"), ("POST", "/api/v1/referee/verdicts")):
            status, payload = gated.json(method, path, {"pair_id": "x", "choice": "a"})
            assert (status, payload["error"]) == (403, "playback_not_approved")
        results = RefereeResults.model_validate_json(
            gated.call("GET", "/api/v1/referee/results")[2]
        )
        assert results.enabled is False and results.pool_size > 0
    config = _referee(tmp_path, corpus_root=tmp_path / "corpus", playback_approval="fixture")
    with running(tmp_path, config) as api:
        raw_next = api.call("GET", "/api/v1/referee/next")[2]
        following = RefereeNext.model_validate_json(raw_next)
        assert following.pair is not None and following.judged == 0
        # The trial is blind: nothing in it names a year or a cluster.
        trial = json.dumps(json.loads(raw_next)["pair"])
        assert "year" not in trial.lower() and "cluster" not in trial.lower()
        assert following.pair.a.audio_url.startswith("/api/v1/recordings/")
        assert following.pair.a.audio_url.endswith("/audio")
        body = {"pair_id": following.pair.pair_id, "choice": "too_close"}
        status, _, raw = api.call("POST", "/api/v1/referee/verdicts", json.dumps(body).encode())
        assert status == 200
        answer = RefereeVerdictAnswer.model_validate_json(raw)
        assert answer.judged == 1 and answer.reveal.cluster_id is not None
        state = read_json(config.referee_state)
        assert [judgment["choice"] for judgment in state["judgments"]] == ["too_close"]
        after = RefereeNext.model_validate_json(api.call("GET", "/api/v1/referee/next")[2])
        assert after.pair is None or after.pair.pair_id != following.pair.pair_id
        status, payload = api.json("POST", "/api/v1/referee/verdicts", {**body, "choice": "x"})
        assert (status, payload["error"]) == (422, "invalid_request")
        status, payload = api.json(
            "POST", "/api/v1/referee/verdicts", {**body, "pair_id": "f" * 16}
        )
        assert (status, payload["error"]) == (404, "pair_not_found")
        fit = RefereeResults.model_validate_json(api.call("GET", "/api/v1/referee/results")[2])
        assert fit.fit["byChoice"]["too_close"] == 1
        for pair in api.store.current().referee_pool if api.store else []:
            api.store.record_judgment(pair, "skip", None)
        done = RefereeNext.model_validate_json(api.call("GET", "/api/v1/referee/next")[2])
        assert done.pair is None


# ---- the server's remaining branches --------------------------------------------------


def test_v1_body_needs_a_content_length(tmp_path: Path) -> None:
    with running(tmp_path, journey_config(_data(tmp_path))) as api:
        request = (
            "POST /api/v1/review/verdicts HTTP/1.1\r\nHost: x\r\n"
            f"Authorization: Bearer {mint()}\r\nConnection: close\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", api.port), timeout=10) as connection:
            connection.sendall(request.encode())
            reply = b""
            while chunk := connection.recv(65536):
                reply += chunk
        head, _, body = reply.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.0 411") or head.startswith(b"HTTP/1.1 411")
        assert json.loads(body)["error"] == "content_length_required"


def test_head_and_a_client_that_leaves_mid_answer(tmp_path: Path) -> None:
    corpus, recording_key = _one_recording(tmp_path)
    config = journey_config(tmp_path / "data", corpus_root=corpus, playback_approval="approved")
    with running(tmp_path, config) as api:
        assert api.call("HEAD", "/api/v1/summary")[0] == 405
        status, headers, body = api.call("HEAD", "/")
        assert (status, body) == (308, b"")
        audio = corpus / "20190101 000000-A.m4a"
        audio.write_bytes(b"x" * (3 * 1024 * 1024))
        with socket.create_connection(("127.0.0.1", api.port), timeout=10) as connection:
            connection.sendall(
                (
                    f"GET /api/v1/recordings/{recording_key}/audio HTTP/1.1\r\nHost: x\r\n"
                    f"Authorization: Bearer {mint()}\r\n\r\n"
                ).encode()
            )
            connection.recv(1024)
        assert api.json("GET", "/api/v1/summary")[0] == 200


def test_serve_reports_a_missing_manifest_without_a_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("VOICE_JOURNEY_DATA", raising=False)
    monkeypatch.chdir(tmp_path)
    assert server_module.main(_serve_arguments(tmp_path, "--journey")) == 1
    assert capsys.readouterr().err.startswith("error: ")
    assert server_module.main(_serve_arguments(tmp_path)) == 0
    assert "api journey" not in capsys.readouterr().err
    with pytest.raises(SystemExit):
        server_module.main(_serve_arguments(tmp_path, "--journey", "--queue-size", "0"))


def test_local_files_of_an_id_that_escapes_its_directory_are_refused(tmp_path: Path) -> None:
    index = {"recordings": [recording("../x", "x.m4a", "2019-01-01T00:00:00Z", 1)]}
    transcripts = {
        "results": [
            {
                "recordingId": "../x",
                "status": "completed",
                "localArtifacts": {"transcriptTextPath": "t", "textIncludedInManifest": False},
            }
        ]
    }
    write_manifests(
        tmp_path / "data", index=index, results={"results": []}, transcripts=transcripts
    )
    config = journey_config(
        tmp_path / "data", transcript_dir=tmp_path / "t", feature_dir=tmp_path / "f"
    )
    with running(tmp_path, config) as api:
        status, payload = api.json("GET", "/api/v1/recordings/..%2Fx/transcript")
        assert (status, payload["error"]) == (400, "invalid_transcript_path")
        status, payload = api.json("GET", "/api/v1/recordings/..%2Fx/features")
        assert (status, payload["error"]) == (400, "invalid_feature_detail_path")


def test_serve_takes_the_journey_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VOICE_JOURNEY_DATA", str(_data(tmp_path)))
    monkeypatch.setenv("VOICE_JOURNEY_STATE_DIR", str(tmp_path / "state"))
    arguments = _serve_arguments(
        tmp_path,
        "--journey",
        "--queue-size",
        "3",
        "--referee-seed",
        "9",
        "--playback-approval",
        "Cole approved playback on the PC, 2026-10-09",
        "--transcript-dir",
        str(tmp_path / "t"),
        "--feature-dir",
        str(tmp_path / "f"),
    )
    assert server_module.main(arguments) == 0
    _, started = _started(capsys.readouterr().err)
    assert started["reviewQueue"]["targetSize"] == 3
    assert started["playback"]["enabled"] is True
    assert started["statePath"] == str(tmp_path / "state" / "corpus-browser-state.json")
    with pytest.raises(SystemExit):
        server_module.main(_serve_arguments(tmp_path, "--journey", "--referee-seed", "1.5"))


def _raw_exchange(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(request)
        reply = b""
        while chunk := connection.recv(65536):
            reply += chunk
    return reply


def test_an_oversized_body_is_answered_and_the_connection_closed(tmp_path: Path) -> None:
    """More unread body than the server will drain: it answers, then closes the connection
    (the reply is read to its end, so a connection left open would time the test out)."""

    huge = "Content-Length: 100000000\r\n\r\n"
    with running(tmp_path, journey_config(_data(tmp_path))) as api:
        # A route that refuses the body ...
        reply = _raw_exchange(
            api.port,
            (
                "POST /api/v1/review/verdicts HTTP/1.1\r\nHost: x\r\n"
                f"Authorization: Bearer {mint()}\r\n{huge}"
            ).encode(),
        )
        head, _, body = reply.partition(b"\r\n\r\n")
        assert b" 413 " in head.split(b"\r\n")[0]
        assert json.loads(body)["error"] == "body_too_large"
        # ... and one that answers without reading it.
        reply = _raw_exchange(
            api.port,
            (
                "GET /api/v1/summary HTTP/1.1\r\nHost: x\r\n"
                f"Authorization: Bearer {mint()}\r\n{huge}"
            ).encode(),
        )
        head, _, body = reply.partition(b"\r\n\r\n")
        assert b" 200 " in head.split(b"\r\n")[0]
        JourneySummary.model_validate_json(body)


def test_a_file_that_shrinks_mid_answer_closes_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cole_voice_lab.api import journey_routes
    from cole_voice_lab.journey.browser import AudioHandle

    corpus, recording_key = _one_recording(tmp_path)

    def short(row: Any, **_: Any) -> AudioHandle:
        handle = (corpus / "20190101 000000-A.m4a").open("rb")
        return AudioHandle(handle, 100, "audio/mp4")

    monkeypatch.setattr(journey_routes, "open_recording_audio", short)
    config = journey_config(tmp_path / "data", corpus_root=corpus, playback_approval="approved")
    with running(tmp_path, config) as api:
        reply = _raw_exchange(
            api.port,
            (
                f"GET /api/v1/recordings/{recording_key}/audio HTTP/1.1\r\nHost: x\r\n"
                f"Authorization: Bearer {mint()}\r\n\r\n"
            ).encode(),
        )
        head, _, body = reply.partition(b"\r\n\r\n")
        assert b"Content-Length: 100" in head and body == b"fixture-audio"
