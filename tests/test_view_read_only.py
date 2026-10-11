"""The web view is read-only and never serves audio (design §6.5, §9 phase 4; decision 8, D-025).

Every ``/view`` route is enumerated from the code (``view.routes.PAGE_NAMES``), so a page
added later is checked the day it exists. Two properties, over every route:

- **Only GET reads.** Every other method is refused before anything is read or written:
  ``POST``, ``PUT``, ``DELETE`` and ``PATCH`` answer ``405`` (``Allow: GET, HEAD``) whether
  or not the caller is signed in, a body is never read into any state, and methods the
  server has no handler for (``OPTIONS``, ``TRACE``, WebDAV's, made-up ones) answer ``501``.
  ``HEAD`` is ``GET`` without the body (RFC 9110 §9.3.2: a server that answers ``GET``
  answers ``HEAD``): the same status and headers, no bytes.
- **No audio.** With playback approved on the server and real recordings in the corpus and
  in ``raw/``, no ``/view`` route, whatever its query, ``Range`` header or a path that tries
  to climb out of ``/view/``, answers with audio, an audio type, a byte range, or a
  reference to an audio route; and serving every route opens no file under the corpus,
  ``raw/``, ``derived/`` or ``memos/``. The view's modules import nothing of the API's
  audio seam.
"""

from __future__ import annotations

import ast
import hashlib
import http.client
from pathlib import Path
from typing import Any

import pytest
from home_host_fixtures import mint
from test_view import (
    _OPENED,
    _WATCHING,
    FORWARDED,
    add_goals,
    data_root,
    running,
    view_config,
)

from cole_voice_lab.journey.corpus import recording_id
from cole_voice_lab.view.routes import PAGE_NAMES, ROOT

VIEW_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cole_voice_lab" / "view"
ROUTES = ("/view", *(ROOT + name for name in PAGE_NAMES), "/view/no-such-page")
REFUSED_405 = ("POST", "PUT", "DELETE", "PATCH")
UNSUPPORTED = ("OPTIONS", "TRACE", "PROPFIND", "MKCOL", "BREW", "get")
# A small, valid-looking audio payload for each container the corpus can hold.
M4A = b"\x00\x00\x00\x18ftypM4A \x00\x00\x02\x00M4A isomiso2" + b"memo-bytes" * 64
WAV = b"RIFF" + (36 + 64).to_bytes(4, "little") + b"WAVEfmt " + bytes(24) + b"data" + bytes(64)
MEMO = "20190101 000000-A.m4a"
SESSION = "s-20260101-120000"


def raw_request(
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One request with the path sent exactly as given (no client-side normalisation)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.putrequest(method, path, skip_accept_encoding=True)
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        data = b"" if method == "HEAD" else response.read()
        answer = {name.lower(): value for name, value in response.getheaders()}
        return response.status, answer, data
    finally:
        connection.close()


@pytest.fixture
def served(tmp_path: Path) -> Any:
    """The view over a synthetic data root whose corpus and raw/ hold real files, with
    playback approved (so the API itself would serve audio)."""
    data = data_root(tmp_path, hostile=False)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / MEMO).write_bytes(M4A)
    # The measurement sessions' raw/ (on the PC, the lab's; here beside the API's own lab).
    raw = tmp_path / "accepted" / "raw"
    (raw / SESSION).mkdir(parents=True)
    (raw / SESSION / "01-room-silence.wav").write_bytes(WAV)
    config = view_config(data, corpus_root=corpus, measurement_dir=raw)
    with running(tmp_path, config) as api:
        assert api.store is not None
        add_goals(api.store)
        yield api, data, corpus, raw


def state_digests(root: Path) -> dict[str, str]:
    """Every file under the data root and the lab, by digest (to show nothing changed)."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "jwks.json"
    }


def test_the_routes_under_test_are_every_page() -> None:
    assert PAGE_NAMES == ("", "recordings", "journeys", "trends", "goals", "report")
    assert set(ROUTES) >= {"/view", "/view/", "/view/report"}


# ---- only GET reads -------------------------------------------------------------------------


def test_every_view_route_refuses_methods_other_than_get(served: Any) -> None:
    api, data, _corpus, _raw = served
    port = api.server.server_address[1]
    before = state_digests(data.parent)
    goal = b'{"metric": "cpps", "direction": "at_least", "target": 1, "takesRequired": 3}'
    for path in ROUTES:
        for method in REFUSED_405:
            for headers in (FORWARDED, {}, {"Authorization": f"Bearer {mint()}"}):
                status, answer, body = raw_request(port, method, path, headers, goal)
                assert status == 405, (method, path, headers)
                assert answer["allow"] == "GET, HEAD"
                assert b"GET and HEAD only" in body
                assert b"clean singing" not in body and b"<table" not in body
        for method in UNSUPPORTED:
            status, answer, body = raw_request(port, method, path, FORWARDED)
            assert status == 501, (method, path)
            assert b"<table" not in body and b"singing" not in body
    assert state_digests(data.parent) == before  # no body reached any state


def test_head_is_get_without_the_body(served: Any) -> None:
    api, *_ = served
    port = api.server.server_address[1]
    for path in ROUTES:
        get_status, get_headers, get_body = raw_request(port, "GET", path, FORWARDED)
        head_status, head_headers, head_body = raw_request(port, "HEAD", path, FORWARDED)
        assert head_status == get_status and head_body == b""
        assert head_headers["content-type"] == get_headers["content-type"]
        assert head_headers["content-length"] == str(len(get_body))


# ---- no audio -------------------------------------------------------------------------------


def _no_audio(path: str, status: int, headers: dict[str, str], body: bytes) -> None:
    content_type = headers.get("content-type", "")
    assert not content_type.startswith("audio/"), (path, content_type)
    assert "content-range" not in headers and "accept-ranges" not in headers, path
    assert status != 206, path
    assert M4A not in body and WAV not in body and b"memo-bytes" not in body, path
    for marker in (b"<audio", b"<source", b"/audio", b".m4a", b".wav", b"/api/"):
        assert marker not in body, (path, marker)


def test_no_view_route_serves_audio(served: Any) -> None:
    api, _data, corpus, raw = served
    port = api.server.server_address[1]
    memo_id = recording_id(MEMO)
    queries = (
        "",
        f"?recording={memo_id}",
        f"?id={memo_id}&audio=1",
        "?page=2&bucket=clean_singing",
        "?path=raw/s-20260101-120000/01-room-silence.wav",
        "?file=" + MEMO.replace(" ", "%20"),
    )
    _OPENED.clear()
    _WATCHING.append(True)
    try:
        for path in ROUTES:
            for query in queries:
                for headers in (FORWARDED, {**FORWARDED, "Range": "bytes=0-15"}):
                    status, answer, body = raw_request(port, "GET", path + query, headers)
                    _no_audio(path + query, status, answer, body)
                    if path != "/view" and path != "/view/no-such-page":
                        assert status == 200, (path, query)
                        assert answer["content-type"] == "text/html; charset=utf-8"
    finally:
        _WATCHING.clear()
    forbidden = (str(corpus), str(raw), "/raw/", "/derived/", "/memos/")
    opened = [path for path in _OPENED if any(part in path for part in forbidden)]
    assert _OPENED and opened == []


def test_no_path_climbs_out_of_view_to_audio(served: Any) -> None:
    api, *_ = served
    port = api.server.server_address[1]
    memo_id = recording_id(MEMO)
    measurement = recording_id(f"measurement/{SESSION}/01-room-silence.wav")
    for target in (f"api/v1/recordings/{memo_id}/audio",):
        for climb in ("../", "%2e%2e/", "%2E%2E/", "..%2f", "./../", "recordings/../../", "/"):
            path = f"/view/{climb}{target}"
            status, answer, body = raw_request(port, "GET", path, FORWARDED)
            _no_audio(path, status, answer, body)
            assert status == 404, path
    # The API's own audio route is behind the bearer check: the door's cookie identity
    # alone (what the phone has) never reaches it.
    for path in (f"/api/v1/recordings/{memo_id}/audio", f"/api/v1/recordings/{measurement}/audio"):
        status, answer, body = raw_request(port, "GET", path, FORWARDED)
        assert status == 401, path
        _no_audio(path, status, answer, body)


def test_the_view_imports_nothing_of_the_audio_seam() -> None:
    seam = {
        "open_recording_audio",
        "AudioHandle",
        "parse_range",
        "_audio_response",
        "_open_audio",
        "_v1_audio",
    }
    for path in sorted(VIEW_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = {alias.name for alias in node.names}
                assert not names & seam, (path.name, names & seam)
                assert node.module not in {"cole_voice_lab.api.journey_routes"}, path.name
                assert not (node.level and node.module == "api.journey_routes"), path.name
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert name not in seam, (path.name, name)
