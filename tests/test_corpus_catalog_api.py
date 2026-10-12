"""The catalog on the PC (D-052, docs/design/RECORDING-V1.md step 2).

The corpus store's writers (``voice-lab catalog init|publish|activate`` and every intake
publisher pass) keep the read model current; ``GET /api/v1/catalog`` serves the active
version from that model and the digest-checked snapshot, never opening the store.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from home_host_fixtures import jwks, mint, policy

from cole_voice_lab import cli
from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.corpus_contracts import CatalogView
from cole_voice_lab.api.corpus_routes import CorpusRoutes
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.corpus_read_model import (
    active_catalog_path,
    read_active_catalog,
    refresh_active_catalog,
)
from cole_voice_lab.intake_publisher import publish_pending
from cole_voice_lab.library_classification import classify
from cole_voice_lab.models import COLE_SUBJECT
from cole_voice_lab.paths import AssetLayout

#: cole-corpus version 1: pilot-v2 without its room tones (docs/CORPUS_STORE.md).
VERSION_1 = "e234266eccb7b8a1c55e7454b32a2a61a99e72a3939bc4850a6951acc25fd7b6"


def catalog(layout: AssetLayout, *argv: str) -> dict[str, Any]:
    """``voice-lab catalog <argv> --asset-root <lab>``; its printed JSON."""
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["catalog", *argv, "--asset-root", str(layout.root)]) == 0
    return dict(json.loads(out.getvalue()))


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "data" / "voice-lab" / "lab")


@pytest.fixture
def seeded(layout: AssetLayout) -> AssetLayout:
    printed = catalog(
        layout, "init", "--subject-id", COLE_SUBJECT, "--from-builtin", "pilot-v2",
        "--drop-room-tones",
    )  # fmt: skip
    assert printed["reference"]["catalog_sha256"] == VERSION_1
    assert printed["read_model"] == str(active_catalog_path(layout, COLE_SUBJECT))
    return layout


@contextmanager
def serving(layout: AssetLayout, tmp_path: Path) -> Iterator[int]:
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    server = ApiServer(
        ("127.0.0.1", 0),
        staging=IntakeStaging(layout, min_free_bytes=0),
        verifier=TokenVerifier(policy(jwks_path)),
        meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
        log=False,
        routes=(CorpusRoutes(layout),),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def get(
    port: int, *, method: str = "GET", token: str | None = "mint", **headers: str
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    sent = dict(headers)
    if token is not None:
        sent["Authorization"] = f"Bearer {mint() if token == 'mint' else token}"
    connection.request(method, "/api/v1/catalog", headers=sent)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, body


def test_the_active_catalog_is_served_with_its_snapshot_digest_as_the_tag(
    seeded: AssetLayout, tmp_path: Path
) -> None:
    with serving(seeded, tmp_path) as port:
        status, headers, body = get(port)
        assert status == 200
        view = CatalogView.model_validate_json(body)
        assert view.subject_id == COLE_SUBJECT
        assert view.reference.catalog_sha256 == VERSION_1 and view.reference.catalog_version == 1
        assert len(view.suite.prompts) == 34
        assert not any(prompt.category == "room-tone" for prompt in view.suite.prompts)
        tag = f'"{view.reference.snapshot_sha256}"'
        assert headers["etag"] == tag
        # A client that holds this version gets nothing again; HEAD sends no body.
        status, headers, body = get(port, **{"If-None-Match": tag})
        assert (status, body, headers["etag"]) == (304, b"", tag)
        assert get(port, **{"If-None-Match": f'"other", W/{tag}'})[0] == 304
        assert get(port, **{"If-None-Match": '"other"'})[0] == 200
        status, _headers, body = get(port, method="HEAD")
        assert status == 200 and body == b""
        assert get(port, method="POST")[0] == 405
        assert get(port, token=None)[0] == 401


def test_a_new_active_version_is_served_after_activate(seeded: AssetLayout, tmp_path: Path) -> None:
    catalog(
        seeded, "add-prompt", "warm-05", "--text", "The kettle clicked off just as the rain began."
    )
    published = catalog(seeded, "publish")
    assert published["reference"]["catalog_version"] == 2
    record = read_active_catalog(seeded, COLE_SUBJECT)
    assert record is not None and record.reference.catalog_version == 1  # not active yet
    catalog(seeded, "activate", "--subject-id", COLE_SUBJECT, "--version", "2")
    with serving(seeded, tmp_path) as port:
        view = CatalogView.model_validate_json(get(port)[2])
    assert view.reference.catalog_version == 2 and len(view.suite.prompts) == 35


def test_without_a_catalog_the_api_says_so(layout: AssetLayout, tmp_path: Path) -> None:
    assert refresh_active_catalog(layout) is None
    assert not active_catalog_path(layout, COLE_SUBJECT).exists()
    with serving(layout, tmp_path) as port:
        status, _headers, body = get(port)
    assert status == 503 and json.loads(body)["error"] == "catalog_unavailable"


def test_a_damaged_read_model_or_snapshot_is_refused(seeded: AssetLayout, tmp_path: Path) -> None:
    record = read_active_catalog(seeded, COLE_SUBJECT)
    assert record is not None
    snapshot = seeded.root / record.reference.snapshot_path
    original = snapshot.read_bytes()
    os.chmod(snapshot, 0o600)
    snapshot.write_bytes(original.replace(b"quiet", b"QUIET", 1))
    with serving(seeded, tmp_path) as port:
        status, _headers, body = get(port)
        assert status == 500 and json.loads(body)["error"] == "catalog_invalid"
        snapshot.write_bytes(original)
        assert get(port)[0] == 200
        model = active_catalog_path(seeded, COLE_SUBJECT)
        damaged = model.read_bytes().replace(b'{"reference"', b'{ "reference"')
        assert damaged != model.read_bytes()
        model.write_bytes(damaged)
        status, _headers, body = get(port)
        assert status == 500 and json.loads(body)["error"] == "catalog_invalid"


def test_every_intake_pass_keeps_the_read_model_current(seeded: AssetLayout) -> None:
    model = active_catalog_path(seeded, COLE_SUBJECT)
    model.unlink()
    summary = publish_pending(seeded, min_free_bytes=0)
    assert summary.read_model == "current" and model.is_file()
    # An unchanged reference is not rewritten.
    before = model.stat().st_mtime_ns
    assert publish_pending(seeded, min_free_bytes=0).read_model == "current"
    assert model.stat().st_mtime_ns == before


def test_a_pass_reports_a_broken_store_and_still_publishes(
    layout: AssetLayout, seeded: AssetLayout
) -> None:
    assert publish_pending(layout, min_free_bytes=0).read_model == "current"
    corpus = layout.directory("provenance") / "corpus"
    os.chmod(corpus, 0o755)  # the store refuses a directory that is not private
    try:
        summary = publish_pending(layout, min_free_bytes=0)
    finally:
        os.chmod(corpus, 0o700)
    assert summary.read_model.startswith("error: ")
    assert summary.accepted == [] and summary.refused == []


def test_an_empty_lab_has_no_catalog_and_the_pass_says_so(layout: AssetLayout) -> None:
    assert publish_pending(layout, min_free_bytes=0).read_model == "no catalog"


def test_the_read_model_never_goes_to_the_vault() -> None:
    found = classify("derived/corpus/active-catalog-cole-self.json")
    assert found.rule.name == "corpus-read-model" and found.rule.disposition == "mac-only"
