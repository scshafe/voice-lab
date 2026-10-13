"""Takes into the PC (D-052, docs/design/RECORDING-V1.md step 3): the take package, its
validator, the publisher's dispatch, the take store and the classification of new paths."""

from __future__ import annotations

import json
import os
import shutil
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from corpus_take_fixtures import (
    DEVICE,
    NOW,
    active_snapshot,
    evidence_for,
    make_take,
    qc_of,
    reseal,
    run_catalog,
    seed_catalog,
    writable,
    write_tone,
)

from cole_voice_lab.capture import READY_NAME
from cole_voice_lab.corpus_read_model import read_library
from cole_voice_lab.corpus_take_store import (
    CorpusTakeState,
    TakeDecision,
    TakeTrim,
    read_take_state,
    read_take_states,
    record_takes,
    state_path,
)
from cole_voice_lab.corpus_takes import (
    AUDIO_NAME,
    CONTINUITY_NAME,
    TAKE_FILES,
    ValidatedTake,
    is_take_package,
    take_id_for,
    validate_take_package,
)
from cole_voice_lab.diagnostic_roots import DIAGNOSTIC_SUBJECT_PREFIX, initialize_diagnostic_root
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.intake import ValidatedPackage, validate_package
from cole_voice_lab.intake_publisher import publish_pending, read_refusal
from cole_voice_lab.library_classification import classify
from cole_voice_lab.library_replication import RawPackageSource, ReplicationItem, library_sources
from cole_voice_lab.models import COLE_SUBJECT, Prompt
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import CatalogReference, CatalogSnapshot
from cole_voice_lab.synthetic_capture import build_synthetic_session

type Catalog = tuple[CatalogReference, CatalogSnapshot]


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "data" / "voice-lab" / "lab")


@pytest.fixture
def catalog(layout: AssetLayout) -> Catalog:
    return seed_catalog(layout)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    directory = tmp_path / "work"
    directory.mkdir()
    return directory


def incoming(layout: AssetLayout, take: ValidatedTake, package: Path) -> Path:
    """A copy of ``package`` where the API's staging would finalize it."""
    target = layout.directory("incoming") / take.session_id
    shutil.copytree(package, target)
    return target


# -- The package ---------------------------------------------------------------------------


def test_a_take_package_is_built_and_validated(catalog: Catalog, work: Path) -> None:
    take = make_take(work / "take", catalog=catalog)
    package = work / "take"
    assert {path.name for path in package.iterdir()} == TAKE_FILES
    manifest = take.manifest
    assert manifest.take_id == take_id_for(manifest.audio.sha256) == take.session_id
    assert manifest.subject_id == COLE_SUBJECT
    assert manifest.prompt == catalog[1].suite.prompts[0]
    assert manifest.catalog == catalog[0]
    assert manifest.capture_device.backend == "portaudio"
    assert manifest.continuity.simulated is False
    assert take.qc_report.status == "pass" and take.qc_report.metrics[0].path == AUDIO_NAME
    assert qc_of(package) == take.source_qc_report
    assert validate_take_package(package) == take
    assert is_take_package(package)
    assert isinstance(validate_package(package), ValidatedTake)
    # No temporary is left beside it.
    assert sorted(path.name for path in work.iterdir()) == ["take"]


def test_a_capture_package_is_not_a_take(work: Path) -> None:
    session = build_synthetic_session(work / "s", session_id="synthetic-001", seconds=0.2)
    assert not is_take_package(session)
    assert isinstance(validate_package(session), ValidatedPackage)
    assert not is_take_package(work / "missing")


def test_a_silent_prompt_allows_a_quiet_take_and_a_spoken_one_does_not(
    catalog: Catalog, work: Path
) -> None:
    quiet = make_take(work / "spoken", catalog=catalog, frequency=None)
    assert quiet.qc_report.status == "review"
    assert "peak-too-quiet" in quiet.qc_report.metrics[0].findings
    room = Prompt(
        prompt_id="room-tone-01",
        text="Remain silent.",
        category="room-tone",
        style="Silent",
        expected_seconds=10.0,
    )
    silent = make_take(work / "silent", catalog=catalog, frequency=None, seed=3, prompt=room)
    assert silent.qc_report.status == "pass"


def test_the_builder_refuses_an_existing_directory_and_leaves_nothing_on_failure(
    catalog: Catalog, work: Path
) -> None:
    (work / "taken").mkdir()
    with pytest.raises(FileExistsError):
        make_take(work / "taken", catalog=catalog)
    source = write_tone(work / "narrow.wav", width=2)
    with pytest.raises(ContractError):
        make_take(work / "narrow", catalog=catalog, wav=source, evidence=evidence_for_any(work))
    with pytest.raises(ContractError, match="cannot be described"):
        make_take(work / "coreaudio", catalog=catalog, device=DEVICE.model_copy(
            update={"backend": "coreaudio"}
        ))  # fmt: skip
    assert sorted(path.name for path in work.iterdir()) == ["narrow.wav", "taken"]


def evidence_for_any(work: Path) -> Any:
    """Evidence of some other canonical WAV (the builder fails on the WAV first)."""
    other = write_tone(work / ".other.wav", seed=9)
    try:
        return evidence_for(other)
    finally:
        other.unlink()


# -- The validator, one rule at a time ---------------------------------------------------------


@pytest.fixture
def package(catalog: Catalog, work: Path) -> Path:
    make_take(work / "take", catalog=catalog)
    return work / "take"


def test_an_undeclared_file_or_a_directory_is_refused(package: Path) -> None:
    writable(package)
    (package / "notes.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(SecurityError, match=r"undeclared=\['notes.txt'\]"):
        validate_take_package(package)
    (package / "notes.txt").unlink()
    (package / "audio").mkdir()
    with pytest.raises(SecurityError, match="directories"):
        validate_take_package(package)


def test_intake_owned_files_are_refused_from_a_client(package: Path) -> None:
    writable(package)
    (package / "intake-receipt.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SecurityError, match="intake-receipt.json"):
        validate_take_package(package)


def test_a_missing_ready_marker_is_a_partial_take(package: Path) -> None:
    writable(package)
    (package / READY_NAME).unlink()
    with pytest.raises(ContractError, match="partial take"):
        validate_take_package(package)


def test_links_are_refused(package: Path, work: Path) -> None:
    writable(package)
    target = package / CONTINUITY_NAME
    moved = work / "continuity.json"
    os.replace(target, moved)
    target.symlink_to(moved)
    with pytest.raises(SecurityError, match="not a regular file"):
        validate_take_package(package)
    target.unlink()
    os.replace(moved, target)
    os.link(target, work / "second-link")
    with pytest.raises(SecurityError, match="hard-linked"):
        validate_take_package(package)


def test_tampered_audio_is_refused(package: Path) -> None:
    writable(package)
    audio = package / AUDIO_NAME
    data = bytearray(audio.read_bytes())
    data[-1] ^= 0x01
    audio.write_bytes(bytes(data))
    with pytest.raises(SecurityError, match="tampered: original.wav"):
        validate_take_package(package)


def test_tampered_evidence_is_refused(package: Path) -> None:
    writable(package)
    evidence = package / CONTINUITY_NAME
    evidence.write_bytes(evidence.read_bytes().replace(b'"ALSA"', b'"JACK"'))
    with pytest.raises(SecurityError, match="continuity evidence hash"):
        validate_take_package(package)


def test_json_in_another_form_is_refused(package: Path) -> None:
    writable(package)
    manifest = package / "manifest.json"
    manifest.write_bytes(json.dumps(json.loads(manifest.read_bytes()), indent=1).encode() + b"\n")
    with pytest.raises(ContractError, match="not canonical"):
        validate_take_package(package)


def test_a_manifest_changed_after_ready_is_refused(package: Path) -> None:
    writable(package)
    document = json.loads((package / "manifest.json").read_bytes())
    document["capture_host"] = "another-host"
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    (package / "manifest.json").write_bytes(payload)
    with pytest.raises(SecurityError, match="READY manifest digest"):
        validate_take_package(package)


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        ({"subject_id": "synthetic-test"}, ContractError, "a take is of cole-self"),
        ({"take_id": "take-" + "0" * 64}, ContractError, "the take id is take-"),
        ({"prompt_sha256": "0" * 64}, ContractError, "prompt_sha256"),
        (
            {"capture_device": DEVICE.model_dump() | {"backend": "coreaudio"}},
            ContractError,
            "PortAudio",
        ),
        (
            {"capture_device": DEVICE.model_dump() | {"display_name": "Mic"}},
            ContractError,
            "UMIK-1",
        ),
        ({"requested_at": "2026-10-12T12:00:00"}, ContractError, "explicit timezone"),
    ],
)
def test_each_manifest_rule_is_checked(
    package: Path, changes: dict[str, Any], error: type[Exception], message: str
) -> None:
    reseal(package, manifest=changes)
    with pytest.raises(error, match=message):
        validate_take_package(package)


def test_the_continuity_summary_must_match_the_evidence(package: Path) -> None:
    manifest = json.loads((package / "manifest.json").read_bytes())
    reseal(package, manifest={"continuity": {**manifest["continuity"], "host_api": "JACK"}})
    with pytest.raises(ContractError, match="continuity summary disagrees"):
        validate_take_package(package)


def test_unsuccessful_or_unbound_evidence_is_refused(package: Path) -> None:
    good = evidence_for(package / AUDIO_NAME)
    failed = good.model_copy(update={"success": False, "error": "the device went away"})
    reseal(package, evidence=failed)
    with pytest.raises(ContractError, match="the device went away"):
        validate_take_package(package)
    reseal(package, evidence=good.model_copy(update={"wav_sha256": "1" * 64}))
    with pytest.raises(SecurityError, match="describes other audio"):
        validate_take_package(package)


def test_qc_must_cover_the_recording_once(package: Path) -> None:
    report = json.loads((package / "qc.json").read_bytes())
    metric = report["metrics"][0]
    reseal(package, qc={"metrics": [{**metric, "path": "other.wav"}]})
    with pytest.raises(ContractError, match="exactly once"):
        validate_take_package(package)


# -- The publisher ---------------------------------------------------------------------------


def test_a_take_is_published_read_only_with_its_record_and_library_row(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    take = make_take(work / "take", catalog=catalog)
    incoming(layout, take, work / "take")
    summary = publish_pending(layout, min_free_bytes=0, clock=lambda: NOW)
    assert [receipt.session_id for receipt in summary.accepted] == [take.session_id]
    assert summary.refused == [] and summary.read_model == "current"
    assert summary.takes_recorded == [take.session_id]
    raw = layout.directory("raw") / take.manifest_sha256
    assert stat.S_IMODE(raw.stat().st_mode) == 0o500
    assert {stat.S_IMODE(child.stat().st_mode) for child in raw.iterdir()} == {0o400}
    published = validate_package(raw, allow_intake_receipt=True)
    assert isinstance(published, ValidatedTake) and published.manifest == take.manifest
    acceptance = layout.directory("provenance") / "intake" / "sessions"
    assert (acceptance / f"{take.session_id}.json").is_file()
    state = read_take_state(layout, take.session_id)
    assert state is not None
    assert state.raw_path == f"raw/{take.manifest_sha256}" and state.accepted_at == NOW
    assert state.decision == TakeDecision(
        decision="draft", revision=0, reason=None, decided_at=None
    )
    assert state.trim == TakeTrim(revision=0, start_frame=None, end_frame=None)
    assert state.qc_status == "pass" and state.device == DEVICE.display_name
    found = read_library(layout, COLE_SUBJECT)
    assert found is not None
    library = found[0]
    row = next(row for row in library.rows if row.prompt_id == take.manifest.prompt.prompt_id)
    assert (row.status, row.take_count, row.latest_take_id) == ("draft", 1, take.session_id)
    assert library.coverage.draft == 1 and library.coverage.takes == 1
    # A second pass writes nothing new.
    again = publish_pending(layout, min_free_bytes=0)
    assert again.accepted == [] and again.takes_recorded == []


def test_a_take_of_a_prompt_its_catalog_does_not_hold_is_quarantined(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    prompt = catalog[1].suite.prompts[0].model_copy(update={"text": "Words nobody was asked."})
    take = make_take(work / "take", catalog=catalog, prompt=prompt)
    incoming(layout, take, work / "take")
    summary = publish_pending(layout, min_free_bytes=0)
    assert summary.accepted == []
    [refusal] = summary.refused
    assert refusal.error.startswith("take_prompt_not_in_catalog: ")
    assert refusal.manifest_sha256 == take.manifest_sha256
    assert read_refusal(layout, take.session_id) == refusal
    assert list(layout.directory("raw").iterdir()) == []
    assert read_take_states(layout) == ()


def test_simulated_audio_and_unknown_catalogs_are_quarantined(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    simulated = make_take(work / "simulated", catalog=catalog, simulated=True)
    incoming(layout, simulated, work / "simulated")
    reference = catalog[0]
    other_sha = "e" * 64
    unknown_reference = reference.model_copy(
        update={
            "catalog_sha256": other_sha,
            "snapshot_path": reference.snapshot_path.replace(reference.catalog_sha256, other_sha),
        }
    )
    unknown = make_take(
        work / "unknown", catalog=(unknown_reference, catalog[1]), seed=1, frequency=330.0
    )
    incoming(layout, unknown, work / "unknown")
    errors = sorted(record.error for record in publish_pending(layout, min_free_bytes=0).refused)
    assert [error.split(":")[0] for error in errors] == ["take_catalog_missing", "take_simulated"]


def test_a_take_id_with_another_manifest_is_a_collision(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    first = make_take(work / "first", catalog=catalog)
    incoming(layout, first, work / "first")
    assert len(publish_pending(layout, min_free_bytes=0).accepted) == 1
    # The same audio described again (another request time) is another manifest.
    later = make_take(work / "later", catalog=catalog, requested_at=NOW + timedelta(minutes=1))
    assert later.session_id == first.session_id
    assert later.manifest_sha256 != first.manifest_sha256
    incoming(layout, later, work / "later")
    [refusal] = publish_pending(layout, min_free_bytes=0).refused
    assert "session ID collision" in refusal.error
    # The identical package again is a verified duplicate.
    incoming(layout, first, work / "first")
    assert publish_pending(layout, min_free_bytes=0).accepted[0].session_id == first.session_id


def test_capture_packages_still_publish_beside_takes(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    take = make_take(work / "take", catalog=catalog)
    incoming(layout, take, work / "take")
    build_synthetic_session(
        layout.directory("incoming") / "synthetic-001", session_id="synthetic-001", seconds=0.2
    )
    summary = publish_pending(layout, min_free_bytes=0)
    assert sorted(receipt.session_id for receipt in summary.accepted) == sorted(
        ["synthetic-001", take.session_id]
    )
    assert summary.takes_recorded == [take.session_id]


def test_the_take_store_is_rebuilt_from_raw(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    take = make_take(work / "take", catalog=catalog)
    incoming(layout, take, work / "take")
    publish_pending(layout, min_free_bytes=0, clock=lambda: NOW)
    before = state_path(layout, take.session_id).read_bytes()
    state_path(layout, take.session_id).unlink()
    states, recorded = record_takes(layout)
    assert recorded == (take.session_id,) and len(states) == 1
    assert state_path(layout, take.session_id).read_bytes() == before
    assert record_takes(layout)[1] == ()


def test_a_record_naming_another_raw_package_stops_the_store(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    take = make_take(work / "take", catalog=catalog)
    incoming(layout, take, work / "take")
    publish_pending(layout, min_free_bytes=0)
    path = state_path(layout, take.session_id)
    state = CorpusTakeState.model_validate_json(path.read_bytes())
    wrong = state.model_copy(update={"manifest_sha256": "a" * 64, "raw_path": f"raw/{'a' * 64}"})
    path.write_bytes(json.dumps(wrong.model_dump(mode="json"), sort_keys=True,
                                separators=(",", ":")).encode() + b"\n")  # fmt: skip
    with pytest.raises(SecurityError, match="is recorded for raw/"):
        record_takes(layout)
    summary = publish_pending(layout, min_free_bytes=0)
    assert summary.read_model.startswith("error: ")


def test_take_records_are_bound_to_their_take(layout: AssetLayout) -> None:
    with pytest.raises(ValueError, match="not a take id"):
        state_path(layout, "../escape")
    directory = layout.directory("provenance") / "corpus-takes"
    directory.mkdir()
    (directory / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ContractError, match="unexpected entry"):
        read_take_states(layout)


def test_trims_and_decisions_hold_their_rules() -> None:
    with pytest.raises(ValueError, match="undecided draft"):
        TakeDecision(decision="kept", revision=0, reason=None, decided_at=None)
    with pytest.raises(ValueError, match="only a rejection"):
        TakeDecision(decision="kept", revision=1, reason="noisy", decided_at=NOW)
    with pytest.raises(ValueError, match="only a rejection"):
        TakeDecision(decision="rejected", revision=1, reason=None, decided_at=NOW)
    TakeDecision(decision="rejected", revision=2, reason="a cough", decided_at=NOW)
    with pytest.raises(ValueError, match="both ends"):
        TakeTrim(revision=1, start_frame=0, end_frame=None)
    with pytest.raises(ValueError, match="at least one frame"):
        TakeTrim(revision=1, start_frame=5, end_frame=5)
    with pytest.raises(ValueError, match="untrimmed"):
        TakeTrim(revision=0, start_frame=0, end_frame=5)


def test_a_take_against_an_earlier_revision_counts_through_its_alias(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    kept, reworded = catalog[1].suite.prompts[0], catalog[1].suite.prompts[1]
    assert kept.text is not None and reworded.text is not None
    first = make_take(work / "kept", catalog=catalog, prompt_id=kept.prompt_id)
    second = make_take(work / "reworded", catalog=catalog, prompt_id=reworded.prompt_id, seed=2)
    for take, name in ((first, "kept"), (second, "reworded")):
        incoming(layout, take, work / name)
    assert len(publish_pending(layout, min_free_bytes=0).accepted) == 2
    # A typo-class fix keeps its recordings; a new sentence does not.
    run_catalog(
        layout, "edit-prompt", kept.prompt_id, "--text", kept.text.rstrip(".") + "!",
        "--keep-recordings", "--json",
    )  # fmt: skip
    run_catalog(
        layout, "edit-prompt", reworded.prompt_id, "--text", "An entirely different sentence.",
        "--json",
    )  # fmt: skip
    run_catalog(layout, "publish")
    run_catalog(layout, "activate", "--subject-id", COLE_SUBJECT, "--version", "2")
    found = read_library(layout, COLE_SUBJECT)  # rebuilt by activate, before any pass
    assert found is not None
    library = found[0]
    assert library.catalog.catalog_version == 2
    rows = {row.prompt_id: row for row in library.rows}
    assert rows[kept.prompt_id].status == "draft"
    assert rows[kept.prompt_id].prompt_sha256 != first.manifest.prompt_sha256
    assert rows[reworded.prompt_id].status == "missing"
    assert (library.coverage.takes, library.coverage.uncounted_takes) == (1, 1)
    [history] = library.histories
    assert history.takes[0].recorded_prompt_sha256 == first.manifest.prompt_sha256
    # A take of the earlier revision uploaded now is admitted against its own version.
    late = make_take(work / "late", catalog=catalog, prompt_id=kept.prompt_id, seed=4)
    incoming(layout, late, work / "late")
    assert len(publish_pending(layout, min_free_bytes=0).accepted) == 1
    assert active_snapshot(layout)[0].catalog_version == 2
    found = read_library(layout, COLE_SUBJECT)
    assert found is not None
    assert {row.prompt_id: row for row in found[0].rows}[kept.prompt_id].take_count == 2


# -- Classification and the vault ---------------------------------------------------------------


def test_new_paths_are_classified() -> None:
    sha = "a" * 64
    states = classify(f"provenance/corpus-takes/take-{sha}.json")
    assert (states.rule.name, states.rule.disposition) == ("corpus-take-states", "records")
    library = classify("derived/corpus/library-cole-self.json")
    assert (library.rule.name, library.rule.disposition) == ("corpus-read-model", "mac-only")
    for name in (AUDIO_NAME, CONTINUITY_NAME, "manifest.json", "intake-receipt.json"):
        assert classify(f"raw/{sha}/{name}").rule.name == "raw-packages"
    temporary = classify(f"provenance/corpus-takes/.take-{sha}.json.tmp-0123456789abcdef")
    assert temporary.rule.disposition == "mac-only"


def test_the_vault_reads_a_raw_take_and_its_record(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    take = make_take(work / "take", catalog=catalog)
    incoming(layout, take, work / "take")
    publish_pending(layout, min_free_bytes=0)
    items = {
        item.path: item
        for item in RawPackageSource().items(layout)
        if isinstance(item, ReplicationItem)
    }
    raw = f"raw/{take.manifest_sha256}"
    audio = items[f"{raw}/{AUDIO_NAME}"]
    assert (audio.expected_sha256, audio.expected_size) == (
        take.manifest.audio.sha256,
        take.manifest.audio.size_bytes,
    )
    evidence = items[f"{raw}/{CONTINUITY_NAME}"]
    assert evidence.expected_sha256 == take.manifest.continuity.sha256
    assert set(items) == {f"{raw}/{name}" for name in (*TAKE_FILES, "intake-qc.json",
                                                         "intake-receipt.json")}  # fmt: skip
    [source] = [source for source in library_sources() if source.name == "corpus-takes"]
    paths = [item.path for item in source.items(layout) if isinstance(item, ReplicationItem)]
    assert paths == [f"provenance/corpus-takes/{take.session_id}.json"]


def test_qc_must_name_the_take_and_its_manifest(package: Path) -> None:
    reseal(package, qc={"session_id": "take-" + "1" * 64})
    with pytest.raises(ContractError, match="take IDs disagree"):
        validate_take_package(package)
    reseal(package, qc={"manifest_sha256": "1" * 64})
    with pytest.raises(SecurityError, match="not bound to the take's manifest"):
        validate_take_package(package)
    reseal(package)
    report = json.loads((package / "qc.json").read_bytes())
    report["generated_at"] = "2026-10-12T13:00:00Z"
    payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    (package / "qc.json").write_bytes(payload)
    with pytest.raises(SecurityError, match="READY QC digest"):
        validate_take_package(package)


def test_audio_format_evidence_must_match_the_wav(package: Path) -> None:
    manifest = json.loads((package / "manifest.json").read_bytes())
    audio = manifest["audio"]
    spec = audio["audio"]
    longer = {
        **spec,
        "frames": spec["frames"] + 48,
        "duration_seconds": (spec["frames"] + 48) / 48_000,
    }
    reseal(package, manifest={"audio": {**audio, "audio": longer}})
    with pytest.raises(ContractError, match="format evidence disagrees"):
        validate_take_package(package)


def test_a_published_take_must_keep_intakes_evidence(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    take = make_take(work / "take", catalog=catalog)
    incoming(layout, take, work / "take")
    publish_pending(layout, min_free_bytes=0)
    raw = layout.directory("raw") / take.manifest_sha256
    copy = work / "copy"
    shutil.copytree(raw, copy)
    writable(copy)
    report = json.loads((copy / "intake-qc.json").read_bytes())
    report["generated_at"] = "2030-01-01T00:00:00Z"
    (copy / "intake-qc.json").write_bytes(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    with pytest.raises(SecurityError, match="receipt disagrees"):
        validate_take_package(copy, allow_intake_receipt=True)
    report["metrics"][0]["peak_dbfs"] = -1.0
    (copy / "intake-qc.json").write_bytes(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    with pytest.raises(SecurityError, match="independently recomputed"):
        validate_take_package(copy, allow_intake_receipt=True)


def test_a_diagnostic_root_or_a_damaged_snapshot_admits_no_take(
    layout: AssetLayout, catalog: Catalog, work: Path, tmp_path: Path
) -> None:
    marked, _marker = initialize_diagnostic_root(
        tmp_path / "diagnostic" / "assets", subject_id=f"{DIAGNOSTIC_SUBJECT_PREFIX}one"
    )
    take = make_take(work / "take", catalog=catalog)
    incoming(marked, take, work / "take")
    [refusal] = publish_pending(marked, min_free_bytes=0).refused
    assert refusal.error.startswith("take_diagnostic_root: ")
    snapshot = layout.root / catalog[0].snapshot_path
    os.chmod(snapshot, 0o600)
    snapshot.write_bytes(snapshot.read_bytes().replace(b"quiet", b"QUIET", 1))
    incoming(layout, take, work / "take")
    [refusal] = publish_pending(layout, min_free_bytes=0).refused
    assert refusal.error.startswith("take_catalog_invalid: ")
