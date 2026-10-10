"""The read-only web view (``/view/``): every page from a synthetic data root, escaping,
the no-audio rule (D-025), the door's identity and bearer checks, the headers, GET only,
and 503 without the journey routes."""

from __future__ import annotations

import http.client
import json
import re
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from home_host_fixtures import SUBJECT, jwks, mint, policy
from journey_browser_fixtures import (
    fake_features,
    fake_index,
    fake_lyrics,
    fake_results,
    fake_state,
    fake_transcripts,
    journey_config,
    recording,
    result,
    write_manifests,
)

from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.journey_routes import JourneyRoutes
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.journey.browser import JourneyConfig, JourneyStore
from cole_voice_lab.journey.jsonio import write_json
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.view import charts, pages, render
from cole_voice_lab.view.routes import CONTENT_SECURITY_POLICY, ViewRoutes

Json = dict[str, Any]
HOSTILE = "<script>alert(1)</script>"
TRANSCRIPT_TEXT = "the secret transcript line nobody may read"
LYRIC_TEXT = "a secret lyric line that never leaves"
PAGES = (
    "/view/",
    "/view/recordings",
    "/view/journeys",
    "/view/trends",
    "/view/goals",
    "/view/report",
)
FORWARDED = {"X-Forwarded-User": SUBJECT, "X-Forwarded-Email": "cole@example.invalid"}


# ---- a synthetic data root ---------------------------------------------------------------


def journeys_manifest(label: str = "recurring_lyric_cluster_001") -> Json:
    return {
        "schemaVersion": "voice-journey.song-journeys.v1",
        "totals": {"reliableTakes": 24, "takesWithNoteCore": 24, "clustersEligible": 2},
        "method": {"bootstrap": {"resamples": 1000, "seed": 2026}},
        "noteCore": {"takes": [{"id": "y19-0", "year": "2019", "centErrorMedian": 20.1}]},
        "journeys": [
            {
                "clusterId": "lyric-cluster-001",
                "label": label,
                "takesUsed": 12,
                "firstYear": "2019",
                "lastYear": "2021",
                "perYear": {
                    "2019": {"n": 6, "noteCoreCentError": 20.1},
                    "2021": {"n": 6, "noteCoreCentError": 17.4},
                },
                "slopesPerYear": {"noteCoreCentError": -1.3, "cpps": 0.2},
            },
            {
                "clusterId": "lyric-cluster-002",
                "takesUsed": 30,
                "firstYear": "2020",
                "lastYear": "2022",
                "perYear": {},
                "slopesPerYear": {},
            },
        ],
        "improvementIndex": [
            {
                "key": "noteCoreCentError",
                "label": label,
                "medianSlopePerYear": -1.3,
                "ci95": [-2.1, -0.5],
                "clustersUsed": 1,
                "verdict": "improving",
            },
            {
                "key": "cpps",
                "label": "voice clarity (CPPS dB)",
                "medianSlopePerYear": None,
                "ci95": None,
                "clustersUsed": 0,
                "verdict": "insufficient_data",
            },
        ],
    }


def hostile_lyrics() -> Json:
    lyrics = fake_lyrics()
    lyrics["clusters"][0]["label"] = HOSTILE
    lyrics["clusters"][0]["lyricText"] = LYRIC_TEXT
    for entry in lyrics["results"]:
        entry["lyricMatch"]["matchedClusterLabel"] = (
            HOSTILE if entry["lyricMatch"]["marked"] else None
        )
        entry["lyricMatch"]["text"] = LYRIC_TEXT
    return lyrics


def hostile_transcripts() -> Json:
    transcripts = fake_transcripts()
    for entry in transcripts["results"]:
        entry["text"] = TRANSCRIPT_TEXT
    return transcripts


def data_root(tmp_path: Path, *, hostile: bool = True, **manifests: Any) -> Path:
    data = tmp_path / "data"
    results = fake_results()
    if hostile:
        results["results"][0]["classification"]["finalBucket"] = HOSTILE
        results["results"][0]["classification"]["rationale"] = TRANSCRIPT_TEXT
    defaults: Json = {
        "index": fake_index(),
        "results": results,
        "transcripts": hostile_transcripts(),
        "lyrics": hostile_lyrics() if hostile else fake_lyrics(),
        "features": fake_features(),
        "journeys": journeys_manifest(HOSTILE if hostile else "recurring_lyric_cluster_001"),
    }
    write_manifests(data, **{**defaults, **manifests})
    write_json(data / "artifacts" / "corpus-browser-state.json", fake_state(), announce=False)
    texts = data / "artifacts" / "stt-transcripts"
    texts.mkdir(parents=True, exist_ok=True)
    for recording_id in "abcdefg":
        (texts / f"{recording_id}.txt").write_text(TRANSCRIPT_TEXT, encoding="utf-8")
    return data


def view_config(data: Path, **changes: Any) -> JourneyConfig:
    return journey_config(
        data,
        transcript_dir=data / "artifacts" / "stt-transcripts",
        feature_dir=data / "artifacts" / "voice-features",
        playback_approval="approved for the test",
        **changes,
    )


@dataclass
class Running:
    server: ApiServer
    store: JourneyStore | None
    view: ViewRoutes

    def call(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, dict[str, str], str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        sent = dict(FORWARDED if headers is None else headers)
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        connection.request(method, path, headers=sent)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        answer = {name.lower(): value for name, value in response.getheaders()}
        connection.close()
        return response.status, answer, body

    def page(self, path: str) -> str:
        status, headers, body = self.call("GET", path)
        assert status == 200, (path, status, body)
        assert headers["content-type"] == "text/html; charset=utf-8"
        return body


@contextmanager
def running(tmp_path: Path, config: JourneyConfig | None) -> Iterator[Running]:
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    lab = tmp_path / "lab"
    layout = AssetLayout.validate(lab) if lab.exists() else AssetLayout.initialize(lab)
    store = JourneyStore(config, log=lambda message: None) if config is not None else None
    verifier = TokenVerifier(policy(jwks_path))
    view = ViewRoutes(store, verifier, log=lambda message: None)
    server = ApiServer(
        ("127.0.0.1", 0),
        staging=IntakeStaging(layout, min_free_bytes=0),
        verifier=verifier,
        meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
        log=False,
        routes=(JourneyRoutes(store), view),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Running(server, store, view)
    finally:
        server.shutdown()
        server.server_close()


def add_goals(store: JourneyStore) -> None:
    store.create_goal({"metric": "cpps", "direction": "at_least", "target": 10, "takesRequired": 3})
    hostile = store.create_goal(
        {
            "metric": "cpps",
            "direction": "at_most",
            "target": 9,
            "takesRequired": 4,
            "title": HOSTILE,
        }
    )
    store.abandon_goal(hostile["goalId"])


# ---- every page ----------------------------------------------------------------------------


def test_every_page_renders_from_a_synthetic_data_root(tmp_path: Path) -> None:
    with running(tmp_path, view_config(data_root(tmp_path, hostile=False))) as api:
        assert api.store is not None
        add_goals(api.store)
        summary = api.page("/view/")
        assert '7</div><div class="l">recordings' in summary
        assert "2019–2024" in summary and "working top" in summary
        assert 'href="/view/recordings?bucket=clean_singing"' in summary
        recordings = api.page("/view/recordings")
        assert recordings.count("<tr>") == 8
        assert "2024-01-01" in recordings and "1:10" in recordings
        assert "recurring_lyric_cluster_001" in recordings and "lyric match" in recordings
        assert "reviewed: clean singing" in recordings and "wordless" in recordings
        assert "review queue" in recordings
        journeys = api.page("/view/journeys")
        assert "Improvement index" in journeys and "chip improving" in journeys
        assert "20.1 → 17.4" in journeys and "−" not in journeys
        songs = journeys.split("<h2>Songs</h2>")[1]
        assert songs.index("lyric-cluster-002") < songs.index("recurring_lyric_cluster_001")
        trends = api.page("/view/trends")
        assert "Pitch range by quarter" in trends and trends.count("<svg") >= 4
        assert "By quarter" in trends and "Working top by year" in trends
        goals = api.page("/view/goals")
        assert "chip abandoned" in goals and "≥ 10" in goals and "≤ 9" in goals
        assert "0 / 3" in goals and "chip active" in goals
        evidence = api.page("/view/report")
        assert "Did I get better" in evidence and 'class="report"' in evidence
        assert "Rendered " in evidence and "listening judgments" in evidence
        for path in PAGES:
            page = api.page(path)
            assert f'href="{path}" aria-current="page"' in page or path == "/view/"
            assert page.startswith("<!doctype html>")


def test_recordings_are_paged_and_filtered(tmp_path: Path) -> None:
    index = fake_index()
    results = fake_results()
    for number in range(120):
        identifier = f"r{number:03d}"
        captured = f"2018-{(number % 12) + 1:02d}-{(number % 27) + 1:02d}T00:00:00Z"
        index["recordings"].append(recording(identifier, f"{identifier}.m4a", captured, 10))
        results["results"].append(
            result(identifier, f"{identifier}.m4a", "2018", 3700 + number, "non_singing", 0.9, "x")
        )
    data = data_root(tmp_path, hostile=False, index=index, results=results)
    with running(tmp_path, view_config(data)) as api:
        first = api.page("/view/recordings")
        assert "127 recordings" in first and "page 1 of 3" in first
        assert first.count("<tr>") == 51
        assert 'href="/view/recordings?page=2">older →' in first and "← newer" not in first
        assert "1:01:40" in first and "1:03:39" in api.page("/view/recordings?page=3")
        last = api.page("/view/recordings?page=99")
        assert "page 3 of 3" in last and "older →" not in last
        assert 'href="/view/recordings?page=2">← newer' in last
        assert "page 1 of 3" in api.page("/view/recordings?page=nope")
        filtered = api.page("/view/recordings?bucket=non_singing&year=2018")
        assert "120 recordings · bucket not singing · year 2018" in filtered
        assert 'href="/view/recordings?bucket=non_singing&amp;year=2018&amp;page=2"' in filtered
        assert 'href="/view/recordings">all</a>' in filtered
        empty = api.page("/view/recordings?bucket=nothing")
        assert "0 recordings" in empty and "page 1 of 1" in empty


def test_pages_say_when_the_pipeline_has_not_run(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_manifests(data, index={"recordings": []}, results={"results": []})
    with running(tmp_path, view_config(data)) as api:
        assert "No recordings are indexed yet" in api.page("/view/")
        assert "Not computed yet (song_journeys_manifest_not_loaded)" in api.page("/view/journeys")
        assert "Not computed yet (features_manifest_not_loaded)" in api.page("/view/trends")
        assert "No goals yet" in api.page("/view/goals")
        assert "No judgments yet" in api.page("/view/report")
        (data / "manifests" / "full-corpus-filter-results.json").unlink()
        assert "needs the filter results manifest" in api.page("/view/report")


def test_trends_and_journeys_without_enough_data(tmp_path: Path) -> None:
    features = fake_features()
    features["results"] = features["results"][:1]
    journeys = journeys_manifest()
    journeys["improvementIndex"] = []
    journeys["journeys"] = []
    data = data_root(tmp_path, hostile=False, features=features, journeys=journeys)
    with running(tmp_path, view_config(data)) as api:
        trends = api.page("/view/trends")
        assert "Recordings per month" in trends and "Working top by year" not in trends
        journeys_page = api.page("/view/journeys")
        assert "No dimension has enough songs yet" in journeys_page
        assert "No song has enough takes yet" in journeys_page


def test_the_report_is_rendered_live_and_cached(tmp_path: Path) -> None:
    data = data_root(tmp_path, hostile=False)
    config = view_config(data)
    cache = pages.ReportCache()
    first = cache.sections(config)
    assert cache.sections(config) is first
    write_json(
        config.referee_state,
        {"schemaVersion": "voice-journey.referee-state.v1", "judgments": []},
        announce=False,
    )
    second = cache.sections(config)
    assert second is not first and second.sources.endswith("0 listening judgments")


# ---- what the view never shows -----------------------------------------------------------


def test_values_are_escaped(tmp_path: Path) -> None:
    with running(tmp_path, view_config(data_root(tmp_path))) as api:
        assert api.store is not None
        add_goals(api.store)
        for path in (*PAGES, "/view/recordings?bucket=%3Cscript%3E", "/view/nowhere"):
            status, _, body = api.call("GET", path)
            assert "<script" not in body.lower(), path
        escaped = "&lt;script&gt;alert(1)&lt;/script&gt;"
        assert escaped in api.page("/view/recordings")
        assert escaped in api.page("/view/journeys")
        assert escaped in api.page("/view/goals")
        assert escaped in api.page("/view/trends")
        assert escaped in api.page("/view/")


_OPENED: list[str] = []
_WATCHING: list[bool] = []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _WATCHING and event == "open" and isinstance(args[0], str | Path):
        _OPENED.append(str(args[0]))


sys.addaudithook(_audit)


def test_no_page_serves_or_links_audio_transcripts_or_lyrics(tmp_path: Path) -> None:
    data = data_root(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "20190101 000000-A.m4a").write_bytes(b"not really audio")
    config = view_config(data, corpus_root=corpus)
    with running(tmp_path, config) as api:
        assert api.store is not None
        add_goals(api.store)
        _OPENED.clear()
        _WATCHING.append(True)
        try:
            bodies = {path: api.page(path) for path in PAGES}
            bodies["/view/recordings?page=2"] = api.page("/view/recordings?page=2")
        finally:
            _WATCHING.clear()
    forbidden = (
        str(corpus),
        str(data / "artifacts" / "stt-transcripts"),
        str(data / "artifacts" / "voice-features"),
        "/raw/",
        "/derived/",
        "/memos/",
    )
    assert _OPENED and not [path for path in _OPENED if any(part in path for part in forbidden)]
    for path, body in bodies.items():
        assert TRANSCRIPT_TEXT not in body, path
        assert LYRIC_TEXT not in body, path
        links = re.findall(r'href="([^"]*)"', body)
        assert all(link.startswith("/view/") for link in links), (path, links)
        for word in ("/api/", "/audio", "/transcript", ".m4a", ".wav", "<audio", "<source"):
            assert word not in body, (path, word)


# ---- auth ----------------------------------------------------------------------------------


def test_the_forwarded_user_must_be_the_allowed_subject(tmp_path: Path) -> None:
    with running(tmp_path, view_config(data_root(tmp_path, hostile=False))) as api:
        assert api.call("GET", "/view/")[0] == 200
        for headers, status in (
            ({}, 401),
            ({"X-Forwarded-Email": "cole@example.invalid"}, 401),
            ({"X-Forwarded-User": ""}, 401),
            ({"X-Forwarded-User": "someone-else"}, 403),
            ({"X-Forwarded-User": f"{SUBJECT}x"}, 403),
            ({"X-Forwarded-User": SUBJECT.upper()}, 403),
        ):
            for path in PAGES:
                answer, response_headers, body = api.call("GET", path, headers=headers)
                assert answer == status, (headers, path)
                assert response_headers["content-security-policy"] == CONTENT_SECURITY_POLICY
                assert "clean singing" not in body and "recordings" not in body.split("<main>")[1]


def test_a_bearer_token_is_verified_as_the_api_does(tmp_path: Path) -> None:
    with running(tmp_path, view_config(data_root(tmp_path, hostile=False))) as api:
        assert api.call("GET", "/view/", headers={}, token=mint())[0] == 200
        for token in (mint({"exp": 1}), mint({"sub": "someone-else"}), "not.a.jwt"):
            assert api.call("GET", "/view/", headers={}, token=token)[0] == 401
            assert api.call("GET", "/view/", headers=FORWARDED, token=token)[0] == 401
        status, _, body = api.call("GET", "/view/", headers={"Authorization": "Basic Y29sZTpwdw=="})
        assert status == 401 and "bearer token was refused" in body


# ---- headers, methods, and the gates -------------------------------------------------------


def test_pages_carry_the_security_headers(tmp_path: Path) -> None:
    with running(tmp_path, view_config(data_root(tmp_path, hostile=False))) as api:
        for path in (*PAGES, "/view/nowhere"):
            _, headers, body = api.call("GET", path)
            assert headers["content-security-policy"] == CONTENT_SECURITY_POLICY
            assert "script-src" not in headers["content-security-policy"]
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["referrer-policy"] == "no-referrer"
            assert headers["x-frame-options"] == "DENY"
            assert headers["cache-control"] == "no-store"
            assert headers["content-length"] == str(len(body.encode("utf-8")))
        status, headers, body = api.call("HEAD", "/view/trends")
        assert status == 200 and body == "" and int(headers["content-length"]) > 1000
        status, headers, _ = api.call("GET", "/view")
        assert (status, headers["location"]) == (308, "/view/")
        status, _, body = api.call("GET", "/view/nowhere")
        assert status == 404 and "no such page" in body
        assert api.call("GET", "/view/report/extra")[0] == 404
        assert api.call("GET", "/viewer", headers={})[0] == 404


def test_only_get_and_head_are_answered(tmp_path: Path) -> None:
    with running(tmp_path, view_config(data_root(tmp_path, hostile=False))) as api:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            for path in (*PAGES, "/view", "/view/nowhere"):
                for headers in (FORWARDED, {}):
                    status, response_headers, body = api.call(method, path, headers=headers)
                    assert status == 405, (method, path)
                    assert response_headers["allow"] == "GET, HEAD"
                    assert "GET and HEAD only" in body


def test_without_the_journey_routes_the_view_answers_503(tmp_path: Path) -> None:
    with running(tmp_path, None) as api:
        for path in PAGES:
            status, headers, body = api.call("GET", path)
            assert status == 503 and "without the journey routes" in body
            assert headers["content-type"] == "text/html; charset=utf-8"
        assert api.call("GET", "/view/", headers={})[0] == 401
        assert api.call("GET", "/view")[0] == 308


def test_a_page_that_fails_answers_500_without_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(store: JourneyStore) -> str:
        raise KeyError("rows")

    monkeypatch.setattr(pages, "summary_page", broken)
    logged: list[str] = []
    with running(tmp_path, view_config(data_root(tmp_path, hostile=False))) as api:
        api.view._log = logged.append
        status, _, body = api.call("GET", "/view/")
        assert status == 500 and "could not be rendered" in body and "rows" not in body
    assert logged == ["view summary failed: KeyError: 'rows'"]
    default = ViewRoutes(None, api.view.verifier)
    default._log("checked")


def test_serve_wires_the_view_with_the_api_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cole_voice_lab.api import server as server_module

    built: dict[str, Any] = {}
    original = server_module.build_server

    def spy(*args: Any, **kwargs: Any) -> ApiServer:
        built.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(server_module, "build_server", spy)
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    AssetLayout.initialize(tmp_path / "lab")
    code = server_module.main(
        [
            "serve",
            "--asset-root",
            str(tmp_path / "lab"),
            "--jwks",
            str(jwks_path),
            "--issuer",
            "https://id.example.invalid",
            "--audience",
            "voice-lab-desktop",
            "--allowed-subject",
            SUBJECT,
            "--server-build",
            "test",
            "--port",
            "0",
            "--once",
        ]
    )
    assert code == 0
    journey, view = built["routes"]
    assert isinstance(view, ViewRoutes) and view.store is None
    assert view.verifier is built["verifier"]
    assert view.verifier.policy.allowed_subjects == (SUBJECT,)
    capsys.readouterr()


# ---- the helpers ---------------------------------------------------------------------------


def test_render_helpers_format_and_escape() -> None:
    assert render.fmt(None) == "—" and render.fmt("3") == "—" and render.fmt(float("nan")) == "—"
    assert render.fmt(-0.01) == "0" and render.fmt(1234.5, 1, " h") == "1,234.5 h"
    assert render.fmt(2.0) == "2" and render.fmt(7, 0) == "7"
    assert render.fmt_signed(1.5) == "+1.5" and render.fmt_signed(-1.5) == "-1.5"
    assert render.fmt_signed(None) == "—"
    assert render.duration(None) == "—" and render.duration(-1) == "—"
    assert render.duration(59.6) == "1:00" and render.duration(3725) == "1:02:05"
    assert render.date_of(None) == "—" and render.date_of("") == "—"
    assert render.href("/view/x", [("a", None), ("b", 'x"&<')]) == "/view/x?b=x%22%26%3C"
    assert render.esc(None) == "null" and render.esc('"') == "&quot;"
    assert render.bucket_label("some_new_bucket") == "some new bucket"
    assert render.message_page("A <b>", "B & C").count("&lt;b&gt;") == 2


def test_charts_handle_gaps_and_empty_series() -> None:
    assert charts.line_chart("x", [charts.LinePoint("a", 1.0)]) == ""
    line = charts.line_chart(
        "gappy",
        [
            charts.LinePoint("a", 1.0, "2019"),
            charts.LinePoint("b", 2.0),
            charts.LinePoint("c", None),
            charts.LinePoint("d", 3.0),
            charts.LinePoint("e", 2.5, "2020"),
        ],
    )
    assert line.count("<polyline") == 2 and line.count("<circle") == 4
    assert ">2019</text>" in line and "<title>c" not in line
    assert charts.range_chart("x", [charts.RangeBand("q", "", None, None, None)]) == ""
    bands = charts.range_chart(
        "r",
        [
            charts.RangeBand("2019-Q1", "2019", (100.0, 400.0), (150.0, 250.0), 200.0),
            charts.RangeBand("2019-Q2", "", (0.0, 300.0), (0.0, 1.0), None),
        ],
    )
    assert bands.count('class="band"') == 1 and bands.count('class="core"') == 1
    assert "(G3)" in bands and ">C4</text>" in bands
    assert charts.column_chart("x", []) == ""
    many = [charts.ColumnValue(str(n), float(n % 3), "y" if n == 0 else "") for n in range(200)]
    columns = charts.column_chart("many", many)
    assert 'x="8"' in columns and columns.count("<rect") == 133
    assert charts.note_name(261.63) == "C4"
