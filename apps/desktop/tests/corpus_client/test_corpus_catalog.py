"""The PC's catalog on this machine (D-052, RECORDING-V1.md §3 "Clients"): kept by snapshot
digest and checked against it, refreshed with ``If-None-Match`` (a ``304`` changes nothing),
pinned for a session, a new version on the PC recorded for the next session, and recording
offline from the cached version. Without a cached version and without the PC, nothing can be
recorded."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from corpus_rig import PC, client, record, signed_in
from corpus_take_fixtures import run_catalog
from voice_lab_desktop.api import ApiClient, ApiError
from voice_lab_desktop.corpus.catalog import CatalogCache, CatalogError, check_view
from voice_lab_desktop.corpus.spool import SpoolError
from voice_lab_desktop.corpus.status import NO_CATALOG

from cole_voice_lab.api.corpus_contracts import CatalogView
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.corpus_takes import validate_take_package
from cole_voice_lab.models import COLE_SUBJECT


def view_of(pc: PC) -> CatalogView:
    status, document = pc.send("GET", "/api/v1/catalog")
    assert status == 200
    return CatalogView.model_validate_json(json.dumps(document))


def test_without_a_cached_catalog_and_without_the_pc_nothing_is_recorded(
    pc: PC, data_dir: Path
) -> None:
    pc.stop()
    corpus = client(data_dir, pc)
    status = corpus.status()
    assert (status.can_record, status.record_blocked, status.pinned_catalog) == (
        False,
        NO_CATALOG,
        None,
    )
    with pytest.raises(SpoolError, match="No catalog yet"):
        corpus.begin_take("anything")
    assert corpus.run_pass().offline
    assert not corpus.status().can_record
    assert not (data_dir / "corpus" / "recording").exists()


def test_the_catalog_is_kept_refreshed_and_pinned_per_session(
    pc: PC, data_dir: Path, library: Any
) -> None:
    corpus = client(data_dir, pc)
    result = corpus.run_pass()
    assert result.catalog_changed
    version_1 = pc.catalog[0]
    digest = version_1.snapshot_sha256
    # The first version seen is pinned at once: nothing was recorded in this session yet.
    status = corpus.status()
    assert status.can_record and status.pinned_catalog == status.pc_catalog == version_1
    view_file = data_dir / "corpus" / "catalog" / "views" / f"{digest}.json"
    assert stat.S_IMODE(view_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(view_file.parent.stat().st_mode) == 0o700
    assert view_file.read_bytes() == canonical_json_bytes(view_of(pc)) + b"\n"

    # Unchanged on the PC: a 304, and nothing changes here.
    api = ApiClient(pc.url, signed_in(pc.url, data_dir), signed_in(pc.url, data_dir).http)
    answer = api.catalog(digest)
    assert (answer.snapshot_sha256, answer.view) == (digest, None)
    assert api.catalog().view == view_of(pc)
    checked = corpus.catalog.state.checked_at
    assert not corpus.run_pass().catalog_changed
    assert corpus.catalog.state.latest == digest
    assert checked is not None and corpus.catalog.state.checked_at is not None
    assert corpus.catalog.state.checked_at >= checked

    # The owner publishes and activates version 2 on the PC: this session stays on version 1,
    # and says that the next one switches.
    run_catalog(pc.layout, "add-prompt", "warm-05", "--text", "The kettle clicked off.")
    run_catalog(pc.layout, "publish")
    run_catalog(pc.layout, "activate", "--subject-id", COLE_SUBJECT, "--version", "2")
    assert corpus.run_pass().catalog_changed
    status = corpus.status()
    assert status.pinned_catalog == version_1 and status.switch_pending
    assert status.pc_catalog is not None and status.pc_catalog.catalog_version == 2
    with pytest.raises(SpoolError, match="has no prompt warm-05"):
        corpus.begin_take("warm-05")
    # A take of this session names version 1, whole: prompt and catalog of one version.
    take = record(corpus, pc.catalog[1].suite.prompts[0].prompt_id, seed=11)
    manifest = validate_take_package(take.package).manifest
    assert manifest.catalog == version_1 and manifest.prompt == pc.catalog[1].suite.prompts[0]
    # The PC holds version 1 too: it accepts the take.
    assert corpus.run_pass().uploaded == 1

    # The next session (a new client, or the UI's switch between takes) records on version 2.
    switched = corpus.start_new_session()
    assert switched is not None and switched.reference == status.pc_catalog
    assert not corpus.status().switch_pending
    pending = corpus.begin_take("warm-05")
    assert pending.catalog == status.pc_catalog and pending.prompt.prompt_id == "warm-05"
    corpus.discard_take(pending)
    assert not pending.directory.exists()
    later = client(data_dir, pc)
    assert later.status().pinned_catalog == status.pc_catalog
    # Only the pinned and the latest views are kept.
    assert [path.stem for path in view_file.parent.iterdir()] == [status.pc_catalog.snapshot_sha256]


def test_recording_works_offline_from_the_cached_catalog(
    pc: PC, data_dir: Path, library: Any
) -> None:
    client(data_dir, pc).run_pass()
    pc.stop()
    corpus = client(data_dir, pc)
    take = record(corpus, pc.catalog[1].suite.prompts[3].prompt_id, seed=12)
    assert take.record.catalog == pc.catalog[0]
    assert corpus.run_pass().offline
    status = corpus.status()
    assert status.can_record and status.offline and status.queued == 1
    assert status.pinned_catalog == status.pc_catalog == pc.catalog[0]


def test_a_cached_view_is_checked_against_its_digest(pc: PC, data_dir: Path) -> None:
    client(data_dir, pc).run_pass()
    view = view_of(pc)
    assert check_view(view).snapshot_sha256 == pc.catalog[0].snapshot_sha256
    # A view whose suite or aliases are not the snapshot its reference names is refused.
    shorter = view.suite.model_copy(update={"prompts": view.suite.prompts[:-1]})
    with pytest.raises(CatalogError, match="does not describe its version"):
        check_view(view.model_copy(update={"suite": shorter}))
    other = view.reference.model_copy(update={"snapshot_sha256": "0" * 64})
    with pytest.raises(CatalogError, match="not the snapshot its reference names"):
        check_view(view.model_copy(update={"reference": other}))
    with pytest.raises(CatalogError, match="cole-self"):
        check_view(view.model_copy(update={"subject_id": "someone-else"}))
    # A damaged cache reads as no catalog (it is fetched again), never as another version.
    root = data_dir / "corpus" / "catalog"
    view_file = root / "views" / f"{pc.catalog[0].snapshot_sha256}.json"
    os.chmod(view_file, 0o600)
    data = bytearray(view_file.read_bytes())
    at = data.index(b'"text":"') + len(b'"text":"')
    data[at] = ord("X") if data[at] != ord("X") else ord("Y")  # one letter of one prompt
    view_file.write_bytes(bytes(data))
    damaged = CatalogCache(root)
    assert damaged.open_session() is None and damaged.state.latest is None
    pc.stop()
    assert not client(data_dir, pc).status().can_record
    pc.start()
    again = client(data_dir, pc)
    assert again.run_pass().catalog_changed and again.status().can_record
    # A state file that cannot be read is an empty cache too.
    (root / "state.json").write_text("{}")
    assert CatalogCache(root).open_session() is None


def test_the_pc_without_an_active_catalog(tmp_path: Path, data_dir: Path) -> None:
    bare = PC.create(tmp_path / "bare", seeded=False).start()
    try:
        corpus = client(data_dir, bare)
        result = corpus.run_pass()
        assert result.reached and not result.offline and result.error == ""
        status = corpus.status()
        assert status.pc_catalog_unavailable and not status.can_record
    finally:
        bare.stop()


def test_an_answer_that_is_not_its_version_is_refused(pc: PC, data_dir: Path) -> None:
    import httpx

    digest = pc.catalog[0].snapshot_sha256
    body = canonical_json_bytes(view_of(pc))
    answers = iter(
        [
            httpx.Response(304, headers={"ETag": f'"{"b" * 64}"'}),
            httpx.Response(200, headers={"ETag": f'"{"b" * 64}"'}, content=body),
            httpx.Response(200, headers={"ETag": f'"{digest}"'}, content=b"{}"),
        ]
    )
    http = httpx.Client(transport=httpx.MockTransport(lambda _request: next(answers)))
    api = ApiClient("http://127.0.0.1:9", signed_in("http://127.0.0.1:9", data_dir), http)
    with pytest.raises(ApiError, match="304 for another catalog version"):
        api.catalog(digest)
    with pytest.raises(ApiError, match="ETag is not its snapshot's digest"):
        api.catalog(digest)
    with pytest.raises(ApiError, match="invalid CatalogView"):
        api.catalog(digest)
