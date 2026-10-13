"""The corpus library on the PC (D-052, docs/design/RECORDING-V1.md step 3): a synthetic take
uploaded end to end through ``/api/v1/intake`` appears in ``GET /api/v1/corpus/library``, its
prompt's history and, with the playback approval, its audio."""

from __future__ import annotations

import http.client
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from corpus_take_fixtures import NOW, make_take, run_catalog, seed_catalog
from home_host_fixtures import NOW as APPROVAL_NOW
from home_host_fixtures import bind_approval, jwks, mint, policy

from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.corpus_contracts import CorpusLibrary, TakeHistory
from cole_voice_lab.api.corpus_routes import CorpusRoutes, byte_range
from cole_voice_lab.api.routing import RouteRefusal
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.corpus_read_model import library_path, read_library
from cole_voice_lab.corpus_takes import AUDIO_NAME, ValidatedTake
from cole_voice_lab.home_host import HomeHostLedger
from cole_voice_lab.intake_client import HomeHostClient, upload_session
from cole_voice_lab.intake_publisher import publish_pending
from cole_voice_lab.models import COLE_SUBJECT
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import CatalogReference, CatalogSnapshot

APPROVAL = "Cole approved take playback on the PC, 2026-10-12."

type Catalog = tuple[CatalogReference, CatalogSnapshot]


@pytest.fixture
def server_layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "srv" / "voice-lab" / "lab")


@pytest.fixture
def catalog(server_layout: AssetLayout) -> Catalog:
    return seed_catalog(server_layout)


@contextmanager
def serving(
    layout: AssetLayout, tmp_path: Path, *, playback_approval: str | None = APPROVAL
) -> Iterator[int]:
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    server = ApiServer(
        ("127.0.0.1", 0),
        staging=IntakeStaging(layout, min_free_bytes=0),
        verifier=TokenVerifier(policy(jwks_path)),
        meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
        log=False,
        routes=(CorpusRoutes(layout, playback_approval=playback_approval),),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def get(
    port: int, path: str, *, method: str = "GET", **headers: str
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, headers={"Authorization": f"Bearer {mint()}", **headers})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, body


def error(answer: tuple[int, dict[str, str], bytes]) -> tuple[int, str]:
    return answer[0], json.loads(answer[2])["error"]


def published_take(layout: AssetLayout, catalog: Catalog, work: Path) -> ValidatedTake:
    work.mkdir(exist_ok=True)
    take = make_take(work / "take", catalog=catalog)
    os.rename(work / "take", layout.directory("incoming") / take.session_id)
    assert len(publish_pending(layout, min_free_bytes=0).accepted) == 1
    return take


def test_a_take_uploaded_end_to_end_appears_in_the_library(
    server_layout: AssetLayout, catalog: Catalog, tmp_path: Path
) -> None:
    client_layout = AssetLayout.initialize(tmp_path / "air" / "voice-lab" / "lab")
    bind_approval(client_layout)
    work = tmp_path / "work"
    work.mkdir()
    prompt = catalog[1].suite.prompts[2]
    take = make_take(work / "take", catalog=catalog, prompt_id=prompt.prompt_id, seconds=0.5)
    with serving(server_layout, tmp_path) as port:
        status = upload_session(
            work / "take",
            layout=client_layout,
            client=HomeHostClient.loopback_for_tests(port, mint),
            clock=lambda: APPROVAL_NOW,
            sleep=lambda _seconds: publish_pending(server_layout, min_free_bytes=0),
            wait_seconds=30,
        )
        assert status.state == "accepted" and status.receipt is not None
        assert status.receipt.manifest_sha256 == take.manifest_sha256
        assert status.receipt.raw_path == f"raw/{take.manifest_sha256}"
        events = [entry.event for entry, _ in HomeHostLedger(client_layout).entries()]
        assert events == ["upload-started", "upload-completed"]
        assert (work / "take").is_dir()  # the client keeps its take

        code, headers, body = get(port, "/api/v1/corpus/library")
        assert code == 200
        library = CorpusLibrary.model_validate_json(body)
        assert library.catalog == catalog[0] and len(library.rows) == 34
        row = next(row for row in library.rows if row.prompt_id == prompt.prompt_id)
        assert (row.status, row.take_count, row.latest_take_id) == ("draft", 1, take.session_id)
        assert row.latest_duration_seconds == pytest.approx(0.5)
        assert (library.coverage.draft, library.coverage.missing) == (1, 33)
        found = read_library(server_layout, COLE_SUBJECT)
        assert found is not None and headers["etag"] == f'"{found[1]}"'

        code, _headers, body = get(port, f"/api/v1/corpus/prompts/{prompt.prompt_id}/takes")
        assert code == 200
        history = TakeHistory.model_validate_json(body)
        assert history.row == row
        [listed] = history.takes
        assert (listed.take_id, listed.decision, listed.decision_revision) == (
            take.session_id, "draft", 0,
        )  # fmt: skip
        assert listed.requested_at == NOW and listed.capture_host == "synthetic-host"

        code, headers, body = get(port, f"/api/v1/corpus/takes/{take.session_id}/audio")
        assert code == 200 and headers["content-type"] == "audio/wav"
        assert body == (work / "take" / AUDIO_NAME).read_bytes()
        assert headers["etag"] == f'"{take.manifest.audio.sha256}"'


def test_the_library_answers_with_its_digest_as_the_tag(
    server_layout: AssetLayout, catalog: Catalog, tmp_path: Path
) -> None:
    with serving(server_layout, tmp_path) as port:
        code, headers, body = get(port, "/api/v1/corpus/library")
        assert code == 200
        empty = CorpusLibrary.model_validate_json(body)
        assert empty.coverage.missing == 34 and empty.coverage.takes == 0
        tag = headers["etag"]
        assert get(port, "/api/v1/corpus/library", **{"If-None-Match": tag})[0] == 304
        status, _headers, body = get(port, "/api/v1/corpus/library", method="HEAD")
        assert status == 200 and body == b""
        assert error(get(port, "/api/v1/corpus/library", method="POST")) == (
            405, "method_not_allowed",
        )  # fmt: skip
        # A take changes the library and so its tag; the history answers with the same tag.
        take = published_take(server_layout, catalog, tmp_path / "work")
        code, headers, _body = get(port, "/api/v1/corpus/library", **{"If-None-Match": tag})
        assert code == 200 and headers["etag"] != tag
        prompt_id = take.manifest.prompt.prompt_id
        code, history_headers, _body = get(port, f"/api/v1/corpus/prompts/{prompt_id}/takes")
        assert code == 200 and history_headers["etag"] == headers["etag"]
        assert get(
            port, f"/api/v1/corpus/prompts/{prompt_id}/takes", **{"If-None-Match": headers["etag"]}
        )[0] == 304  # fmt: skip


def test_unknown_prompts_and_paths_are_not_found(
    server_layout: AssetLayout, catalog: Catalog, tmp_path: Path
) -> None:
    with serving(server_layout, tmp_path) as port:
        for prompt_id in ("no-such-prompt", "UPPER", "%2e%2e"):
            assert error(get(port, f"/api/v1/corpus/prompts/{prompt_id}/takes")) == (
                404, "prompt_not_found",
            )  # fmt: skip
        assert get(port, "/api/v1/corpus/elsewhere")[0] == 404


def test_without_a_catalog_or_library_the_api_says_so(
    server_layout: AssetLayout, tmp_path: Path
) -> None:
    with serving(server_layout, tmp_path) as port:
        assert error(get(port, "/api/v1/corpus/library")) == (503, "catalog_unavailable")
        seed_catalog(server_layout)
        assert get(port, "/api/v1/corpus/library")[0] == 200
        library_path(server_layout, COLE_SUBJECT).unlink()
        assert error(get(port, "/api/v1/corpus/library")) == (503, "library_unavailable")
        assert publish_pending(server_layout, min_free_bytes=0).read_model == "current"
        assert get(port, "/api/v1/corpus/library")[0] == 200


def test_a_library_of_another_version_or_damaged_is_refused(
    server_layout: AssetLayout, catalog: Catalog, tmp_path: Path
) -> None:
    with serving(server_layout, tmp_path) as port:
        assert get(port, "/api/v1/corpus/library")[0] == 200
        path = library_path(server_layout, COLE_SUBJECT)
        original = path.read_bytes()
        path.write_bytes(original.replace(b'{"catalog"', b'{ "catalog"', 1))
        assert error(get(port, "/api/v1/corpus/library")) == (500, "library_invalid")
        path.write_bytes(original)
        assert get(port, "/api/v1/corpus/library")[0] == 200
        # A newer active version whose library is not built yet: say so, never mix versions.
        run_catalog(server_layout, "add-prompt", "warm-05", "--text", "The kettle clicked off.")
        run_catalog(server_layout, "publish")
        run_catalog(server_layout, "activate", "--subject-id", COLE_SUBJECT, "--version", "2")
        path.write_bytes(original)
        assert error(get(port, "/api/v1/corpus/library")) == (503, "library_unavailable")
        publish_pending(server_layout, min_free_bytes=0)
        code, _headers, body = get(port, "/api/v1/corpus/library")
        assert code == 200
        assert CorpusLibrary.model_validate_json(body).catalog.catalog_version == 2


def test_take_audio_needs_the_playback_approval_and_answers_ranges(
    server_layout: AssetLayout, catalog: Catalog, tmp_path: Path
) -> None:
    take = published_take(server_layout, catalog, tmp_path / "work")
    raw = server_layout.directory("raw") / take.manifest_sha256 / AUDIO_NAME
    audio = raw.read_bytes()
    size = len(audio)
    path = f"/api/v1/corpus/takes/{take.session_id}/audio"
    with serving(server_layout, tmp_path, playback_approval=None) as port:
        assert error(get(port, path)) == (403, "playback_not_approved")
    with serving(server_layout, tmp_path) as port:
        code, headers, body = get(port, path, Range="bytes=0-99")
        assert (code, body) == (206, audio[:100])
        assert headers["content-range"] == f"bytes 0-99/{size}"
        assert headers["accept-ranges"] == "bytes"
        code, headers, body = get(port, path, Range="bytes=-10")
        assert (code, body) == (206, audio[-10:])
        code, _headers, body = get(port, path, Range="bytes=44-")
        assert (code, body) == (206, audio[44:])
        assert get(port, path, Range="bytes=0-1,4-5")[2] == audio  # several ranges: all of it
        code, headers, _body = get(port, path, Range=f"bytes={size}-")
        assert code == 416 and headers["content-range"] == f"bytes */{size}"
        code, headers, body = get(port, path, method="HEAD")
        assert (code, body, headers["content-length"]) == (200, b"", str(size))
        assert error(get(port, "/api/v1/corpus/takes/take-" + "0" * 64 + "/audio")) == (
            404, "take_not_found",
        )  # fmt: skip
        assert error(get(port, "/api/v1/corpus/takes/not-a-take/audio")) == (
            404, "take_not_found",
        )  # fmt: skip


def test_take_audio_that_disagrees_with_its_record_is_refused(
    server_layout: AssetLayout, catalog: Catalog, tmp_path: Path
) -> None:
    take = published_take(server_layout, catalog, tmp_path / "work")
    package = server_layout.directory("raw") / take.manifest_sha256
    os.chmod(package, 0o700)
    os.chmod(package / AUDIO_NAME, 0o600)
    with (package / AUDIO_NAME).open("ab") as stream:
        stream.write(b"\0\0\0")
    with serving(server_layout, tmp_path) as port:
        assert error(get(port, f"/api/v1/corpus/takes/{take.session_id}/audio")) == (
            500, "take_invalid",
        )  # fmt: skip


def test_byte_ranges() -> None:
    assert byte_range(None, 10) is None
    assert byte_range("items=0-1", 10) is None
    assert byte_range("bytes=-", 10) is None
    assert byte_range("bytes=2-4", 10) == (2, 4)
    assert byte_range("bytes=8-20", 10) == (8, 9)
    assert byte_range("bytes=-20", 10) == (0, 9)
    for header in ("bytes=10-", "bytes=5-4", "bytes=-0"):
        with pytest.raises(RouteRefusal) as refused:
            byte_range(header, 10)
        assert refused.value.status == 416
